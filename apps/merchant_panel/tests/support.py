"""Fixtures shared by the merchant panel's tests.

The client record is the important one. Every identifying field is seeded with a
string that appears **nowhere else in the system** — no status label, no
reference, no wallet number — so a sweep for those strings in a response body is
a genuine test rather than a coincidence waiting to happen. If any of them ever
turns up in a merchant-facing byte, the anonymity guarantee has been broken and
the sweep says so, whatever route the leak took.
"""

import json
from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import Client as PortalClient
from apps.accounts.models import Role
from apps.accounts.permissions import sync_role_groups
from apps.accounts.tests import make_user, verify_otp
from apps.core.choices import ActorRole
from apps.merchant_panel.anonymity import is_forbidden_key
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.transactions.models import Message, Request, RequestStatus, RequestType

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

#: Deliberately unmistakable. Nothing else in the fixtures spells any of these.
IDENTITY_MARKERS = {
    "display_name": "زينب-الجبوري-QQZZ",
    "email": "zainab-qqzz@example.com",
    "account_number": "MX-QQZZ-90210",
    "b2core_id": "sub-qqzz-77771",
}


class MerchantPanelTestCase(TestCase):
    """One merchant with a request routed to them, and a second merchant who has
    nothing to do with it — because "assigned only" (spec §8) is only tested by
    having somebody to be excluded."""

    def setUp(self):
        sync_role_groups()

        self.user = make_user("merchant-a@example.com", Role.MERCHANT)
        self.other_user = make_user("merchant-b@example.com", Role.MERCHANT)
        self.finance = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)

        self.method = PaymentMethod.objects.create(
            code="zaincash", caption_ar="زين كاش", caption_en="ZainCash"
        )
        self.merchant, self.merchant_method = self.make_merchant("تاجر أ", user=self.user)
        self.other, _link = self.make_merchant("تاجر ب", user=self.other_user)

        self.client_record = PortalClient.objects.create(**IDENTITY_MARKERS)
        self.request_obj = self.make_request()

    # -- fixtures ----------------------------------------------------------

    def make_merchant(self, name, user=None):
        merchant = Merchant.objects.create(name=name, user=user)
        link = MerchantMethod.objects.create(
            merchant=merchant, payment_method=self.method
        )
        Wallet.objects.create(
            merchant_method=link, number="07700000001", label="الرئيسية"
        )
        return merchant, link

    def make_request(self, *, assigned_to=None, **overrides) -> Request:
        merchant = assigned_to if assigned_to is not None else self.merchant
        defaults = dict(
            type=RequestType.DEPOSIT,
            client=self.client_record,
            payment_method=self.method,
            merchant_selected=merchant,
            merchant_assigned=merchant,
            wallet_number_snapshot="07700000001",
            amount_usd=Decimal("100.00"),
            amount_iqd=Decimal("145500.00"),
            rate_applied=Decimal("1450.00"),
            commission_applied=Decimal("500.00"),
            status=RequestStatus.ASSIGNED,
        )
        defaults.update(overrides)
        return Request.objects.create(**defaults)

    def add_thread(self, request_obj=None):
        """A thread with one of everything the filters have to deal with."""
        request_obj = request_obj or self.request_obj
        Message.objects.create(
            request=request_obj,
            sender_role=ActorRole.CLIENT,
            sender_id=self.client_record.pk,
            body="حوّلت المبلغ، هذا رقم العملية 4471.",
        )
        Message.objects.create(
            request=request_obj,
            sender_role=ActorRole.FINANCE_STAFF,
            sender_id=self.finance.pk,
            body="بانتظار تأكيدك.",
        )
        internal = Message.objects.create(
            request=request_obj,
            sender_role=ActorRole.FINANCE_STAFF,
            sender_id=self.finance.pk,
            body=f"العميل {IDENTITY_MARKERS['display_name']} سبق أن تأخر في الدفع.",
            is_internal_note=True,
        )
        return internal

    def login(self, user=None):
        return verify_otp(self.client, user or self.user)

    # -- assertions --------------------------------------------------------

    def assertNoIdentity(self, payload, where="payload"):
        """Fail if ``payload`` names the client, by key **or** by value.

        Two sweeps, because they fail differently. A key sweep catches a field
        that should never have been whitelisted even when it happens to be
        empty in this fixture. A value sweep catches identity that arrived
        under an innocent name — a ``reference`` sourced from the client's
        account number would pass every key check ever written.
        """
        for key, path in walk_keys(payload):
            self.assertFalse(
                is_forbidden_key(key),
                f"{where} exposes an identifying key at {path!r} (spec §2).",
            )
        haystack = json.dumps(payload, ensure_ascii=False, default=str)
        for field, marker in IDENTITY_MARKERS.items():
            self.assertNotIn(
                marker,
                haystack,
                f"{where} leaks the client's {field} (spec §2).",
            )

    def assertBodyHasNoIdentity(self, response, where="response"):
        """The same sweep, on the raw bytes actually sent to the browser.

        Deliberately not on ``response.data``: what reaches the merchant is the
        rendered body, and a template, a header, or a form error is just as
        capable of carrying a name as a serializer field is.
        """
        charset = response.charset or "utf-8"
        if getattr(response, "streaming", False):
            # A served attachment. The bytes are the uploaded file, but the
            # headers are ours — Content-Disposition carries a filename, and a
            # filename is somewhere a name could end up.
            raw = b"".join(response.streaming_content)
        else:
            raw = response.content
        body = raw.decode(charset, errors="replace")
        headers = "\n".join(f"{k}: {v}" for k, v in response.items())
        for field, marker in IDENTITY_MARKERS.items():
            self.assertNotIn(
                marker, body, f"{where} leaks the client's {field} (spec §2)."
            )
            self.assertNotIn(
                marker,
                headers,
                f"{where} leaks the client's {field} in a header (spec §2).",
            )


def walk_keys(payload, path=""):
    """Every dictionary key in ``payload``, with the path that reached it."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            here = f"{path}.{key}" if path else str(key)
            yield key, here
            yield from walk_keys(value, here)
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            yield from walk_keys(item, f"{path}[{index}]")
