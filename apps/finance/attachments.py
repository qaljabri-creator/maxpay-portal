"""A signed, time-limited link to an attachment, minted for one Finance user.

Finance is already authenticated by a Django session with a verified second
factor, so the signature here is not what proves who is asking — the view checks
the session, the Finance role and ``transactions.view_attachment`` on every hit.
What the signature adds is that a *link* stops working: a proof-of-payment URL
pasted into a chat, a ticket or a browser history is inert within minutes and
inert for anyone but the person it was minted for.

Deliberately a separate salt and a separate holder from
:mod:`apps.portal.attachments`. A client's token must not open a Finance URL and
a Finance token must not open a client's, even though both name the same file.
"""

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.http import Http404
from django.urls import reverse

from apps.core.attachments import INLINE_TYPES, content_type_of, serve

__all__ = [
    "INLINE_TYPES",
    "content_type_of",
    "max_age",
    "serve",
    "sign",
    "unsign",
    "url_for",
]

SALT = "finance.attachment"


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=SALT)


def max_age() -> int:
    return int(getattr(settings, "FINANCE_ATTACHMENT_URL_MAX_AGE", 600))


def sign(attachment, user) -> str:
    """A token good for ``max_age()`` seconds, for this file and this user."""
    return _signer().sign(f"{attachment.pk}:{user.pk}")


def unsign(token: str, user) -> int:
    """The attachment id a valid token names, or raise :class:`Http404`.

    Every failure — tampered, expired, minted for someone else — collapses to
    the same 404, so a prober is never told which part they got right.
    """
    try:
        value = _signer().unsign(token, max_age=max_age())
    except (BadSignature, SignatureExpired) as exc:
        raise Http404("Invalid attachment token.") from exc

    attachment_id, _, user_id = value.partition(":")
    if not attachment_id.isdigit() or user_id != str(user.pk):
        raise Http404("Attachment token does not belong to this user.")
    return int(attachment_id)


def url_for(attachment, user) -> str:
    return reverse(
        "finance:attachment",
        kwargs={"pk": attachment.pk, "token": sign(attachment, user)},
    )
