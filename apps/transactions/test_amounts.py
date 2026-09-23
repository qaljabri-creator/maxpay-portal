"""Correcting an amount, timing, attribution and the title.

Finance review of 24 Aug 2026, phase 3. The centre of it is one sentence with a
lot of money in it: *the client asks for $100 and transfers $60*. What the
request becomes, what it remembers, and who is allowed to say so.

Two decisions Finance took by hand, and both are asserted here rather than left
to be inferred from the arithmetic:

* the rate does **not** move — a correction reprices at the rate the request was
  quoted on, never at today's;
* the commission **does** — it is defined per 100 dollars, so 5,000 dinars of
  fee on a request that turned out to be $60 is not what that rate says.

And since 15 Sep 2026 a third: the corrected dinar figure is rounded to the
nearest thousand, exactly as a fresh request's is, because it goes through the
same :func:`apps.portal.pricing.price`. A corrected request that came out at
91,200 would be the one odd transfer in a stream of round ones, which is the
pattern the rule exists to avoid.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Client as PortalClient
from apps.accounts.models import Role
from apps.accounts.permissions import sync_role_groups
from apps.accounts.tests import make_user, verify_otp
from apps.core.choices import AuditAction
from apps.core.models import AuditLog
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.portal.payloads import converted_iqd, rounding_iqd
from apps.rates.models import ExchangeRate, RateType
from apps.transactions.messaging import FINANCE, visible_messages
from apps.transactions.models import Request, RequestStatus, RequestType
from apps.transactions.services import (
    TransitionError,
    apply_transition,
    correct_amount,
)

#: 1,470 dinars to the dollar, 5,000 dinars of fee per 100 dollars.
RATE = Decimal("1470.00")
FEE_PER_100 = Decimal("5000.00")


class AmountTestCase(TestCase):
    def setUp(self):
        sync_role_groups()
        self.admin = make_user("root@maxifyfx.com", Role.FINANCE_ADMIN)
        self.merchant_user = make_user("merch@example.com", Role.MERCHANT)
        self.other_user = make_user("other@example.com", Role.MERCHANT)

        self.method = PaymentMethod.objects.create(
            code="zaincash", caption_ar="زين كاش", caption_en="ZainCash"
        )
        self.merchant = self.make_merchant("تاجر أ", self.merchant_user)
        self.other = self.make_merchant("تاجر ب", self.other_user)

        ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT,
            iqd_per_usd=RATE,
            commission_iqd_per_100usd=FEE_PER_100,
        )
        self.client_record = PortalClient.objects.create(b2core_id="sub-1")

        # $100 at 1,470 with a 5,000 fee: 147,000 + 5,000 = 152,000.
        self.deposit = self.make_request()

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
            amount_iqd=Decimal("152000.00"),
            submitted_amount_usd=Decimal("100.00"),
            submitted_amount_iqd=Decimal("152000.00"),
            rate_applied=RATE,
            commission_applied=Decimal("5000.00"),
            commission_rate_applied=FEE_PER_100,
            status=RequestStatus.ASSIGNED,
        )
        defaults.update(overrides)
        return Request.objects.create(**defaults)

    def make_withdrawal(self, **overrides) -> Request:
        ExchangeRate.objects.create(
            rate_type=RateType.WITHDRAWAL,
            iqd_per_usd=RATE,
            commission_iqd_per_100usd=FEE_PER_100,
        )
        return self.make_request(
            type=RequestType.WITHDRAWAL,
            destination_account="4257880011937742",
            wallet_number_snapshot="",
            amount_iqd=Decimal("142000.00"),
            **overrides,
        )


# ---------------------------------------------------------------------------
# 3.1 — the arithmetic
# ---------------------------------------------------------------------------


class CorrectionArithmeticTests(AmountTestCase):
    def test_the_real_case(self):
        """$100 asked for, $60 arrived."""
        corrected = correct_amount(self.deposit, "60", actor=self.admin)

        self.assertEqual(corrected.amount_usd, Decimal("60.00"))
        # 60 × 1,470 = 88,200, fee re-prorated to 3,000, total 91,200 — then
        # rounded down to the nearest thousand, which is what actually moves.
        # The 200 is the company's own, and it is not taken out of the fee: the
        # fee is still exactly what the rate prorates (Finance, 15 Sep 2026).
        self.assertEqual(corrected.amount_iqd, Decimal("91000.00"))
        self.assertEqual(corrected.commission_applied, Decimal("3000.00"))

    def test_the_original_amount_is_kept_not_overwritten(self):
        corrected = correct_amount(self.deposit, "60", actor=self.admin)

        self.assertEqual(corrected.submitted_amount_usd, Decimal("100.00"))
        self.assertEqual(corrected.submitted_amount_iqd, Decimal("152000.00"))
        self.assertTrue(corrected.was_amount_corrected)

    def test_the_rate_does_not_move_even_when_a_newer_one_is_in_force(self):
        """Finance was asked and said so: the client transferred against a
        quoted figure, and re-pricing after the fact changes a settled
        agreement."""
        ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT,
            iqd_per_usd=Decimal("2000.00"),
            commission_iqd_per_100usd=Decimal("9000.00"),
        )

        corrected = correct_amount(self.deposit, "60", actor=self.admin)

        self.assertEqual(corrected.rate_applied, RATE)
        self.assertEqual(corrected.amount_iqd, Decimal("91000.00"))

    def test_the_commission_is_re_prorated_not_kept(self):
        """It is defined per 100 dollars. 5,000 on a $60 request is not what
        that rate says; 3,000 is, and the transfer rounding does not touch it."""
        corrected = correct_amount(self.deposit, "60", actor=self.admin)
        self.assertEqual(corrected.commission_applied, Decimal("3000.00"))

    def test_correcting_upward_works_the_same_way(self):
        corrected = correct_amount(self.deposit, "150", actor=self.admin)

        self.assertEqual(corrected.amount_usd, Decimal("150.00"))
        self.assertEqual(corrected.commission_applied, Decimal("7500.00"))
        self.assertEqual(corrected.amount_iqd, Decimal("228000.00"))

    def test_the_figures_still_come_out_in_whole_dinars(self):
        corrected = correct_amount(self.deposit, "33.33", actor=self.admin)

        for figure in (corrected.amount_iqd, corrected.commission_applied):
            self.assertEqual(figure, figure.to_integral_value())

    def test_a_corrected_amount_is_rounded_to_the_nearest_thousand_too(self):
        """A correction reprices through the same function a fresh request
        does, so it cannot come out as the one odd transfer in a run of round
        ones — which is the whole point of the rule (Finance, 15 Sep 2026)."""
        for value in ("33.33", "60", "80", "17.77", "123.45"):
            with self.subTest(value=value):
                fresh = self.make_request()
                corrected = correct_amount(fresh, value, actor=self.admin)
                self.assertEqual(
                    corrected.amount_iqd % 1000, 0, corrected.amount_iqd
                )

    def test_the_conversion_is_still_recoverable_after_a_correction(self):
        """Not by subtracting the fee from the total — the rounding is in there
        too, and taking only the fee back off would be 200 short. The conversion
        is recovered from its own definition, which is what the Finance queue
        and the client's breakdown both now use."""
        corrected = correct_amount(self.deposit, "60", actor=self.admin)

        self.assertEqual(converted_iqd(corrected), Decimal("88200"))
        self.assertEqual(rounding_iqd(corrected), Decimal("-200"))
        self.assertEqual(
            converted_iqd(corrected)
            + corrected.commission_applied
            + rounding_iqd(corrected),
            corrected.amount_iqd,
        )

    def test_a_second_correction_still_measures_from_the_original(self):
        correct_amount(self.deposit, "60", actor=self.admin)
        fresh = Request.objects.get(pk=self.deposit.pk)

        corrected = correct_amount(fresh, "80", actor=self.admin)

        self.assertEqual(corrected.amount_usd, Decimal("80.00"))
        # 117,600 + 4,000 = 121,600, rounded up to 122,000. The 400 is the
        # company's contribution and lives on none of the other lines.
        self.assertEqual(corrected.amount_iqd, Decimal("122000.00"))
        self.assertEqual(corrected.commission_applied, Decimal("4000.00"))
        self.assertEqual(corrected.submitted_amount_usd, Decimal("100.00"))

    def test_a_withdrawal_deducts_the_re_prorated_fee(self):
        ExchangeRate.objects.create(
            rate_type=RateType.WITHDRAWAL,
            iqd_per_usd=RATE,
            commission_iqd_per_100usd=FEE_PER_100,
        )
        withdrawal = self.make_request(
            type=RequestType.WITHDRAWAL,
            destination_account="4257880011937742",
            wallet_number_snapshot="",
            amount_iqd=Decimal("142000.00"),
        )

        corrected = correct_amount(withdrawal, "60", actor=self.admin)

        # 88,200 − 3,000 on a withdrawal: the fee comes off the payout. 85,200
        # then rounds down to 85,000, so the client receives a figure their
        # wallet will not flag, and the 200 is the company's.
        self.assertEqual(corrected.amount_iqd, Decimal("85000.00"))
        self.assertEqual(corrected.commission_applied, Decimal("3000.00"))

    def test_an_unchanged_amount_is_refused_rather_than_written(self):
        with self.assertRaises(TransitionError) as caught:
            correct_amount(self.deposit, "100", actor=self.admin)
        self.assertEqual(caught.exception.code, "amount_unchanged")

    def test_an_invalid_amount_is_refused_by_the_same_rules_a_client_faces(self):
        for value, code in (("0", "amount_below_min"), ("مئة", "amount_invalid")):
            with self.subTest(value=value):
                with self.assertRaises(TransitionError) as caught:
                    correct_amount(self.deposit, value, actor=self.admin)
                self.assertEqual(caught.exception.code, code)


# ---------------------------------------------------------------------------
# 3.1 — who, and when
# ---------------------------------------------------------------------------


class CorrectionAccessTests(AmountTestCase):
    def test_finance_may_correct(self):
        self.assertEqual(
            correct_amount(self.deposit, "60", actor=self.admin).amount_usd,
            Decimal("60.00"),
        )

    def test_the_merchant_holding_it_may_correct(self):
        """They are the one who watches the money land."""
        self.assertEqual(
            correct_amount(self.deposit, "60", actor=self.merchant_user).amount_usd,
            Decimal("60.00"),
        )

    def test_the_merchant_holding_a_withdrawal_may_not(self):
        """Finance manager, Sep 2026: a withdrawal's amount is Finance's."""
        withdrawal = self.make_withdrawal()

        with self.assertRaises(TransitionError) as caught:
            correct_amount(withdrawal, "60", actor=self.merchant_user)

        self.assertEqual(caught.exception.code, "wrong_type")
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.amount_usd, Decimal("100.00"))

    def test_finance_still_may_correct_a_withdrawal(self):
        withdrawal = self.make_withdrawal()
        self.assertEqual(
            correct_amount(withdrawal, "60", actor=self.admin).amount_usd,
            Decimal("60.00"),
        )

    def test_another_merchant_may_not(self):
        with self.assertRaises(TransitionError) as caught:
            correct_amount(self.deposit, "60", actor=self.other_user)
        self.assertEqual(caught.exception.code, "not_assigned")

    def test_it_needs_the_permission(self):
        Group.objects.get(name=Role.MERCHANT).permissions.remove(
            Permission.objects.get(codename="change_request_amount")
        )
        fresh = type(self.merchant_user).objects.get(pk=self.merchant_user.pk)

        with self.assertRaises(TransitionError) as caught:
            correct_amount(self.deposit, "60", actor=fresh)

        self.assertEqual(caught.exception.code, "no_permission")

    def test_a_credited_request_is_past_correcting(self):
        """The figure here and the figure in the B2CORE Back Office have to
        agree, and this system cannot change the one over there."""
        self.deposit.status = RequestStatus.CREDITED
        self.deposit.save(update_fields=["status"])

        with self.assertRaises(TransitionError) as caught:
            correct_amount(self.deposit, "60", actor=self.admin)

        self.assertEqual(caught.exception.code, "not_editable")

    def test_a_closed_request_is_too(self):
        self.deposit.status = RequestStatus.CLOSED
        self.deposit.save(update_fields=["status"])

        with self.assertRaises(TransitionError) as caught:
            correct_amount(self.deposit, "60", actor=self.admin)

        self.assertEqual(caught.exception.code, "not_editable")

    def test_a_confirmed_request_is_still_correctable(self):
        """This is exactly when a correction happens: the merchant has just
        looked at the money and it was not what the request says."""
        self.deposit.status = RequestStatus.MERCHANT_CONFIRMED
        self.deposit.save(update_fields=["status"])

        self.assertEqual(
            correct_amount(self.deposit, "60", actor=self.admin).amount_usd,
            Decimal("60.00"),
        )


class CorrectionRecordTests(AmountTestCase):
    def test_every_edit_records_old_new_who_and_when(self):
        correct_amount(self.deposit, "60", actor=self.admin, reason="وصل 60 فقط")

        entry = AuditLog.objects.filter(action=AuditAction.AMOUNT_CHANGE).latest("pk")
        self.assertEqual(entry.before["amount_usd"], "100.00")
        self.assertEqual(entry.after["amount_usd"], "60.00")
        self.assertEqual(entry.after["reason"], "وصل 60 فقط")
        self.assertEqual(entry.actor_id, self.admin.pk)
        self.assertIsNotNone(entry.created_at)

    def test_it_is_not_filed_as_a_status_change(self):
        """"Who moved this along" and "who changed what it is worth" are not
        answered by the same filter."""
        correct_amount(self.deposit, "60", actor=self.admin)

        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditAction.STATUS_CHANGE, target_id=str(self.deposit.pk)
            ).exists()
        )

    def test_the_thread_carries_it_as_an_internal_note(self):
        correct_amount(self.deposit, "60", actor=self.admin)

        fresh = Request.objects.get(pk=self.deposit.pk)
        notes = visible_messages(fresh, audience=FINANCE)
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].is_internal_note)
        self.assertIn("100.00", notes[0].body)
        self.assertIn("60.00", notes[0].body)


# ---------------------------------------------------------------------------
# 3.2 — timing, 3.3 — attribution, 3.6 — the title
# ---------------------------------------------------------------------------


class TimingTests(AmountTestCase):
    def test_an_open_request_has_no_resolution_and_a_running_duration(self):
        self.assertIsNone(self.deposit.resolved_at)
        self.assertIsNotNone(self.deposit.elapsed)

    def test_resolution_is_when_it_stopped_being_live(self):
        closed = apply_transition(
            self.deposit, "cancel", actor=self.admin, reason="لم يعد مطلوبًا"
        )
        self.assertIsNotNone(closed.resolved_at)
        self.assertEqual(closed.resolved_at, closed.closed_at)

    def test_a_parked_request_has_not_resolved(self):
        parked = apply_transition(
            self.deposit, "park", actor=self.admin, reason="بانتظار شيء"
        )
        self.assertIsNone(parked.resolved_at)

    def test_the_duration_stops_at_resolution(self):
        Request.objects.filter(pk=self.deposit.pk).update(
            submitted_at=timezone.now() - timedelta(hours=3)
        )
        fresh = Request.objects.get(pk=self.deposit.pk)
        closed = apply_transition(
            fresh, "cancel", actor=self.admin, reason="لم يعد مطلوبًا"
        )

        first = closed.elapsed
        self.assertGreaterEqual(first.total_seconds(), 3 * 3600)
        self.assertEqual(Request.objects.get(pk=closed.pk).elapsed, first)

    def test_the_duration_reads_as_a_desk_would_say_it(self):
        Request.objects.filter(pk=self.deposit.pk).update(
            submitted_at=timezone.now() - timedelta(minutes=32)
        )
        self.assertIn("32", Request.objects.get(pk=self.deposit.pk).elapsed_display)


class AttributionTests(AmountTestCase):
    def test_a_transition_records_who_made_it(self):
        moved = apply_transition(
            self.deposit, "confirm", actor=self.merchant_user
        )
        self.assertEqual(moved.handled_by_id, self.merchant_user.pk)
        self.assertIsNotNone(moved.handled_at)

    def test_a_correction_records_it_too(self):
        corrected = correct_amount(self.deposit, "60", actor=self.admin)
        self.assertEqual(corrected.handled_by_id, self.admin.pk)

    def test_the_latest_operator_wins(self):
        apply_transition(self.deposit, "confirm", actor=self.merchant_user)
        fresh = Request.objects.get(pk=self.deposit.pk)

        moved = apply_transition(fresh, "credit", actor=self.admin)

        self.assertEqual(moved.handled_by_id, self.admin.pk)

    def test_the_systems_own_route_is_attributed_to_nobody(self):
        """A performance report must not say a person did something nobody
        did."""
        from apps.transactions.services import auto_route

        unrouted = self.make_request(
            status=RequestStatus.SUBMITTED, merchant_assigned=None
        )

        routed = auto_route(unrouted)

        self.assertEqual(routed.status, RequestStatus.ASSIGNED)
        self.assertIsNone(routed.handled_by_id)


class TitleTests(AmountTestCase):
    def test_a_title_is_generated_from_the_type_and_amount(self):
        self.assertIn("100.00", self.deposit.display_title)
        self.assertIn(str(self.deposit.get_type_display()), self.deposit.display_title)

    def test_it_follows_a_corrected_amount(self):
        """A request still titled "إيداع 100.00 $" when $60 arrived is a
        request being reconciled against the wrong figure."""
        corrected = correct_amount(self.deposit, "60", actor=self.admin)

        self.assertIn("60.00", corrected.display_title)
        self.assertNotIn("100.00", corrected.display_title)

    def test_a_written_title_is_kept_and_stops_following(self):
        self.deposit.title = "إيداع العميل الجديد"
        self.deposit.save(update_fields=["title"])

        corrected = correct_amount(self.deposit, "60", actor=self.admin)

        self.assertEqual(corrected.display_title, "إيداع العميل الجديد")


# ---------------------------------------------------------------------------
# The screens
# ---------------------------------------------------------------------------


class CorrectionEndpointTests(AmountTestCase):
    def test_finance_can_correct_from_the_panel(self):
        verify_otp(self.client, self.admin)

        response = self.client.post(
            reverse("finance:request_amount", args=[self.deposit.public_ref]),
            {"amount-amount_usd": "60", "amount-reason": "وصل 60"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Request.objects.get(pk=self.deposit.pk).amount_usd, Decimal("60.00")
        )

    def test_a_merchant_can_correct_from_theirs(self):
        verify_otp(self.client, self.merchant_user)

        response = self.client.post(
            reverse("merchant_panel:request_amount", args=[self.deposit.public_ref]),
            {"amount-amount_usd": "60"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Request.objects.get(pk=self.deposit.pk).amount_usd, Decimal("60.00")
        )

    def test_a_merchant_cannot_correct_somebody_elses(self):
        verify_otp(self.client, self.other_user)

        response = self.client.post(
            reverse("merchant_panel:request_amount", args=[self.deposit.public_ref]),
            {"amount-amount_usd": "60"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            Request.objects.get(pk=self.deposit.pk).amount_usd, Decimal("100.00")
        )

    def test_the_merchant_route_is_shut_for_a_withdrawal(self):
        """Refused at the endpoint, not just missing its button: somebody who
        knows the URL gets a 403 and the amount does not move."""
        withdrawal = self.make_withdrawal()
        verify_otp(self.client, self.merchant_user)

        response = self.client.post(
            reverse("merchant_panel:request_amount", args=[withdrawal.public_ref]),
            {"amount-amount_usd": "60"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            Request.objects.get(pk=withdrawal.pk).amount_usd, Decimal("100.00")
        )

    def test_finance_can_still_correct_a_withdrawal_from_the_panel(self):
        withdrawal = self.make_withdrawal()
        verify_otp(self.client, self.admin)

        response = self.client.post(
            reverse("finance:request_amount", args=[withdrawal.public_ref]),
            {"amount-amount_usd": "60", "amount-reason": "حُوّل 60"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Request.objects.get(pk=withdrawal.pk).amount_usd, Decimal("60.00")
        )

    def test_the_merchant_sees_the_form_on_a_deposit_only(self):
        withdrawal = self.make_withdrawal()
        verify_otp(self.client, self.merchant_user)

        for request_obj, offered in ((self.deposit, True), (withdrawal, False)):
            with self.subTest(type=request_obj.type):
                response = self.client.get(
                    reverse(
                        "merchant_panel:request_detail", args=[request_obj.public_ref]
                    )
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["amount_form"] is not None, offered)
                url = reverse(
                    "merchant_panel:request_amount", args=[request_obj.public_ref]
                )
                if offered:
                    self.assertContains(response, url)
                else:
                    self.assertNotContains(response, url)

    def test_the_correction_endpoint_refuses_a_get(self):
        verify_otp(self.client, self.admin)
        response = self.client.get(
            reverse("finance:request_amount", args=[self.deposit.public_ref])
        )
        self.assertEqual(response.status_code, 405)

    def test_the_merchant_export_still_carries_no_client_identity(self):
        """3.3 added an operator column to the *Finance* report only. The
        merchant's own file must not have grown one."""
        from apps.merchant_panel.anonymity import is_forbidden_key
        from apps.merchant_panel.report_views import COLUMNS

        for column in COLUMNS:
            self.assertFalse(is_forbidden_key(column.key), column.key)
