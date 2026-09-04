from django.urls import path

from . import (
    audit_views,
    client_views,
    integration_views,
    live_views,
    queue_views,
    report_views,
    user_views,
    views,
)

app_name = "finance"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),

    # Live updates: the ten-second poll and the fragments it swaps in
    # (spec §10) — build-order step 13. Read-only, every one of them.
    path("pulse/", live_views.FinancePulseView.as_view(), name="pulse"),

    # The request queue, routing and approval (build-order step 7)
    path("requests/", queue_views.RequestQueueView.as_view(), name="request_list"),
    # Ahead of <slug:ref>, which would otherwise match "rows".
    path("requests/rows/", live_views.FinanceQueueRowsView.as_view(), name="request_rows"),
    path("requests/<slug:ref>/", queue_views.RequestDetailView.as_view(), name="request_detail"),
    # Ahead of the action route: <slug:action> would otherwise match
    # "messages" and turn a reply into an unknown lifecycle move. A message is
    # not an action — see RequestMessageView.
    path(
        "requests/<slug:ref>/messages/",
        queue_views.RequestMessageView.as_view(),
        name="request_message",
    ),
    # Same reason: <slug:action> would swallow "thread".
    path(
        "requests/<slug:ref>/thread/",
        live_views.FinanceThreadView.as_view(),
        name="request_thread",
    ),
    # Ahead of the action route for the same reason as the two above: it
    # changes what a request is worth, not where it is, so it is not a
    # lifecycle move and <slug:action> must not swallow it.
    path(
        "requests/<slug:ref>/amount/",
        queue_views.AmountCorrectionView.as_view(),
        name="request_amount",
    ),
    path(
        "requests/<slug:ref>/<slug:action>/",
        queue_views.RequestActionView.as_view(),
        name="request_action",
    ),
    # Proof files, behind a Finance session and a short-lived signature (spec §11)
    path(
        "attachments/<int:pk>/<str:token>/",
        queue_views.AttachmentView.as_view(),
        name="attachment",
    ),

    # One client's whole history (Finance review 4.1). Behind
    # `accounts.view_client_identity` rather than behind the Finance role: the
    # queue's client column merely disappears without that permission, and a
    # page whose entire subject is the client has nothing left to show.
    path("clients/<int:pk>/", client_views.ClientHistoryView.as_view(), name="client_history"),

    # Merchants, their methods, and their wallets (build-order step 3)
    path("merchants/", views.MerchantListView.as_view(), name="merchant_list"),
    path("merchants/new/", views.MerchantCreateView.as_view(), name="merchant_create"),
    path("merchants/<int:pk>/", views.MerchantDetailView.as_view(), name="merchant_detail"),
    path("merchants/<int:pk>/edit/", views.MerchantUpdateView.as_view(), name="merchant_update"),
    path("merchants/<int:pk>/toggle/", views.MerchantToggleView.as_view(), name="merchant_toggle"),
    # Retiring one, and bringing one back. Behind its own permission — see
    # apps.merchants.lifecycle. There is no delete route here and there will
    # not be: a merchant's name on a request from last March is part of that
    # request (spec §11).
    path("merchants/<int:pk>/archive/", views.MerchantArchiveView.as_view(), name="merchant_archive"),
    path(
        "merchants/<int:merchant_pk>/methods/new/",
        views.MerchantMethodCreateView.as_view(),
        name="merchant_method_create",
    ),
    path("methods/<int:pk>/toggle/", views.MerchantMethodToggleView.as_view(), name="merchant_method_toggle"),
    path("methods/<int:method_pk>/wallets/new/", views.WalletCreateView.as_view(), name="wallet_create"),
    path("wallets/<int:pk>/edit/", views.WalletUpdateView.as_view(), name="wallet_update"),
    path("wallets/<int:pk>/activate/", views.WalletActivateView.as_view(), name="wallet_activate"),
    path("wallets/<int:pk>/deactivate/", views.WalletDeactivateView.as_view(), name="wallet_deactivate"),
    # Deleted if no request was ever submitted against it, archived if one was.
    # One route for both, because the operator is asking one question and the
    # data decides the answer.
    path("wallets/<int:pk>/remove/", views.WalletRemoveView.as_view(), name="wallet_remove"),

    # Payment method catalogue
    path("payment-methods/", views.PaymentMethodListView.as_view(), name="payment_method_list"),
    path("payment-methods/new/", views.PaymentMethodCreateView.as_view(), name="payment_method_create"),
    path("payment-methods/<int:pk>/edit/", views.PaymentMethodUpdateView.as_view(), name="payment_method_update"),
    path("payment-methods/<int:pk>/toggle/", views.PaymentMethodToggleView.as_view(), name="payment_method_toggle"),

    # Exchange rates with history (build-order step 4)
    path("rates/", views.RateListView.as_view(), name="rate_list"),
    path("rates/new/", views.RateCreateView.as_view(), name="rate_create"),

    # Business hours and the closed notice (build-order step 11)
    path("hours/", views.BusinessHoursView.as_view(), name="business_hours"),

    # What the B2CORE side is pointed at and how it is faring (spec §4, §9).
    # One route, GET only. There is deliberately no companion route that
    # writes: this configuration is a deployment act and lives in the
    # environment — see apps/finance/integration_views.py.
    path(
        "integration/b2core/",
        integration_views.B2CoreIntegrationView.as_view(),
        name="b2core_integration",
    ),

    # Reporting and export (spec §9) — build-order step 16. Reading a report
    # needs what the queue needs; taking a copy out of the building needs
    # `transactions.export_reports` on top.
    path("reports/", report_views.FinanceReportView.as_view(), name="report"),
    path(
        "reports/export/",
        report_views.FinanceReportExportView.as_view(),
        name="report_export",
    ),

    # Users, roles and permissions (spec §3, §9) — build-order step 15.
    # Behind `accounts.manage_internal_users`; the permission editor and the
    # two-factor reset are behind two further permissions of their own.
    path("users/", user_views.UserListView.as_view(), name="user_list"),
    path("users/new/", user_views.UserCreateView.as_view(), name="user_create"),
    path("users/<int:pk>/", user_views.UserDetailView.as_view(), name="user_detail"),
    path("users/<int:pk>/edit/", user_views.UserUpdateView.as_view(), name="user_update"),
    path(
        "users/<int:pk>/permissions/",
        user_views.UserPermissionsView.as_view(),
        name="user_permissions",
    ),
    # Ahead of nothing in particular, but last on purpose: <slug:action> would
    # swallow "edit" and "permissions" if it came first.
    path(
        "users/<int:pk>/<slug:action>/",
        user_views.UserActionView.as_view(),
        name="user_action",
    ),

    # The audit log viewer (build-order step 12). Read-only by construction —
    # there is no route here that changes anything, which is spec §11's
    # "no delete or update path exposed anywhere" expressed as a URLconf.
    path("audit/", audit_views.AuditLogView.as_view(), name="audit_list"),
    path("audit/<int:pk>/", audit_views.AuditEntryView.as_view(), name="audit_detail"),
]
