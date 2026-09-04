"""The lifecycle state machine (build-order step 7, spec §6).

These test the rules rather than the screens: what may follow what, who may do
it, what it writes, and what it refuses. The panel that drives them is covered
in :mod:`apps.finance.test_queue`.
"""

from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Client as PortalClient
from apps.accounts.models import Role
from apps.accounts.permissions import sync_role_groups
from apps.accounts.tests import make_user
from apps.core.choices import ActorRole, AuditAction
from apps.core.models import AuditLog
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.transactions.models import Message, Request, RequestStatus, RequestType
from apps.transactions.services import (
    TransitionError,
    apply_transition,
    available_transitions,
    eligible_merchants,
    get_transition,
    track,
)


class LifecycleTestCase(TestCase):
    """One deposit, two merchants that can take it, and the three roles."""

    def setUp(self):
        sync_role_groups()
        self.admin = make_user("root@maxifyfx.com", Role.FINANCE_ADMIN)
        self.staff = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)
        self.merchant_user = make_user("merch@example.com", Role.MERCHANT)

        self.method = PaymentMethod.objects.create(
            code="zaincash", caption_ar="زين كاش", caption_en="ZainCash"
        )
        self.other_method = PaymentMethod.objects.create(
            code="fastpay", caption_ar="فاست باي", caption_en="FastPay"
        )
        self.merchant = self.make_merchant("تاجر أ", self.method)
        # The merchant who can actually sign in. Spec §8 scopes a merchant to
        # the requests routed to them, so the link matters to the rules here.
        self.merchant.user = self.merchant_user
        self.merchant.save(update_fields=["user"])
        self.alternate = self.make_merchant("تاجر ب", self.method)
        # Covers a method this request does not use, so it must never be
        # offered as a routing target for it.
        self.unrelated = self.make_merchant("تاجر ج", self.other_method)

        self.client_record = PortalClient.objects.create(
            b2core_id="sub-1", display_name="زينب الجبوري", email="z@example.com"
        )
        self.deposit = self.make_request()

    def make_merchant(self, name, method) -> Merchant:
        merchant = Merchant.objects.create(name=name)
        link = MerchantMethod.objects.create(merchant=merchant, payment_method=method)
        Wallet.objects.create(merchant_method=link, number="07700000001")
        return merchant

    def make_request(self, **overrides) -> Request:
        defaults = dict(
            type=RequestType.DEPOSIT,
            client=self.client_record,
            payment_method=self.method,
            merchant_selected=self.merchant,
            wallet_number_snapshot="07700000001",
            amount_usd=Decimal("100.00"),
            amount_iqd=Decimal("145500.00"),
            rate_applied=Decimal("1450.00"),
            commission_applied=Decimal("500.00"),
            status=RequestStatus.SUBMITTED,
        )
        defaults.update(overrides)
        return Request.objects.create(**defaults)

    def advance(self, request_obj, *actions, actor=None, **kwargs):
        for action in actions:
            request_obj = apply_transition(
                request_obj, action, actor=actor or self.admin, **kwargs
            )
        return request_obj


class HappyPathTests(LifecycleTestCase):
    def test_deposit_runs_submitted_to_closed(self):
        deposit = self.advance(self.deposit, "review")
        self.assertEqual(deposit.status, RequestStatus.UNDER_REVIEW)

        deposit = apply_transition(deposit, "route", actor=self.admin, merchant=self.merchant)
        self.assertEqual(deposit.status, RequestStatus.ASSIGNED)
        self.assertEqual(deposit.merchant_assigned_id, self.merchant.pk)
        self.assertIsNotNone(deposit.assigned_at)

        deposit = apply_transition(deposit, "confirm", actor=self.merchant_user)
        self.assertEqual(deposit.status, RequestStatus.MERCHANT_CONFIRMED)
        self.assertIsNotNone(deposit.merchant_actioned_at)

        deposit = self.advance(deposit, "credit", "close")
        self.assertEqual(deposit.status, RequestStatus.CLOSED)
        self.assertIsNotNone(deposit.closed_at)

    def test_every_move_writes_an_audit_entry(self):
        deposit = self.advance(self.deposit, "review")
        apply_transition(deposit, "route", actor=self.admin, merchant=self.merchant)

        entries = AuditLog.objects.filter(
            action=AuditAction.STATUS_CHANGE, target_id=str(self.deposit.pk)
        ).order_by("id")
        self.assertEqual([e.after["action"] for e in entries], ["review", "route"])
        self.assertEqual(entries[0].before["status"], RequestStatus.SUBMITTED)
        self.assertEqual(entries[1].after["merchant_assigned"], self.merchant.pk)
        self.assertEqual(entries[0].actor_id, self.admin.pk)

    def test_rerouting_moves_an_assigned_request_to_another_merchant(self):
        deposit = self.advance(self.deposit, "review")
        deposit = apply_transition(deposit, "route", actor=self.admin, merchant=self.merchant)
        deposit = apply_transition(deposit, "route", actor=self.admin, merchant=self.alternate)

        self.assertEqual(deposit.status, RequestStatus.ASSIGNED)
        self.assertEqual(deposit.merchant_assigned_id, self.alternate.pk)
        # The client's own choice is untouched: they paid into that merchant's
        # wallet, and rewriting it would lose where the money actually went.
        self.assertEqual(deposit.merchant_selected_id, self.merchant.pk)

    def test_a_withdrawal_takes_the_other_branch(self):
        withdrawal = self.make_request(
            type=RequestType.WITHDRAWAL,
            wallet_number_snapshot="",
            destination_account="1234567890",
        )
        withdrawal = self.advance(withdrawal, "review")
        withdrawal = apply_transition(
            withdrawal, "route", actor=self.admin, merchant=self.merchant
        )
        withdrawal = apply_transition(withdrawal, "pay", actor=self.merchant_user)
        self.assertEqual(withdrawal.status, RequestStatus.MERCHANT_PAID)

        withdrawal = self.advance(withdrawal, "close")
        self.assertEqual(withdrawal.status, RequestStatus.CLOSED)

    def test_reviewing_a_withdrawal_names_the_b2core_debit(self):
        """Spec §6 puts the debit inside this step, and no code can perform it.

        The confirmation an operator reads before pressing the button is the
        only place the system can carry that instruction, so it is asserted
        rather than left to whoever last edited the transition table.
        """
        withdrawal = self.make_request(
            type=RequestType.WITHDRAWAL,
            wallet_number_snapshot="",
            destination_account="1234567890",
        )
        move = get_transition("review")

        self.assertIn("B2CORE", str(move.confirm_for(withdrawal)))
        self.assertNotIn("B2CORE", str(move.confirm_for(self.deposit)))

    def test_rejecting_a_withdrawal_names_the_reversal(self):
        """Spec §6: "any debit reversed". A deposit has nothing to reverse."""
        withdrawal = self.make_request(
            type=RequestType.WITHDRAWAL,
            wallet_number_snapshot="",
            destination_account="1234567890",
        )
        move = get_transition("reject")

        self.assertIn("B2CORE", str(move.confirm_for(withdrawal)))
        self.assertNotIn("B2CORE", str(move.confirm_for(self.deposit)))


class RefusalTests(LifecycleTestCase):
    def test_a_step_cannot_be_skipped(self):
        with self.assertRaises(TransitionError) as caught:
            apply_transition(self.deposit, "credit", actor=self.admin)
        # Same refusal as a genuine race: from the panel a move is only ever
        # offered when its source status holds, so reaching one that does not
        # means the row is not where the caller thought it was.
        self.assertEqual(caught.exception.code, "stale")
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, RequestStatus.SUBMITTED)

    def test_a_closed_request_cannot_be_reopened_or_rejected(self):
        deposit = self.advance(self.deposit, "review")
        deposit = apply_transition(deposit, "route", actor=self.admin, merchant=self.merchant)
        deposit = apply_transition(deposit, "confirm", actor=self.merchant_user)
        deposit = self.advance(deposit, "credit", "close")

        for action in ("review", "route", "reject"):
            with self.assertRaises(TransitionError):
                apply_transition(
                    deposit, action, actor=self.admin, merchant=self.merchant, reason="لا"
                )

    def test_a_move_made_by_someone_else_first_is_refused_as_stale(self):
        deposit = self.advance(self.deposit, "review")
        # Another operator routes it between this page render and this POST.
        Request.objects.filter(pk=deposit.pk).update(status=RequestStatus.ASSIGNED)

        with self.assertRaises(TransitionError) as caught:
            apply_transition(deposit, "review", actor=self.admin)
        self.assertEqual(caught.exception.code, "stale")

    def test_the_wrong_direction_is_refused(self):
        deposit = self.advance(self.deposit, "review")
        deposit = apply_transition(deposit, "route", actor=self.admin, merchant=self.merchant)
        with self.assertRaises(TransitionError) as caught:
            # "pay" belongs to withdrawals only.
            apply_transition(deposit, "pay", actor=self.merchant_user)
        self.assertEqual(caught.exception.code, "wrong_type")

    def test_routing_to_a_merchant_who_does_not_cover_the_method_is_refused(self):
        deposit = self.advance(self.deposit, "review")
        with self.assertRaises(TransitionError) as caught:
            apply_transition(deposit, "route", actor=self.admin, merchant=self.unrelated)
        self.assertEqual(caught.exception.code, "merchant_method_missing")

    def test_routing_to_a_stopped_merchant_is_refused(self):
        self.alternate.is_active = False
        self.alternate.save(update_fields=["is_active"])
        deposit = self.advance(self.deposit, "review")
        with self.assertRaises(TransitionError) as caught:
            apply_transition(deposit, "route", actor=self.admin, merchant=self.alternate)
        self.assertEqual(caught.exception.code, "merchant_inactive")

    def test_a_merchant_cannot_make_a_finance_move(self):
        with self.assertRaises(TransitionError) as caught:
            apply_transition(self.deposit, "review", actor=self.merchant_user)
        self.assertEqual(caught.exception.code, "wrong_role")

    def test_finance_cannot_confirm_on_the_merchants_behalf(self):
        deposit = self.advance(self.deposit, "review")
        deposit = apply_transition(deposit, "route", actor=self.admin, merchant=self.merchant)
        with self.assertRaises(TransitionError) as caught:
            apply_transition(deposit, "confirm", actor=self.admin)
        self.assertEqual(caught.exception.code, "wrong_role")

    def test_a_permission_that_was_revoked_stops_the_move(self):
        self.staff.groups.clear()  # every permission came from the role group
        self.staff = type(self.staff).objects.get(pk=self.staff.pk)
        with self.assertRaises(TransitionError) as caught:
            apply_transition(self.deposit, "review", actor=self.staff)
        self.assertEqual(caught.exception.code, "no_permission")

    def test_an_unknown_action_is_refused(self):
        with self.assertRaises(TransitionError) as caught:
            apply_transition(self.deposit, "vanish", actor=self.admin)
        self.assertEqual(caught.exception.code, "unknown_action")


class RejectionTests(LifecycleTestCase):
    def test_the_reason_is_stored_and_posted_to_the_thread(self):
        deposit = apply_transition(
            self.deposit, "reject", actor=self.admin, reason="الإيصال غير مقروء."
        )
        self.assertEqual(deposit.status, RequestStatus.REJECTED)
        self.assertEqual(deposit.rejection_reason, "الإيصال غير مقروء.")
        self.assertIsNotNone(deposit.closed_at)

        message = deposit.messages.get()
        self.assertEqual(message.body, "الإيصال غير مقروء.")
        self.assertEqual(message.sender_role, ActorRole.FINANCE_ADMIN)
        # Not internal: spec §6 says the client is owed the reason.
        self.assertFalse(message.is_internal_note)

    def test_an_empty_reason_is_refused_before_anything_is_written(self):
        with self.assertRaises(TransitionError) as caught:
            apply_transition(self.deposit, "reject", actor=self.admin, reason="   ")
        self.assertEqual(caught.exception.code, "reason_missing")
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, RequestStatus.SUBMITTED)
        self.assertFalse(Message.objects.exists())

    def test_a_merchant_may_reject_an_assigned_request(self):
        deposit = self.advance(self.deposit, "review")
        deposit = apply_transition(deposit, "route", actor=self.admin, merchant=self.merchant)
        deposit = apply_transition(
            deposit, "reject", actor=self.merchant_user, reason="المبلغ لم يصل."
        )
        self.assertEqual(deposit.status, RequestStatus.REJECTED)
        self.assertEqual(deposit.messages.get().sender_role, ActorRole.MERCHANT)


class InternalNoteTests(LifecycleTestCase):
    def test_a_note_is_flagged_internal_and_never_reaches_the_client(self):
        from apps.portal.payloads import detail_payload

        apply_transition(
            self.deposit, "review", actor=self.admin, note="راجعت الإيصال مع البنك."
        )
        note = self.deposit.messages.get()
        self.assertTrue(note.is_internal_note)

        payload = detail_payload(self.deposit, self.client_record)
        self.assertEqual(payload["messages"], [])

    def test_no_note_means_no_message(self):
        apply_transition(self.deposit, "review", actor=self.admin, note="   ")
        self.assertFalse(self.deposit.messages.exists())


class AvailabilityTests(LifecycleTestCase):
    def test_finance_sees_only_the_moves_the_status_allows(self):
        moves = {m.action for m in available_transitions(self.deposit, self.admin)}
        # `park` and `cancel` reach a submitted request too: a desk can shelve
        # or drop one before anybody has looked at it (Finance review, 24 Aug).
        self.assertEqual(moves, {"review", "park", "cancel", "reject"})

    def test_a_merchant_sees_no_move_on_an_unrouted_request(self):
        self.assertEqual(available_transitions(self.deposit, self.merchant_user), [])

    def test_a_merchant_sees_nothing_on_a_request_routed_to_someone_else(self):
        deposit = self.advance(self.deposit, "review")
        deposit = apply_transition(deposit, "route", actor=self.admin, merchant=self.alternate)
        self.assertEqual(available_transitions(deposit, self.merchant_user), [])

    def test_a_merchant_cannot_act_on_a_request_routed_to_someone_else(self):
        deposit = self.advance(self.deposit, "review")
        deposit = apply_transition(deposit, "route", actor=self.admin, merchant=self.alternate)
        with self.assertRaises(TransitionError) as caught:
            apply_transition(deposit, "reject", actor=self.merchant_user, reason="ليس لي")
        self.assertEqual(caught.exception.code, "not_assigned")

    def test_a_merchant_sees_confirm_once_it_is_routed(self):
        deposit = self.advance(self.deposit, "review")
        deposit = apply_transition(deposit, "route", actor=self.admin, merchant=self.merchant)
        moves = {m.action for m in available_transitions(deposit, self.merchant_user)}
        # `hand_back` is the third thing a merchant can do with a request that
        # is theirs: take it, refuse it, or say it is not for them.
        self.assertEqual(moves, {"confirm", "hand_back", "reject"})

    def test_a_closed_request_offers_nothing(self):
        deposit = apply_transition(self.deposit, "reject", actor=self.admin, reason="مكرر")
        self.assertEqual(available_transitions(deposit, self.admin), [])

    def test_eligible_merchants_excludes_those_that_cannot_execute_it(self):
        names = set(eligible_merchants(self.deposit).values_list("name", flat=True))
        self.assertEqual(names, {"تاجر أ", "تاجر ب"})

    def test_a_deposit_needs_the_merchant_to_have_an_active_wallet(self):
        Wallet.objects.filter(merchant_method__merchant=self.alternate).update(is_active=False)
        names = set(eligible_merchants(self.deposit).values_list("name", flat=True))
        self.assertEqual(names, {"تاجر أ"})

    def test_a_withdrawal_does_not_need_a_wallet(self):
        Wallet.objects.all().update(is_active=False)
        withdrawal = self.make_request(
            type=RequestType.WITHDRAWAL,
            wallet_number_snapshot="",
            destination_account="1234567890",
        )
        names = set(eligible_merchants(withdrawal).values_list("name", flat=True))
        self.assertEqual(names, {"تاجر أ", "تاجر ب"})


class TrackTests(LifecycleTestCase):
    def test_the_current_step_is_the_one_the_request_is_on(self):
        states = {step["key"]: step["state"] for step in track(self.deposit)}
        self.assertEqual(states[RequestStatus.SUBMITTED], "current")
        self.assertEqual(states[RequestStatus.CLOSED], "pending")

    def test_a_closed_request_has_no_current_step(self):
        deposit = self.advance(self.deposit, "review")
        deposit = apply_transition(deposit, "route", actor=self.admin, merchant=self.merchant)
        deposit = apply_transition(deposit, "confirm", actor=self.merchant_user)
        deposit = self.advance(deposit, "credit", "close")
        self.assertNotIn("current", [step["state"] for step in track(deposit)])

    def test_rejection_cancels_the_rest_and_appends_itself(self):
        deposit = apply_transition(self.deposit, "reject", actor=self.admin, reason="مكرر")
        steps = track(deposit)
        self.assertEqual(steps[-1]["key"], RequestStatus.REJECTED)
        self.assertEqual(steps[0]["state"], "done")
        self.assertTrue(all(s["state"] == "cancelled" for s in steps[1:-1]))

    def test_a_withdrawal_track_omits_the_deposit_only_steps(self):
        withdrawal = self.make_request(
            type=RequestType.WITHDRAWAL,
            wallet_number_snapshot="",
            destination_account="1234567890",
        )
        keys = [step["key"] for step in track(withdrawal)]
        self.assertIn(RequestStatus.MERCHANT_PAID, keys)
        self.assertNotIn(RequestStatus.CREDITED, keys)
        self.assertNotIn(RequestStatus.MERCHANT_CONFIRMED, keys)


class TimestampTests(LifecycleTestCase):
    def test_only_the_stamping_moves_write_a_timestamp(self):
        before = timezone.now()
        deposit = self.advance(self.deposit, "review")
        # "review" stamps nothing; the request has only its submission time.
        self.assertIsNone(deposit.assigned_at)
        self.assertIsNone(deposit.merchant_actioned_at)
        self.assertIsNone(deposit.closed_at)

        deposit = apply_transition(deposit, "route", actor=self.admin, merchant=self.merchant)
        self.assertGreaterEqual(deposit.assigned_at, before)
