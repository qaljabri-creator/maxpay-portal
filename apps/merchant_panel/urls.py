"""The merchant panel's routes (spec §8) — build-order step 8.

Two surfaces on one scope. The screens under ``/merchant/`` are server-rendered
from serializer output; the JSON under ``/merchant/api/`` is the same data plus
the ten-second pulse step 13 added, and is what the anonymity tests sweep.

Step 13 also added two *fragment* routes — ``queue_rows`` and
``request_thread``. They render pieces of the screens above from the same
templates and the same serializer output, so a live-refreshed queue is what a
reload would have produced rather than a second implementation of it. They are
swept exactly like everything else here: a fragment is a merchant-facing
response whatever its content type.

Every name here is enumerated by
``apps.merchant_panel.tests.test_api.MerchantSurfaceTests``. That is not
bookkeeping: adding a route without adding it there fails the suite, so a new
merchant endpoint cannot be shipped without someone deciding, in writing, that
it carries no client identity.
"""

from django.urls import path, re_path

from . import api, embed_views, report_views, views

app_name = "merchant_panel"

#: Matched by shape rather than by ``str``, so a malformed reference is a 404
#: from the router instead of a database round trip.
REFERENCE = r"(?P<reference>MP-\d{4,12})"

urlpatterns = [
    # The B2CORE door. The panel is a menu item inside B2CORE, framed from
    # https://my.maxifyfx.com and restricted there to the merchant client type
    # — a restriction that is B2CORE's convenience and not a control we can
    # verify, which is why `embed_views` binds the token's subject to a
    # merchant record instead of believing the menu. Neither route renders a
    # request, a client or a merchant's data, so neither is part of the surface
    # the anonymity sweep walks; both are covered by `tests.test_embed`.
    path("embed/", embed_views.MerchantEmbedView.as_view(), name="embed"),
    path("session/", embed_views.MerchantSessionView.as_view(), name="session"),

    # Screens (spec §8)
    path("", views.MerchantQueueView.as_view(), name="queue"),
    re_path(rf"^requests/{REFERENCE}/$", views.MerchantRequestDetailView.as_view(), name="request_detail"),
    # Ahead of the action route on purpose: that one matches ``[a-z_]+`` and
    # would happily swallow "messages", turning a reply into an unknown
    # lifecycle move. A message is not an action — see MerchantMessageView.
    re_path(
        rf"^requests/{REFERENCE}/messages/$",
        views.MerchantMessageView.as_view(),
        name="request_message",
    ),
    # Ahead of the action route for the same reason "messages" is: a slug that
    # matches [a-z_]+ would swallow it.
    re_path(
        rf"^requests/{REFERENCE}/thread/$",
        views.MerchantThreadView.as_view(),
        name="request_thread",
    ),
    # Ahead of the action route, like the two above: a correction changes what
    # the request is worth, not where it is, and [a-z_]+ would swallow it.
    re_path(
        rf"^requests/{REFERENCE}/amount/$",
        views.MerchantAmountCorrectionView.as_view(),
        name="request_amount",
    ),
    re_path(
        rf"^requests/{REFERENCE}/(?P<action>[a-z_]+)/$",
        views.MerchantActionView.as_view(),
        name="request_action",
    ),
    path("wallets/", views.MerchantWalletListView.as_view(), name="wallets"),

    # Reporting and export (spec §8, §9) — build-order step 16. Scoped by the
    # same queryset as every other screen, and the export is swept for client
    # identity before a cell of it is written.
    path("reports/", report_views.MerchantReportView.as_view(), name="report"),
    path(
        "reports/export/",
        report_views.MerchantReportExportView.as_view(),
        name="report_export",
    ),

    # Live fragments for the ten-second poll (spec §8, §10) — step 13
    path("queue/rows/", views.MerchantQueueRowsView.as_view(), name="queue_rows"),

    # Proof files. MEDIA_ROOT is never mapped to a URL prefix (spec §11), so
    # this is the only way a stored file reaches a merchant's browser.
    path(
        "attachments/<int:pk>/<str:token>/",
        views.MerchantAttachmentView.as_view(),
        name="attachment",
    ),

    # JSON (spec §8, §11) — read-only, masked, and the poll's data source
    path("api/requests/", api.MerchantRequestQueueAPI.as_view(), name="api_requests"),
    re_path(
        rf"^api/requests/{REFERENCE}/$",
        api.MerchantRequestDetailAPI.as_view(),
        name="api_request_detail",
    ),
    path("api/wallets/", api.MerchantWalletListAPI.as_view(), name="api_wallets"),
    path("api/pulse/", api.MerchantPulseAPI.as_view(), name="api_pulse"),
]
