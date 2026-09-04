"""Streaming a stored file to whoever has already been allowed to see it.

``MEDIA_ROOT`` is never mapped to a URL prefix, so a stored path is not a link.
Reaching a file always goes through a view that has authorised the caller in its
own terms — a client's portal session (:mod:`apps.portal.attachments`) or a
Finance user's permission (:mod:`apps.finance.attachments`). What is shared
between those two surfaces, and lives here, is the part that must be identical
either way: what the file is called, and the headers that stop it acting as a
page inside our own origin (spec §11).
"""

import mimetypes
from pathlib import Path

from django.http import FileResponse, Http404

#: Types safe to render in place. Everything else is sent as a download, so the
#: browser never executes an uploaded file in our own origin.
INLINE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})


def content_type_of(attachment) -> str:
    """The stored type, falling back to the extension. Never client-declared."""
    declared = (attachment.content_type or "").lower()
    if declared:
        return declared
    guessed, _encoding = mimetypes.guess_type(attachment.file.name or "")
    return guessed or "application/octet-stream"


def serve(attachment) -> FileResponse:
    """Stream ``attachment`` with headers that stop it acting as a page."""
    content_type = content_type_of(attachment)
    try:
        handle = attachment.file.open("rb")
    except (FileNotFoundError, OSError) as exc:
        # The row outlived its file — a restore gone wrong, or a manual delete.
        raise Http404("The stored file is missing.") from exc

    filename = Path(attachment.original_name or attachment.file.name).name
    response = FileResponse(
        handle,
        content_type=content_type,
        # An image is shown in place; anything else is downloaded rather than
        # rendered, so an uploaded file never runs inside our origin.
        as_attachment=content_type not in INLINE_TYPES,
        filename=filename,
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    # The file is data, not a document: nothing it contains may load or run,
    # and nobody may frame it. SecurityHeadersMiddleware leaves this alone
    # precisely because it is stricter than the page policy it would otherwise
    # apply.
    response["Content-Security-Policy"] = (
        "default-src 'none'; sandbox; frame-ancestors 'none'"
    )
    response["Referrer-Policy"] = "no-referrer"
    return response
