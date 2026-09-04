"""Routes that belong to no panel.

Only the password screen lives here, and deliberately so: every internal role
reaches it, from either panel or from neither, so putting it under
``/finance/`` would make it unreachable for the merchants who need it most.
"""

from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("password/", views.InternalPasswordChangeView.as_view(), name="password_change"),
    path("password/done/", views.PasswordChangeDoneView.as_view(), name="password_change_done"),
]
