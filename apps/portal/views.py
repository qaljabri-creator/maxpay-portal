"""The embedded portal's session surface (spec §4).

Three things live here:

* :class:`BootstrapView` — the page B2CORE frames. It renders no client data;
  all it does is run the ``postMessage`` handshake and, once that yields a
  token, hand it to the session endpoint.
* :class:`SessionView` — exchange a verified B2CORE JWT for a portal session,
  report the current one, or end it.
* :class:`PreferencesView` — theme and language, which B2CORE announces over
  ``embed-theme-change`` / ``embed-language-change``.

Two things are unusual about the request handling, and both follow from living
in a third-party iframe:

**Django's CSRF machinery cannot work here.** Its cookie is ``SameSite=Lax`` and
so never arrives inside the frame. These views are therefore ``csrf_exempt`` and
carry their own two guards instead: every unsafe request must present an
acceptable ``Origin``, and every unsafe request against an *existing* session
must echo the per-session token issued when that session was created. A
cross-site page can make the browser send the cookie; it cannot read the
response that carried the token.

**Creating a session is not CSRF-relevant.** It requires a valid B2CORE JWT in
the body, which an attacker cannot obtain, and its whole effect is to log the
caller in as whoever that token names.
"""

import logging
import secrets

from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView

from apps.core import embed
from apps.core import hours as business_hours

from . import ratelimit, session
from .b2core import (
    B2CoreConfigurationError,
    B2CoreKeyError,
    B2CoreTokenError,
    verify_token,
)

logger = logging.getLogger("maxpay.b2core")

#: The per-session token comes back in this header, not in a cookie.
CSRF_HEADER = "HTTP_X_PORTAL_CSRF"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Both live in apps.core.embed: the merchant panel is framed by B2CORE too now,
# and "which origins may post here" must have one answer for both surfaces.
allowed_origins = embed.allowed_origins
origin_is_acceptable = embed.origin_is_acceptable


# The same four helpers the merchant embed uses; see apps.core.embed.
error = embed.error
read_json = embed.read_json
MAX_BODY_BYTES = embed.MAX_BODY_BYTES


def session_payload(request, client) -> dict:
    """What the embed is told about its own session.

    The identity in here is the client's *own*, returned to the browser that
    just proved it. Nothing from this shape reaches a merchant — spec §2 governs
    merchant-scoped surfaces, which this is not.
    """
    return {
        "authenticated": True,
        "client": {
            "reference": client.b2core_id,
            "display_name": client.display_name,
            "email": client.email,
            "account_number": client.account_number,
        },
        "expires_at": session.expires_at(request),
        "csrf_token": session.csrf_token(request),
        "theme": session.get_theme(request),
        "language": session.get_language(request),
    }


class PortalEndpoint(View):
    """Shared guards for the JSON endpoints: no caching, origin, session token."""

    #: Unsafe methods that must echo the per-session token. Session creation is
    #: absent on purpose — it has no session to carry a token from yet.
    token_protected_methods = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    #: Set by an endpoint that only means anything to a signed-in client. When
    #: true, ``self.client`` is resolved for the handler and a request without a
    #: session is turned away before it reaches one. The check runs *after* the
    #: origin and token guards, so a cross-site caller learns nothing about
    #: whether a session exists.
    requires_client = False

    @method_decorator(csrf_exempt)
    @method_decorator(never_cache)
    def dispatch(self, request, *args, **kwargs):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not origin_is_acceptable(request):
                logger.info(
                    "Portal request refused: origin=%r path=%s",
                    request.headers.get("Origin", ""),
                    request.path,
                )
                return error(
                    "forbidden_origin",
                    _("طلب من مصدر غير مسموح به."),
                    status=403,
                    remedy="contact_support",
                )
            if request.method in self.token_protected_methods and not self._token_ok(request):
                return error(
                    "invalid_csrf",
                    _("رمز الجلسة غير صالح."),
                    status=403,
                    remedy="reauthenticate",
                )

        if self.requires_client:
            self.client = session.current_client(request)
            if self.client is None:
                return error(
                    "no_session",
                    _("انتهت الجلسة. أعد الاتصال بحسابك."),
                    status=401,
                    remedy="reauthenticate",
                )

        response = super().dispatch(request, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _token_ok(request) -> bool:
        expected = session.csrf_token(request)
        if not expected:
            # No established session, so nothing to forge a request against.
            # The view itself decides what an absent session means.
            return True
        supplied = request.META.get(CSRF_HEADER, "")
        return bool(supplied) and secrets.compare_digest(supplied, expected)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class SessionView(PortalEndpoint):
    """``GET`` the session, ``POST`` a token to create one, ``DELETE`` to end it."""

    #: Creating a session authenticates the caller from scratch; there is no
    #: prior session whose token could be echoed back.
    token_protected_methods = frozenset({"DELETE"})

    def get(self, request, *args, **kwargs):
        client = session.current_client(request)
        if client is None:
            return JsonResponse(
                {
                    "authenticated": False,
                    "reason": "no_session",
                    "remedy": "reauthenticate",
                }
            )
        return JsonResponse(session_payload(request, client))

    def post(self, request, *args, **kwargs):
        if not ratelimit.allow(request, scope="session"):
            return error(
                "rate_limited",
                _("محاولات كثيرة. أعد المحاولة بعد قليل."),
                status=429,
            )

        try:
            body = read_json(request)
        except ValueError as exc:
            return error("invalid_request", str(exc), status=400, remedy="contact_support")

        token = body.get("token")
        if not isinstance(token, str) or not token.strip():
            return error(
                "missing_token",
                _("لم يصل رمز B2CORE."),
                status=400,
                remedy="reauthenticate",
            )

        try:
            identity = verify_token(token)
        except B2CoreTokenError as exc:
            # The reason stays in the log. The browser is told only to go back
            # to B2CORE for a fresh token — anything more precise is a probe.
            logger.info("Portal session refused: %s", exc)
            return error(
                "invalid_token",
                _("تعذّر التحقق من هوية العميل."),
                status=401,
                remedy="reauthenticate",
            )
        except B2CoreKeyError as exc:
            logger.warning("Portal session unavailable: %s", exc)
            return error(
                "key_unavailable",
                _("تعذّر التحقق من الرمز حاليًا. أعد المحاولة."),
                status=503,
                remedy="retry",
            )
        except B2CoreConfigurationError as exc:
            logger.error("Portal session misconfigured: %s", exc)
            return error(
                "not_configured",
                _("التكامل مع B2CORE غير مهيّأ. تواصل مع الدعم."),
                status=503,
                remedy="contact_support",
            )

        client = session.upsert_client(identity)
        if not client.is_active:
            # Deactivating a client has to survive a fresh, entirely valid
            # token, so the check sits after verification and before the session.
            session.end(request)
            logger.warning("Refused a session for deactivated client %s", client.pk)
            return error(
                "client_disabled",
                _("هذا الحساب غير مفعّل. تواصل مع الدعم."),
                status=403,
                remedy="contact_support",
            )

        session.start(request, identity, client=client)
        logger.info("Portal session started for client %s", client.pk)
        return JsonResponse(session_payload(request, client), status=201)

    def delete(self, request, *args, **kwargs):
        """Spec §4, step 5 — ``embed-logout`` clears the session."""
        client = session.current_client(request)
        session.end(request)
        if client is not None:
            logger.info("Portal session ended for client %s", client.pk)
        return JsonResponse({"authenticated": False, "reason": "logged_out"})


class PreferencesView(PortalEndpoint):
    """Spec §4, step 6 — theme and language follow B2CORE."""

    def post(self, request, *args, **kwargs):
        try:
            body = read_json(request)
        except ValueError as exc:
            return error("invalid_request", str(exc), status=400, remedy="contact_support")

        applied: dict[str, str] = {}
        rejected: list[str] = []

        if "theme" in body:
            theme = session.set_theme(request, body.get("theme"))
            if theme:
                applied["theme"] = theme
            else:
                rejected.append("theme")

        if "language" in body:
            language = session.set_language(request, body.get("language"))
            if language:
                applied["language"] = language
            else:
                rejected.append("language")

        if not applied and rejected:
            return error(
                "unsupported_preference",
                _("قيمة غير مدعومة."),
                status=400,
                remedy="ignore",
                rejected=rejected,
            )

        return JsonResponse(
            {
                "applied": applied,
                "rejected": rejected,
                "theme": session.get_theme(request),
                "language": session.get_language(request),
            }
        )


@method_decorator(never_cache, name="dispatch")
class BootstrapView(TemplateView):
    """The page inside the iframe. Renders no client data of its own.

    Everything it needs to run the handshake is passed as one JSON island, so
    the page carries no inline script and the CSP can stay ``default-src 'self'``.
    """

    template_name = "portal/bootstrap.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        origin = getattr(settings, "B2CORE_ORIGIN", "")
        context["embed_config"] = {
            "parentOrigin": origin,
            "sessionUrl": reverse("portal:session"),
            "preferencesUrl": reverse("portal:preferences"),
            "allowStandalone": bool(getattr(settings, "PORTAL_ALLOW_STANDALONE", False)),
            # Ask B2CORE for a replacement this long before the current token
            # expires, rather than after the session has already died.
            "renewMarginSeconds": 90,
            "tokenTimeoutMs": 15000,
        }
        context["flow_config"] = self.flow_config()
        context["configured"] = bool(origin and getattr(settings, "B2CORE_JWKS_URL", ""))
        context["theme"] = session.get_theme(self.request)
        context["language"] = session.get_language(self.request)
        context["authenticated"] = session.current_client(self.request) is not None
        return context

    @staticmethod
    def flow_config() -> dict:
        """What the client flow needs to know, as one JSON island (step 6).

        The upload limits are sent rather than hard-coded in the script so the
        browser refuses an oversized file with the same number the server
        would, instead of after a pointless upload.
        """
        # A reference-shaped placeholder swapped for the real one client-side.
        # Reversing with a sample keeps the route's shape in one place — here it
        # would otherwise be a second, silently drifting copy of the URLconf.
        detail = reverse("portal:request_detail", kwargs={"reference": "MP-00000"})
        thread = reverse("portal:request_messages", kwargs={"reference": "MP-00000"})
        return {
            "optionsUrl": reverse("portal:options"),
            "requestsUrl": reverse("portal:requests"),
            "requestUrlTemplate": detail.replace("MP-00000", "{reference}"),
            "messagesUrlTemplate": thread.replace("MP-00000", "{reference}"),
            "maxUploadBytes": settings.MAX_UPLOAD_SIZE_BYTES,
            "acceptedTypes": list(settings.ALLOWED_UPLOAD_CONTENT_TYPES),
            "acceptedExtensions": list(settings.ALLOWED_UPLOAD_EXTENSIONS),
            "messageMaxChars": getattr(settings, "PORTAL_MESSAGE_MAX_CHARS", 1000),
            # The request view polls, the same ten seconds the two panels use
            # (spec §10). Sent rather than hard-coded so one setting governs
            # every surface's idea of "live".
            "pollMs": int(getattr(settings, "PANEL_POLL_SECONDS", 10)) * 1000,
            # Build-order step 11. Sent with the page rather than waited for:
            # spec §7 says the submission screens are *replaced* outside
            # business hours, and a screen that appears for half a second
            # before the first catalogue call answers has not been replaced.
            # The options payload carries the same block and supersedes this
            # one on every call after the first.
            "hours": business_hours.evaluate().payload(),
            # Step 10. Sent rather than hard-coded for the same reason as the
            # upload limits: the number the screen refuses on has to be the
            # number the server refuses on.
            "destinationDigits": {
                "min": getattr(settings, "PORTAL_DESTINATION_MIN_DIGITS", 6),
                "max": getattr(settings, "PORTAL_DESTINATION_MAX_DIGITS", 32),
            },
        }
