"""A signed, time-limited link to an attachment, minted for one merchant user.

The third of three holders, and the salt is different again for the same reason
it differs between :mod:`apps.portal.attachments` and
:mod:`apps.finance.attachments`: a token minted on one surface must be inert on
the others, even though all three name the same stored file.

The signature is not what authorises the read — the view checks the session, the
merchant role, ``transactions.view_attachment``, and that the file hangs off a
request actually routed to this merchant. What the signature adds is that the
*link* dies: a proof-of-payment URL pasted into a chat is useless within minutes
and useless to anyone but the merchant it was minted for.
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

SALT = "merchant.attachment"


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=SALT)


def max_age() -> int:
    return int(getattr(settings, "MERCHANT_ATTACHMENT_URL_MAX_AGE", 600))


def sign(attachment, user) -> str:
    """A token good for ``max_age()`` seconds, for this file and this user."""
    return _signer().sign(f"{attachment.pk}:{user.pk}")


def unsign(token: str, user) -> int:
    """The attachment id a valid token names, or raise :class:`Http404`.

    Tampered, expired, or minted for somebody else all collapse to the same
    404, so a prober is never told which part they got right.
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
        "merchant_panel:attachment",
        kwargs={"pk": attachment.pk, "token": sign(attachment, user)},
    )
