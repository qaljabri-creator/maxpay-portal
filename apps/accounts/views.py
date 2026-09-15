"""The one screen every internal role shares — choosing your own password.

Build-order step 15. It belongs to no panel: a merchant reaches it as often as a
finance_admin does, and both arrive by the same route, which is
:class:`apps.accounts.middleware.ForcePasswordChangeMiddleware` refusing to
serve them anything else until the one-time password they were issued is gone.

It is also reachable voluntarily, from the "الأمان" link both panels carry —
Finance in its rail, the merchant panel at the end of its navigation bar —
because an account whose password a colleague once read over a phone call should
not have to wait for an administrator to reset it before it can be changed.
"""

from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import PasswordChangeView
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from apps.core.choices import AuditAction
from apps.core.services import record_audit


class InternalPasswordChangeView(LoginRequiredMixin, PasswordChangeView):
    """Django's own form, plus the two things this product needs around it."""

    template_name = "accounts/password_change.html"
    success_url = reverse_lazy("accounts:password_change_done")

    def form_valid(self, form):
        response = super().form_valid(form)
        user = form.user

        # Whatever brought them here, the obligation is discharged. Written
        # through the manager rather than `user.save()` so it cannot race the
        # password Django has just written, and through `get_user_model()`
        # rather than `type(user)` because `form.user` is the request's
        # `SimpleLazyObject`, not a model class.
        if user.must_change_password:
            get_user_model()._default_manager.filter(pk=user.pk).update(
                must_change_password=False
            )
            user.must_change_password = False

        # `PasswordChangeView` already rotates the session hash, but only for
        # `self.request.user`; being explicit costs nothing and means a future
        # refactor cannot log the user out of their own success page.
        update_session_auth_hash(self.request, user)

        record_audit(
            action=AuditAction.USER_CHANGE,
            target=user,
            actor=user,
            request=self.request,
            # No password, no hash, and no "before" that could be compared
            # against anything. What is worth recording is that it happened.
            after={"event": "password_changed", "email": user.email},
        )
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["forced"] = self.request.user.must_change_password
        return context


class PasswordChangeDoneView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/password_change_done.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["heading"] = _("تم تغيير كلمة المرور")
        return context
