"""The client's compose screen, and the walk the server still sees behind it.

This module exists because of a bug the rest of the suite could not have
caught. When the merchant was moved in front of the method (25 Aug 2026) the
`WIZARD` array in ``flow.js`` was updated and three of the four places that
navigated forward; the fourth — the handler on screen 1 — went on sending the
client to ``"method"`` by name, over the top of the merchant screen, into a
method list that is *correctly* empty until a merchant has been chosen. Every
existing test passed, because they check the steps one at a time: ask for the
merchants and get merchants, ask for a merchant's methods and get methods. None
asked what the client is shown *next*, which is the only question that was
wrong.

**The wizard is gone.** The four screens are one: the three choices sit in a
row, all of them on screen at once, and the details appear underneath when the
third is answered. There is no forward navigation left to get wrong — nothing
to advance to.

What survived the restructure is the *reason* the bug happened, and so the
reason for this module: the order those three choices depend on each other in
was written down twice, and the two copies drifted. It is written down once now
(``CHAIN``), and the two halves below check different things:

* :class:`PickerChainTests` reads ``flow.js`` and ``flow.html`` and asserts the
  chain is declared once and derived from everywhere — the unlocking, the
  resetting, and whether the details are shown. It also pins the three
  properties the row exists for: all three columns are always in the DOM, a
  locked column offers nothing, and the details are not on screen until every
  answer is in.
* :class:`WalkTests` and the classes after it drive the *server* through the
  same sequence, and assert at each step both what has arrived and what has
  *not*: the methods are absent until a merchant is chosen, the wallet absent
  until a method is. None of that moved — the endpoints, the payloads and the
  filtering rules are exactly what they were, and the row asks for them in the
  same order the wizard did. That is why these tests are unchanged.
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
FLOW_HTML = Path(settings.BASE_DIR) / "templates" / "portal" / "flow.html"
FLOW_CSS = Path(settings.BASE_DIR) / "static" / "css" / "flow.css"

#: The three answers a request is made of, in the order they depend on each
#: other (spec §7). The details are not in it: they are what the chain produces,
#: not a link in it.
EXPECTED_CHAIN = ["type", "merchant", "method"]

#: The id of each column in the row, keyed by the step it answers.
COLUMN_IDS = {
    "type": "pick-type",
    "merchant": "pick-merchant",
    "method": "pick-method",
}


class PickerChainTests(SimpleTestCase):
    """What the row says about itself."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = FLOW_JS.read_text(encoding="utf-8")
        cls.markup = FLOW_HTML.read_text(encoding="utf-8")
        cls.styles = FLOW_CSS.read_text(encoding="utf-8")

    def declared_chain(self) -> list[str]:
        match = re.search(r"var CHAIN = (\[[^\]]*\]);", self.source)
        self.assertIsNotNone(match, "flow.js no longer declares a CHAIN array")
        return json.loads(match.group(1))

    # -- the order, declared once ------------------------------------------

    def test_the_chain_is_declared_in_the_order_the_client_meets_it(self):
        self.assertEqual(self.declared_chain(), EXPECTED_CHAIN)

    def test_the_merchant_comes_before_the_method(self):
        """The reversal itself, asserted on its own so a future edit that
        reorders the chain has to come here and say so."""
        chain = self.declared_chain()
        self.assertLess(chain.index("merchant"), chain.index("method"))

    def test_nothing_navigates_to_a_step_by_name(self):
        """The original bug, and it is now unrepresentable.

        A ``go("merchant")`` was a second copy of the order. There are no such
        screens left to name — answering a step unlocks the next column in
        place — so any survivor is a reference to a screen that no longer
        exists.
        """
        named = re.findall(r'\bgo\("(type|merchant|method|details)"', self.source)
        self.assertEqual(
            named, [], "flow.js navigates to a screen the restructure removed"
        )

    def test_the_wizard_machinery_is_gone_rather_than_left_lying_about(self):
        """`advance()` and `retreat()` existed to get forward movement right.

        There is no forward movement. Leaving them behind would leave a second
        way to express the order, which is the whole failure this module is
        named after.
        """
        for dead in ("function advance(", "function retreat(", "var WIZARD"):
            self.assertNotIn(dead, self.source, f"{dead} survived the restructure")

    # -- everything derived from it ----------------------------------------

    def test_unlocking_walks_the_chain_rather_than_naming_three_columns(self):
        body = re.search(
            r"function renderPicker\(\) \{(.*?)\n  \}", self.source, re.S
        )
        self.assertIsNotNone(body, "renderPicker() is gone")
        self.assertIn("CHAIN.forEach", body.group(1))

    def test_changing_an_answer_clears_the_rest_by_reading_the_chain(self):
        """"Reset everything after this one" is the chain's own question.

        Written as three handlers each listing what it invalidates, it is three
        copies of the order — and the one that forgets an entry leaves a method
        selected under a merchant who no longer offers it.
        """
        body = re.search(
            r"function resetAfter\(step\) \{(.*?)\n  \}", self.source, re.S
        )
        self.assertIsNotNone(body, "resetAfter() is gone")
        self.assertIn("CHAIN.indexOf(step) + 1", body.group(1))

    def test_every_choice_handler_resets_what_came_after_it(self):
        """One call per step that has anything after it — and the last one too,
        since a method change still drops the wallet."""
        calls = re.findall(r'\bresetAfter\("(\w+)"\)', self.source)
        self.assertEqual(calls, EXPECTED_CHAIN)

    def test_every_step_in_the_chain_knows_how_to_identify_its_answer(self):
        """`KEY_OF` is what marks the chosen row in each column.

        The three answers do not agree on a shape — a merchant has an `id`, a
        method a `code`, the direction is the string — so a step added to the
        chain without an entry here would render a column in which nothing ever
        looks selected.
        """
        block = re.search(r"var KEY_OF = \{(.*?)\n  \};", self.source, re.S)
        self.assertIsNotNone(block, "flow.js no longer declares KEY_OF")
        for step in self.declared_chain():
            self.assertRegex(
                block.group(1), rf"\b{step}\s*:", f"KEY_OF has no rule for {step!r}"
            )

    # -- what the row is for -----------------------------------------------

    def test_all_three_columns_are_always_in_the_dom(self):
        """The point of the restructure. A client can see every choice they
        have made and every one still to make, without navigating."""
        for step, element_id in COLUMN_IDS.items():
            match = re.search(
                r'<div class="picker__col" id="' + re.escape(element_id) + r'"(?P<rest>[^>]*)>',
                self.markup,
            )
            self.assertIsNotNone(match, f"the {step} column is not in flow.html")
            self.assertNotIn(
                "hidden",
                match.group("rest"),
                f"the {step} column ships hidden; all three are always on screen",
            )

    def test_every_column_is_a_button_and_a_listbox(self):
        """Built, not native, and the reason is the icon.

        An `<option>` renders no markup, so a payment method's logo cannot sit
        beside its name in a native list — and the logo is how a client
        recognises the rail. What a `<select>` gave away for free is paid for
        explicitly instead; the tests below are the receipt.
        """
        for step in EXPECTED_CHAIN:
            self.assertIn(
                f'class="dropdown__button" id="{step}-button"',
                self.markup,
                f"the {step} column has no dropdown button",
            )
            self.assertIn(
                f'class="dropdown__list" id="{step}-list" role="listbox"',
                self.markup,
                f"the {step} column has no listbox",
            )

    def test_the_button_says_what_it_opens_and_what_it_is(self):
        """`aria-labelledby` names it with the column's own label followed by
        the current value, so it is announced as "the merchant, Ahmed Kadhim"
        rather than as an unlabelled button."""
        for step in EXPECTED_CHAIN:
            match = re.search(
                r'id="' + step + r'-button"(?P<rest>[\s\S]*?)>', self.markup
            )
            self.assertIsNotNone(match, step)
            rest = match.group("rest")
            self.assertIn('aria-haspopup="listbox"', rest, step)
            self.assertIn('aria-expanded="false"', rest, step)
            self.assertIn(f'aria-labelledby="{step}-label {step}-value"', rest, step)

    def test_the_icon_is_inside_the_options_not_only_beside_the_control(self):
        """The whole reason the native control was given up.

        `fillOptions()` paints a mark into every row it builds, through the same
        function the button uses, so the list and the button cannot end up
        showing different things.
        """
        body = re.search(
            r"function fillOptions\(step, rows\) \{(.*?)\n  \}", self.source, re.S
        )
        self.assertIsNotNone(body, "fillOptions() is gone")
        self.assertIn("dropdown__icon", body.group(1))
        self.assertIn("paintIcon(slot", body.group(1))

        # And the method column is the one with an icon to paint.
        icons = re.search(r"var ICON_OF = \{(.*?)\n  \};", self.source, re.S)
        self.assertIsNotNone(icons, "flow.js no longer declares ICON_OF")
        self.assertIn("method.icon", icons.group(1))

    def test_the_keyboard_is_paid_for_rather_than_skipped(self):
        """A native select brought all of this. A built one has to say it."""
        for key in ('"Escape"', '"ArrowDown"', '"ArrowUp"', '"Home"', '"End"',
                    '"Enter"', '"Tab"'):
            self.assertIn(key, self.source, f"the listbox does not handle {key}")
        self.assertIn("aria-activedescendant", self.source)
        self.assertIn('setAttribute("aria-selected"', self.source)

    def test_the_two_later_columns_ship_locked(self):
        """Nothing is answered on the first paint, so nothing after the first
        column may be usable before the script has run."""
        for step in ("merchant", "method"):
            match = re.search(
                r'id="' + step + r'-button"(?P<rest>[\s\S]*?)>', self.markup
            )
            self.assertIsNotNone(match, step)
            self.assertIn("disabled", match.group("rest"), step)

    def test_a_locked_column_is_emptied_and_not_merely_disabled(self):
        """The merchants sitting behind a locked merchant column are the
        *deposit* ones — that is what the options endpoint defaults to, and it
        hands them over on every payload regardless.

        Disabling alone would leave them in the DOM, one removed attribute away
        from being offered as the answer to a question nobody asked. So the
        options go too.
        """
        body = re.search(
            r"function renderPicker\(\) \{(.*?)\n  \}", self.source, re.S
        )
        self.assertIsNotNone(body, "renderPicker() is gone")
        self.assertIn("box.button.disabled = !open", body.group(1))
        self.assertIn("box.list.replaceChildren()", body.group(1))

    def test_no_column_carries_a_line_of_explanation_under_its_label(self):
        """The label names the column and the list shows what is in it. A
        sentence saying "choose the merchant you will transfer to" under a
        control already labelled "the merchant" is the screen reading itself
        out loud — and three of them at three different lengths is what stopped
        the columns lining up."""
        self.assertNotIn("picker__lead", self.markup)
        self.assertNotIn("picker__lead", self.styles)
        for dead in ("merchantLead", "methodLead"):
            self.assertNotIn(dead, self.source, f"{dead} outlived the line it wrote")

    def test_the_three_columns_line_up_by_construction(self):
        """Not by matching min-heights and hoping the text stays short.

        `display: contents` on the column makes the label, the control and the
        empty state direct grid items, so every label lands in row 1, every
        control in row 2 and every empty state in row 3. The rows size to their
        tallest member, which is what makes the three columns the same height
        whatever any of them says.
        """
        grid = re.search(
            # The picker's own breakpoint, not merely the first one in the file
            # — `.app__body` has one too, and matching that instead is how this
            # assertion would pass while saying nothing.
            r"@media \(min-width: \d+rem\) \{\s*\.picker \{(.*?)\n\}", self.styles, re.S
        )
        self.assertIsNotNone(grid, "the picker's breakpoint is gone")
        self.assertIn("grid-template-rows", grid.group(1))
        self.assertRegex(grid.group(1), r"\.picker__col \{ display: contents; \}")

    def test_the_details_ship_hidden_and_are_shown_by_the_chain(self):
        match = re.search(r'<div class="details" id="details"(?P<rest>[^>]*)>', self.markup)
        self.assertIsNotNone(match, "the details block is not in flow.html")
        self.assertIn(
            "hidden",
            match.group("rest"),
            "the details are on screen before anything has been chosen",
        )
        self.assertIn("show(nodes.details,", self.source)

    def test_the_details_can_actually_be_hidden(self):
        """`.details` sets a display, so `[hidden]` on it is inert without a
        companion rule — the trap that put the withdrawal proof upload back on
        screen. Asserted here as well as in the withdrawal suite because this
        block is the one the whole restructure hangs on."""
        self.assertRegex(
            self.styles,
            r"(?m)^\.details\[hidden\]\s*\{[^}]*display\s*:\s*none",
            "static/css/flow.css needs `.details[hidden] { display: none; }`",
        )

    def test_the_icon_slot_can_actually_be_hidden(self):
        """`.dropdown__icon` sets `display: grid` — the same trap as `.field`,
        on the slot that comes and goes with the answer. A merchant has no mark
        to show, so its slot is hidden on every row of that column."""
        self.assertRegex(
            self.styles,
            r"(?m)^\.dropdown__icon\[hidden\]\s*\{[^}]*display\s*:\s*none",
            "static/css/flow.css needs `.dropdown__icon[hidden] { display: none; }`",
        )

    def test_the_row_collapses_to_one_column_on_a_phone(self):
        """Column first, grid only once there is width for three legible ones.

        Written this way round on purpose: a three-column grid squeezed onto a
        phone is not a smaller version of this design, it is captions wrapping
        mid-word.
        """
        base = re.search(r"(?m)^\.picker \{([^}]*)\}", self.styles)
        self.assertIsNotNone(base, "the .picker rule is gone")
        self.assertIn("flex-direction: column", base.group(1))
        self.assertRegex(
            self.styles,
            r"@media \(min-width: \d+rem\) \{\s*\.picker \{[^}]*grid-template-columns",
            "the three-column layout is not behind a min-width media query",
        )

    def test_every_column_has_an_empty_state_to_render_into(self):
        """A list that came back empty is a fact the client is owed a reason
        for. A bordered box with nothing in it is not one."""
        for step in EXPECTED_CHAIN:
            self.assertIn(f'el("{step}-empty-text")', self.source, step)

    def test_the_empty_method_column_still_explains_the_case_that_broke(self):
        """No merchant chosen means no methods to list — the correct answer to
        a question the client was never asked. The column is locked before that
        happens now, so it should be unreachable; the wording stays because an
        emptiness arriving out of order should still say why."""
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
