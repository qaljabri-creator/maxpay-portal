"""Turning a B2CORE embed session into the merchant panel's ``request.user``.

The panel was built for an internal password session and every line of it reads
``request.user``: the scoping refuses anyone who is not a merchant, the
serializers mint attachment signatures per person, and every lifecycle move
names the user who made it. Nothing about that changes because B2CORE is now the
thing that authenticated the person. What changes is only *where the answer
comes from*, so this middleware answers it and leaves the panel alone.

Three jobs, and only ever under ``MERCHANT_URL_PREFIX``:

1. **Identity.** A live embed session resolves to a merchant, and
   ``request.user`` becomes that merchant's login account for the rest of the
   request. ``request.session`` — the internal cookie — is not read, not
   written and not consulted, so a Finance session open in the same browser
   neither grants nor blocks anything here.

2. **Standing down the two internal gates.** ``EnforceTwoFactorMiddleware`` and
   ``ForcePasswordChangeMiddleware`` both exist to make a *password* session
   safe: a second factor in front of a password, and a forced change of a
   one-time password an administrator read off a screen. An embed session has
   no password in it at all — B2CORE authenticated the person, under whatever
   policy B2CORE enforces — so there is nothing for either gate to add and
   plenty for them to break: both redirect, and a redirect to the two-factor
   wizard inside a frame whose ``frame-ancestors`` excludes it is a blank
   rectangle. They skip a request this middleware has flagged.

3. **CSRF, which Django cannot do for us here.** ``CsrfViewMiddleware`` reads
   its secret from a cookie that is ``SameSite=Lax`` and therefore never
   arrives inside the frame — every post would be rejected, and relaxing that
   cookie would relax it for the Finance panel too, which is the one thing
   these three cookies exist to prevent. So the same two guards the client
   portal carries are applied here instead: an acceptable ``Origin``, and the
   per-session token issued when the session was created, echoed back in the
   form or in a header. A cross-site page can make a browser send the cookie;
   it cannot read the response that carried the token.

Order in ``MIDDLEWARE`` is load-bearing. This must sit **after**
``AuthenticationMiddleware`` and ``OTPMiddleware`` — otherwise the user it
installs is overwritten by the one the internal cookie names — and **before**
``EnforceTwoFactorMiddleware``, which is what it stands down.
"""

import logging
import secrets

from django.conf import settings
from django.http import JsonResponse
from django.utils.translation import gettext as _

from apps.core.embed import origin_is_acceptable

from . import session as embed_session

logger = logging.getLogger("maxpay.b2core")

#: The per-session token, for callers that post JSON rather than a form.
CSRF_HEADER = "HTTP_X_MERCHANT_CSRF"

#: …and the form field, which is the one that actually carries it: the panel's
#: screens are server-rendered forms. The name is Django's own so
#: ``{% csrf_token %}`` keeps working unchanged — what the tag *renders* is
#: swapped for the embed token by :func:`embed_csrf_token`.
CSRF_FIELD = "csrfmiddlewaretoken"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def prefix() -> str:
    return getattr(settings, "MERCHANT_URL_PREFIX", "/merchant/")


def is_embed(request) -> bool:
    """Whether this request is being served under a B2CORE embed session."""
    return getattr(request, "merchant_embed", None) is not None


def embed_csrf_token(request) -> dict:
    """Context processor: inside the frame, ``{% csrf_token %}`` renders ours.

    Registered after Django's own ``csrf`` processor, so it wins where it
    applies and is silent everywhere else — a Finance page, or a merchant page
    served over an ordinary password session, still renders Django's token and
    is still checked by Django's middleware.
    """
    token = embed_session.csrf_token(request) if is_embed(request) else ""
    return {"csrf_token": token} if token else {}


class MerchantEmbedAuthMiddleware:
    """Authenticates merchant-panel requests from the embed session."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.merchant_embed = None

        if not request.path.startswith(prefix()):
            return self.get_response(request)

        merchant = embed_session.get_merchant(request)
        if merchant is None:
            # No embed session. The panel falls back to whatever the internal
            # cookie says, which is the emergency password path — and, when
            # MERCHANT_PASSWORD_LOGIN is off, says nothing at all.
            return self.get_response(request)

        request.merchant_embed = merchant
        request.user = merchant.user

        if request.method not in SAFE_METHODS:
            refusal = self._refuse_unsafe(request)
            if refusal is not None:
                return refusal
            # Django's own check would reject this request for want of a cookie
            # that cannot reach us. The two guards above have already been
            # applied in its place.
            request.csrf_processing_done = True

        response = self.get_response(request)

        # A page rendered in the frame must never be cached: it is one
        # merchant's worklist, served from a cookie a shared browser may swap.
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    # -- the two guards ----------------------------------------------------

    def _refuse_unsafe(self, request):
        if not origin_is_acceptable(request):
            logger.info(
                "Merchant embed request refused: origin=%r path=%s",
                request.headers.get("Origin", ""),
                request.path,
            )
            return self._error(
                "forbidden_origin",
                _("طلب من مصدر غير مسموح به."),
            )

        expected = embed_session.csrf_token(request)
        supplied = request.META.get(CSRF_HEADER, "") or request.POST.get(CSRF_FIELD, "")
        if not (expected and supplied and secrets.compare_digest(supplied, expected)):
            logger.info("Merchant embed request refused: bad session token.")
            return self._error(
                "invalid_csrf",
                _("انتهت صلاحية الجلسة. أعد تحميل الصفحة."),
            )
        return None

    @staticmethod
    def _error(code: str, message):
        response = JsonResponse(
            {"error": code, "detail": message, "remedy": "reauthenticate"},
            status=403,
        )
        response["Cache-Control"] = "no-store"
        return response
