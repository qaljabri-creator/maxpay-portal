"""The merchant panel's door, when the panel is a menu item inside B2CORE.

Two views, and between them they are the whole of the handshake:

* :class:`MerchantEmbedView` — the page B2CORE frames. It renders no merchant
  data at all; everything visible on it is a state of the handshake, and its one
  successful outcome is a redirect into the panel proper.
* :class:`MerchantSessionView` — exchange a verified B2CORE JWT for a merchant
  embed session, report the current one, or end it.

The protocol is the portal's, unchanged and deliberately so: the frame announces
``embed-iframe-ready``, asks for a token with ``embed-request-jwt-token``, and
B2CORE answers ``embed-jwt-token`` or refuses with ``embed-jwt-token-error``.
The same :func:`~apps.portal.b2core.verify_token` checks the signature against
the same JWKS. There is one B2CORE and one token format; a second implementation
of either would be a second thing to keep true.

What is *not* the portal's is what a verified subject buys. See
:mod:`apps.merchant_panel.session`: the subject must be the ``b2core_id`` of an
active, unarchived merchant with a working login account. The token itself says
nothing about who is a merchant — it carries no client type — so that binding is
the only guard there is, and it is tested as such.
"""

import logging

from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView

from apps.core.embed import error, origin_is_acceptable, read_json
from apps.portal import ratelimit
from apps.portal.b2core import (
    B2CoreConfigurationError,
    B2CoreKeyError,
    B2CoreTokenError,
    verify_token,
)

from . import session as embed_session

logger = logging.getLogger("maxpay.b2core")


def session_payload(request, merchant) -> dict:
    """What the frame is told about its own session.

    The merchant's own name and nothing else. In particular nothing about any
    *request* is in here — spec §2 governs everything this surface says, and the
    door is no exception to it.
    """
    return {
        "authenticated": True,
        "merchant": {"name": merchant.name},
        "expires_at": embed_session.expires_at(request),
        "csrf_token": embed_session.csrf_token(request),
        "panel_url": reverse("merchant_panel:queue"),
    }


@method_decorator(csrf_exempt, name="dispatch")
@method_decorator(never_cache, name="dispatch")
class MerchantSessionView(View):
    """``GET`` the session, ``POST`` a token to create one, ``DELETE`` to end it."""

    def dispatch(self, request, *args, **kwargs):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not origin_is_acceptable(request):
            logger.info(
                "Merchant session request refused: origin=%r",
                request.headers.get("Origin", ""),
            )
            return error(
                "forbidden_origin",
                _("طلب من مصدر غير مسموح به."),
                status=403,
                remedy="contact_support",
            )
        response = super().dispatch(request, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    def get(self, request, *args, **kwargs):
        merchant = embed_session.get_merchant(request)
        if merchant is None:
            return JsonResponse(
                {"authenticated": False, "reason": "no_session", "remedy": "reauthenticate"}
            )
        return JsonResponse(session_payload(request, merchant))

    def post(self, request, *args, **kwargs):
        # Unauthenticated, and it does public-key cryptography plus — for an
        # unknown `kid` — an outbound JWKS fetch. Capped per caller like the
        # portal's twin endpoint, and for the same reason.
        if not ratelimit.allow(
            request,
            scope="merchant-session",
            rate=getattr(settings, "MERCHANT_SESSION_RATE", "30/minute"),
        ):
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
            logger.info("Merchant session refused: %s", exc)
            return error(
                "invalid_token",
                _("تعذّر التحقق من الهوية."),
                status=401,
                remedy="reauthenticate",
            )
        except B2CoreKeyError as exc:
            logger.warning("Merchant session unavailable: %s", exc)
            return error(
                "key_unavailable",
                _("تعذّر التحقق من الرمز حاليًا. أعد المحاولة."),
                status=503,
                remedy="retry",
            )
        except B2CoreConfigurationError as exc:
            logger.error("Merchant session misconfigured: %s", exc)
            return error(
                "not_configured",
                _("التكامل مع B2CORE غير مهيّأ. تواصل مع الدعم."),
                status=503,
                remedy="contact_support",
            )

        try:
            merchant = embed_session.resolve_merchant(identity.subject)
        except embed_session.MerchantEmbedRefused as exc:
            # A refusal here is not a token problem and must not be dressed up
            # as one: the token was perfectly good and the person behind it is
            # simply not a merchant of ours, or no longer one. Retrying with a
            # fresher token would change nothing, so the remedy says so.
            embed_session.end(request)
            logger.warning(
                "Merchant embed refused for subject %r: %s", identity.subject, exc.code
            )
            return error(
                exc.code,
                exc.message,
                status=exc.status,
                remedy="contact_support",
            )

        embed_session.start(
            request,
            merchant,
            subject=identity.subject,
            token_expires_at=identity.expires_at,
        )
        logger.info("Merchant embed session started for merchant %s", merchant.pk)
        return JsonResponse(session_payload(request, merchant), status=201)

    def delete(self, request, *args, **kwargs):
        """``embed-logout`` clears the session, as it does for the portal.

        The per-session token is not re-checked here: this path is under
        ``MERCHANT_URL_PREFIX``, so a DELETE against a live session has
        already been through
        :class:`~apps.merchant_panel.embed_auth.MerchantEmbedAuthMiddleware`'s
        origin and token guards. Checking twice would be two places for the
        rule to be written differently. A DELETE with no session to end has
        nothing to forge against and is simply answered.
        """
        embed_session.end(request)
        return JsonResponse({"authenticated": False, "reason": "logged_out"})


@method_decorator(never_cache, name="dispatch")
class MerchantEmbedView(TemplateView):
    """The page inside the iframe. Renders no merchant data of its own.

    Everything the handshake needs is passed as one JSON island, so the page
    carries no inline script and the panel's CSP stays ``script-src 'self'``.
    """

    template_name = "merchant/embed.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        origin = getattr(settings, "B2CORE_ORIGIN", "")
        context["embed_config"] = {
            "parentOrigin": origin,
            "sessionUrl": reverse("merchant_panel:session"),
            "panelUrl": reverse("merchant_panel:queue"),
            "allowStandalone": bool(getattr(settings, "PORTAL_ALLOW_STANDALONE", False)),
            "tokenTimeoutMs": 15000,
        }
        context["configured"] = bool(origin and getattr(settings, "B2CORE_JWKS_URL", ""))
        return context
