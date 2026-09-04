"""Handing a request back, parking it, and cancelling it.

Finance review of 24 Aug 2026, phase 2. Three states that are *not* the happy
path and are not each other, so most of what is asserted here is that they stay
apart: a returned request is not a rejected one, a cancelled one is not a
failed one, and a parked one has not ended at all.

The other half is who reads what. A merchant's handback note is the first
internal note a merchant has ever been able to write, and the rule it lives
under is narrow on purpose — its author and Finance, nobody else.
"""

from decimal import Decimal

from django.contrib.auth.models import Group, Permission
from django.test import TestCase

from apps.accounts.models import Role
from apps.accounts.permissions import sync_role_groups
from apps.accounts.tests import make_user
from apps.core.choices import ActorRole, AuditAction
from apps.core.models import AuditLog
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.transactions import messaging
from apps.transactions.models import Request, RequestStatus, RequestType
from apps.transactions.services import (
    TransitionError,
    apply_transition,
    available_transitions,
    track,
)


class ReturnTestCase(TestCase):
    def setUp(self):
        sync_role_groups()
        self.admin = make_user("root@maxifyfx.com", Role.FINANCE_ADMIN)
        self.staff = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)
        self.merchant_user = make_user("merch@example.com", Role.MERCHANT)
        self.other_user = make_user("other@example.com", Role.MERCHANT)

        self.method = PaymentMethod.objects.create(
            code="zaincash", caption_ar="زين كاش", caption_en="ZainCash"
        )
        self.merchant = self.make_merchant("تاجر أ", self.merchant_user)
        self.other = self.make_merchant("تاجر ب", self.other_user)

        from apps.accounts.models import Client as PortalClient

        self.client_record = PortalClient.objects.create(b2core_id="sub-1")
        self.deposit = self.make_request(status=RequestStatus.ASSIGNED)

    def make_merchant(self, name, user) -> Merchant:
        merchant = Merchant.objects.create(name=name, user=user)
        link = MerchantMethod.objects.create(
            merchant=merchant, payment_method=self.method
        )
        Wallet.objects.create(merchant_method=link, number="07700000001")
        return merchant

    def make_request(self, **overrides) -> Request:
        defaults = dict(
            type=RequestType.DEPOSIT,
            client=self.client_record,
            payment_method=self.method,
            merchant_selected=self.merchant,
            merchant_assigned=self.merchant,
            wallet_number_snapshot="07700000001",
            amount_usd=Decimal("100.00"),
            amount_iqd=Decimal("145500.00"),
            rate_applied=Decimal("1450.00"),
            commission_applied=Decimal("500.00"),
            status=RequestStatus.ASSIGNED,
        )
        defaults.update(overrides)
        return Request.objects.create(**defaults)

    def hand_back(self, reason="لا سيولة اليوم", **kwargs):
        return apply_transition(
            self.deposit, "hand_back", actor=self.merchant_user, reason=reason, **kwargs
        )


# ---------------------------------------------------------------------------
# 2.1 — the merchant hands it back
# ---------------------------------------------------------------------------


class HandBackTests(ReturnTestCase):
    def test_it_parks_the_request_rather_than_ending_it(self):
        returned = self.hand_back()

        self.assertEqual(returned.status, RequestStatus.PENDING)
        self.assertFalse(returned.is_closed)

    def test_the_reason_is_mandatory(self):
        with self.assertRaises(TransitionError) as caught:
            apply_transition(
                self.deposit, "hand_back", actor=self.merchant_user, reason=""
            )
        self.assertEqual(caught.exception.code, "reason_missing")

    def test_the_reason_is_written_as_an_internal_note(self):
        self.hand_back(reason="الإيصال غير مقروء")

        note = self.deposit.messages.get()
        self.assertTrue(note.is_internal_note)
        self.assertEqual(note.sender_role, ActorRole.MERCHANT)
        self.assertEqual(note.body, "الإيصال غير مقروء")

    def test_the_reason_never_lands_in_the_field_the_client_reads(self):
        """`rejection_reason` is why a request *ended*, and this one has not."""
        returned = self.hand_back(reason="لا سيولة اليوم")

        self.assertEqual(returned.rejection_reason, "")

    def test_only_the_merchant_holding_it_may_hand_it_back(self):
        with self.assertRaises(TransitionError) as caught:
            apply_transition(
                self.deposit, "hand_back", actor=self.other_user, reason="ليس لي"
            )
        self.assertEqual(caught.exception.code, "not_assigned")

    def test_finance_cannot_hand_a_request_back_to_itself(self):
        with self.assertRaises(TransitionError) as caught:
            apply_transition(
                self.deposit, "hand_back", actor=self.admin, reason="لا"
            )
        self.assertEqual(caught.exception.code, "wrong_role")

    def test_it_needs_its_own_permission(self):
        Group.objects.get(name=Role.MERCHANT).permissions.remove(
            Permission.objects.get(codename="return_request")
        )
        fresh = type(self.merchant_user).objects.get(pk=self.merchant_user.pk)

        with self.assertRaises(TransitionError) as caught:
            apply_transition(self.deposit, "hand_back", actor=fresh, reason="لا سيولة")

        self.assertEqual(caught.exception.code, "no_permission")

    def test_a_returned_request_leaves_the_merchants_hands(self):
        self.hand_back()

        fresh = Request.objects.get(pk=self.deposit.pk)
        self.assertEqual(available_transitions(fresh, self.merchant_user), [])

    def test_it_is_audited(self):
        self.hand_back()

        entry = AuditLog.objects.filter(action=AuditAction.STATUS_CHANGE).latest("pk")
        self.assertEqual(entry.after["action"], "hand_back")
        self.assertEqual(entry.after["status"], RequestStatus.PENDING)
        self.assertEqual(entry.actor_id, self.merchant_user.pk)


# ---------------------------------------------------------------------------
# Who may read the handback note
# ---------------------------------------------------------------------------


class HandBackNoteVisibilityTests(ReturnTestCase):
    def setUp(self):
        super().setUp()
        self.hand_back(reason="لا سيولة اليوم")
        self.note = self.deposit.messages.get()
        messaging.post(
            self.deposit,
            sender_role=ActorRole.FINANCE_STAFF,
            sender_id=self.staff.pk,
            body="ملاحظة المالية وحدها",
            is_internal_note=True,
        )

    def visible(self, audience, viewer_id=None):
        fresh = Request.objects.get(pk=self.deposit.pk)
        return [
            m.body
            for m in messaging.visible_messages(
                fresh, audience=audience, viewer_id=viewer_id
            )
        ]

    def test_finance_reads_both_kinds(self):
        bodies = self.visible(messaging.FINANCE)
        self.assertIn("لا سيولة اليوم", bodies)
        self.assertIn("ملاحظة المالية وحدها", bodies)

    def test_the_client_reads_neither(self):
        self.assertEqual(self.visible(messaging.CLIENT), [])

    def test_the_author_reads_their_own_note_back(self):
        """A mandatory field whose content vanishes from its author is a field
        nobody trusts."""
        bodies = self.visible(messaging.MERCHANT, viewer_id=self.merchant_user.pk)
        self.assertIn("لا سيولة اليوم", bodies)

    def test_the_author_still_never_reads_a_finance_note(self):
        bodies = self.visible(messaging.MERCHANT, viewer_id=self.merchant_user.pk)
        self.assertNotIn("ملاحظة المالية وحدها", bodies)

    def test_the_replacement_merchant_reads_neither(self):
        """2.3: the previous merchant's words do not travel, and a handback
        note is the most pointed example of that."""
        bodies = self.visible(messaging.MERCHANT, viewer_id=self.other_user.pk)
        self.assertEqual(bodies, [])


# ---------------------------------------------------------------------------
# 2.3 — reassignment
# ---------------------------------------------------------------------------


class ReassignmentTests(ReturnTestCase):
    def test_a_returned_request_can_be_routed_to_somebody_else(self):
        self.hand_back()
        fresh = Request.objects.get(pk=self.deposit.pk)

        routed = apply_transition(
            fresh, "route", actor=self.admin, merchant=self.other
        )

        self.assertEqual(routed.status, RequestStatus.ASSIGNED)
        self.assertEqual(routed.merchant_assigned_id, self.other.pk)

    def test_it_does_not_have_to_be_reviewed_from_scratch(self):
        self.hand_back()
        fresh = Request.objects.get(pk=self.deposit.pk)

        moves = {m.action for m in available_transitions(fresh, self.admin)}

        self.assertIn("route", moves)
        self.assertIn("cancel", moves)

    def test_the_client_is_still_never_told_which_merchant_holds_it(self):
        self.hand_back()
        fresh = Request.objects.get(pk=self.deposit.pk)
        apply_transition(fresh, "route", actor=self.admin, merchant=self.other)

        # `merchant_selected` is what the client chose and is never rewritten.
        self.assertEqual(
            Request.objects.get(pk=self.deposit.pk).merchant_selected_id,
            self.merchant.pk,
        )

    def test_the_new_merchant_can_act_on_it(self):
        self.hand_back()
        fresh = Request.objects.get(pk=self.deposit.pk)
        apply_transition(fresh, "route", actor=self.admin, merchant=self.other)

        fresh = Request.objects.get(pk=self.deposit.pk)
        moves = {m.action for m in available_transitions(fresh, self.other_user)}

        self.assertEqual(moves, {"confirm", "hand_back", "reject"})


# ---------------------------------------------------------------------------
# 2.2 — cancelling, and 2.4 — parking
# ---------------------------------------------------------------------------


class CancelTests(ReturnTestCase):
    def test_cancelling_is_not_rejecting(self):
        cancelled = apply_transition(
            self.deposit, "cancel", actor=self.admin, reason="طلبه العميل مرتين"
        )

        self.assertEqual(cancelled.status, RequestStatus.CANCELLED)
        self.assertNotEqual(cancelled.status, RequestStatus.REJECTED)
        self.assertTrue(cancelled.is_closed)

    def test_the_reason_reaches_the_client(self):
        """Spec §6's rule for a rejection, applied to the other way of ending
        one: the client is owed the reason either way."""
        apply_transition(
            self.deposit, "cancel", actor=self.admin, reason="طلبه العميل مرتين"
        )

        message = self.deposit.messages.get()
        self.assertFalse(message.is_internal_note)
        self.assertEqual(message.body, "طلبه العميل مرتين")
        self.assertEqual(
            Request.objects.get(pk=self.deposit.pk).rejection_reason,
            "طلبه العميل مرتين",
        )

    def test_a_reason_is_mandatory(self):
        with self.assertRaises(TransitionError) as caught:
            apply_transition(self.deposit, "cancel", actor=self.admin, reason="")
        self.assertEqual(caught.exception.code, "reason_missing")

    def test_it_needs_its_own_permission(self):
        Group.objects.get(name=Role.FINANCE_STAFF).permissions.remove(
            Permission.objects.get(codename="cancel_request")
        )
        fresh_staff = type(self.staff).objects.get(pk=self.staff.pk)

        with self.assertRaises(TransitionError) as caught:
            apply_transition(
                self.deposit, "cancel", actor=fresh_staff, reason="لم يعد مطلوبًا"
            )

        self.assertEqual(caught.exception.code, "no_permission")

    def test_a_merchant_cannot_cancel(self):
        with self.assertRaises(TransitionError) as caught:
            apply_transition(
                self.deposit, "cancel", actor=self.merchant_user, reason="لا"
            )
        self.assertEqual(caught.exception.code, "wrong_role")

    def test_a_cancelled_request_is_finished(self):
        cancelled = apply_transition(
            self.deposit, "cancel", actor=self.admin, reason="لم يعد مطلوبًا"
        )
        self.assertEqual(available_transitions(cancelled, self.admin), [])

    def test_a_returned_request_can_be_cancelled_instead_of_reassigned(self):
        self.hand_back()
        fresh = Request.objects.get(pk=self.deposit.pk)

        cancelled = apply_transition(
            fresh, "cancel", actor=self.admin, reason="لا يوجد تاجر بديل"
        )

        self.assertEqual(cancelled.status, RequestStatus.CANCELLED)


class ParkTests(ReturnTestCase):
    def test_finance_can_park_a_request(self):
        parked = apply_transition(
            self.deposit, "park", actor=self.admin, reason="بانتظار مصرف العميل"
        )
        self.assertEqual(parked.status, RequestStatus.PENDING)
        self.assertFalse(parked.is_closed)

    def test_the_reason_is_an_internal_note(self):
        apply_transition(
            self.deposit, "park", actor=self.admin, reason="بانتظار مصرف العميل"
        )

        note = self.deposit.messages.get()
        self.assertTrue(note.is_internal_note)
        self.assertEqual(
            Request.objects.get(pk=self.deposit.pk).rejection_reason, ""
        )

    def test_a_reason_is_mandatory(self):
        with self.assertRaises(TransitionError) as caught:
            apply_transition(self.deposit, "park", actor=self.admin, reason="")
        self.assertEqual(caught.exception.code, "reason_missing")

    def test_a_parked_request_sits_with_finance(self):
        from apps.transactions.services import AWAITING_FINANCE, OPEN_STATUSES

        self.assertIn(RequestStatus.PENDING, AWAITING_FINANCE)
        self.assertIn(RequestStatus.PENDING, OPEN_STATUSES)

    def test_a_cancelled_request_is_not_open(self):
        from apps.transactions.services import OPEN_STATUSES

        self.assertNotIn(RequestStatus.CANCELLED, OPEN_STATUSES)


# ---------------------------------------------------------------------------
# The track
# ---------------------------------------------------------------------------


class TrackTests(ReturnTestCase):
    def states(self, request_obj):
        return {step["key"]: step["state"] for step in track(request_obj)}

    def test_a_parked_request_shows_a_step_of_its_own(self):
        parked = apply_transition(
            self.deposit, "park", actor=self.admin, reason="بانتظار شيء"
        )

        states = self.states(parked)
        self.assertEqual(states[RequestStatus.PENDING], "current")

    def test_a_cancelled_request_shows_one_too_and_it_is_not_the_rejected_one(self):
        cancelled = apply_transition(
            self.deposit, "cancel", actor=self.admin, reason="لم يعد مطلوبًا"
        )

        states = self.states(cancelled)
        self.assertIn(RequestStatus.CANCELLED, states)
        self.assertNotIn(RequestStatus.REJECTED, states)

    def test_neither_claims_to_know_how_far_the_request_got(self):
        """The audit log is where the actual sequence is read."""
        parked = apply_transition(
            self.deposit, "park", actor=self.admin, reason="بانتظار شيء"
        )

        states = self.states(parked)
        self.assertEqual(states[RequestStatus.ASSIGNED], "cancelled")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


class ReportTotalsTests(ReturnTestCase):
    def test_a_cancelled_request_is_counted_but_not_added_up(self):
        """Nothing moved, so including it would overstate every figure a desk
        reconciles against — the rule a rejection already lives under."""
        from apps.reports import aggregates

        apply_transition(
            self.deposit, "cancel", actor=self.admin, reason="لم يعد مطلوبًا"
        )

        summary = aggregates.summarise(Request.objects.all())

        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["cancelled"], 1)
        self.assertEqual(summary["total_usd"], Decimal("0.00"))

    def test_a_parked_request_is_still_counted(self):
        """It is live. Dropping it would understate what the desk is carrying."""
        from apps.reports import aggregates

        apply_transition(
            self.deposit, "park", actor=self.admin, reason="بانتظار شيء"
        )

        summary = aggregates.summarise(Request.objects.all())

        self.assertEqual(summary["total_usd"], Decimal("100.00"))
