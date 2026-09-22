"""Who is allowed to make an unsafe request against a framed surface.

Both embedded surfaces — the client portal and, since B2CORE grew a merchant
menu item, the merchant panel — are in the same position: Django's CSRF cookie
is ``SameSite=Lax`` and never reaches them, so the ``Origin`` header does the
work that cookie normally would. There is exactly one list of acceptable
origins, and it lives here so the two surfaces cannot answer the question
differently.
"""

import json
from urllib.parse import urlsplit

from django.conf import settings
from django.http import JsonResponse

#: Bounds the JSON an embedded surface is willing to parse from an
#: unauthenticated caller.
MAX_BODY_BYTES = 16 * 1024


def own_origin(request) -> str:
    return f"{request.scheme}://{request.get_host()}"


def allowed_origins(request) -> set[str]:
    """Origins permitted to make an unsafe request against an embedded surface.

    Our own, because the embed page is served from here and its ``fetch`` and
    its form posts are same-origin; and B2CORE's, because the protocol allows
    it to call directly.
    """
    origins = {own_origin(request)}
    configured = getattr(settings, "B2CORE_ORIGIN", "")
    if configured:
        origins.add(configured.rstrip("/"))
    return origins


def origin_is_acceptable(request) -> bool:
    """Whether this unsafe request came from somewhere we accept.

    A missing ``Origin`` is a rejection, not a pass: browsers send it on every
    request that matters here, so its absence means the caller is not one.
    """
    origin = request.headers.get("Origin", "")
    if not origin:
        # Referer is a weaker signal and the only fallback that exists; it is
        # consulted solely because some embedded webviews omit Origin.
        referer = request.headers.get("Referer", "")
        if not referer:
            return False
        parts = urlsplit(referer)
        if not parts.scheme or not parts.netloc:
            return False
        origin = f"{parts.scheme}://{parts.netloc}"
    return origin.rstrip("/") in allowed_origins(request)


def error(code: str, message: str, *, status: int, remedy: str = "retry", **extra):
    """One shape for every failure, so an embed can branch on ``code`` alone."""
    payload = {"error": code, "detail": message, "remedy": remedy, **extra}
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store"
    return response


def read_json(request) -> dict:
    """Parse a JSON body, raising :class:`ValueError` on anything unusable."""
    if len(request.body) > MAX_BODY_BYTES:
        raise ValueError("Request body is too large.")
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Request body is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    return data
