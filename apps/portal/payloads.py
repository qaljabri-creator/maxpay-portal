"""A request as its own client sees it (spec §7, screen 6).

This is the mirror image of the merchant serializers spec §2 calls for. Those
whitelist fields to keep client identity *out*; this one whitelists to keep
everything that is not the client's own business out — internal notes, which
merchant Finance actually routed to, who reviewed it. The client is told what
happened to their money and nothing about how the desk works.

Two rules hold throughout:

* **Internal notes never leave.** ``Message.is_internal_note`` exists so Finance
  can talk among themselves on the thread; a filter that forgets it leaks the
  desk's private reasoning straight to the client.
* **The routed merchant is not disclosed.** The client chose
  ``merchant_selected`` and paid into that merchant's wallet, so they see that
  one. ``merchant_assigned`` — who Finance actually routed to, which may differ
  (spec §5) — is an internal decision and stays internal.
"""

from decimal import ROUND_HALF_UP

from django.utils.translation import gettext as _

from apps.core.choices import ActorRole
from apps.transactions import messaging
from apps.transactions.models import Message, Request, RequestStatus, RequestType

from . import attachments as attachment_urls
from . import pricing

#: The deposit lifecycle as a client experiences it (spec §6). The labels are
#: outcomes rather than internal status names: "assigned" is a desk activity,
#: "waiting for the merchant to confirm" is what it means for the client.
#: A deposit is routed to its merchant the moment it is submitted, so there is
#: no review step in front of it and the client is not shown one. The status
#: still exists and Finance still reaches it by hand when auto-routing could
#: not place the request; :func:`timeline` falls back to showing such a request
#: as still at submission, which is where the client's own request actually is.
DEPOSIT_STEPS: list[tuple[str, str, str | None]] = [
    (RequestStatus.SUBMITTED, "تم استلام طلبك", "submitted_at"),
    (RequestStatus.ASSIGNED, "بانتظار تأكيد التاجر", "assigned_at"),
    (RequestStatus.MERCHANT_CONFIRMED, "أكّد التاجر استلام المبلغ", "merchant_actioned_at"),
    (RequestStatus.CREDITED, "أُضيف المبلغ إلى حسابك", None),
    (RequestStatus.CLOSED, "اكتمل الطلب", "closed_at"),
]

WITHDRAWAL_STEPS: list[tuple[str, str, str | None]] = [
    (RequestStatus.SUBMITTED, "تم استلام طلبك", "submitted_at"),
    (RequestStatus.UNDER_REVIEW, "قيد المراجعة لدى المالية", None),
    (RequestStatus.ASSIGNED, "بانتظار تنفيذ التاجر", "assigned_at"),
    (RequestStatus.MERCHANT_PAID, "حوّل التاجر المبلغ", "merchant_actioned_at"),
    (RequestStatus.CLOSED, "اكتمل الطلب", "closed_at"),
]

#: The three ways a request leaves the track, as the client is told about them.
#: Parked is deliberately worded as waiting rather than as a problem: it has not
#: failed and it has not been abandoned, and saying otherwise would have a
#: client chasing something that is simply in a queue.
OFF_TRACK_LABELS: dict[str, tuple[str, str]] = {
    RequestStatus.PENDING: ("طلبك بانتظار المالية", "current"),
    RequestStatus.REJECTED: ("طلب مرفوض", "rejected"),
    RequestStatus.CANCELLED: ("طلب مُلغى", "rejected"),
}


def steps_for(request_type: str) -> list[tuple[str, str, str | None]]:
    return DEPOSIT_STEPS if request_type == RequestType.DEPOSIT else WITHDRAWAL_STEPS


def _iso(value):
    return value.isoformat() if value else None


def timeline(deposit: Request) -> list[dict]:
    """The lifecycle as a list of steps, each ``done``, ``current`` or ``pending``.

    A rejected request keeps whatever it had reached — the client should still
    be able to see how far it got — and gains a final rejected step carrying the
    reason Finance or the merchant gave (spec §6).
    """
    steps = steps_for(deposit.type)
    order = [status for status, _label, _field in steps]
    off_track = deposit.status in OFF_TRACK_LABELS
    rejected = off_track

    if rejected:
        # Nothing after submission is claimed as done: the request stopped
        # somewhere, and pretending to know exactly where would be a guess.
        reached = 0
    else:
        reached = order.index(deposit.status) if deposit.status in order else 0

    entries = []
    for index, (status, label, field) in enumerate(steps):
        if rejected:
            state = "done" if index == 0 else "cancelled"
        elif index < reached:
            state = "done"
        elif index == reached:
            state = "done" if status == RequestStatus.CLOSED else "current"
        else:
            state = "pending"
        entries.append(
            {
                "key": status,
                "label": str(_(label)),
                "state": state,
                "at": _iso(getattr(deposit, field, None)) if field else None,
            }
        )

    if off_track:
        label, state = OFF_TRACK_LABELS[deposit.status]
        entries.append(
            {
                "key": deposit.status,
                "label": str(_(label)),
                "state": state,
                "at": _iso(deposit.closed_at),
                # Only a reason the client is owed is on the row: a handback
                # note or a park note lives in the thread as a note and never
                # reaches here. A parked request therefore carries no reason,
                # which is the truthful answer rather than an empty promise.
                "reason": deposit.rejection_reason,
            }
        )
    return entries


def message_payload(message: Message, client) -> dict:
    """One entry in the thread, labelled by role rather than by person."""
    payload = {
        "id": message.pk,
        "sender_role": message.sender_role,
        "sender": str(message.display_sender),
        "mine": (
            message.sender_role == ActorRole.CLIENT
            and message.sender_id == client.pk
        ),
        "body": message.body,
        "created_at": _iso(message.created_at),
        "attachment": None,
    }
    if message.attachment_id and message.attachment:
        payload["attachment"] = attachment_payload(message.attachment, client)
    return payload


def attachment_payload(attachment, client) -> dict:
    """A file, reachable only through a signed time-limited URL (spec §11)."""
    content_type = attachment_urls.content_type_of(attachment)
    return {
        "id": attachment.pk,
        "name": attachment.original_name or "",
        "content_type": content_type,
        "is_image": content_type in attachment_urls.INLINE_TYPES,
        "size_bytes": attachment.size_bytes,
        "uploaded_by": str(attachment.get_uploaded_by_role_display()),
        "uploaded_at": _iso(attachment.uploaded_at),
        "url": attachment_urls.url_for(attachment, client),
    }


def summary_payload(deposit: Request) -> dict:
    """Enough for a row in the client's own history, and nothing more."""
    return {
        "reference": deposit.public_ref,
        "type": deposit.type,
        "type_label": str(deposit.get_type_display()),
        "status": deposit.status,
        "status_label": str(deposit.get_status_display()),
        "is_closed": deposit.is_closed,
        "amount_usd": f"{deposit.amount_usd:.2f}",
        "amount_iqd": f"{deposit.amount_iqd:.2f}",
        "method": deposit.payment_method.caption_ar or deposit.payment_method.code,
        "submitted_at": _iso(deposit.submitted_at),
    }


def converted_iqd(deposit: Request):
    """The conversion before the commission: ``amount_usd × rate_applied``.

    Computed from its own definition rather than reconstructed by undoing the
    commission. Subtraction was how this worked, and it had two faults that the
    rounding rule turned from latent into real: it needed the sign of the
    direction, which is a rule a second caller can get wrong (and one did), and
    it silently absorbed anything else inside ``amount_iqd`` — which since the
    transfer rounding is up to 500 dinars of it.

    Multiplication needs neither. It is what pricing computed, so it is what
    comes back, in both directions and whatever else the total is carrying.
    """
    return (deposit.amount_usd * deposit.rate_applied).quantize(
        pricing.DINAR, rounding=ROUND_HALF_UP
    )


def rounding_iqd(deposit: Request):
    """What the company put into, or took out of, the total to round it.

    Not stored: it is exactly what ``amount_iqd`` has that the conversion and
    the commission do not account for, so it is recovered rather than carried in
    a column of its own. Requests filed before the rounding rule come back as
    zero, which is the truth about them.
    """
    metered = converted_iqd(deposit)
    if deposit.type == RequestType.WITHDRAWAL:
        metered -= deposit.commission_applied
    else:
        metered += deposit.commission_applied
    return deposit.amount_iqd - metered


def detail_payload(deposit: Request, client) -> dict:
    """Screen 6 in full: the figures, the timeline, the thread, the files."""
    # The rule is one rule, shared with the merchant panel and the Finance
    # queue (build-order step 9). Internal notes are filtered there, not here.
    visible = messaging.visible_messages(deposit, audience=messaging.CLIENT)
    return {
        **summary_payload(deposit),
        "merchant": deposit.merchant_selected.name,
        "wallet_number": deposit.wallet_number_snapshot,
        "destination_account": deposit.destination_account,
        "rate_applied": f"{deposit.rate_applied:.2f}",
        "commission_applied": f"{deposit.commission_applied:.2f}",
        # amount_iqd already has the commission in it — added on a deposit,
        # deducted on a withdrawal — and the rounding on top of that. Every part
        # travels, so the screen can show the same breakdown the client agreed
        # to and have it reconcile to the figure in their own bank app.
        "converted_iqd": f"{converted_iqd(deposit):.2f}",
        "rounding_iqd": f"{rounding_iqd(deposit):.2f}",
        "rejection_reason": deposit.rejection_reason,
        "timeline": timeline(deposit),
        "messages": [message_payload(m, client) for m in visible],
        "attachments": [
            attachment_payload(a, client) for a in deposit.attachments.all()
        ],
    }
