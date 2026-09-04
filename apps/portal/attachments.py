"""A signed, time-limited link to a file, minted for one client (spec §11).

``MEDIA_ROOT`` is never mapped to a URL prefix, so a stored path is not a link
and guessing one gets nobody anything. A file is reachable only through a token
minted here, and a token is only accepted alongside three other things:

* a live portal session — the signature *authenticates the link*, not the
  caller, and a link that leaked out of the frame must not stand in for a login;
* the client the token was minted for, so one client's link is inert in another
  client's session;
* ownership of the request the attachment hangs off, checked against the
  database rather than trusted from the token.

Any one of those alone would be weaker than the four together, which is why all
four are required rather than whichever is most convenient.

Streaming the bytes is not client-specific and lives in
:mod:`apps.core.attachments`; ``serve``, ``content_type_of`` and
``INLINE_TYPES`` are re-exported here so callers keep one import.
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

#: Namespaced so a token minted here can never be replayed against another
#: signer in the project.
SALT = "portal.attachment"


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=SALT)


def max_age() -> int:
    return int(getattr(settings, "PORTAL_ATTACHMENT_URL_MAX_AGE", 300))


def sign(attachment, client) -> str:
    """A token good for ``max_age()`` seconds, for this file and this client."""
    return _signer().sign(f"{attachment.pk}:{client.pk}")


def unsign(token: str, client) -> int:
    """The attachment id a valid token names, or raise :class:`Http404`.

    Every failure — tampered, expired, minted for someone else — collapses to
    the same 404. Distinguishing them would tell a prober which part they got
    right.
    """
    try:
        value = _signer().unsign(token, max_age=max_age())
    except (BadSignature, SignatureExpired) as exc:
        raise Http404("Invalid attachment token.") from exc

    attachment_id, _, client_id = value.partition(":")
    if not attachment_id.isdigit() or client_id != str(client.pk):
        raise Http404("Attachment token does not belong to this session.")
    return int(attachment_id)


def url_for(attachment, client) -> str:
    return reverse(
        "portal:attachment",
        kwargs={"pk": attachment.pk, "token": sign(attachment, client)},
    )
