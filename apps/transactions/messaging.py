"""The message thread on a request (spec §5, §9) — build-order step 9.

The conversation is an ordinary one. A client writes to the merchant whenever
they want, the merchant writes back whenever they want, and Finance reads
everything and writes into the thread as a third participant. Nothing here is
gated on the request's status: a question after a request closed is still a
question, and refusing it would only move the conversation to some channel
nobody can audit.

This module is to ``Message`` what :mod:`apps.transactions.services` is to
``Request.status`` — the only place that writes one. That matters because
posting a message is never *only* inserting a row: it may carry a file, which
has to be validated the way every other upload is (spec §11) and stored in the
same transaction, or neither should exist.

The second thing living here is :func:`visible_messages`, the rule about who
may read what. It is here rather than in each surface's serializer because
three surfaces ask the same question and three copies of an answer is three
chances to get it wrong. What it enforces:

``finance``
    Everything, internal notes included. Spec §9 gives Finance full read access
    to every thread; the notes are theirs to begin with.
``client``
    Everything except internal notes. Spec §5 labels each sender by role, so a
    client reads "التاجر" and never which merchant, and "المالية" and never
    which member of it.
``merchant``
    Everything except internal notes, and except messages written by a
    *different* merchant. A re-routed request (spec §5) carries whatever the
    previous merchant wrote, and handing that to their replacement is a leak
    between two third parties even when no client is named in it.

    One exception, added by the Finance review of 24 Aug 2026: a merchant reads
    back the **handback note they wrote themselves**. Finance's own notes are
    still invisible to every merchant without exception, and so is another
    merchant's handback note. The two kinds are told apart by who wrote them —
    see :data:`NOTE_ROLES`.

Note the shape of the rule: it is a whitelist of what each audience may see,
not a blacklist of what to hide. A sender role added to
:class:`~apps.core.choices.ActorRole` later is invisible to everyone until
somebody decides otherwise.

**Free text is not, and cannot be, masked.** Spec §2 keeps client identity out
of every *field* a merchant receives, and
:mod:`apps.merchant_panel.anonymity` enforces exactly that. A message body is
prose a merchant is meant to read, so no filter can run over it without
breaking the feature. What stands in its place is a reminder on Finance's reply
box, and it is the reason that reminder exists.
"""

import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.choices import ActorRole
from apps.core.validators import (
    validate_real_content_type,
    validate_upload_content,
    validate_upload_size,
)

from .models import Attachment, Message

logger = logging.getLogger("maxpay.transactions")

#: Audiences a thread is read by. Not roles: "finance" is two roles, and a
#: client is not a Django user at all.
CLIENT = "client"
MERCHANT = "merchant"
FINANCE = "finance"

#: Senders whose messages leave the desk at all. Deliberately a whitelist.
DISCLOSABLE_SENDERS = frozenset({
    ActorRole.CLIENT,
    ActorRole.FINANCE_ADMIN,
    ActorRole.FINANCE_STAFF,
    ActorRole.MERCHANT,
    ActorRole.SYSTEM,
})

#: Roles that may mark a message as an internal note.
#:
#: There are **two kinds**, told apart by who wrote them rather than by a second
#: field, and they are not variations of one rule:
#:
#: * a **Finance note** is Finance's private record of why. It never leaves the
#:   desk — no merchant sees one, not even on a request they hold, and they are
#:   not told one exists.
#: * a **merchant's handback note** is the reason a merchant gave for sending a
#:   request back (Finance review, 24 Aug 2026). Finance reads it, and so does
#:   its author, because a mandatory field whose content vanishes from the
#:   person who wrote it is a field nobody trusts. No other merchant sees it,
#:   and no client sees either kind.
#:
#: ``SYSTEM`` is listed because a lifecycle transition driven by an account with
#: no business role still has to be able to record one (see
#: :func:`apps.transactions.services.actor_role_of`).
NOTE_ROLES = frozenset({
    ActorRole.FINANCE_ADMIN,
    ActorRole.FINANCE_STAFF,
    ActorRole.MERCHANT,
    ActorRole.SYSTEM,
})


class MessageError(Exception):
    """A refusal with a message fit to show whoever tried to post it."""

    def __init__(self, code: str, message, *, status: int = 400):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status = status


def max_body_chars() -> int:
    """How long one message may be.

    Shares ``PORTAL_MESSAGE_MAX_CHARS`` with the note a client attaches at
    submission, because they are the same thing written at different moments and
    two settings for one limit is one setting too many.
    """
    return int(getattr(settings, "PORTAL_MESSAGE_MAX_CHARS", 1000))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Draft:
    """A message that has passed every check and is ready to be written."""

    body: str
    upload: object = None
    upload_content_type: str = ""
    is_internal_note: bool = False


def build_draft(*, sender_role: str, body, upload=None, is_internal_note: bool = False) -> Draft:
    """Validate what somebody typed and attached, without writing anything.

    Separated from :func:`post_message` so a surface can reject bad input before
    it opens a transaction, and so the rules can be tested without a database.
    """
    body = (body or "").strip()
    limit = max_body_chars()
    if len(body) > limit:
        raise MessageError(
            "message_too_long",
            _("الرسالة أطول من %(limit)s حرف.") % {"limit": limit},
        )

    if upload is not None:
        upload_type = _validate_upload(upload)
    else:
        upload_type = ""

    if not body and upload is None:
        # An empty message is not a message. Said plainly, because a send button
        # that appears to do nothing is worse than a refusal.
        raise MessageError(
            "message_empty", _("اكتب رسالة أو أرفق ملفًا.")
        )

    if is_internal_note and sender_role not in NOTE_ROLES:
        raise MessageError(
            "note_not_allowed",
            _("الملاحظات الداخلية للمالية والتجار فقط."),
            status=403,
        )

    return Draft(
        body=body,
        upload=upload,
        upload_content_type=upload_type,
        is_internal_note=is_internal_note,
    )


def _validate_upload(upload) -> str:
    """The same three gates every other upload passes (spec §11).

    Cheapest first: refuse an oversized file before reading any of it, a
    forbidden extension before trusting anything about the name, and only then
    read the bytes — which is what actually decides what the file is.
    """
    try:
        validate_upload_size(upload)
        validate_upload_content(upload)
        return validate_real_content_type(upload)
    except ValidationError as exc:
        raise MessageError("attachment_invalid", " ".join(exc.messages)) from exc


@transaction.atomic
def post_message(
    request_obj,
    draft: Draft,
    *,
    sender_role: str,
    sender_id=None,
) -> Message:
    """Write ``draft`` into ``request_obj``'s thread, file and all.

    Atomic on purpose: a message whose attachment failed to store would point
    at nothing, and an attachment with no message would sit on the request with
    nobody able to say who sent it or why.
    """
    attachment = None
    if draft.upload is not None:
        attachment = Attachment(
            request=request_obj,
            file=draft.upload,
            content_type=draft.upload_content_type,
            uploaded_by_role=sender_role,
            uploaded_by_id=sender_id,
        )
        # original_name and size_bytes are filled in by the model's own save().
        attachment.full_clean(exclude=["request", "original_name", "size_bytes"])
        attachment.save()

    message = Message(
        request=request_obj,
        sender_role=sender_role,
        sender_id=sender_id,
        body=draft.body,
        attachment=attachment,
        is_internal_note=draft.is_internal_note,
    )
    message.full_clean(exclude=["request", "attachment"])
    message.save()

    logger.info(
        "Message %s posted on %s by %s#%s%s",
        message.pk,
        request_obj.public_ref,
        sender_role,
        sender_id,
        " (internal note)" if draft.is_internal_note else "",
    )
    return message


def post(
    request_obj,
    *,
    sender_role: str,
    sender_id=None,
    body="",
    upload=None,
    is_internal_note: bool = False,
) -> Message:
    """Validate and write in one call — what every surface actually wants."""
    draft = build_draft(
        sender_role=sender_role,
        body=body,
        upload=upload,
        is_internal_note=is_internal_note,
    )
    return post_message(
        request_obj, draft, sender_role=sender_role, sender_id=sender_id
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def is_visible_to(message: Message, audience: str, viewer_id=None) -> bool:
    """Whether one message may be shown to one audience. See the module docstring."""
    if audience == FINANCE:
        return True
    if message.is_internal_note:
        # Two kinds, and only one of them is ever readable outside Finance.
        # A Finance note stops here for everybody. A merchant's handback note
        # is readable by the merchant who wrote it and by nobody else — not the
        # client, and not the merchant a returned request is re-routed to.
        return (
            audience == MERCHANT
            and message.sender_role == ActorRole.MERCHANT
            and viewer_id is not None
            and message.sender_id == viewer_id
        )
    if message.sender_role not in DISCLOSABLE_SENDERS:
        return False
    if audience == MERCHANT and message.sender_role == ActorRole.MERCHANT:
        # Their own words, or a predecessor's. Only the first travels.
        return viewer_id is not None and message.sender_id == viewer_id
    return True


def visible_messages(request_obj, *, audience: str, viewer_id=None) -> list[Message]:
    """The thread as ``audience`` may read it, oldest first.

    Filtered in Python rather than in SQL because the caller has almost always
    prefetched ``messages__attachment`` already, and a second query to re-fetch
    a list it is holding would cost more than the comprehension.
    """
    return [
        message
        for message in request_obj.messages.all()
        if is_visible_to(message, audience, viewer_id)
    ]
