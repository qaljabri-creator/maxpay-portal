"""The client deposit flow, end to end (spec §6, §7) — build-order step 6.

Grouped by the thing being protected rather than by the endpoint being called:
what the catalogue is allowed to offer, what the money is, what happens when
the ground moves under a half-filled form, and who can read a stored file.

Every case goes through the real handshake in :meth:`authenticate`, so nothing
here can pass with a session that the verification path would have refused.
"""

import datetime
import json
import re
import time
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Client as PortalClient
from apps.accounts.models import Role
from apps.accounts.permissions import sync_role_groups
from apps.accounts.tests import make_user, verify_otp
from apps.core.choices import ActorRole
from apps.core.models import AuditLog, SystemSettings
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.portal import attachments as attachment_urls
from apps.portal import payloads, pricing
from apps.rates.models import ExchangeRate, RateType
from apps.transactions import services as transitions
from apps.transactions.models import (
    Attachment,
    Message,
    Request,
    RequestStatus,
    RequestType,
)

from .test_views import VIEW_SETTINGS, PortalViewTestCase

FLOW_SETTINGS = dict(
    VIEW_SETTINGS,
    PORTAL_SUBMISSION_RATE="1000/hour",
    PORTAL_CATALOG_RATE="1000/minute",
    PORTAL_DEPOSIT_MIN_USD="1",
    PORTAL_DEPOSIT_MAX_USD="100000",
    PORTAL_WITHDRAWAL_MIN_USD="1",
    PORTAL_WITHDRAWAL_MAX_USD="100000",
)

#: A one-pixel PNG. Real bytes, because the upload path sniffs them.
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def png_upload(name="receipt.png"):
    return SimpleUploadedFile(name, PNG, content_type="image/png")


def open_the_desk(**overrides) -> SystemSettings:
    """Pin business hours open (build-order step 11).

    Without this the whole flow suite would pass or fail depending on the hour
    it was run at: the shipped default schedule is 09:00–21:00 Baghdad, and
    from step 11 on a submission outside it is refused. Equal open and close
    times read as round-the-clock, so this exercises the ordinary schedule
    path rather than the manual override — the override has its own tests.
    """
    row = SystemSettings.load()
    row.open_time = datetime.time(0, 0)
    row.close_time = datetime.time(0, 0)
    row.is_open_override = None
    for name, value in overrides.items():
        setattr(row, name, value)
    row.save()
    return row


@override_settings(**FLOW_SETTINGS)
class FlowTestCase(PortalViewTestCase):
    """A configured desk: one method, one merchant, one wallet, one rate."""

    def setUp(self):
        super().setUp()
        cache.clear()
        open_the_desk()
        self.options_url = reverse("portal:options")
        self.requests_url = reverse("portal:requests")

        self.method = PaymentMethod.objects.create(
            code="zaincash",
            caption_ar="زين كاش",
            caption_en="ZainCash",
            supports_deposit=True,
            supports_withdrawal=True,
        )
        self.merchant = Merchant.objects.create(name="تاجر بغداد")
        self.link = MerchantMethod.objects.create(
            merchant=self.merchant, payment_method=self.method
        )
        self.wallet = Wallet.objects.create(
            merchant_method=self.link, number="07701234567", label="المحفظة الرئيسية"
        )
        self.rate = ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT,
            iqd_per_usd=Decimal("1470.00"),
            commission_iqd_per_100usd=Decimal("5000.00"),
        )

    # -- helpers -----------------------------------------------------------

    def withdrawal_rate(self, iqd_per_usd="1470.00", commission="5000.00"):
        ExchangeRate.objects.create(
            rate_type=RateType.WITHDRAWAL,
            iqd_per_usd=Decimal(iqd_per_usd),
            commission_iqd_per_100usd=Decimal(commission),
        )
        return pricing.current_rate(RequestType.WITHDRAWAL)

    def sign_in(self, **overrides):
        payload = self.authenticate(**overrides)
        self.csrf = payload["csrf_token"]
        return payload

    def options(self, **params):
        response = self.client.get(self.options_url, params)
        return response, json.loads(response.content.decode("utf-8"))

    def submit(self, *, origin="http://testserver", token=None, **overrides):
        """POST a deposit, defaulting every field to something valid."""
        data = {
            "type": "deposit",
            "method": self.method.code,
            "merchant": str(self.merchant.pk),
            "wallet": str(self.wallet.pk),
            "rate": str(self.rate.pk),
            "amount_usd": "100",
            "proof": png_upload(),
        }
        data.update(overrides)
        data = {key: value for key, value in data.items() if value is not None}

        headers = {}
        if origin is not None:
            headers["origin"] = origin
        supplied = token if token is not None else getattr(self, "csrf", "")
        if supplied:
            headers["x-portal-csrf"] = supplied
        return self.client.post(self.requests_url, data=data, headers=headers)

    def body(self, response):
        return json.loads(response.content.decode("utf-8"))


# ---------------------------------------------------------------------------
# The catalogue — screens 1 to 4
# ---------------------------------------------------------------------------


class CatalogueTests(FlowTestCase):
    def test_no_session_gets_no_catalogue(self):
        response, payload = self.options()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(payload["error"], "no_session")

    def test_the_deposit_catalogue_opens_on_the_merchants(self):
        """Screen 2 is who, not how: the client settles the counterparty
        first and sees what that counterparty covers second."""
        self.sign_in()

        _response, payload = self.options()

        self.assertEqual(
            payload["merchants"],
            [{"id": self.merchant.pk, "name": "تاجر بغداد", "method_count": 1}],
        )

    def test_the_methods_arrive_only_once_a_merchant_is_chosen(self):
        self.sign_in()

        _response, payload = self.options()

        self.assertEqual(payload["methods"], [])

    def test_the_rate_travels_with_the_catalogue(self):
        self.sign_in()

        _response, payload = self.options()

        self.assertEqual(payload["rate"]["id"], self.rate.pk)
        self.assertEqual(payload["rate"]["iqd_per_usd"], "1470.00")
        self.assertEqual(payload["rate"]["commission_iqd_per_100usd"], "5000.00")

    def test_a_merchant_whose_only_wallet_stood_down_is_not_offered(self):
        """A row that leads nowhere is worse than no row (spec §7). On a
        deposit the wallet is what makes the method offerable, and a merchant
        with no offerable method is not a merchant to show."""
        self.wallet.deactivate()
        self.sign_in()

        _response, payload = self.options()

        self.assertEqual(payload["merchants"], [])

    def test_a_deactivated_merchant_is_gone_from_screen_two(self):
        self.merchant.is_active = False
        self.merchant.save(update_fields=["is_active"])
        self.sign_in()

        _response, payload = self.options()

        self.assertEqual(payload["merchants"], [])

    def test_a_withdrawal_only_method_is_absent_from_the_deposit_catalogue(self):
        self.method.supports_deposit = False
        self.method.save(update_fields=["supports_deposit"])
        self.sign_in()

        _response, payload = self.options(merchant=self.merchant.pk)

        self.assertEqual(payload["methods"], [])
        # And with nothing left to cover, the merchant goes too.
        self.assertEqual(payload["merchants"], [])

    def test_choosing_a_merchant_yields_the_methods_they_cover(self):
        self.sign_in()

        _response, payload = self.options(merchant=self.merchant.pk)

        self.assertEqual([m["code"] for m in payload["methods"]], ["zaincash"])
        self.assertEqual(payload["methods"][0]["caption"], "زين كاش")

    def test_a_method_another_merchant_covers_is_not_listed_under_this_one(self):
        other = Merchant.objects.create(name="تاجر البصرة")
        elsewhere = PaymentMethod.objects.create(
            code="fastpay", caption_ar="فاست باي", caption_en="FastPay"
        )
        link = MerchantMethod.objects.create(
            merchant=other, payment_method=elsewhere
        )
        Wallet.objects.create(merchant_method=link, number="07709999999")
        self.sign_in()

        _response, payload = self.options(merchant=self.merchant.pk)

        self.assertEqual([m["code"] for m in payload["methods"]], ["zaincash"])

    def test_choosing_a_merchant_yields_the_wallet_screen_four_shows(self):
        self.sign_in()

        _response, payload = self.options(
            method=self.method.code, merchant=self.merchant.pk
        )

        self.assertEqual(payload["wallet"]["number"], "07701234567")
        self.assertEqual(payload["wallet"]["id"], self.wallet.pk)

    def test_a_choice_that_went_stale_names_the_screen_to_go_back_to(self):
        """The wallet was the only thing making this merchant offerable, so
        what the client has to go back and change is the merchant."""
        self.sign_in()
        self.wallet.deactivate()

        _response, payload = self.options(
            method=self.method.code, merchant=self.merchant.pk
        )

        self.assertEqual(payload["unavailable"], "merchant")

    def test_both_directions_are_tappable_on_screen_one(self):
        """Step 10 opened the withdrawal tile; the deposit one is unchanged."""
        self.sign_in()

        _response, payload = self.options()

        types = {entry["value"]: entry["available"] for entry in payload["types"]}
        self.assertEqual(types, {"deposit": True, "withdrawal": True})

    def test_a_desk_with_no_rate_says_so_rather_than_quoting_zero(self):
        ExchangeRate.objects.all().delete()
        self.sign_in()

        _response, payload = self.options()

        self.assertEqual(payload["unavailable"], "rate")
        self.assertIsNone(payload["rate"])


# ---------------------------------------------------------------------------
# The money
# ---------------------------------------------------------------------------


class PricingTests(FlowTestCase):
    def test_the_total_is_amount_times_rate_plus_commission(self):
        quote = pricing.quote(self.rate, Decimal("100.00"), RequestType.DEPOSIT)

        self.assertEqual(quote.converted_iqd, Decimal("147000.00"))
        self.assertEqual(quote.commission_iqd, Decimal("5000.00"))
        self.assertEqual(quote.total_iqd, Decimal("152000.00"))

    def test_the_commission_is_prorated_below_a_hundred_dollars(self):
        quote = pricing.quote(self.rate, Decimal("50.00"), RequestType.DEPOSIT)

        self.assertEqual(quote.commission_iqd, Decimal("2500.00"))
        self.assertEqual(quote.total_iqd, Decimal("76000.00"))

    def test_arabic_indic_digits_are_a_number_not_a_typo(self):
        self.assertEqual(pricing.parse_amount_usd("١٢٣٫٥٠", RequestType.DEPOSIT), Decimal("123.50"))

    def test_a_pasted_figure_keeps_its_value(self):
        self.assertEqual(pricing.parse_amount_usd("1,250.75", RequestType.DEPOSIT), Decimal("1250.75"))

    def test_nonsense_is_refused(self):
        with self.assertRaises(pricing.PricingError) as caught:
            pricing.parse_amount_usd("مئة دولار", RequestType.DEPOSIT)
        self.assertEqual(caught.exception.code, "amount_invalid")

    def test_a_figure_finer_than_a_cent_is_refused_rather_than_rounded(self):
        """Rounding 0.999 up would charge for a figure nobody typed."""
        with self.assertRaises(pricing.PricingError) as caught:
            pricing.parse_amount_usd("0.999", RequestType.DEPOSIT)
        self.assertEqual(caught.exception.code, "amount_precision")

    def test_two_decimal_places_are_accepted_exactly(self):
        self.assertEqual(pricing.parse_amount_usd("100.55", RequestType.DEPOSIT), Decimal("100.55"))

    def test_a_figure_under_the_floor_is_refused(self):
        with self.assertRaises(pricing.PricingError) as caught:
            pricing.parse_amount_usd("0.50", RequestType.DEPOSIT)
        self.assertEqual(caught.exception.code, "amount_below_min")

    def test_the_ceiling_holds(self):
        with self.assertRaises(pricing.PricingError) as caught:
            pricing.parse_amount_usd("100000.01", RequestType.DEPOSIT)
        self.assertEqual(caught.exception.code, "amount_above_max")

    # -- the dinar carries no fils (Finance review, 24 Aug 2026) -----------

    def test_every_dinar_figure_is_a_whole_dinar(self):
        """The fils is out of circulation. A quote showing one is quoting a
        denomination nobody can transfer."""
        for amount in ("100.55", "33.33", "7.07", "1250.75", "99.99"):
            with self.subTest(amount=amount):
                quote = pricing.quote(
                    self.rate, Decimal(amount), RequestType.DEPOSIT
                )
                for figure in (quote.converted_iqd, quote.commission_iqd, quote.total_iqd):
                    self.assertEqual(figure, figure.to_integral_value(), figure)

    def test_the_four_figures_still_add_up_to_the_total(self):
        """Conversion, fee, and the rounding the company wore. A receipt that
        does not add up is worse than one line more."""
        quote = pricing.quote(self.rate, Decimal("33.33"), RequestType.DEPOSIT)

        self.assertEqual(
            quote.converted_iqd + quote.commission_iqd + quote.rounding_iqd,
            quote.total_iqd,
        )

    def test_a_withdrawal_breakdown_subtracts_to_its_total(self):
        ExchangeRate.objects.create(
            rate_type=RateType.WITHDRAWAL,
            iqd_per_usd=Decimal("1470.00"),
            commission_iqd_per_100usd=Decimal("5000.00"),
        )
        rate = pricing.current_rate(RequestType.WITHDRAWAL)

        quote = pricing.quote(rate, Decimal("33.33"), RequestType.WITHDRAWAL)

        self.assertEqual(
            quote.converted_iqd - quote.commission_iqd + quote.rounding_iqd,
            quote.total_iqd,
        )
        self.assertEqual(quote.total_iqd, quote.total_iqd.to_integral_value())

    def test_the_usd_amount_keeps_its_cents(self):
        """A cent is real money in the currency the client's balance is held
        in, and rounding it would change the amount they asked for."""
        quote = pricing.quote(self.rate, Decimal("100.55"), RequestType.DEPOSIT)

        self.assertEqual(quote.amount_usd, Decimal("100.55"))

    def test_a_stored_request_carries_whole_dinars(self):
        """Not only the quote: what is written at submission is the same
        figure, from the same function."""
        self.sign_in()

        self.submit(amount_usd="33.33")

        deposit = Request.objects.get()
        self.assertEqual(deposit.amount_iqd, deposit.amount_iqd.to_integral_value())
        self.assertEqual(
            deposit.commission_applied, deposit.commission_applied.to_integral_value()
        )
        self.assertEqual(deposit.amount_usd, Decimal("33.33"))

    def test_a_payout_under_one_dinar_is_refused_rather_than_rounded_to_zero(self):
        ExchangeRate.objects.create(
            rate_type=RateType.WITHDRAWAL,
            iqd_per_usd=Decimal("1470.00"),
            commission_iqd_per_100usd=Decimal("147000.00"),
        )
        rate = pricing.current_rate(RequestType.WITHDRAWAL)

        with self.assertRaises(pricing.PricingError) as caught:
            pricing.quote(rate, Decimal("1.00"), RequestType.WITHDRAWAL)

        self.assertEqual(caught.exception.code, "amount_below_commission")

    def test_the_newest_rate_in_force_wins(self):
        newer = ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT,
            iqd_per_usd=Decimal("1500.00"),
            commission_iqd_per_100usd=Decimal("0.00"),
        )

        self.assertEqual(pricing.current_rate("deposit").pk, newer.pk)

    def test_a_future_rate_is_not_in_force_yet(self):
        ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT,
            iqd_per_usd=Decimal("9999.00"),
            effective_from=timezone.now() + timedelta(days=1),
        )

        self.assertEqual(pricing.current_rate("deposit").pk, self.rate.pk)


class RoundThousandTests(FlowTestCase):
    """The transferred dinar figure is rounded to the nearest thousand.

    Finance, 15 Sep 2026, and the reason is not tidiness. Every one of these
    transfers lands in a *personal* wallet or card in Iraq, and a personal
    account taking 151,847 then 74,312 then 208,655 across a month does not
    read as a person being paid — it reads as a business trading through a
    personal account, which is how one gets frozen. Round thousands are what
    transfers between people look like.

    So the rule is: the dollar figure is whatever the client typed, the dinar
    figure that moves is rounded to the nearest 1,000 in both directions, and
    the company wears the difference.
    """

    #: Amounts whose raw totals land all over the place — above and below a
    #: half-thousand, exactly on one, and on figures that were already round.
    AMOUNTS = ("1.00", "7.07", "33.33", "50.00", "99.99", "100.55", "1250.75", "9999.99")

    #: What :attr:`FlowTestCase.rate` prorates, for the cases that check how far
    #: the fee actually taken drifted from it.
    NOMINAL_PER_100 = Decimal("5000.00")

    # -- the rule itself ---------------------------------------------------

    def test_a_deposit_total_is_a_whole_thousand(self):
        for amount in self.AMOUNTS:
            with self.subTest(amount=amount):
                quote = pricing.quote(self.rate, Decimal(amount), RequestType.DEPOSIT)
                self.assertEqual(quote.total_iqd % 1000, 0, quote.total_iqd)

    def test_a_withdrawal_total_is_a_whole_thousand_too(self):
        """Both directions. A withdrawal is the one that actually *arrives* in
        the client's personal account, so if only one direction were rounded it
        would have to be this one."""
        rate = self.withdrawal_rate()

        for amount in self.AMOUNTS:
            with self.subTest(amount=amount):
                quote = pricing.quote(rate, Decimal(amount), RequestType.WITHDRAWAL)
                self.assertEqual(quote.total_iqd % 1000, 0, quote.total_iqd)

    def test_it_rounds_to_the_nearest_not_always_up(self):
        """Nearest in both senses: a raw total below the half-thousand comes
        down, one above it goes up. Always rounding up would quietly overcharge
        every deposit, and always down would give the money away."""
        # 10 × 1470 = 14,700, + 500 commission = 15,200 → down to 15,000.
        down = pricing.quote(self.rate, Decimal("10.00"), RequestType.DEPOSIT)
        self.assertEqual(down.converted_iqd, Decimal("14700"))
        self.assertEqual(down.total_iqd, Decimal("15000"))

        # 11 × 1470 = 16,170, + 550 commission = 16,720 → up to 17,000.
        up = pricing.quote(self.rate, Decimal("11.00"), RequestType.DEPOSIT)
        self.assertEqual(up.converted_iqd, Decimal("16170"))
        self.assertEqual(up.total_iqd, Decimal("17000"))

    def test_a_halfway_total_rounds_up(self):
        """One tie-breaking rule in the codebase, and it is the one the whole-
        dinar quantiser already used — so ``Math.round`` in flow.js agrees with
        it for free."""
        rate = ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT,
            iqd_per_usd=Decimal("1500.00"),
            commission_iqd_per_100usd=Decimal("0.00"),
        )

        # 1.00 × 1500 = 1,500 exactly, with no commission to tip it either way.
        quote = pricing.quote(rate, Decimal("1.00"), RequestType.DEPOSIT)

        self.assertEqual(quote.converted_iqd, Decimal("1500"))
        self.assertEqual(quote.total_iqd, Decimal("2000"))

    # -- who pays for it ---------------------------------------------------

    def test_the_dollar_amount_is_untouched(self):
        """The client's trading balance moves by what they asked for. Rounding
        that would be changing the amount, not tidying the transfer."""
        quote = pricing.quote(self.rate, Decimal("100.55"), RequestType.DEPOSIT)

        self.assertEqual(quote.amount_usd, Decimal("100.55"))

    def test_the_conversion_line_is_still_exactly_amount_times_rate(self):
        """It is the figure a client checks against the published rate, so the
        rounding is not allowed to move it — the commission absorbs instead."""
        quote = pricing.quote(self.rate, Decimal("100.55"), RequestType.DEPOSIT)

        self.assertEqual(quote.converted_iqd, Decimal("147809"))
        self.assertNotEqual(quote.converted_iqd % 1000, 0)

    def test_the_company_wears_at_most_five_hundred_dinars(self):
        """Nearest-thousand caps the company's contribution at half a step,
        either way."""
        withdrawal = self.withdrawal_rate()

        for direction, priced_at in (
            (RequestType.DEPOSIT, self.rate),
            (RequestType.WITHDRAWAL, withdrawal),
        ):
            for amount in self.AMOUNTS:
                with self.subTest(direction=direction, amount=amount):
                    quote = pricing.quote(priced_at, Decimal(amount), direction)
                    self.assertLessEqual(abs(quote.rounding_iqd), 500)

    def test_the_fee_is_what_the_rate_says_and_nothing_else(self):
        """It stopped absorbing the rounding. The rate prorates a figure and
        that figure is what is charged, so a desk on no commission charges
        none — see :class:`ZeroCommissionTests`."""
        withdrawal = self.withdrawal_rate()

        for direction, priced_at in (
            (RequestType.DEPOSIT, self.rate),
            (RequestType.WITHDRAWAL, withdrawal),
        ):
            for amount in self.AMOUNTS:
                with self.subTest(direction=direction, amount=amount):
                    quote = pricing.quote(priced_at, Decimal(amount), direction)
                    nominal = (
                        Decimal(amount) / 100 * self.NOMINAL_PER_100
                    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                    self.assertEqual(quote.commission_iqd, nominal)

    def test_the_breakdown_still_adds_up_in_both_directions(self):
        """Whatever the rounding did, the lines on screen reconcile to the
        figure the client will see in their own bank app."""
        withdrawal = self.withdrawal_rate()

        for amount in self.AMOUNTS:
            with self.subTest(amount=amount, direction="deposit"):
                q = pricing.quote(self.rate, Decimal(amount), RequestType.DEPOSIT)
                self.assertEqual(
                    q.converted_iqd + q.commission_iqd + q.rounding_iqd, q.total_iqd
                )
            with self.subTest(amount=amount, direction="withdrawal"):
                q = pricing.quote(withdrawal, Decimal(amount), RequestType.WITHDRAWAL)
                self.assertEqual(
                    q.converted_iqd - q.commission_iqd + q.rounding_iqd, q.total_iqd
                )

    # -- stored, not merely displayed --------------------------------------

    def test_the_stored_figure_is_the_rounded_one(self):
        """The point of doing this in pricing rather than in a template filter.
        ``amount_iqd`` is what a merchant matches the receipt against, and a
        screen that rounded on its own would show a figure no receipt agrees
        with."""
        self.sign_in()

        self.submit(amount_usd="100.55")

        deposit = Request.objects.get()
        self.assertEqual(deposit.amount_iqd, Decimal("153000.00"))
        self.assertEqual(deposit.amount_usd, Decimal("100.55"))
        # The fee the rate prorates, untouched by the rounding.
        self.assertEqual(deposit.commission_applied, Decimal("5028.00"))
        # And the conversion is recovered by multiplying, not by subtracting.
        self.assertEqual(payloads.converted_iqd(deposit), Decimal("147809"))
        self.assertEqual(payloads.rounding_iqd(deposit), Decimal("163"))

    def test_what_the_client_was_quoted_is_what_was_stored(self):
        """Live preview and submission come out of the same function, so the
        confirmation cannot disagree with the screen the client accepted."""
        self.sign_in()
        quoted = pricing.quote(self.rate, Decimal("1250.75"), RequestType.DEPOSIT)

        self.submit(amount_usd="1250.75")

        deposit = Request.objects.get()
        self.assertEqual(deposit.amount_iqd, quoted.total_iqd)
        self.assertEqual(deposit.commission_applied, quoted.commission_iqd)

    def test_the_daily_cap_is_measured_against_the_rounded_figure(self):
        """The cap counts dinars into a wallet, and what goes into the wallet is
        the rounded total — so that is what has to be measured against it, not
        the figure before rounding."""
        self.sign_in()
        # $100.55 rounds up to 153,000 from a raw 153,000-. A cap a dinar under
        # the rounded figure has to refuse it.
        self.wallet.daily_cap = Decimal("152999.00")
        self.wallet.save(update_fields=["daily_cap"])

        response = self.submit(amount_usd="100.55")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.body(response)["error"], "wallet_cap_exceeded")

    def test_a_cap_that_exactly_fits_the_rounded_figure_lets_it_through(self):
        self.sign_in()
        self.wallet.daily_cap = Decimal("153000.00")
        self.wallet.save(update_fields=["daily_cap"])

        response = self.submit(amount_usd="100.55")

        self.assertEqual(response.status_code, 201, self.body(response))
        self.assertEqual(Request.objects.get().amount_iqd, Decimal("153000.00"))

    # -- the floor still holds ---------------------------------------------

    def test_a_payout_that_rounds_away_to_nothing_is_refused(self):
        """Rounded, a payout is either nothing or a whole thousand, and nothing
        is a refusal rather than a smaller withdrawal."""
        rate = self.withdrawal_rate(commission="146000.00")

        with self.assertRaises(pricing.PricingError) as caught:
            pricing.quote(rate, Decimal("1.00"), RequestType.WITHDRAWAL)

        self.assertEqual(caught.exception.code, "amount_below_commission")

    def test_the_smallest_payout_that_survives_is_a_whole_thousand(self):
        # 1,470 converted, 600 of fee: a raw 870 that rounds up to one step.
        rate = self.withdrawal_rate(commission="60000.00")

        quote = pricing.quote(rate, Decimal("1.00"), RequestType.WITHDRAWAL)

        self.assertEqual(quote.total_iqd, Decimal("1000"))


class ZeroCommissionTests(FlowTestCase):
    """A desk that charges nothing, which is what this one charges.

    Reported 15 Sep 2026: $155 at 1,510 came to 234,050 and stayed there. The
    rounding was being taken out of the commission, and a commission of zero has
    nothing to take it out of — so the adjustment had nowhere to go and turned
    up as a fee of −50 dinars, which is not a fee anybody charged.

    The rounding is its own figure now. These cases are the configuration the
    desk actually runs, so they are the ones most worth keeping honest.
    """

    def free_rate(self, rate_type=RateType.DEPOSIT, iqd_per_usd="1510.00"):
        return ExchangeRate.objects.create(
            rate_type=rate_type,
            iqd_per_usd=Decimal(iqd_per_usd),
            commission_iqd_per_100usd=Decimal("0.00"),
        )

    def test_the_reported_case(self):
        """$155 × 1,510 = 234,050, and what moves is 234,000."""
        quote = pricing.quote(self.free_rate(), Decimal("155.00"), RequestType.DEPOSIT)

        self.assertEqual(quote.converted_iqd, Decimal("234050"))
        self.assertEqual(quote.commission_iqd, Decimal("0"))
        self.assertEqual(quote.rounding_iqd, Decimal("-50"))
        self.assertEqual(quote.total_iqd, Decimal("234000"))

    def test_the_commission_stays_zero_rather_than_absorbing_the_rounding(self):
        """The defect, stated directly: a fee of −50 is not a fee."""
        for amount in ("1.00", "33.33", "100.55", "155.00", "1250.75"):
            with self.subTest(amount=amount):
                quote = pricing.quote(
                    self.free_rate(), Decimal(amount), RequestType.DEPOSIT
                )
                self.assertEqual(quote.commission_iqd, Decimal("0"))

    def test_the_total_is_still_rounded_with_no_fee_to_round_through(self):
        rate = self.free_rate()

        for amount in ("1.00", "33.33", "100.55", "155.00", "1250.75", "9999.99"):
            with self.subTest(amount=amount):
                quote = pricing.quote(rate, Decimal(amount), RequestType.DEPOSIT)
                self.assertEqual(quote.total_iqd % 1000, 0, quote.total_iqd)

    def test_a_free_withdrawal_rounds_the_same_way(self):
        rate = self.free_rate(RateType.WITHDRAWAL)

        quote = pricing.quote(rate, Decimal("155.00"), RequestType.WITHDRAWAL)

        self.assertEqual(quote.converted_iqd, Decimal("234050"))
        self.assertEqual(quote.commission_iqd, Decimal("0"))
        self.assertEqual(quote.rounding_iqd, Decimal("-50"))
        self.assertEqual(quote.total_iqd, Decimal("234000"))

    def test_the_breakdown_reconciles_with_no_commission_in_it(self):
        rate = self.free_rate()

        for amount in ("1.00", "33.33", "155.00", "9999.99"):
            with self.subTest(amount=amount):
                q = pricing.quote(rate, Decimal(amount), RequestType.DEPOSIT)
                self.assertEqual(
                    q.converted_iqd + q.commission_iqd + q.rounding_iqd, q.total_iqd
                )

    def test_a_submitted_request_stores_the_rounded_total_and_no_fee(self):
        ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT,
            iqd_per_usd=Decimal("1510.00"),
            commission_iqd_per_100usd=Decimal("0.00"),
        )
        self.sign_in()

        response = self.submit(
            amount_usd="155", rate=str(pricing.current_rate("deposit").pk)
        )

        self.assertEqual(response.status_code, 201, self.body(response))
        deposit = Request.objects.get()
        self.assertEqual(deposit.amount_iqd, Decimal("234000.00"))
        self.assertEqual(deposit.commission_applied, Decimal("0.00"))
        self.assertEqual(payloads.converted_iqd(deposit), Decimal("234050"))
        self.assertEqual(payloads.rounding_iqd(deposit), Decimal("-50"))


class ReconstructionTests(FlowTestCase):
    """Recovering the breakdown from what a request stored.

    The conversion used to be recovered by taking the commission back off
    ``amount_iqd``. That needed the sign of the direction — a rule a second
    caller can get wrong, and one in the Finance queue did — and it silently
    swallowed anything else inside the total, which since the rounding is up to
    500 dinars of it. It is computed from its own definition now.
    """

    def stored(self, direction=RequestType.DEPOSIT, amount="100.55", **overrides):
        quote = pricing.quote(self.rate, Decimal(amount), RequestType.DEPOSIT)
        defaults = dict(
            type=direction,
            client=PortalClient.objects.create(b2core_id="sub-recon"),
            payment_method=self.method,
            merchant_selected=self.merchant,
            merchant_assigned=self.merchant,
            wallet_number_snapshot="07701234567",
            amount_usd=quote.amount_usd,
            amount_iqd=quote.total_iqd,
            rate_applied=self.rate.iqd_per_usd,
            commission_applied=quote.commission_iqd,
            commission_rate_applied=self.rate.commission_iqd_per_100usd,
        )
        defaults.update(overrides)
        return Request.objects.create(**defaults)

    def test_the_conversion_comes_back_by_multiplying_not_subtracting(self):
        deposit = self.stored()

        self.assertEqual(payloads.converted_iqd(deposit), Decimal("147809"))

    def test_it_is_right_for_a_withdrawal_too(self):
        """The direction used to decide the sign of the subtraction, so getting
        it wrong returned a figure that was off by twice the fee. Multiplication
        has no sign to get wrong."""
        quote = pricing.quote(
            self.withdrawal_rate(), Decimal("100.55"), RequestType.WITHDRAWAL
        )
        withdrawal = self.stored(
            direction=RequestType.WITHDRAWAL,
            amount_iqd=quote.total_iqd,
            commission_applied=quote.commission_iqd,
            destination_account="4257880011937742",
            wallet_number_snapshot="",
        )

        self.assertEqual(payloads.converted_iqd(withdrawal), Decimal("147809"))

    def test_the_rounding_comes_back_as_what_the_total_has_left_over(self):
        deposit = self.stored()

        recovered = payloads.rounding_iqd(deposit)

        self.assertEqual(
            payloads.converted_iqd(deposit)
            + deposit.commission_applied
            + recovered,
            deposit.amount_iqd,
        )

    def test_a_request_filed_before_the_rule_reports_no_rounding(self):
        """Its total was the conversion plus the fee exactly, so there is no
        rounding in it — and saying zero is the truth about it rather than an
        apology for it."""
        legacy = self.stored(
            amount_usd=Decimal("100.00"),
            amount_iqd=Decimal("152000.00"),
            commission_applied=Decimal("5000.00"),
            rate_applied=Decimal("1470.00"),
        )

        self.assertEqual(payloads.rounding_iqd(legacy), 0)

    def test_the_finance_queue_shows_the_same_conversion_as_the_client(self):
        """It used to keep its own subtraction, which is how it came to be
        wrong for withdrawals while the client's screen was right."""
        quote = pricing.quote(
            self.withdrawal_rate(), Decimal("100.55"), RequestType.WITHDRAWAL
        )
        withdrawal = self.stored(
            direction=RequestType.WITHDRAWAL,
            amount_iqd=quote.total_iqd,
            commission_applied=quote.commission_iqd,
            destination_account="4257880011937742",
            wallet_number_snapshot="",
        )
        sync_role_groups()
        staff = make_user("queue@maxifyfx.com", Role.FINANCE_ADMIN)
        verify_otp(self.client, staff)

        # The queue addresses a request by its reference, never by its pk.
        response = self.client.get(
            reverse("finance:request_detail", args=[withdrawal.public_ref])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["converted_iqd"], payloads.converted_iqd(withdrawal)
        )
        self.assertEqual(
            response.context["rounding_iqd"], payloads.rounding_iqd(withdrawal)
        )
        self.assertEqual(response.context["commission_sign"], "−")


class PricingContractTests(TestCase):
    """``tests/pricing_cases.json``, from the Python side.

    The live quote is computed in JavaScript and the stored figure in Python,
    and the two are not allowed to disagree — a preview reading 234,050 against
    a confirmation reading 234,000 is the desk taking a phone call. Neither file
    can see the other, so both are held to one committed table:
    ``tests/js/flow_quote.test.js`` asserts the shipped ``flow.js`` displays it,
    and this class asserts :func:`apps.portal.pricing.price` produces it.

    The table carries two rate rows. One charges no commission, which is what
    this desk actually runs and the case that broke when the rounding was being
    taken out of the fee; one charges, so neither path can rot while the other
    is exercised.

    Every invariant below is checked against the *table* rather than against
    what the code happens to return, so regenerating it cannot launder a bug
    into the contract.
    """

    CONTRACT = Path(settings.BASE_DIR) / "tests" / "pricing_cases.json"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(cls.CONTRACT, encoding="utf-8") as handle:
            cls.contract = json.load(handle)
        cls.cases = cls.contract["cases"]
        cls.step = Decimal(cls.contract["transfer_step"])

    @staticmethod
    def nominal_fee(case):
        """What the rate alone says the fee is, with no rounding anywhere near it."""
        return (
            Decimal(case["amount_usd"])
            / 100
            * Decimal(case["commission_iqd_per_100usd"])
        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    @staticmethod
    def signed(case, commission):
        return -commission if case["direction"] == "withdrawal" else commission

    def test_pricing_produces_the_table_case_for_case(self):
        for case in self.cases:
            with self.subTest(**case):
                converted, commission, rounding, total = pricing.price(
                    Decimal(case["iqd_per_usd"]),
                    Decimal(case["commission_iqd_per_100usd"]),
                    Decimal(case["amount_usd"]),
                    case["direction"],
                )

                self.assertEqual(converted, Decimal(case["converted_iqd"]))
                self.assertEqual(commission, Decimal(case["commission_iqd"]))
                self.assertEqual(rounding, Decimal(case["rounding_iqd"]))
                self.assertEqual(total, Decimal(case["total_iqd"]))

    def test_the_table_covers_both_directions_and_both_kinds_of_rate(self):
        """A table exercising only the charging rate would have let the desk's
        own configuration — no commission at all — break unnoticed, which is
        exactly what happened."""
        self.assertEqual(
            {case["direction"] for case in self.cases}, {"deposit", "withdrawal"}
        )
        fees = {case["commission_iqd_per_100usd"] for case in self.cases}
        self.assertIn("0.00", fees)
        self.assertTrue(any(fee != "0.00" for fee in fees))

    # -- the rule the table encodes ----------------------------------------

    def test_every_total_is_a_whole_step(self):
        for case in self.cases:
            with self.subTest(**case):
                self.assertEqual(Decimal(case["total_iqd"]) % self.step, 0)

    def test_every_conversion_is_exactly_amount_times_rate(self):
        for case in self.cases:
            with self.subTest(**case):
                expected = (
                    Decimal(case["amount_usd"]) * Decimal(case["iqd_per_usd"])
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                self.assertEqual(Decimal(case["converted_iqd"]), expected)

    def test_every_commission_is_exactly_what_the_rate_prorates(self):
        """The fee carries no rounding remainder any more. It is what the rate
        says and nothing else — which is why a zero-commission rate now yields a
        commission of zero rather than a −50 nobody charged."""
        for case in self.cases:
            with self.subTest(**case):
                self.assertEqual(
                    Decimal(case["commission_iqd"]), self.nominal_fee(case)
                )

    def test_a_rate_with_no_commission_produces_no_commission(self):
        free = [c for c in self.cases if c["commission_iqd_per_100usd"] == "0.00"]

        self.assertTrue(free)
        for case in free:
            with self.subTest(**case):
                self.assertEqual(Decimal(case["commission_iqd"]), 0)

    def test_every_row_reconciles(self):
        """conversion ± commission + rounding == total, in both directions. The
        breakdown the client reads has to add up to the figure in their own bank
        app; that was true before the rounding and the rounding does not get to
        make it false."""
        for case in self.cases:
            with self.subTest(**case):
                metered = Decimal(case["converted_iqd"]) + self.signed(
                    case, Decimal(case["commission_iqd"])
                )
                self.assertEqual(
                    metered + Decimal(case["rounding_iqd"]),
                    Decimal(case["total_iqd"]),
                )

    def test_no_rounding_exceeds_half_a_step(self):
        """The company's side of the bargain, stated as a number."""
        for case in self.cases:
            with self.subTest(**case):
                self.assertLessEqual(
                    abs(Decimal(case["rounding_iqd"])), self.step / 2
                )

    def test_the_rounding_goes_both_ways_across_the_table(self):
        """Nearest, not up. A table whose roundings were all one sign would
        have been produced by a rule that always rounds that way."""
        roundings = [Decimal(case["rounding_iqd"]) for case in self.cases]

        self.assertTrue(any(value > 0 for value in roundings))
        self.assertTrue(any(value < 0 for value in roundings))
        self.assertTrue(any(value == 0 for value in roundings))


class WalletQrTests(FlowTestCase):
    """A wallet paid by scanning rather than by typing (Finance review, 24 Aug).

    Some rails — Super QI, and any wallet issuing a static QR — have no account
    a client can type. The wallet carries a picture instead, and what governs
    whether that is allowed is the *payment method*, because it is the method
    that is either paid by number or not.
    """

    def scannable_method(self):
        self.method.requires_wallet_number = False
        self.method.save(update_fields=["requires_wallet_number"])
        return self.method

    def scan_wallet(self, **overrides):
        self.wallet.is_active = False
        self.wallet.save(update_fields=["is_active"])
        fields = dict(
            merchant_method=self.link,
            number="",
            qr_image=png_upload("qr.png"),
            label="رمز الدفع",
            is_active=True,
        )
        fields.update(overrides)
        wallet = Wallet(**fields)
        wallet.full_clean()
        wallet.save()
        return wallet

    # -- what the model allows ---------------------------------------------

    def test_a_wallet_with_neither_a_number_nor_a_qr_is_refused(self):
        """It has to tell the client *something* to pay into."""
        with self.assertRaises(ValidationError):
            Wallet(merchant_method=self.link, number="", qr_image="").full_clean()

    def test_a_number_is_still_required_when_the_method_is_paid_by_number(self):
        """The default, and the case every existing rail is in."""
        self.assertTrue(self.method.requires_wallet_number)

        with self.assertRaises(ValidationError) as caught:
            Wallet(
                merchant_method=self.link, number="", qr_image=png_upload("qr.png")
            ).full_clean()

        self.assertIn("number", caught.exception.message_dict)

    def test_a_scan_only_wallet_is_allowed_once_the_method_says_so(self):
        self.scannable_method()

        wallet = self.scan_wallet()

        self.assertEqual(wallet.number, "")
        self.assertTrue(wallet.is_scan_only)

    def test_a_wallet_may_carry_both(self):
        """A rail can perfectly well issue a QR *and* an account."""
        self.scannable_method()

        wallet = self.scan_wallet(number="07709999999")

        self.assertFalse(wallet.is_scan_only)
        self.assertTrue(bool(wallet.qr_image))

    # -- what the client is shown ------------------------------------------

    def test_the_options_payload_carries_the_qr_url(self):
        self.scannable_method()
        wallet = self.scan_wallet()
        self.sign_in()

        _response, payload = self.options(
            type="deposit", method=self.method.code, merchant=str(self.merchant.pk)
        )

        self.assertEqual(payload["wallet"]["number"], "")
        self.assertEqual(
            payload["wallet"]["qr"],
            reverse("portal:wallet_qr", kwargs={"pk": wallet.pk}),
        )

    def test_a_numbered_wallet_carries_no_qr_url(self):
        self.sign_in()

        _response, payload = self.options(
            type="deposit", method=self.method.code, merchant=str(self.merchant.pk)
        )

        self.assertEqual(payload["wallet"]["number"], "07701234567")
        self.assertEqual(payload["wallet"]["qr"], "")

    def test_the_qr_is_served_and_labelled_as_an_image(self):
        self.scannable_method()
        wallet = self.scan_wallet()

        response = self.client.get(
            reverse("portal:wallet_qr", kwargs={"pk": wallet.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("image/"))
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_a_retired_wallets_qr_stops_being_fetchable(self):
        """A QR that was stood down should not keep working for whoever kept
        the URL."""
        self.scannable_method()
        wallet = self.scan_wallet()
        wallet.is_active = False
        wallet.save(update_fields=["is_active"])

        response = self.client.get(
            reverse("portal:wallet_qr", kwargs={"pk": wallet.pk})
        )

        self.assertEqual(response.status_code, 404)

    def test_a_wallet_with_no_qr_has_none_to_serve(self):
        response = self.client.get(
            reverse("portal:wallet_qr", kwargs={"pk": self.wallet.pk})
        )
        self.assertEqual(response.status_code, 404)

    # -- submitting against one --------------------------------------------

    def test_a_deposit_into_a_scan_only_wallet_is_accepted(self):
        self.scannable_method()
        wallet = self.scan_wallet()
        self.sign_in()

        response = self.submit(wallet=str(wallet.pk))

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Request.objects.get().wallet_number_snapshot, "")

    def test_the_stale_wallet_check_still_holds_for_an_image_wallet(self):
        """Consent is to a specific wallet, whether it is a number or a code."""
        self.scannable_method()
        self.scan_wallet()
        self.sign_in()

        response = self.submit(wallet="999999")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.body(response)["error"], "wallet_changed")


class AutomaticRoutingTests(FlowTestCase):
    """A deposit reaches its merchant without passing a desk first.

    The rule is one sentence — *a deposit is routed to the merchant the client
    chose, at submission* — and almost all of these cases are about what that
    sentence must **not** drag along with it: it must not touch withdrawals, it
    must not take Finance's levers away, and it must not lose a request when
    the merchant it names has stopped being usable.
    """

    def setUp(self):
        super().setUp()
        self.sign_in()

    def deposit(self) -> Request:
        self.submit()
        return Request.objects.get()

    # -- the rule ----------------------------------------------------------

    def test_a_deposit_is_assigned_to_the_merchant_the_client_chose(self):
        deposit = self.deposit()

        self.assertEqual(deposit.status, RequestStatus.ASSIGNED)
        self.assertEqual(deposit.merchant_assigned_id, self.merchant.pk)
        self.assertEqual(deposit.merchant_assigned_id, deposit.merchant_selected_id)

    def test_the_routing_stamps_the_time_it_happened(self):
        deposit = self.deposit()

        self.assertIsNotNone(deposit.assigned_at)
        self.assertGreaterEqual(deposit.assigned_at, deposit.submitted_at)

    def test_the_route_is_audited_as_the_system_and_not_as_the_client(self):
        """The client did not route anything; nobody did. The log says so."""
        self.submit()

        route = AuditLog.objects.filter(target_type="transactions.Request").latest("pk")

        self.assertEqual(route.after["action"], "auto_route")
        self.assertEqual(route.after["status"], RequestStatus.ASSIGNED)
        self.assertEqual(route.before["status"], RequestStatus.SUBMITTED)
        self.assertEqual(route.actor_label, "system")
        self.assertIsNone(route.actor)

    def test_both_the_submission_and_the_route_are_recorded(self):
        """Two events, so the log still shows the request arriving unrouted."""
        self.submit()

        entries = list(
            AuditLog.objects.filter(target_type="transactions.Request").order_by("pk")
        )

        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[0].actor_label.startswith("client#"))
        self.assertEqual(entries[1].actor_label, "system")

    def test_the_confirmation_the_client_gets_back_is_already_routed(self):
        """The payload is rendered after the route, not before it."""
        payload = self.body(self.submit())["request"]

        states = {step["key"]: step["state"] for step in payload["timeline"]}
        self.assertEqual(states["assigned"], "current")

    # -- what it must not touch -------------------------------------------

    def test_a_withdrawal_is_not_routed_and_still_waits_for_review(self):
        """Spec §6 debits the client's B2CORE wallet during review. A merchant
        asked to pay out before that is how money leaves twice."""
        ExchangeRate.objects.create(
            rate_type=RateType.WITHDRAWAL,
            iqd_per_usd=Decimal("1400.00"),
            commission_iqd_per_100usd=Decimal("2000.00"),
        )
        rate = pricing.current_rate(RequestType.WITHDRAWAL)

        response = self.submit(
            type="withdrawal",
            wallet=None,
            proof=None,
            rate=str(rate.pk),
            destination_account="4257880011937742",
        )

        self.assertEqual(response.status_code, 201, response.content)
        withdrawal = Request.objects.get()
        self.assertEqual(withdrawal.status, RequestStatus.SUBMITTED)
        self.assertIsNone(withdrawal.merchant_assigned_id)
        self.assertIsNone(withdrawal.assigned_at)

    def test_the_client_is_still_never_told_which_merchant_holds_it(self):
        """`merchant_assigned` was never disclosed and routing it early does
        not start disclosing it."""
        reference = self.body(self.submit())["request"]["reference"]

        payload = self.body(
            self.client.get(
                reverse("portal:request_detail", kwargs={"reference": reference})
            )
        )["request"]

        self.assertNotIn("merchant_assigned", json.dumps(payload, ensure_ascii=False))

    # -- when the merchant cannot take it ----------------------------------

    def test_a_deposit_survives_a_merchant_that_went_inactive_mid_submission(self):
        """The wizard offered this merchant; the submission arrives after they
        were switched off. Losing the client's request is not an option, so it
        stays with Finance."""
        original = Request.objects.count()

        with self.settings():
            self.merchant.is_active = False
            self.merchant.save(update_fields=["is_active"])
            # The catalogue would refuse the merchant outright, so the refusal
            # under test is the routing one: bypass the catalogue check by
            # deactivating between validation and write.
            deposit = Request.objects.create(
                type=RequestType.DEPOSIT,
                client=PortalClient.objects.get(),
                payment_method=self.method,
                merchant_selected=self.merchant,
                wallet_number_snapshot=self.wallet.number,
                amount_usd=Decimal("100.00"),
                amount_iqd=Decimal("152000.00"),
                rate_applied=Decimal("1470.00"),
                commission_applied=Decimal("5000.00"),
                status=RequestStatus.SUBMITTED,
            )
            routed = transitions.auto_route(deposit)

        self.assertEqual(Request.objects.count(), original + 1)
        self.assertEqual(routed.status, RequestStatus.SUBMITTED)
        self.assertIsNone(routed.merchant_assigned_id)

    def test_an_unroutable_deposit_reads_as_still_at_submission(self):
        """`under_review` is off the deposit track, so a request that never got
        routed must not fall off the timeline."""
        self.submit()
        deposit = Request.objects.get()
        deposit.status = RequestStatus.UNDER_REVIEW
        deposit.save(update_fields=["status"])

        payload = self.body(
            self.client.get(
                reverse(
                    "portal:request_detail",
                    kwargs={"reference": deposit.public_ref},
                )
            )
        )["request"]

        states = {step["key"]: step["state"] for step in payload["timeline"]}
        self.assertEqual(states["submitted"], "current")
        self.assertEqual(states["assigned"], "pending")


# ---------------------------------------------------------------------------
# Submitting — screens 4 and 5
# ---------------------------------------------------------------------------


class SubmissionTests(FlowTestCase):
    def test_a_complete_deposit_is_stored_with_its_proof_and_message(self):
        self.sign_in()

        response = self.submit(message="حوّلت المبلغ الساعة الثالثة")

        self.assertEqual(response.status_code, 201, response.content)
        deposit = Request.objects.get()
        # Routed on submission, not left for a desk to pick up.
        self.assertEqual(deposit.status, RequestStatus.ASSIGNED)
        self.assertEqual(deposit.amount_usd, Decimal("100.00"))
        self.assertEqual(deposit.attachments.count(), 1)
        self.assertEqual(deposit.messages.get().body, "حوّلت المبلغ الساعة الثالثة")

    def test_the_reference_comes_back_for_the_confirmation_screen(self):
        self.sign_in()

        payload = self.body(self.submit())["request"]

        self.assertTrue(payload["reference"].startswith("MP-"))
        self.assertEqual(payload["reference"], Request.objects.get().public_ref)

    def test_the_stored_figures_are_the_ones_the_client_was_shown(self):
        self.sign_in()

        self.submit(amount_usd="100")

        deposit = Request.objects.get()
        self.assertEqual(deposit.rate_applied, Decimal("1470.00"))
        self.assertEqual(deposit.commission_applied, Decimal("5000.00"))
        # amount_iqd is the total the client transfers, commission included.
        self.assertEqual(deposit.amount_iqd, Decimal("152000.00"))

    def test_the_wallet_number_is_snapshotted_not_referenced(self):
        self.sign_in()
        self.submit()

        self.wallet.number = "07809999999"
        self.wallet.save(update_fields=["number"])

        self.assertEqual(Request.objects.get().wallet_number_snapshot, "07701234567")

    def test_a_later_rate_never_reprices_a_stored_request(self):
        self.sign_in()
        self.submit()

        ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT, iqd_per_usd=Decimal("2000.00")
        )

        self.assertEqual(Request.objects.get().rate_applied, Decimal("1470.00"))

    def test_the_request_belongs_to_the_client_the_token_named(self):
        payload = self.sign_in()

        self.submit()

        client = PortalClient.objects.get(b2core_id=payload["client"]["reference"])
        self.assertEqual(Request.objects.get().client_id, client.pk)

    def test_submission_writes_an_audit_entry(self):
        self.sign_in()

        self.submit()

        entry = AuditLog.objects.filter(target_type="transactions.Request").earliest("pk")
        self.assertEqual(entry.after["status"], "submitted")
        self.assertTrue(entry.actor_label.startswith("client#"))

    def test_the_proof_is_attributed_to_the_client(self):
        self.sign_in()

        self.submit()

        proof = Attachment.objects.get()
        self.assertEqual(proof.uploaded_by_role, ActorRole.CLIENT)
        self.assertEqual(proof.content_type, "image/png")

    def test_a_deposit_without_proof_is_refused(self):
        """Spec §6 makes the message optional and the proof not."""
        self.sign_in()

        response = self.submit(proof=None)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.body(response)["error"], "proof_missing")
        self.assertFalse(Request.objects.exists())

    def test_a_file_that_is_not_what_it_claims_is_refused(self):
        self.sign_in()
        disguised = SimpleUploadedFile(
            "receipt.pdf", b"not a pdf at all", content_type="application/pdf"
        )

        response = self.submit(proof=disguised)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.body(response)["error"], "proof_invalid")
        self.assertFalse(Request.objects.exists())

    def test_an_executable_wearing_an_image_extension_is_refused(self):
        self.sign_in()
        disguised = SimpleUploadedFile(
            "receipt.png", b"MZ\x90\x00executable", content_type="image/png"
        )

        response = self.submit(proof=disguised)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Request.objects.exists())

    def test_an_amount_below_the_floor_is_refused(self):
        self.sign_in()

        response = self.submit(amount_usd="0.5")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.body(response)["error"], "amount_below_min")

    def test_a_type_that_is_neither_direction_is_refused(self):
        self.sign_in()

        response = self.submit(type="transfer")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.body(response)["error"], "type_unknown")
        self.assertFalse(Request.objects.exists())

    def test_no_session_submits_nothing(self):
        response = self.submit(token="")

        self.assertEqual(response.status_code, 401)
        self.assertFalse(Request.objects.exists())

    def test_a_submission_without_the_session_token_is_refused(self):
        """Django's CSRF cookie cannot reach the frame; this stands in for it."""
        self.sign_in()

        response = self.submit(token="")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.body(response)["error"], "invalid_csrf")
        self.assertFalse(Request.objects.exists())

    def test_a_submission_from_a_foreign_origin_is_refused(self):
        self.sign_in()

        response = self.submit(origin="https://evil.example")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Request.objects.exists())

    @override_settings(PORTAL_SUBMISSION_RATE="2/hour")
    def test_the_submission_endpoint_is_rate_limited(self):
        """Spec §11."""
        cache.clear()
        self.sign_in()

        self.assertEqual(self.submit().status_code, 201)
        self.assertEqual(self.submit().status_code, 201)
        third = self.submit()

        self.assertEqual(third.status_code, 429)
        self.assertEqual(Request.objects.count(), 2)


class GroundMovedTests(FlowTestCase):
    """What happens when the desk changes while a form is open."""

    def test_a_swapped_wallet_stops_the_submission_and_shows_the_new_number(self):
        self.sign_in()
        stale = self.wallet.pk
        replacement = Wallet.objects.create(
            merchant_method=self.link, number="07809999999"
        )

        response = self.submit(wallet=str(stale))
        payload = self.body(response)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(payload["error"], "wallet_changed")
        self.assertEqual(payload["wallet"]["number"], "07809999999")
        self.assertEqual(payload["wallet"]["id"], replacement.pk)
        self.assertFalse(Request.objects.exists())

    def test_a_changed_rate_stops_the_submission_and_shows_the_new_figures(self):
        self.sign_in()
        stale = self.rate.pk
        ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT,
            iqd_per_usd=Decimal("1600.00"),
            commission_iqd_per_100usd=Decimal("5000.00"),
        )

        response = self.submit(rate=str(stale))
        payload = self.body(response)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(payload["error"], "rate_changed")
        self.assertEqual(payload["rate"]["iqd_per_usd"], "1600.00")
        self.assertEqual(payload["quote"]["total_iqd"], "165000.00")
        self.assertFalse(Request.objects.exists())

    def test_a_merchant_that_went_offline_stops_the_submission(self):
        self.sign_in()
        self.merchant.is_active = False
        self.merchant.save(update_fields=["is_active"])

        response = self.submit()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.body(response)["error"], "method_unavailable")
        self.assertFalse(Request.objects.exists())


# ---------------------------------------------------------------------------
# Screen 6 and the history behind it
# ---------------------------------------------------------------------------


class RequestViewTests(FlowTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.reference = self.body(self.submit(message="ملاحظة العميل"))["request"][
            "reference"
        ]
        self.deposit = Request.objects.get(public_ref=self.reference)
        self.detail_url = reverse(
            "portal:request_detail", kwargs={"reference": self.reference}
        )

    def test_the_timeline_marks_where_the_request_has_reached(self):
        payload = self.body(self.client.get(self.detail_url))["request"]

        states = {step["key"]: step["state"] for step in payload["timeline"]}
        # Submission and routing happen together, so the client's first look at
        # the request already has it waiting on the merchant.
        self.assertEqual(states["submitted"], "done")
        self.assertEqual(states["assigned"], "current")
        self.assertEqual(states["closed"], "pending")
        # There is no review step in front of a deposit any more.
        self.assertNotIn("under_review", states)

    def test_the_timeline_follows_the_request_forward(self):
        self.deposit.status = RequestStatus.MERCHANT_CONFIRMED
        self.deposit.merchant_actioned_at = timezone.now()
        self.deposit.save(update_fields=["status", "merchant_actioned_at"])

        payload = self.body(self.client.get(self.detail_url))["request"]

        states = {step["key"]: step["state"] for step in payload["timeline"]}
        self.assertEqual(states["submitted"], "done")
        self.assertEqual(states["assigned"], "done")
        self.assertEqual(states["merchant_confirmed"], "current")
        self.assertEqual(states["closed"], "pending")

    def test_credited_and_closed_are_one_step_for_the_client(self):
        """Finance manager, Sep 2026: "added to your account" and "completed"
        are shown as one step. Display only — see the Finance side below."""
        for status in (RequestStatus.CREDITED, RequestStatus.CLOSED):
            with self.subTest(status=status):
                self.deposit.status = status
                self.deposit.save(update_fields=["status"])

                timeline = self.body(self.client.get(self.detail_url))["request"][
                    "timeline"
                ]

                self.assertEqual(
                    [step["key"] for step in timeline],
                    ["submitted", "assigned", "merchant_confirmed", "closed"],
                )
                last = timeline[-1]
                self.assertEqual(last["label"], "أُضيف المبلغ إلى حسابك")
                self.assertEqual(last["state"], "done")
                self.assertNotIn("اكتمل الطلب", [step["label"] for step in timeline])

    def test_the_merge_does_not_reach_the_status_or_finances_track(self):
        from apps.transactions.services import track

        self.deposit.status = RequestStatus.CREDITED
        self.deposit.save(update_fields=["status"])
        self.client.get(self.detail_url)

        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, RequestStatus.CREDITED)
        finance = {step["key"]: step["state"] for step in track(self.deposit)}
        self.assertEqual(finance["credited"], "current")
        self.assertEqual(finance["closed"], "pending")

    def test_a_rejection_carries_its_reason(self):
        self.deposit.status = RequestStatus.REJECTED
        self.deposit.rejection_reason = "الإيصال غير واضح"
        self.deposit.save(update_fields=["status", "rejection_reason"])

        payload = self.body(self.client.get(self.detail_url))["request"]

        last = payload["timeline"][-1]
        self.assertEqual(last["key"], "rejected")
        self.assertEqual(last["reason"], "الإيصال غير واضح")

    def test_the_breakdown_adds_up_to_the_total(self):
        payload = self.body(self.client.get(self.detail_url))["request"]

        self.assertEqual(payload["converted_iqd"], "147000.00")
        self.assertEqual(payload["commission_applied"], "5000.00")
        self.assertEqual(payload["amount_iqd"], "152000.00")

    def test_the_client_sees_their_own_message(self):
        payload = self.body(self.client.get(self.detail_url))["request"]

        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["body"], "ملاحظة العميل")
        self.assertTrue(payload["messages"][0]["mine"])

    def test_an_internal_note_never_reaches_the_client(self):
        """Finance talks among themselves on the same thread (spec §5)."""
        Message.objects.create(
            request=self.deposit,
            sender_role=ActorRole.FINANCE_STAFF,
            body="العميل مشبوه، راجع سجله",
            is_internal_note=True,
        )

        payload = self.body(self.client.get(self.detail_url))["request"]

        bodies = [message["body"] for message in payload["messages"]]
        self.assertNotIn("العميل مشبوه، راجع سجله", bodies)

    def test_the_routed_merchant_is_not_disclosed(self):
        """The client sees who they paid, not who Finance routed to (spec §5)."""
        other = Merchant.objects.create(name="تاجر البصرة")
        self.deposit.merchant_assigned = other
        self.deposit.save(update_fields=["merchant_assigned"])

        payload = self.body(self.client.get(self.detail_url))["request"]

        self.assertEqual(payload["merchant"], "تاجر بغداد")
        self.assertNotIn("تاجر البصرة", json.dumps(payload, ensure_ascii=False))

    def test_another_clients_reference_is_a_404_not_a_denial(self):
        reference = self.reference
        self.client.cookies.clear()
        self.sign_in(sub="b2core-subject-other")

        response = self.client.get(
            reverse("portal:request_detail", kwargs={"reference": reference})
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.body(response)["error"], "not_found")

    def test_the_history_lists_only_the_clients_own_requests(self):
        mine = self.reference
        self.client.cookies.clear()
        self.sign_in(sub="b2core-subject-other")
        self.submit()

        payload = self.body(self.client.get(self.requests_url))

        references = [row["reference"] for row in payload["requests"]]
        self.assertEqual(len(references), 1)
        self.assertNotIn(mine, references)

    def test_the_history_pill_reads_as_the_timeline_does(self):
        """Credited and closed are one pill, worded as the merged timeline step,
        never the internal "قُيِّد في B2CORE" or "مغلق"."""
        for status in (RequestStatus.CREDITED, RequestStatus.CLOSED):
            with self.subTest(status=status):
                self.deposit.status = status
                self.deposit.save(update_fields=["status"])

                row = self.body(self.client.get(self.requests_url))["requests"][0]
                detail = self.body(self.client.get(self.detail_url))["request"]

                self.assertEqual(row["status_label"], "أُضيف المبلغ إلى حسابك")
                self.assertEqual(detail["status_label"], row["status_label"])
                self.assertEqual(detail["status_label"], detail["timeline"][-1]["label"])
                # The status itself is untouched; only its wording is.
                self.assertEqual(row["status"], status)

    def test_a_closed_withdrawal_is_not_told_money_was_added(self):
        """The money went the other way, so it reads as its own last step."""
        from apps.portal.payloads import status_label

        self.deposit.type = RequestType.WITHDRAWAL
        self.deposit.status = RequestStatus.CLOSED
        self.assertEqual(status_label(self.deposit), "اكتمل الطلب")

    def test_other_statuses_keep_their_label(self):
        from apps.portal.payloads import status_label

        self.deposit.status = RequestStatus.REJECTED
        self.assertEqual(
            status_label(self.deposit), RequestStatus.REJECTED.label
        )

    def test_a_malformed_reference_never_reaches_a_view(self):
        response = self.client.get("/portal/requests/not-a-reference/")

        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Files (spec §11)
# ---------------------------------------------------------------------------


class AttachmentTests(FlowTestCase):
    def setUp(self):
        super().setUp()
        self.session = self.sign_in()
        self.reference = self.body(self.submit())["request"]["reference"]
        self.deposit = Request.objects.get(public_ref=self.reference)
        self.attachment = self.deposit.attachments.get()
        self.client_record = self.deposit.client
        self.detail_url = reverse(
            "portal:request_detail", kwargs={"reference": self.reference}
        )

    def signed_url(self):
        payload = self.body(self.client.get(self.detail_url))["request"]
        return payload["attachments"][0]["url"]

    def test_media_root_is_never_reachable_directly(self):
        stored = self.attachment.file.name

        response = self.client.get("/" + stored)

        self.assertEqual(response.status_code, 404)

    def test_the_signed_url_serves_the_file(self):
        response = self.client.get(self.signed_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(b"".join(response.streaming_content), PNG)

    def test_a_served_file_cannot_act_as_a_page(self):
        response = self.client.get(self.signed_url())

        self.assertIn("sandbox", response["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", response["Content-Security-Policy"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_a_tampered_token_gets_nothing(self):
        url = self.signed_url()

        response = self.client.get(url[:-4] + "xxxx/")

        self.assertEqual(response.status_code, 404)

    def test_a_token_minted_for_another_client_is_inert(self):
        url = self.signed_url()
        self.client.cookies.clear()
        self.sign_in(sub="b2core-subject-other")

        response = self.client.get(url)

        self.assertEqual(response.status_code, 404)

    def test_a_signed_url_without_a_session_is_not_enough(self):
        """The signature authenticates the link, never the caller."""
        url = self.signed_url()
        self.client.cookies.clear()

        response = self.client.get(url)

        self.assertEqual(response.status_code, 401)

    @override_settings(PORTAL_ATTACHMENT_URL_MAX_AGE=1)
    def test_a_stale_token_expires(self):
        token = attachment_urls.sign(self.attachment, self.client_record)
        url = reverse(
            "portal:attachment", kwargs={"pk": self.attachment.pk, "token": token}
        )
        time.sleep(1.1)

        response = self.client.get(url)

        self.assertEqual(response.status_code, 404)

    def test_a_token_cannot_be_pointed_at_a_different_file(self):
        other = Attachment.objects.create(
            request=self.deposit,
            file=png_upload("second.png"),
            uploaded_by_role=ActorRole.CLIENT,
        )
        token = attachment_urls.sign(self.attachment, self.client_record)

        response = self.client.get(
            reverse("portal:attachment", kwargs={"pk": other.pk, "token": token})
        )

        self.assertEqual(response.status_code, 404)

    def test_a_pdf_is_downloaded_rather_than_rendered(self):
        pdf = Attachment.objects.create(
            request=self.deposit,
            file=SimpleUploadedFile("proof.pdf", b"%PDF-1.4 body", content_type="application/pdf"),
            content_type="application/pdf",
            uploaded_by_role=ActorRole.CLIENT,
        )
        token = attachment_urls.sign(pdf, self.client_record)

        response = self.client.get(
            reverse("portal:attachment", kwargs={"pk": pdf.pk, "token": token})
        )

        self.assertIn("attachment;", response["Content-Disposition"])


class MethodIconTests(FlowTestCase):
    def test_a_method_without_an_icon_is_not_linked_to_one(self):
        self.sign_in()

        _response, payload = self.options(merchant=self.merchant.pk)

        self.assertIsNone(payload["methods"][0]["icon"])

    def test_an_inactive_method_serves_no_icon(self):
        self.method.is_active = False
        self.method.save(update_fields=["is_active"])

        response = self.client.get(
            reverse("portal:method_icon", kwargs={"code": self.method.code})
        )

        self.assertEqual(response.status_code, 404)


class BootstrapPageTests(FlowTestCase):
    def test_the_page_carries_the_flow_configuration(self):
        response = self.client.get(self.bootstrap_url)

        self.assertContains(response, "maxpay-flow-config")
        self.assertContains(response, "js/flow.js")

    def test_the_page_still_carries_no_inline_script(self):
        """The CSP shipped with it is `default-src 'self'` (spec §11).

        The two ``json_script`` islands are data, not code: a
        ``type="application/json"`` block is never executed and CSP does not
        police it. Anything else without a ``src`` would need the policy
        relaxed, which is exactly what this guards.
        """
        page = self.client.get(self.bootstrap_url).content.decode("utf-8")

        for fragment in page.split("<script")[1:]:
            head = fragment.split(">", 1)[0]
            if 'type="application/json"' in head:
                continue
            self.assertIn("src=", head, "an inline <script> appeared on the page")

    def test_no_developer_prose_reaches_the_client(self):
        """The deposit screen is Arabic, and everything on it is for a client.

        A ``{# … #}`` comment that runs past its own line renders the rest of
        itself to the page, and that is how an English note about the rounding
        row came to be printed above it. ``apps/core/test_templates.py`` stops
        the class of defect; this asserts the outcome on the one screen a client
        actually reads.
        """
        page = self.client.get(self.bootstrap_url).content.decode("utf-8")

        # Strip the two json_script islands: they are data, and legitimately
        # carry English keys.
        visible = re.sub(
            r'<script[^>]*type="application/json"[^>]*>.*?</script>',
            "",
            page,
            flags=re.DOTALL,
        )
        visible = re.sub(r"<script.*?</script>", "", visible, flags=re.DOTALL)
        visible = re.sub(r"<style.*?</style>", "", visible, flags=re.DOTALL)
        # What is left of the markup once the tags themselves are gone.
        text = re.sub(r"<[^>]+>", " ", visible)

        for phrase in ("the company", "Hidden whenever", "invites a question",
                       "serializer", "spec §2", "template's context"):
            self.assertNotIn(phrase, text, f"developer prose on the client page: {phrase}")


# ---------------------------------------------------------------------------
# Wallet daily caps
# ---------------------------------------------------------------------------


class DailyCapTests(FlowTestCase):
    """``Wallet.daily_cap`` is in dinars, and so is what is counted against it.

    At 1,470 IQD/USD with 5,000 commission per 100 USD, a 100 USD deposit costs
    the client 152,000 IQD — and that is the figure the cap sees, because that
    is what lands in the wallet.
    """

    def set_cap(self, amount):
        self.wallet.daily_cap = Decimal(amount) if amount is not None else None
        self.wallet.save(update_fields=["daily_cap"])

    def test_a_deposit_inside_the_cap_goes_through(self):
        self.set_cap("200000.00")
        self.sign_in()
        self.assertEqual(self.submit().status_code, 201)
        self.assertEqual(Request.objects.get().amount_iqd, Decimal("152000.00"))

    def test_a_deposit_past_the_remaining_headroom_is_refused(self):
        self.set_cap("200000.00")
        self.sign_in()
        self.assertEqual(self.submit().status_code, 201)

        response = self.submit()
        payload = self.body(response)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(payload["error"], "wallet_cap_exceeded")
        # Told what still fits, not merely refused.
        self.assertEqual(payload["remaining_iqd"], "48000.00")
        self.assertEqual(Request.objects.count(), 1)

    def test_a_smaller_amount_still_fits_under_what_is_left(self):
        self.set_cap("200000.00")
        self.sign_in()
        self.submit()
        # 30 USD → 44,100 + 1,500 = 45,600, inside the 48,000 that remains.
        self.assertEqual(self.submit(amount_usd="30").status_code, 201)
        self.assertEqual(Request.objects.count(), 2)

    def test_a_wallet_already_at_its_cap_sends_the_client_elsewhere(self):
        self.set_cap("152000.00")
        self.sign_in()
        self.assertEqual(self.submit().status_code, 201)

        response = self.submit(amount_usd="1")
        self.assertEqual(response.status_code, 409)
        # A different code from the one above: no amount would fit, so the
        # client belongs back on the merchant list, not on the amount field.
        self.assertEqual(self.body(response)["error"], "wallet_cap_reached")

    def test_an_uncapped_wallet_takes_anything(self):
        self.set_cap(None)
        self.sign_in()
        for _ in range(3):
            self.assertEqual(self.submit().status_code, 201)
        self.assertEqual(Request.objects.count(), 3)

    def test_a_rejected_deposit_gives_its_share_of_the_cap_back(self):
        self.set_cap("200000.00")
        self.sign_in()
        self.submit()
        Request.objects.update(status=RequestStatus.REJECTED)
        self.assertEqual(self.submit().status_code, 201)
