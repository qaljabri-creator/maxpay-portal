"""The embedded client portal (spec §4, §7).

Everything here lives under ``PORTAL_URL_PREFIX``, which is what the
frame-ancestors CSP, the portal session cookie's path, and the two portal
middlewares all key off. Moving the prefix means moving all four together.

Two groups of routes. The session surface (build-order step 5) turns a B2CORE
token into a portal session. The flow surface (step 6) is what that session is
for: the deposit wizard's catalogue, its submission, and the client's own
request history. Withdrawals reuse the same routes in step 10 — the type is a
parameter, not a path.
"""

from django.urls import path, re_path

from . import flow_views, views

app_name = "portal"

urlpatterns = [
    path("", views.BootstrapView.as_view(), name="bootstrap"),

    # The B2CORE handshake and the session it produces (step 5)
    path("session/", views.SessionView.as_view(), name="session"),
    path("preferences/", views.PreferencesView.as_view(), name="preferences"),

    # The client request flow (step 6)
    path("options/", flow_views.OptionsView.as_view(), name="options"),
    path("requests/", flow_views.RequestsView.as_view(), name="requests"),
    # Matched by shape rather than by `str`, so a malformed reference is a 404
    # from the router instead of a database round trip.
    re_path(
        r"^requests/(?P<reference>MP-\d{4,12})/$",
        flow_views.RequestDetailView.as_view(),
        name="request_detail",
    ),
    # The thread on that request (step 9). Two-way and not gated on status:
    # the client writes whenever they want, and the merchant writes back.
    re_path(
        r"^requests/(?P<reference>MP-\d{4,12})/messages/$",
        flow_views.RequestMessagesView.as_view(),
        name="request_messages",
    ),

    # Files. MEDIA_ROOT is never mapped to a URL prefix (spec §11), so both of
    # these are the only way anything stored ever reaches a browser.
    path(
        "attachments/<int:pk>/<str:token>/",
        flow_views.AttachmentView.as_view(),
        name="attachment",
    ),
    path(
        "method-icon/<slug:code>/",
        flow_views.MethodIconView.as_view(),
        name="method_icon",
    ),
    # The scannable code a wallet can carry instead of, or beside, a number.
    path(
        "wallet-qr/<int:pk>/",
        flow_views.WalletQrView.as_view(),
        name="wallet_qr",
    ),
]
