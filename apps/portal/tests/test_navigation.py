"""The whole walk, screen 1 to screen 6, in order.

This module exists because of a bug the rest of the suite could not have
caught. Reversing the wizard so the merchant comes before the method (25 Aug
2026) updated the `WIZARD` array in ``flow.js`` and three of the four places
that navigated forward. The fourth — the handler on screen 1 — went on sending
the client to ``"method"`` by name, over the top of the merchant screen, into a
method list that is *correctly* empty until a merchant has been chosen.

Every existing test passed. They check the steps one at a time: ask for the
merchants and get merchants, ask for a merchant's methods and get methods. None
of them asked what the client is shown *next*, which is the only question that
was wrong.

So there are two halves here, and they check different things:

* :class:`StepOrderTests` reads ``flow.js`` and asserts the order the wizard
  declares, and that no forward move names its destination. Forward navigation
  goes through ``advance()``, which reads the array — one source for the order,
  so the two cannot disagree again. This is the half that would have failed.
* :class:`WalkTests` drives the server through the same sequence the screens
  do, and asserts at each step both what has arrived and what has *not*: the
  methods are absent until a merchant is chosen, the wallet is absent until a
  method is. That is why the skipped screen produced a blank box rather than an
  error, and it is worth pinning down.
"""

import json
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import reverse

from apps.transactions.models import Request, RequestStatus

from .test_flow import FlowTestCase

FLOW_JS = Path(settings.BASE_DIR) / "static" / "js" / "flow.js"

#: The wizard, in the order the client meets it (spec §7). Screens 5 and 6 are
#: outcomes rather than steps and are deliberately not in it.
EXPECTED_ORDER = ["type", "merchant", "method", "details"]


class StepOrderTests(SimpleTestCase):
    """What the script says about its own order."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = FLOW_JS.read_text(encoding="utf-8")

    def declared_order(self) -> list[str]:
        match = re.search(r"var WIZARD = (\[[^\]]*\]);", self.source)
        self.assertIsNotNone(match, "flow.js no longer declares a WIZARD array")
        return json.loads(match.group(1))

    def test_the_wizard_is_declared_in_the_order_the_client_meets_it(self):
        self.assertEqual(self.declared_order(), EXPECTED_ORDER)

    def test_the_merchant_comes_before_the_method(self):
        """The reversal itself, asserted on its own so a future edit that
        reorders the array has to come here and say so."""
        order = self.declared_order()
        self.assertLess(order.index("merchant"), order.index("method"))

    def test_no_forward_move_names_the_screen_it_goes_to(self):
        """The bug in one line.

        A ``go("merchant")`` or ``go("method")`` or ``go("details")`` anywhere
        is a second copy of the order, and a second copy is a copy that can
        disagree. Forward navigation goes through ``advance()``, which reads
        the array.

        Backward moves may still name a screen — a chip on screen 4 goes
        straight to the one it stands for, and that is a jump *to a known
        earlier* screen, not an assumption about what comes next. Those are
        written with ``replace: true`` or through ``retreat()``.
        """
        forward = re.findall(r'\bgo\("(merchant|method|details)"(?!\s*,\s*\{)', self.source)
        self.assertEqual(
            forward,
            [],
            "a wizard screen is navigated to by name; use advance() instead",
        )

    def test_advance_is_what_the_screens_call(self):
        calls = re.findall(r'\badvance\("(\w+)"\)', self.source)
        # One per step that has a next: type → merchant → method → details.
        self.assertEqual(calls, ["type", "merchant", "method"])

    def test_advance_reads_the_array_rather_than_a_second_list(self):
        body = re.search(
            r"function advance\(from\) \{(.*?)\n  \}", self.source, re.S
        )
        self.assertIsNotNone(body, "advance() is gone")
        self.assertIn("WIZARD.indexOf(from)", body.group(1))

    def test_every_choice_screen_has_an_empty_state_to_render_into(self):
        """A list that came back empty is a fact the client is owed a reason
        for. A bordered box with nothing in it is not one."""
        for screen in ("type", "merchant", "method"):
            self.assertIn(f'el("{screen}-empty-text")', self.source, screen)

    def test_the_empty_method_screen_explains_the_case_that_broke(self):
        """No merchant chosen means no methods to list — the correct answer to
        a question the client was never asked. It says so now."""
        self.assertIn("methodNoMerchant", self.source)


class WalkTests(FlowTestCase):
    """The same sequence, driven against the server."""

    def test_the_walk_from_screen_one_to_screen_six(self):
        self.sign_in()

        # --- screen 1: the direction. The catalogue opens on the merchants,
        # because who comes before how.
        _response, step1 = self.options()
        self.assertEqual(
            [m["name"] for m in step1["merchants"]], ["تاجر بغداد"]
        )
        self.assertEqual(step1["methods"], [], "screen 3's list arrived at screen 2")
        self.assertNotIn("wallet", step1)

        # --- screen 2: choose the merchant, and their methods arrive.
        merchant = step1["merchants"][0]
        _response, step2 = self.options(merchant=merchant["id"])
        self.assertEqual([m["code"] for m in step2["methods"]], ["zaincash"])
        self.assertEqual(step2["merchant"]["id"], merchant["id"])
        self.assertNotIn("wallet", step2, "screen 4's wallet arrived at screen 3")

        # --- screen 3: choose the method, and the wallet arrives.
        method = step2["methods"][0]
        _response, step3 = self.options(
            merchant=merchant["id"], method=method["code"]
        )
        self.assertEqual(step3["wallet"]["number"], "07701234567")
        self.assertIsNone(step3["unavailable"])

        # --- screen 4: submit against exactly what was shown.
        response = self.submit(
            merchant=str(merchant["id"]),
            method=method["code"],
            wallet=str(step3["wallet"]["id"]),
            rate=str(step3["rate"]["id"]),
        )
        self.assertEqual(response.status_code, 201, response.content)
        created = self.body(response)["request"]

        # --- screen 5: the reference.
        reference = created["reference"]
        self.assertTrue(reference)
        self.assertEqual(
            Request.objects.get(public_ref=reference).merchant_selected_id,
            merchant["id"],
        )

        # --- screen 6: the request, with its timeline.
        detail = self.client.get(
            reverse("portal:request_detail", kwargs={"reference": reference})
        )
        self.assertEqual(detail.status_code, 200)
        shown = json.loads(detail.content.decode("utf-8"))["request"]
        self.assertEqual(shown["reference"], reference)
        self.assertTrue(shown["timeline"])

    def test_the_walk_holds_for_a_withdrawal_too(self):
        """Screens 1 to 3 are the same in both directions (step 10), so the
        order has to be, and screen 4 is where they part."""
        from decimal import Decimal

        from apps.rates.models import ExchangeRate, RateType

        ExchangeRate.objects.create(
            rate_type=RateType.WITHDRAWAL,
            iqd_per_usd=Decimal("1470.00"),
            commission_iqd_per_100usd=Decimal("5000.00"),
        )
        self.sign_in()

        _response, step1 = self.options(type="withdrawal")
        self.assertEqual([m["name"] for m in step1["merchants"]], ["تاجر بغداد"])
        self.assertEqual(step1["methods"], [])

        merchant = step1["merchants"][0]
        _response, step2 = self.options(type="withdrawal", merchant=merchant["id"])
        self.assertEqual([m["code"] for m in step2["methods"]], ["zaincash"])

        _response, step3 = self.options(
            type="withdrawal", merchant=merchant["id"], method="zaincash"
        )
        # The merchant pays out, so there is nothing to pay into.
        self.assertNotIn("wallet", step3)
        self.assertFalse(step3["needs"]["wallet"])
        self.assertTrue(step3["needs"]["destination"])

    def test_the_second_screen_is_never_skippable_by_the_data(self):
        """The symptom, pinned. Asking for methods without a merchant is not an
        error — it is an empty list, which is why the skipped screen rendered a
        blank box instead of failing."""
        self.sign_in()

        _response, payload = self.options(method="zaincash")

        self.assertEqual(payload["methods"], [])
        self.assertIsNone(payload["unavailable"])
        self.assertNotIn("method", payload)

    def test_stepping_back_and_choosing_a_different_merchant_re_resolves(self):
        """A client walking back and forth always gets what is true now."""
        from apps.merchants.models import Merchant, MerchantMethod, Wallet

        other = Merchant.objects.create(name="تاجر البصرة")
        link = MerchantMethod.objects.create(
            merchant=other, payment_method=self.method
        )
        second = Wallet.objects.create(merchant_method=link, number="07709999999")
        self.sign_in()

        _response, first = self.options(
            merchant=self.merchant.pk, method="zaincash"
        )
        _response, again = self.options(merchant=other.pk, method="zaincash")

        self.assertEqual(first["wallet"]["id"], self.wallet.pk)
        self.assertEqual(again["wallet"]["id"], second.pk)

    def test_a_merchant_who_covers_nothing_leaves_screen_three_empty(self):
        """The state the empty text exists for: a real merchant, a real
        choice, and nothing behind it in this direction."""
        self.method.supports_deposit = False
        self.method.save(update_fields=["supports_deposit"])
        self.sign_in()

        _response, payload = self.options(merchant=self.merchant.pk)

        self.assertEqual(payload["methods"], [])
        # And they are not offered on screen 2 either, so the client never
        # reaches that state by walking forward — only by holding a stale id.
        self.assertEqual(payload["merchants"], [])
        self.assertEqual(payload["unavailable"], "merchant")


class WalkAfterArchivingTests(FlowTestCase):
    """The walk when something is retired half way through it."""

    def test_a_merchant_archived_mid_walk_sends_the_client_back_to_screen_two(self):
        from apps.accounts.models import Role
        from apps.accounts.permissions import sync_role_groups
        from apps.accounts.tests import make_user
        from apps.merchants import lifecycle

        sync_role_groups()
        admin = make_user("root@maxifyfx.com", Role.FINANCE_ADMIN)
        self.sign_in()

        _response, step2 = self.options(merchant=self.merchant.pk)
        self.assertEqual([m["code"] for m in step2["methods"]], ["zaincash"])

        lifecycle.archive_merchant(self.merchant, actor=admin)

        _response, step3 = self.options(
            merchant=self.merchant.pk, method="zaincash"
        )
        self.assertEqual(step3["unavailable"], "merchant")
        self.assertEqual(step3["merchants"], [])


class WalkStatusTests(FlowTestCase):
    def test_a_deposit_lands_with_the_merchant_the_client_chose(self):
        """Phase A: no prior review. The walk ends with the request already
        routed, which is what screen 6's timeline shows."""
        self.sign_in()

        response = self.submit()
        reference = self.body(response)["request"]["reference"]

        created = Request.objects.get(public_ref=reference)
        self.assertEqual(created.status, RequestStatus.ASSIGNED)
        self.assertEqual(created.merchant_assigned_id, self.merchant.pk)
