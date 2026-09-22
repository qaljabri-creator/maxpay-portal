"""Two things an internal session is not allowed to skip past.

Both middlewares here work the same way — refuse to serve anything until a
precondition is met, and send the user to the one page that lets them meet it —
and both exempt that page so the requirement cannot become a redirect loop.

The order they run in is the order they appear in ``MIDDLEWARE`` and it is
deliberate: the second factor is settled first, because an account whose
password is being changed over an unverified session is an account whose
password is being changed by whoever has the password.

---

Makes the second factor genuinely mandatory rather than merely available.

``django_otp``'s middleware only *records* whether the session is OTP-verified.
Without something enforcing it, an internal user who never sets up a device
still gets a fully functional session. Spec §11 requires all internal accounts
to have 2FA, so this middleware refuses to serve anything to an internal user
who has not both enrolled a device and verified it in the current session.
"""

from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse
from django.utils.functional import cached_property
from django.utils.translation import gettext as _

#: Set by ``MerchantEmbedAuthMiddleware`` when the request is authenticated by
#: a B2CORE embed session rather than by a password. Both gates below exist to
#: make a *password* session safe — a second factor in front of a password, and
#: a forced change of a one-time one — and there is no password in an embed
#: session for either of them to protect. Read as an attribute rather than
#: imported, so ``apps.accounts`` keeps knowing nothing about the merchant panel.
EMBED_ATTRIBUTE = "merchant_embed"


def authenticated_by_embed(request) -> bool:
    return getattr(request, EMBED_ATTRIBUTE, None) is not None


#: Views a user must be able to reach *while* they are non-compliant, otherwise
#: they could never become compliant. ``PORTAL_URL_PREFIX`` joins them from the
#: other direction: the client portal is not an internal surface at all, and an
#: internal user who happens to be logged in in the same browser must not have
#: their 2FA state bounce a *client* into the enrolment wizard.
EXEMPT_URL_NAMES = [
    "two_factor:login",
    "two_factor:setup",
    "two_factor:qr",
    "two_factor:setup_complete",
    "two_factor:backup_tokens",
    "logout",
    "set_language",
    "healthz",
]


class EnforceTwoFactorMiddleware:
    """Redirect non-compliant internal users into the 2FA wizard."""

    def __init__(self, get_response):
        self.get_response = get_response

    @cached_property
    def exempt_prefixes(self) -> tuple[str, ...]:
        prefixes = []
        for name in EXEMPT_URL_NAMES:
            try:
                prefixes.append(reverse(name))
            except NoReverseMatch:
                continue
        for setting_name in ("STATIC_URL", "MEDIA_URL", "PORTAL_URL_PREFIX"):
            value = getattr(settings, setting_name, None)
            if value and value.startswith("/"):
                prefixes.append(value)
        return tuple(prefixes)

    def _is_exempt(self, request) -> bool:
        return request.path.startswith(self.exempt_prefixes)

    def __call__(self, request):
        user = getattr(request, "user", None)

        if (
            user is None
            or not user.is_authenticated
            or not getattr(user, "requires_two_factor", False)
            or authenticated_by_embed(request)
            or self._is_exempt(request)
        ):
            return self.get_response(request)

        # Set by django_otp.middleware.OTPMiddleware, which must sit ahead of
        # this one in MIDDLEWARE.
        is_verified = getattr(user, "is_verified", None)
        if callable(is_verified) and is_verified():
            return self.get_response(request)

        enrolled = user.has_verified_two_factor
        target = "two_factor:login" if enrolled else "two_factor:setup"
        reason = "otp_required" if enrolled else "otp_setup_required"

        if self._wants_json(request):
            return JsonResponse(
                {
                    "detail": _("هذا الحساب يتطلب المصادقة الثنائية."),
                    "code": reason,
                    "setup_url": reverse("two_factor:setup"),
                },
                status=403,
            )

        if not enrolled:
            messages.warning(
                request,
                _("يجب تفعيل المصادقة الثنائية قبل استخدام النظام."),
            )
        return redirect(f"{reverse(target)}?{urlencode({'next': request.get_full_path()})}")

    @staticmethod
    def _wants_json(request) -> bool:
        if request.path.startswith("/api/"):
            return True
        accept = request.META.get("HTTP_ACCEPT", "")
        return "application/json" in accept and "text/html" not in accept


#: Reachable while the password still has to be changed, for the same reason
#: the two-factor list exists: otherwise there is no way to become compliant.
#: ``two_factor:setup`` is on it because the second factor is settled first and
#: a user part-way through enrolment must be able to finish.
PASSWORD_EXEMPT_URL_NAMES = [
    "accounts:password_change",
    "accounts:password_change_done",
    "two_factor:login",
    "two_factor:setup",
    "two_factor:qr",
    "two_factor:setup_complete",
    "two_factor:backup_tokens",
    "logout",
    "set_language",
    "healthz",
]


class ForcePasswordChangeMiddleware:
    """Hold an account at the password form until its one-time password is gone.

    Build-order step 15. Every account is created — and every reset issues — a
    password generated by :mod:`apps.accounts.provisioning` and read off an
    administrator's screen. That password has therefore been seen by two people
    and has probably travelled through a chat message, so it is treated as valid
    for exactly one login: ``must_change_password`` is set when it is issued and
    cleared only by the user choosing their own.

    Enforced here rather than in the login view because there is more than one
    way in — the two-factor wizard, the admin's own login, a session that was
    already open when an administrator reset the password — and a check in any
    one of them leaves the others open. This is the choke point they all pass
    through afterwards.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    @cached_property
    def exempt_prefixes(self) -> tuple[str, ...]:
        prefixes = []
        for name in PASSWORD_EXEMPT_URL_NAMES:
            try:
                prefixes.append(reverse(name))
            except NoReverseMatch:
                continue
        for setting_name in ("STATIC_URL", "MEDIA_URL", "PORTAL_URL_PREFIX"):
            value = getattr(settings, setting_name, None)
            if value and value.startswith("/"):
                prefixes.append(value)
        return tuple(prefixes)

    def __call__(self, request):
        user = getattr(request, "user", None)

        if (
            user is None
            or not user.is_authenticated
            or not getattr(user, "must_change_password", False)
            or authenticated_by_embed(request)
            or request.path.startswith(self.exempt_prefixes)
        ):
            return self.get_response(request)

        target = reverse("accounts:password_change")

        if EnforceTwoFactorMiddleware._wants_json(request):
            return JsonResponse(
                {
                    "detail": _("يجب تغيير كلمة المرور قبل استخدام النظام."),
                    "code": "password_change_required",
                    "change_url": target,
                },
                status=403,
            )

        messages.warning(
            request,
            _("هذه كلمة مرور مؤقتة. اختر كلمة مرور جديدة قبل المتابعة."),
        )
        return redirect(f"{target}?{urlencode({'next': request.get_full_path()})}")
