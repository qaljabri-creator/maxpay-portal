"""Creating a client request from what the wizard collected (spec §6, §7).

Every figure written here is computed from the database, never accepted from the
form. The client's browser sends *which* wallet and *which* rate it was shown,
and those are checked against what is in force right now; if either moved while
the client was filling the screen in, the submission is refused with a code the
embed turns into "the number changed, look again" rather than quietly charging
them at a rate they never saw. That is the whole reason ``wallet`` and ``rate``
travel back at all — they are consent, not data.

What is stored is then frozen: ``wallet_number_snapshot``, ``rate_applied`` and
``commission_applied`` come from the objects verified inside this transaction,
so a later wallet swap or rate revision cannot rewrite a request that already
happened (spec §5, §9).

**A deposit does not stop here.** Once written, it is handed to
:func:`apps.transactions.services.auto_route`, which routes it to the merchant
the client picked so they can act on it without waiting for a desk. Finance
sees it the moment it lands and keeps every lever over it. A withdrawal is not
routed: review is where the client's balance is checked and debited (spec §6).

**Both directions run through here** (build-order step 10). They share the first
three screens exactly — type, method, merchant — and diverge only at the fourth,
which is why the resolvers below are shared and only the two ``build_*`` entry
points differ. What actually differs is short:

===============  ==========================  ==========================
                 deposit                     withdrawal
===============  ==========================  ==========================
wallet           the merchant's, snapshotted  none — the merchant pays out
destination      none                         the client's card or wallet
proof            required at submission       refused, not ignored; the
                                              merchant uploads it when they
                                              pay (spec §6)
daily cap        checked against the wallet   nothing to cap
amount_iqd       what the client transfers    what the client receives
===============  ==========================  ==========================
"""

import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext as _

from apps.core import hours as business_hours
from apps.core.choices import ActorRole, AuditAction
from apps.core.services import record_audit, snapshot
from apps.core.validators import (
    validate_real_content_type,
    validate_upload_content,
    validate_upload_size,
)
from apps.merchants import capacity
from apps.merchants.models import Merchant, PaymentMethod
from apps.transactions import services as transitions
from apps.transactions.models import (
    Attachment,
    Message,
    Request,
    RequestStatus,
    RequestType,
)

from . import catalog, destinations, pricing

logger = logging.getLogger("maxpay.portal")

#: What the audit entry records. ``client`` is included deliberately: the audit
#: log is Finance-only (``accounts.view_audit_log``, never granted to a
#: merchant), and tracing a request back to a client is the point of it.
#: ``destination_account`` is in it for the same reason — where a withdrawal was
#: sent is exactly what an investigation later needs to establish.
AUDIT_FIELDS = [
    "public_ref",
    "type",
    "client",
    "payment_method",
    "merchant_selected",
    "wallet_number_snapshot",
    "destination_account",
    "amount_usd",
    "amount_iqd",
    "rate_applied",
    "commission_applied",
    "status",
]


class SubmissionError(Exception):
    """A refusal the embed can act on: ``code`` decides which screen recovers."""

    def __init__(self, code: str, message, *, status: int = 400, **extra):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


@dataclass(frozen=True)
class Draft:
    """A submission that has passed every check and is ready to be written.

    One class for both directions rather than two: everything the type does
    *not* change would otherwise be duplicated, and a field that only one
    direction fills is more honestly an empty field than a second class.
    """

    type: str
    client: object
    method: PaymentMethod
    merchant: Merchant
    quote: pricing.Quote
    message: str = ""
    #: Deposit only — the wallet the client was shown and will pay into.
    wallet: object = None
    #: Deposit only — proof of the transfer they just made.
    proof: object = None
    proof_content_type: str = ""
    #: Withdrawal only — the client's own card or wallet, already normalised.
    destination: str = ""

    @property
    def is_withdrawal(self) -> bool:
        return self.type == RequestType.WITHDRAWAL


# ---------------------------------------------------------------------------
# Reading the form
# ---------------------------------------------------------------------------


def _require(data, field: str, message) -> str:
    value = (data.get(field) or "").strip()
    if not value:
        raise SubmissionError(f"{field}_missing", message)
    return value


def resolve_type(data) -> str:
    """Which direction the client is asking for (spec §6)."""
    value = _require(data, "type", _("اختر نوع الطلب."))
    if value not in RequestType.values:
        raise SubmissionError("type_unknown", _("نوع طلب غير معروف."))
    return value


def resolve_method(request_type: str, data) -> PaymentMethod:
    code = _require(data, "method", _("اختر طريقة الدفع."))
    method = catalog.available_methods(request_type).filter(code=code).first()
    if method is None:
        raise SubmissionError(
            "method_unavailable",
            _("طريقة الدفع هذه لم تعد متاحة. اختر غيرها."),
            status=409,
        )
    return method


def resolve_merchant(request_type: str, method: PaymentMethod, data) -> Merchant:
    raw = _require(data, "merchant", _("اختر التاجر."))
    if not raw.isdigit():
        raise SubmissionError("merchant_unknown", _("تاجر غير معروف."))
    # Still narrowed by the method even though the client now chooses in the
    # other order: what has to hold at submission is that *this pair* is
    # offerable, and that is the same question whichever screen came first.
    merchant = (
        catalog.available_merchants(request_type, method).filter(pk=int(raw)).first()
    )
    if merchant is None:
        raise SubmissionError(
            "merchant_unavailable",
            _("هذا التاجر لم يعد متاحًا لطريقة الدفع المختارة. اختر غيره."),
            status=409,
        )
    return merchant


def resolve_link(request_type: str, method: PaymentMethod, merchant: Merchant):
    """The ``MerchantMethod`` joining the two, still offerable right now."""
    link = catalog.merchant_method(request_type, method, merchant)
    if link is None:
        raise SubmissionError(
            "merchant_unavailable",
            _("هذا التاجر لم يعد متاحًا لطريقة الدفع المختارة. اختر غيره."),
            status=409,
        )
    return link


def resolve_wallet(request_type: str, method: PaymentMethod, merchant: Merchant, data):
    """The wallet in force now, provided it is the one the client was shown.

    Deposits only. A withdrawal has no wallet to verify — the money travels the
    other way, and the merchant pays out of whatever they hold.
    """
    link = resolve_link(request_type, method, merchant)

    wallet = catalog.active_wallet(link)
    if wallet is None:
        raise SubmissionError(
            "wallet_unavailable",
            _("لا توجد محفظة نشطة لهذا التاجر حاليًا. اختر تاجرًا آخر."),
            status=409,
        )

    if capacity.headroom_iqd(wallet) == 0:
        # Already at its daily ceiling. Refused here rather than after the
        # amount is typed, because no amount would fit and the client should
        # be sent back to the merchant list, not to the amount field.
        raise SubmissionError(
            "wallet_cap_reached",
            _("بلغ هذا التاجر سقفه اليومي لهذه الطريقة. اختر تاجرًا آخر."),
            status=409,
        )

    shown = (data.get("wallet") or "").strip()
    if shown != str(wallet.pk):
        # The number on screen is stale. Storing the current one anyway would
        # snapshot a wallet the client never saw — against money they may
        # already have transferred to the one they did.
        raise SubmissionError(
            "wallet_changed",
            _("تغيّر رقم المحفظة أثناء تعبئة الطلب. راجع الرقم الجديد قبل الإرسال."),
            status=409,
            wallet=catalog.wallet_payload(wallet),
        )
    return wallet


def resolve_quote(request_type: str, data) -> pricing.Quote:
    """Price the amount at the rate in force, provided it is the quoted one."""
    try:
        rate = pricing.current_rate(request_type)
        amount = pricing.parse_amount_usd(data.get("amount_usd"), request_type)
    except pricing.PricingError as exc:
        status = 503 if exc.code == "no_rate" else 400
        raise SubmissionError(exc.code, exc.message, status=status) from exc

    quoted = (data.get("rate") or "").strip()
    if quoted != str(rate.pk):
        # Priced at the *current* rate for the "look again" payload — the whole
        # point is to show the client the figure they would be agreeing to now.
        # A withdrawal whose commission now swallows the amount has no such
        # figure, so the plain refusal below carries it instead.
        try:
            moved = pricing.quote(rate, amount, request_type).as_dict()
        except pricing.PricingError:
            moved = None
        raise SubmissionError(
            "rate_changed",
            _("تغيّر سعر الصرف أثناء تعبئة الطلب. راجع المبلغ الجديد قبل الإرسال."),
            status=409,
            rate=pricing.rate_payload(rate, request_type),
            quote=moved,
        )

    try:
        return pricing.quote(rate, amount, request_type)
    except pricing.PricingError as exc:
        # Only reachable on a withdrawal: the commission ate the payout.
        raise SubmissionError(exc.code, exc.message) from exc


def resolve_proof(files) -> tuple[object, str]:
    """The proof of payment. Required for a deposit (spec §6)."""
    proof = files.get("proof")
    if proof is None:
        raise SubmissionError("proof_missing", _("أرفق إثبات التحويل."))
    try:
        # Cheapest first: refuse an oversized file before reading any of it,
        # and a forbidden extension before trusting anything about the name.
        validate_upload_size(proof)
        validate_upload_content(proof)
        real_type = validate_real_content_type(proof)
    except ValidationError as exc:
        raise SubmissionError("proof_invalid", " ".join(exc.messages)) from exc
    return proof, real_type


def refuse_proof(files) -> None:
    """A withdrawal carries no proof of payment, and says so out loud.

    Item 1.1 of the Finance review of 24 Aug 2026: the client does not pay in a
    withdrawal, the merchant does, so the field is wrong on that direction. The
    merchant's own proof upload at ``pay`` is untouched (spec §6).

    Refusing rather than ignoring, which is the part that needed deciding. A
    file dropped on the floor is indistinguishable, from the client's side, from
    a file that was stored: they attached evidence, the request went through,
    and they will say so later when a payment is disputed. Nothing would exist
    to back that up, and nobody would know why. So the submission is refused
    with a code the embed can act on, the same way every other unusable field on
    this screen is.

    It also puts the rule somewhere it can be read. ``build_withdrawal_draft``
    simply never reading ``files`` made the guarantee a property of what the
    function omitted, and an omission is the kind of guarantee a later edit
    breaks without anybody noticing — which is what happened on the display
    side, where the field went on being drawn because a stylesheet rule made
    ``hidden`` inert and nothing asserted otherwise.
    """
    if files and files.get("proof") is not None:
        raise SubmissionError(
            "proof_not_accepted",
            _("طلبات السحب لا تحتاج إثبات تحويل: التاجر هو من يدفع، وهو من يرفع الإثبات."),
        )


def resolve_destination(data) -> str:
    """Where a withdrawal is to be paid (spec §6).

    Stored normalised, because it is what the merchant reads off the screen and
    types into a banking app: the fewer ways the same account can be written,
    the fewer ways it can be re-typed wrong.
    """
    try:
        return destinations.normalise(data.get("destination_account"))
    except destinations.DestinationError as exc:
        raise SubmissionError(exc.code, exc.message) from exc


def resolve_message(data) -> str:
    body = (data.get("message") or "").strip()
    limit = int(getattr(settings, "PORTAL_MESSAGE_MAX_CHARS", 1000))
    if len(body) > limit:
        raise SubmissionError(
            "message_too_long",
            _("الرسالة أطول من %(limit)s حرف.") % {"limit": limit},
        )
    return body


def check_wallet_capacity(wallet, priced: pricing.Quote) -> None:
    """Refuse a deposit that would carry the wallet past its daily cap.

    ``Wallet.daily_cap`` is in **Iraqi dinars**, and what is measured against it
    is ``total_iqd`` — the figure the client actually transfers, commission
    included, because that is what lands in the account being capped.

    The client is told what still fits rather than only that they were refused:
    a cap they cannot see is a cap they will hit again on the next attempt.
    """
    remaining = capacity.headroom_iqd(wallet)
    if remaining is None or priced.total_iqd <= remaining:
        return
    raise SubmissionError(
        "wallet_cap_exceeded",
        _("المبلغ يتجاوز ما تبقّى من السقف اليومي لهذا التاجر (%(left)s دينار). "
          "قلّل المبلغ أو اختر تاجرًا آخر.")
        % {"left": f"{remaining:,.0f}"},
        status=409,
        remaining_iqd=f"{remaining:.2f}",
    )


def check_business_hours() -> None:
    """Refuse a submission made outside business hours (spec §7) — step 11.

    The check lives here rather than in the view so both directions and any
    future submission surface inherit it, and so it runs *before* a proof file
    is read off the wire.

    It applies to submissions only. A client may still open a request they filed
    earlier, read its thread and write into it after hours: spec §7 replaces the
    submission screens, and a question asked at midnight is still a question
    (see :mod:`apps.transactions.messaging`).
    """
    hours = business_hours.evaluate()
    if hours.is_open:
        return
    raise SubmissionError(
        "portal_closed",
        hours.closed_message,
        status=409,
        hours=hours.payload(),
    )


def build_draft(client, data, files) -> Draft:
    """Validate a submission of either direction, dispatching on its type."""
    check_business_hours()
    request_type = resolve_type(data)
    if request_type == RequestType.WITHDRAWAL:
        return build_withdrawal_draft(client, data, files)
    return build_deposit_draft(client, data, files)


def build_deposit_draft(client, data, files) -> Draft:
    """Validate the whole form, in the order the client filled it in.

    Order matters: someone whose merchant just went offline should be told
    that, not told their amount is wrong on a screen they cannot reach.
    """
    request_type = RequestType.DEPOSIT
    method = resolve_method(request_type, data)
    merchant = resolve_merchant(request_type, method, data)
    wallet = resolve_wallet(request_type, method, merchant, data)
    priced = resolve_quote(request_type, data)
    check_wallet_capacity(wallet, priced)
    proof, proof_type = resolve_proof(files)
    message = resolve_message(data)
    return Draft(
        type=request_type,
        client=client,
        method=method,
        merchant=merchant,
        wallet=wallet,
        quote=priced,
        proof=proof,
        proof_content_type=proof_type,
        message=message,
    )


def build_withdrawal_draft(client, data, files=None) -> Draft:
    """The same order, minus the wallet and the proof, plus the destination.

    The destination is resolved *before* the amount deliberately. Both live on
    screen 4, but a client who mistyped the account has typed the more dangerous
    of the two fields, and the refusal that matters most should be the one they
    are shown first.

    ``files`` is taken only to be refused — see :func:`refuse_proof`. It is
    checked first, ahead of every other field, because it is the one input on
    this screen that should not have been collected at all: telling a client
    their account number is malformed while quietly keeping a file they should
    never have been asked for answers the smaller question first.
    """
    refuse_proof(files)
    request_type = RequestType.WITHDRAWAL
    method = resolve_method(request_type, data)
    merchant = resolve_merchant(request_type, method, data)
    # Not for a wallet — a withdrawal needs none — but to refuse a pairing that
    # stopped being offerable between the merchant screen and the submission.
    resolve_link(request_type, method, merchant)
    destination = resolve_destination(data)
    priced = resolve_quote(request_type, data)
    message = resolve_message(data)
    return Draft(
        type=request_type,
        client=client,
        method=method,
        merchant=merchant,
        quote=priced,
        destination=destination,
        message=message,
    )


# ---------------------------------------------------------------------------
# Writing it
# ---------------------------------------------------------------------------


@transaction.atomic
def create_request(draft: Draft, *, http_request=None) -> Request:
    """Write the request, its proof if it has one, and its opening message as
    one unit.

    All of it or none: a deposit whose proof failed to store is a request
    Finance cannot review, and one the client believes they filed.
    """
    if draft.wallet is not None:
        # Re-checked inside the transaction: the headroom was measured before
        # the upload was read, and two clients can fill the same wallet in that
        # gap. A withdrawal has no wallet and so no cap to race against.
        check_wallet_capacity(draft.wallet, draft.quote)

    created = Request(
        type=draft.type,
        client=draft.client,
        payment_method=draft.method,
        merchant_selected=draft.merchant,
        # The row and the number it showed. The number is the snapshot spec §5
        # freezes; the row is provenance, and it is what makes "was anything
        # ever submitted against this wallet" answerable later.
        wallet=draft.wallet,
        wallet_number_snapshot=draft.wallet.number if draft.wallet else "",
        destination_account=draft.destination,
        amount_usd=draft.quote.amount_usd,
        amount_iqd=draft.quote.total_iqd,
        rate_applied=draft.quote.rate.iqd_per_usd,
        commission_applied=draft.quote.commission_iqd,
        # The rule as well as its answer. A later correction reprices at the
        # rate this request was quoted on, and re-prorating a fee needs the
        # per-100 figure, not the total it produced for the original amount.
        commission_rate_applied=draft.quote.rate.commission_iqd_per_100usd,
        # What was asked for, kept whatever it is corrected to later.
        submitted_amount_usd=draft.quote.amount_usd,
        submitted_amount_iqd=draft.quote.total_iqd,
        status=RequestStatus.SUBMITTED,
    )
    # public_ref is allocated in save(). It is not a field the client supplies,
    # and validating its emptiness here would fail for the wrong reason.
    created.full_clean(exclude=["public_ref"])
    created.save()

    if draft.proof is not None:
        attachment = Attachment(
            request=created,
            file=draft.proof,
            content_type=draft.proof_content_type,
            uploaded_by_role=ActorRole.CLIENT,
            uploaded_by_id=draft.client.pk,
        )
        # original_name and size_bytes are filled in by the model's own save().
        attachment.full_clean(exclude=["request", "original_name", "size_bytes"])
        attachment.save()

    if draft.message:
        Message.objects.create(
            request=created,
            sender_role=ActorRole.CLIENT,
            sender_id=draft.client.pk,
            body=draft.message,
        )

    record_audit(
        action=AuditAction.STATUS_CHANGE,
        target=created,
        actor_label=f"client#{draft.client.pk}",
        before=None,
        after=snapshot(created, AUDIT_FIELDS),
        request=http_request,
    )
    logger.info(
        "%s %s submitted by client %s for %s USD",
        created.get_type_display(),
        created.public_ref,
        draft.client.pk,
        created.amount_usd,
    )

    # A deposit goes straight to the merchant the client chose; a withdrawal
    # still waits for Finance to verify the balance and take the B2CORE debit.
    # The decision, the validation and the audit entry all live in
    # `apps.transactions.services` — this is the handoff, not the rule.
    routed = transitions.auto_route(created, http_request=http_request)
    if routed.pk == created.pk:
        # `apply_transition` works on its own locked re-read, so the instance
        # this function was building is now behind. Refreshed rather than
        # replaced: the caller renders the confirmation from it, and the
        # attachment and message written above are prefetchable off this one.
        created.refresh_from_db()
    return created
