"""The client withdrawal flow, end to end (spec §6, §7) — build-order step 10.

A withdrawal is not a deposit with the arrow turned round, and every case here
is about one of the four places the two genuinely differ:

* **No wallet.** The merchant pays out, so a merchant with no active wallet is
  perfectly able to serve a withdrawal — and a deposit's daily cap has nothing
  to say about one.
* **A destination.** The client's own card or wallet number, which is the one
  string in the system that cannot be corrected after a merchant has paid
  against it.
* **No proof at submission.** Nothing has moved yet. The proof arrives from the
  merchant when they pay (spec §6, §8).
* **The commission points the other way.** It is deducted from what the client
  receives rather than added to what they transfer, and a sign error here is a
  fee the desk never collects.

What the two share — the session, the CSRF header, the catalogue's availability
rule, the rate-consent check, the audit entry — is exercised by
:mod:`apps.portal.tests.test_flow` and not repeated.
"""

import json
import re
from decimal import Decimal

from django.conf import settings
from django.urls import reverse

from apps.core.models import AuditLog
from apps.portal import destinations, pricing
from apps.rates.models import ExchangeRate, RateType
from apps.transactions.models import (
    Attachment,
    Message,
    Request,
    RequestStatus,
    RequestType,
)

from .test_flow import FlowTestCase, png_upload


class WithdrawalTestCase(FlowTestCase):
    """The desk of :class:`FlowTestCase`, plus a withdrawal rate to price at.

    The two rates differ on purpose. A withdrawal priced at 1470 would pass
    every assertion below while silently reading the deposit row.
    """

    def setUp(self):
        super().setUp()
        self.withdrawal_rate = ExchangeRate.objects.create(
            rate_type=RateType.WITHDRAWAL,
            iqd_per_usd=Decimal("1450.00"),
            commission_iqd_per_100usd=Decimal("3000.00"),
        )

    def withdraw(self, *, origin="http://testserver", token=None, **overrides):
        """POST a withdrawal, defaulting every field to something valid."""
        data = {
            "type": "withdrawal",
            "method": self.method.code,
            "merchant": str(self.merchant.pk),
            "rate": str(self.withdrawal_rate.pk),
            "amount_usd": "100",
            "destination_account": "07701234567",
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


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


class WithdrawalCatalogueTests(WithdrawalTestCase):
    def test_both_directions_are_offered_on_screen_one(self):
        self.sign_in()

        _response, payload = self.options()

        self.assertEqual(
            [(t["value"], t["available"]) for t in payload["types"]],
            [("deposit", True), ("withdrawal", True)],
        )

    def test_the_withdrawal_catalogue_prices_at_the_withdrawal_rate(self):
        """Spec §5: each direction has its own rate, and they are not the same row."""
        self.sign_in()

        _response, payload = self.options(type="withdrawal")

        self.assertEqual(payload["rate"]["id"], self.withdrawal_rate.pk)
        self.assertEqual(payload["rate"]["iqd_per_usd"], "1450.00")

    def test_the_commission_travels_with_its_sign(self):
        """The embed previews the figure; which way the fee points is ours to say."""
        self.sign_in()

        _response, deposit = self.options(type="deposit")
        _response, withdrawal = self.options(type="withdrawal")

        self.assertEqual(deposit["rate"]["commission_sign"], 1)
        self.assertEqual(withdrawal["rate"]["commission_sign"], -1)

    def test_screen_four_is_told_what_it_collects(self):
        self.sign_in()

        _response, deposit = self.options(type="deposit")
        _response, withdrawal = self.options(type="withdrawal")

        self.assertEqual(
            deposit["needs"], {"wallet": True, "destination": False, "proof": True}
        )
        self.assertEqual(
            withdrawal["needs"], {"wallet": False, "destination": True, "proof": False}
        )

    def test_a_merchant_with_no_active_wallet_still_serves_withdrawals(self):
        """The money goes the other way, so there is nothing to pay into."""
        self.wallet.deactivate()
        self.sign_in()

        _response, deposits = self.options(type="deposit")
        _response, withdrawals = self.options(
            type="withdrawal", merchant=self.merchant.pk
        )

        # No wallet, no deposit — so the merchant is not offerable at all on
        # that side, while the withdrawal side never needed one.
        self.assertEqual(deposits["merchants"], [])
        self.assertEqual([m["code"] for m in withdrawals["methods"]], ["zaincash"])

    def test_a_deposit_only_method_is_absent_from_the_withdrawal_catalogue(self):
        self.method.supports_withdrawal = False
        self.method.save(update_fields=["supports_withdrawal"])
        self.sign_in()

        _response, payload = self.options(
            type="withdrawal", merchant=self.merchant.pk
        )

        self.assertEqual(payload["methods"], [])
        self.assertEqual(payload["merchants"], [])

    def test_choosing_a_merchant_yields_no_wallet_and_no_complaint(self):
        """A withdrawal's screen 4 has no number on it, and that is not a failure."""
        self.sign_in()

        _response, payload = self.options(
            type="withdrawal", method=self.method.code, merchant=self.merchant.pk
        )

        self.assertIsNone(payload["unavailable"])
        self.assertNotIn("wallet", payload)
        self.assertEqual(payload["merchant"]["id"], self.merchant.pk)

    def test_a_merchant_who_stood_down_still_names_the_screen_to_go_back_to(self):
        self.merchant.is_active = False
        self.merchant.save(update_fields=["is_active"])
        self.sign_in()

        _response, payload = self.options(
            type="withdrawal", method=self.method.code, merchant=self.merchant.pk
        )

        self.assertEqual(payload["unavailable"], "merchant")

    def test_no_withdrawal_rate_means_no_withdrawal_screen(self):
        """Finance has set a deposit rate and not a withdrawal one (spec §9)."""
        # A queryset delete, because ExchangeRate is append-only: the model
        # refuses instance deletion, which is the point of it (spec §5).
        ExchangeRate.objects.filter(rate_type=RateType.WITHDRAWAL).delete()
        self.sign_in()

        _response, payload = self.options(type="withdrawal")

        self.assertEqual(payload["unavailable"], "rate")
        self.assertEqual(payload["methods"], [])


# ---------------------------------------------------------------------------
# The money
# ---------------------------------------------------------------------------


class WithdrawalPricingTests(WithdrawalTestCase):
    def test_the_commission_comes_off_what_the_client_receives(self):
        quote = pricing.quote(
            self.withdrawal_rate, Decimal("100.00"), RequestType.WITHDRAWAL
        )

        self.assertEqual(quote.converted_iqd, Decimal("145000.00"))
        self.assertEqual(quote.commission_iqd, Decimal("3000.00"))
        self.assertEqual(quote.total_iqd, Decimal("142000.00"))

    def test_a_deposit_at_the_same_figures_adds_it_instead(self):
        """The one line that must never be shared between the directions."""
        deposit = pricing.quote(
            self.withdrawal_rate, Decimal("100.00"), RequestType.DEPOSIT
        )

        self.assertEqual(deposit.total_iqd, Decimal("148000.00"))

    def test_a_payout_the_commission_would_swallow_is_refused(self):
        """A withdrawal of nothing is not a smaller withdrawal."""
        greedy = ExchangeRate.objects.create(
            rate_type=RateType.WITHDRAWAL,
            iqd_per_usd=Decimal("10.00"),
            commission_iqd_per_100usd=Decimal("5000.00"),
        )

        with self.assertRaises(pricing.PricingError) as caught:
            pricing.quote(greedy, Decimal("1.00"), RequestType.WITHDRAWAL)

        self.assertEqual(caught.exception.code, "amount_below_commission")

    def test_the_bounds_are_the_withdrawal_ones(self):
        with self.settings(PORTAL_WITHDRAWAL_MAX_USD="500", PORTAL_DEPOSIT_MAX_USD="100000"):
            pricing.parse_amount_usd("400", RequestType.WITHDRAWAL)

            with self.assertRaises(pricing.PricingError) as caught:
                pricing.parse_amount_usd("600", RequestType.WITHDRAWAL)
            self.assertEqual(caught.exception.code, "amount_above_max")

            # The same figure against the other direction's ceiling passes,
            # which is the whole reason the two settings exist.
            pricing.parse_amount_usd("600", RequestType.DEPOSIT)


# ---------------------------------------------------------------------------
# The destination
# ---------------------------------------------------------------------------


class DestinationTests(WithdrawalTestCase):
    def test_separators_and_arabic_digits_are_the_same_account(self):
        self.assertEqual(destinations.normalise("٠٧٧٠ ١٢٣-٤٥٦٧"), "07701234567")
        self.assertEqual(destinations.normalise("5321 0000 1111 2222"), "5321000011112222")

    def test_a_letter_stops_the_whole_number(self):
        """Swallowing it would store an account the client never typed."""
        with self.assertRaises(destinations.DestinationError) as caught:
            destinations.normalise("0770ABC4567")
        self.assertEqual(caught.exception.code, "destination_invalid")

    def test_length_is_bounded_at_both_ends(self):
        with self.assertRaises(destinations.DestinationError) as caught:
            destinations.normalise("123")
        self.assertEqual(caught.exception.code, "destination_too_short")

        with self.assertRaises(destinations.DestinationError) as caught:
            destinations.normalise("9" * 64)
        self.assertEqual(caught.exception.code, "destination_too_long")

    def test_an_empty_destination_is_missing_not_invalid(self):
        with self.assertRaises(destinations.DestinationError) as caught:
            destinations.normalise("   ")
        self.assertEqual(caught.exception.code, "destination_missing")

    def test_grouping_is_presentation_only(self):
        self.assertEqual(destinations.grouped("07701234567"), "0770 1234 567")


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


class WithdrawalSubmissionTests(WithdrawalTestCase):
    def test_a_withdrawal_is_written_with_its_figures_snapshotted(self):
        self.sign_in()

        response = self.withdraw()

        self.assertEqual(response.status_code, 201)
        created = Request.objects.get()
        self.assertEqual(created.type, RequestType.WITHDRAWAL)
        self.assertEqual(created.status, RequestStatus.SUBMITTED)
        self.assertEqual(created.client.b2core_id, "b2core-subject-77")
        self.assertEqual(created.merchant_selected, self.merchant)
        self.assertIsNone(created.merchant_assigned)
        self.assertEqual(created.amount_usd, Decimal("100.00"))
        self.assertEqual(created.amount_iqd, Decimal("142000.00"))
        self.assertEqual(created.rate_applied, Decimal("1450.00"))
        self.assertEqual(created.commission_applied, Decimal("3000.00"))
        self.assertEqual(created.destination_account, "07701234567")

    def test_no_wallet_is_snapshotted_because_there_is_none(self):
        self.sign_in()

        self.withdraw()

        self.assertEqual(Request.objects.get().wallet_number_snapshot, "")

    def test_nothing_is_attached_at_submission(self):
        """Spec §6: the proof of transfer is the merchant's, and it comes later."""
        self.sign_in()

        self.withdraw()

        self.assertFalse(Request.objects.get().attachments.exists())

    def test_a_proof_is_not_asked_for(self):
        """The deposit path refuses a submission without one; this must not."""
        self.sign_in()

        response = self.withdraw()

        self.assertEqual(response.status_code, 201)

    def test_the_destination_is_stored_normalised(self):
        self.sign_in()

        response = self.withdraw(destination_account="٠٧٧٠ ١٢٣-٤٥٦٧")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Request.objects.get().destination_account, "07701234567")

    def test_a_missing_destination_is_refused(self):
        self.sign_in()

        response = self.withdraw(destination_account="")
        payload = self.body(response)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"], "destination_missing")
        self.assertFalse(Request.objects.exists())

    def test_a_destination_that_is_not_digits_is_refused(self):
        self.sign_in()

        response = self.withdraw(destination_account="my card")
        payload = self.body(response)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"], "destination_invalid")

    def test_the_optional_message_opens_the_thread(self):
        self.sign_in()

        self.withdraw(message="حوّلوا على نفس البطاقة السابقة")

        message = Message.objects.get()
        self.assertEqual(message.request, Request.objects.get())
        self.assertFalse(message.is_internal_note)

    def test_a_stale_rate_is_refused_with_the_new_one_attached(self):
        """Spec §5: nobody is paid at a revision they were not shown."""
        self.sign_in()
        moved = ExchangeRate.objects.create(
            rate_type=RateType.WITHDRAWAL,
            iqd_per_usd=Decimal("1400.00"),
            commission_iqd_per_100usd=Decimal("3000.00"),
        )

        response = self.withdraw(rate=str(self.withdrawal_rate.pk))
        payload = self.body(response)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(payload["error"], "rate_changed")
        self.assertEqual(payload["rate"]["id"], moved.pk)
        self.assertEqual(payload["quote"]["total_iqd"], "137000.00")
        self.assertFalse(Request.objects.exists())

    def test_the_deposit_rate_cannot_be_used_to_price_a_withdrawal(self):
        self.sign_in()

        response = self.withdraw(rate=str(self.rate.pk))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.body(response)["error"], "rate_changed")

    def test_a_deactivated_merchant_is_refused_at_submission_too(self):
        self.sign_in()
        self.merchant.is_active = False
        self.merchant.save(update_fields=["is_active"])

        response = self.withdraw()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.body(response)["error"], "method_unavailable")

    def test_a_wallet_at_its_daily_cap_does_not_block_a_withdrawal(self):
        """The cap governs money paid *into* a wallet, and this is not that."""
        self.wallet.daily_cap = Decimal("1.00")
        self.wallet.save(update_fields=["daily_cap"])
        self.sign_in()

        response = self.withdraw()

        self.assertEqual(response.status_code, 201)

    def test_the_audit_entry_records_where_the_money_was_sent(self):
        """The one field an investigation later needs (spec §5)."""
        self.sign_in()

        self.withdraw()

        entry = AuditLog.objects.latest("created_at")
        self.assertEqual(entry.after["destination_account"], "07701234567")
        self.assertEqual(entry.after["type"], RequestType.WITHDRAWAL)

    def test_an_unknown_type_is_still_refused(self):
        self.sign_in()

        response = self.withdraw(type="transfer")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.body(response)["error"], "type_unknown")

    def test_a_deposit_still_needs_its_wallet_and_its_proof(self):
        """The generalised path must not have loosened the other direction."""
        self.sign_in()

        no_wallet = self.submit(wallet=None)
        no_proof = self.submit(proof=None)

        self.assertEqual(no_wallet.status_code, 409)
        self.assertEqual(self.body(no_wallet)["error"], "wallet_changed")
        self.assertEqual(no_proof.status_code, 400)
        self.assertEqual(self.body(no_proof)["error"], "proof_missing")


# ---------------------------------------------------------------------------
# What the client sees afterwards
# ---------------------------------------------------------------------------


class WithdrawalRequestViewTests(WithdrawalTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.withdraw()
        self.created = Request.objects.get()

    def detail(self):
        url = reverse(
            "portal:request_detail", kwargs={"reference": self.created.public_ref}
        )
        response = self.client.get(url)
        return json.loads(response.content.decode("utf-8"))["request"]

    def test_the_breakdown_adds_back_up_to_what_was_received(self):
        payload = self.detail()

        self.assertEqual(payload["amount_iqd"], "142000.00")
        self.assertEqual(payload["commission_applied"], "3000.00")
        # converted − commission = received, so converted is received + commission.
        self.assertEqual(payload["converted_iqd"], "145000.00")

    def test_the_destination_is_shown_back_to_its_owner(self):
        self.assertEqual(self.detail()["destination_account"], "07701234567")

    def test_the_timeline_is_the_withdrawal_one(self):
        """No credited step: a withdrawal closes when the merchant has paid."""
        keys = [step["key"] for step in self.detail()["timeline"]]

        self.assertEqual(
            keys,
            [
                RequestStatus.SUBMITTED,
                RequestStatus.UNDER_REVIEW,
                RequestStatus.ASSIGNED,
                RequestStatus.MERCHANT_PAID,
                RequestStatus.CLOSED,
            ],
        )

    def test_it_joins_the_client_s_own_history(self):
        response = self.client.get(self.requests_url)
        rows = json.loads(response.content.decode("utf-8"))["requests"]

        self.assertEqual([row["type"] for row in rows], ["withdrawal"])
        self.assertEqual(rows[0]["reference"], self.created.public_ref)

    def test_another_client_cannot_read_it(self):
        """Scoping is the session's, not the reference's (spec §4)."""
        self.sign_in(sub="b2core-subject-99", email="other@example.com")

        url = reverse(
            "portal:request_detail", kwargs={"reference": self.created.public_ref}
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Item 1.1 of the Finance review, 24 Aug 2026 — the proof field is gone from
# the withdrawal direction, on both sides of the wire
# ---------------------------------------------------------------------------


class WithdrawalProofIsRefusedTests(WithdrawalTestCase):
    """The client does not pay in a withdrawal, so nothing asks them to prove it.

    The rule had three separate holdings and only one of them was tested. The
    server said ``needs.proof: false`` and ``build_withdrawal_draft`` never read
    ``files`` — but *never reading* an input is a guarantee made of an omission,
    and it held only as long as nobody handed one over. A submission that
    carried a proof anyway was accepted, the file dropped on the floor, and the
    client told nothing: from their side, indistinguishable from a proof that
    was stored and would be there in a dispute.

    So the refusal is explicit now, and these are the cases that say so.
    """

    def test_a_withdrawal_carrying_a_proof_is_refused(self):
        self.sign_in()

        response = self.withdraw(proof=png_upload())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.body(response)["error"], "proof_not_accepted")

    def test_the_refusal_writes_nothing(self):
        """Refused, not partially accepted: no request, no attachment, no message."""
        self.sign_in()

        self.withdraw(proof=png_upload())

        self.assertFalse(Request.objects.exists())
        self.assertFalse(Attachment.objects.exists())
        self.assertFalse(Message.objects.exists())

    def test_the_refusal_names_the_merchant_as_the_one_who_proves_it(self):
        """Spec §6: the proof arrives from the merchant at `pay`, not from the client."""
        self.sign_in()

        detail = self.body(self.withdraw(proof=png_upload()))["detail"]

        self.assertIn("التاجر", detail)

    def test_it_is_refused_before_any_other_field_is_judged(self):
        """A field that should never have been collected outranks a mistyped one.

        Sending a proof *and* an unusable destination must report the proof: the
        destination is a value the client got wrong, the proof is a question the
        screen should not have asked.
        """
        self.sign_in()

        response = self.withdraw(proof=png_upload(), destination_account="12")

        self.assertEqual(self.body(response)["error"], "proof_not_accepted")

    def test_a_withdrawal_without_one_is_still_accepted(self):
        """The refusal must not become a requirement pointing the other way."""
        self.sign_in()

        self.assertEqual(self.withdraw().status_code, 201)

    def test_a_deposit_still_requires_its_proof(self):
        """The same file on the other direction is mandatory, and is stored."""
        self.sign_in()

        response = self.submit(proof=png_upload())

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Request.objects.get().attachments.count(), 1)


class HidingWhatIsNotCollectedTests(FlowTestCase):
    """Nothing the flow hides can be left on screen by a stylesheet rule.

    Step 10 made the details one block serving two directions: the server's
    ``needs`` object says which half applies, and ``static/js/flow.js`` hides the
    other with the ``hidden`` attribute. That is the whole display mechanism, and
    it was silently inert.

    ``.field`` sets ``display: flex`` in ``static/css/flow.css``. An author rule
    beats the user agent's ``[hidden] { display: none }`` whatever its
    specificity, so ``node.hidden = true`` on a field changed nothing on screen —
    a withdrawal showed the proof upload (item 1.1) and a deposit showed the
    destination account, both regardless of what the server said.

    The restructure of 4 Sep 2026 put a third element on the same mechanism: the
    details block itself, hidden until all three choices in the row are
    answered. Same trap, one more thing that would have been silently on screen.

    No browser runs in this suite, so what is asserted is the contract the
    browser would enforce: every element the flow hides by id must belong to no
    class that sets a ``display`` without a ``[hidden]`` companion to undo it.
    A stylesheet edit that reintroduces the defect fails here.
    """

    #: The ids the flow toggles, and what each one is hidden *for*. All of them,
    #: because the defect was never specific to the proof field — that just
    #: happened to be the one somebody noticed.
    HIDEABLE_IDS = {
        "proof-field": "withdrawal",
        "destination-field": "deposit",
        # Not a direction's half but the block holding both, hidden until all
        # three choices in the row are answered. Same class of defect, same
        # guard: it sets a display of its own.
        "details": "an unanswered row",
    }

    @classmethod
    def _flow_css(cls):
        return (settings.BASE_DIR / "static" / "css" / "flow.css").read_text(
            encoding="utf-8"
        )

    @classmethod
    def _flow_html(cls):
        return (
            settings.BASE_DIR / "templates" / "portal" / "flow.html"
        ).read_text(encoding="utf-8")

    def _classes_of(self, element_id: str) -> list[str]:
        """The class list on the element carrying this id, read from the template."""
        markup = self._flow_html()
        match = re.search(
            r"<div\b(?P<attrs>[^>]*\bid=\"" + re.escape(element_id) + r"\"[^>]*)>",
            markup,
        )
        self.assertIsNotNone(
            match, f"#{element_id} is no longer a div in templates/portal/flow.html"
        )
        classes = re.search(r'class="([^"]*)"', match.group("attrs"))
        self.assertIsNotNone(classes, f"#{element_id} carries no class attribute")
        return classes.group(1).split()

    def test_every_hideable_field_can_actually_be_hidden(self):
        css = self._flow_css()

        for element_id, direction in self.HIDEABLE_IDS.items():
            for name in self._classes_of(element_id):
                selector = r"^\." + re.escape(name)
                sets_display = re.search(
                    selector + r"\s*\{[^}]*\bdisplay\s*:\s*(?!none\b)",
                    css,
                    re.MULTILINE,
                )
                if not sets_display:
                    continue
                undone = re.search(
                    selector + r"\[hidden\]\s*\{[^}]*\bdisplay\s*:\s*none",
                    css,
                    re.MULTILINE,
                )
                self.assertIsNotNone(
                    undone,
                    f".{name} sets a display, so [hidden] on #{element_id} is "
                    f"inert and a {direction} shows a field it does not collect. "
                    f"static/css/flow.css needs `.{name}[hidden] "
                    "{ display: none; }`.",
                )

    def test_the_proof_field_is_not_hidden_by_the_markup_alone(self):
        """It is a deposit field, so it ships visible and the script hides it.

        Asserted so the test above cannot be satisfied by quietly moving the
        decision into the template, where the server's `needs` object no longer
        reaches it.
        """
        markup = self._flow_html()

        match = re.search(r'<div class="field" id="proof-field"(?P<rest>[^>]*)>', markup)

        self.assertIsNotNone(match, "#proof-field is no longer `class=\"field\"`")
        self.assertNotIn("hidden", match.group("rest"))

    def test_the_server_tells_screen_four_to_drop_it(self):
        """The other half of the mechanism: the payload the script reads.

        Kept beside the CSS assertion deliberately. Either one passing alone is
        the state this bug was already in — the server said drop it and the
        screen showed it anyway.
        """
        self.sign_in()

        _response, payload = self.options(type="withdrawal")

        self.assertIs(payload["needs"]["proof"], False)
        self.assertIs(payload["needs"]["destination"], True)
