"""Root URL configuration.

Three surfaces. The internal one — the two-factor login flow, the Finance
panel, the Django admin. The merchant panel under ``/merchant/``, which is an
internal session too but sees a strictly masked view of the same requests
(spec §2, §8). And the embedded client portal under ``/portal/``, which B2CORE
frames and which authenticates nobody through Django's session at all (spec §4).
"""

from django.contrib import admin
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import include, path
from two_factor.admin import AdminSiteOTPRequired
from two_factor.urls import urlpatterns as two_factor_urls

# Belt and braces alongside EnforceTwoFactorMiddleware: even if the middleware
# were removed, the admin itself would still refuse an unverified session.
admin.site.__class__ = AdminSiteOTPRequired


def healthz(_request):
    """Unauthenticated liveness probe. Deliberately reveals nothing."""
    return JsonResponse({"status": "ok"})


def home(request):
    """Send each internal user to the panel that is theirs.

    A merchant has no business on the Finance dashboard and would be refused
    there; landing them on a 403 immediately after a successful login would be
    a needlessly unwelcoming way to say so. This is also where
    ``LOGIN_REDIRECT_URL`` points, so the second factor hands over to the right
    surface for whoever just proved who they are.
    """
    if getattr(request.user, "role", None) == "merchant":
        return redirect("merchant_panel:queue")
    return redirect("finance:dashboard")


urlpatterns = [
    path("healthz/", healthz, name="healthz"),
    path("", include(two_factor_urls)),
    # Belongs to neither panel: every internal role reaches the password screen,
    # and ForcePasswordChangeMiddleware sends them all to the same one.
    path("account/", include("apps.accounts.urls")),
    path("finance/", include("apps.finance.urls")),
    # The merchant's own surface (build-order step 8). Everything under it is
    # rendered from masked serializers, never from a model instance (spec §2).
    path("merchant/", include("apps.merchant_panel.urls")),
    # Must match PORTAL_URL_PREFIX: the frame-ancestors CSP, the portal session
    # cookie path and both portal middlewares key off that prefix.
    path("portal/", include("apps.portal.urls")),
    path("admin/", admin.site.urls),
    path("i18n/", include("django.conf.urls.i18n")),
    path("", home, name="home"),
]

# MEDIA_ROOT is deliberately never routed, in DEBUG or otherwise: attachments
# must go through the signed time-limited view (spec §11).
