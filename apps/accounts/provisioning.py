"""Creating and administering internal accounts (spec §3) — build-order step 15.

Spec §3 is one sentence with a lot of consequences:

> All merchant and staff permissions are granted and revoked by
> ``finance_admin``. There is no self-registration for any internal role.

Until now the only way to act on it was ``manage.py create_internal_user`` and
the Django admin, which means a shell on the production host and a screen that
knows nothing about roles, merchant records or second factors. This module is
the domain half of doing it from the panel instead; :mod:`apps.finance.user_views`
is the screen half.

**Everything here is audited.** Not as a decoration on top of the operation but
as part of it: each function below writes its own entry inside the same
transaction as the change, so an account cannot be created, disabled, re-roled,
re-passworded or have its second factor cleared without a row in a log nobody
can edit or delete (spec §11). The audit action is chosen to match what the
viewer already renders — ``user_change`` for the account itself,
``permission_change`` for its permissions, ``two_factor_change`` for its
devices.

**Passwords are generated here, never chosen by the administrator.** A password
someone types for someone else is a password they know, will paste into a chat,
and will reuse. What this module produces is a random string from a 62-character
alphabet, shown once, and marked ``must_change_password`` so it survives exactly
as long as it takes the recipient to log in.
"""

import logging
import secrets
import string

from django.contrib.auth.models import Permission
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.choices import AuditAction
from apps.core.services import record_audit, snapshot

from .models import Role, User
from .permissions import MERCHANT_FORBIDDEN, expected_permissions

logger = logging.getLogger("maxpay.audit")

#: What an audit entry captures about an account. Never the password, never the
#: hash: the log is read by people, and a hash in it is a hash to grind offline.
AUDIT_FIELDS = [
    "email",
    "full_name",
    "phone",
    "role",
    "is_active",
    "is_superuser",
    "must_change_password",
]

#: Ambiguous glyphs are left out. This string is read off one screen and typed
#: into another, sometimes over a phone call, and `l` against `1` or `O` against
#: `0` is a support ticket waiting to happen. The alphabet is still 55 symbols,
#: so a 20-character password carries ~115 bits — the readability costs about
#: three bits per character and buys back every one of them in not being retyped.
PASSWORD_ALPHABET = (
    "".join(c for c in string.ascii_lowercase if c not in "lo")
    + "".join(c for c in string.ascii_uppercase if c not in "IO")
    + "".join(c for c in string.digits if c not in "01")
)

#: Comfortably past anything ``AUTH_PASSWORD_VALIDATORS`` asks for, and past
#: anything worth grinding.
PASSWORD_LENGTH = 20


class ProvisioningError(Exception):
    """A refusal with a message fit to put on a form."""

    def __init__(self, message, *, code: str = "not_allowed"):
        super().__init__(str(message))
        self.message = message
        self.code = code


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    """A random password, and one that certainly satisfies the validators.

    ``secrets.choice`` rather than ``random``: this is a credential, and the
    difference between the two modules is the difference between unpredictable
    and merely unlikely.

    Rejection sampling rather than assembling one character of each class and
    shuffling. Both give a valid password; only this one leaves the distribution
    uniform over the whole alphabet, and at 20 characters the loop practically
    never runs twice.
    """
    while True:
        candidate = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
        ):
            return candidate


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def guard_not_self(actor, target: User, message) -> None:
    """Refuse an administrator acting destructively on their own account.

    Not paternalism: ``finance_admin`` is the only role that can restore any of
    this, so an administrator who disables themselves, demotes themselves or
    revokes their own management permission has locked the entire product out
    of being administered. The remedy would be a shell on the production host,
    which is the thing this panel exists to stop being necessary.
    """
    if actor is not None and getattr(actor, "pk", None) == target.pk:
        raise ProvisioningError(message, code="self_target")


def guard_merchant_permissions(role: str, labels: set[str]) -> None:
    """Spec §2, restated where a human could otherwise break it by hand.

    :func:`apps.accounts.permissions.sync_role_groups` already refuses to build
    a merchant *group* holding an identity permission. This is the same rule for
    the other route in — an administrator granting one to a single merchant
    account from a form.
    """
    leaked = labels & MERCHANT_FORBIDDEN
    if leaked:
        raise ProvisioningError(
            _("لا يمكن منح حساب تاجر صلاحيات تكشف هوية العميل: %(perms)s")
            % {"perms": "، ".join(sorted(leaked))},
            code="merchant_anonymity",
        )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@transaction.atomic
def create_account(
    *,
    email: str,
    full_name: str,
    role: str,
    phone: str = "",
    actor=None,
    http_request=None,
) -> tuple[User, str]:
    """Create an internal account and return it with its one-time password.

    The password is returned rather than stored anywhere readable, and the
    account is flagged ``must_change_password`` so it stops working as soon as
    it has been used once.
    """
    email = (email or "").strip().lower()
    password = generate_password()

    user = User.objects.create_user(
        email=email,
        password=password,
        full_name=full_name,
        role=role,
        phone=phone,
        must_change_password=True,
        created_by=actor if isinstance(actor, User) else None,
    )

    record_audit(
        action=AuditAction.USER_CHANGE,
        target=user,
        actor=actor,
        request=http_request,
        before=None,
        after={**snapshot(user, AUDIT_FIELDS), "event": "created"},
    )
    logger.info("Internal account created: %s (%s) by %s", email, role, getattr(actor, "pk", None))
    return user, password


@transaction.atomic
def update_account(
    user: User,
    *,
    full_name: str,
    phone: str,
    role: str,
    actor=None,
    http_request=None,
) -> User:
    """Rename, re-phone or re-role an account."""
    if role != user.role:
        guard_not_self(
            actor, user, _("لا يمكنك تغيير دور حسابك أنت. اطلب ذلك من مدير مالي آخر.")
        )
        if user.role == Role.MERCHANT and hasattr(user, "merchant_profile"):
            raise ProvisioningError(
                _("هذا الحساب مربوط بسجل تاجر. افصل الربط أولًا قبل تغيير الدور."),
                code="merchant_linked",
            )

    before = snapshot(User.objects.get(pk=user.pk), AUDIT_FIELDS)
    user.full_name = full_name
    user.phone = phone
    user.role = role
    # The role group is re-attached by the post_save signal, so a re-role takes
    # its new baseline immediately and drops the old one.
    user.save(update_fields=["full_name", "phone", "role"])

    record_audit(
        action=AuditAction.USER_CHANGE,
        target=user,
        actor=actor,
        request=http_request,
        before=before,
        after={**snapshot(user, AUDIT_FIELDS), "event": "updated"},
    )
    return user


@transaction.atomic
def set_active(user: User, active: bool, *, actor=None, http_request=None) -> User:
    """Enable or disable an account.

    Disabled rather than deleted, always: spec §11 wants the audit trail intact,
    and ``accounts.delete_user`` is in the global deny list precisely so nobody
    can break it (see :mod:`apps.accounts.permissions`).
    """
    if not active:
        guard_not_self(actor, user, _("لا يمكنك تعطيل حسابك أنت."))

    before = snapshot(User.objects.get(pk=user.pk), AUDIT_FIELDS)
    user.is_active = active
    user.save(update_fields=["is_active"])

    record_audit(
        action=AuditAction.USER_CHANGE,
        target=user,
        actor=actor,
        request=http_request,
        before=before,
        after={
            **snapshot(user, AUDIT_FIELDS),
            "event": "enabled" if active else "disabled",
        },
    )
    return user


@transaction.atomic
def reset_password(user: User, *, actor=None, http_request=None) -> str:
    """Issue a fresh one-time password and return it.

    Every session the account has open keeps working. Ending them would be the
    right move for a *compromised* account and the wrong one for the ordinary
    case this serves — somebody who locked themselves out — and the panel has no
    way to tell the two apart. Disabling the account is the lever for the first.
    """
    password = generate_password()
    before = snapshot(User.objects.get(pk=user.pk), AUDIT_FIELDS)

    user.set_password(password)
    user.must_change_password = True
    user.save(update_fields=["password", "must_change_password"])

    record_audit(
        action=AuditAction.USER_CHANGE,
        target=user,
        actor=actor,
        request=http_request,
        before=before,
        # Neither the password nor its hash. The log is read by people.
        after={**snapshot(user, AUDIT_FIELDS), "event": "password_reset"},
    )
    logger.info("Password reset for %s by %s", user.email, getattr(actor, "pk", None))
    return password


@transaction.atomic
def reset_two_factor(user: User, *, actor=None, http_request=None) -> int:
    """Delete every OTP device the user holds, and record that it happened.

    Returns how many devices were removed.

    This is the lock on the whole product being opened for one account, so the
    entry it writes is the point of the function as much as the deletion is. The
    account is not left unprotected: :class:`~apps.accounts.middleware.EnforceTwoFactorMiddleware`
    refuses to serve an internal user with no verified device, so the next login
    lands in the enrolment wizard and nothing else is reachable until it is done.
    """
    from django_otp import devices_for_user

    devices = list(devices_for_user(user, confirmed=None))
    for device in devices:
        device.delete()

    user.two_factor_reset_at = timezone.now()
    user.save(update_fields=["two_factor_reset_at"])

    record_audit(
        action=AuditAction.TWO_FACTOR_CHANGE,
        target=user,
        actor=actor,
        request=http_request,
        before={"devices": len(devices)},
        after={"devices": 0, "event": "two_factor_reset", "email": user.email},
    )
    logger.warning(
        "Two-factor reset for %s (%d device(s)) by %s",
        user.email,
        len(devices),
        getattr(actor, "pk", None),
    )
    return len(devices)


def effective_permissions(user: User) -> set[str]:
    """What the account actually holds: role baseline, plus grants, minus denials."""
    baseline = expected_permissions(user.role) if user.role in Role.values else set()
    extra = {
        f"{p.content_type.app_label}.{p.codename}"
        for p in user.user_permissions.select_related("content_type")
    }
    return (baseline | extra) - user.denied_permission_labels()


@transaction.atomic
def set_permission_overrides(
    user: User,
    *,
    granted: set[str],
    denied: set[str],
    actor=None,
    http_request=None,
) -> User:
    """Replace the account's per-user grants and refusals wholesale.

    Both sets are ``app_label.codename`` labels. Anything in neither is left to
    the role, which is the ordinary case and the one the screen defaults to.
    """
    guard_not_self(
        actor,
        user,
        _("لا يمكنك تعديل صلاحيات حسابك أنت. اطلب ذلك من مدير مالي آخر."),
    )
    overlap = granted & denied
    if overlap:
        raise ProvisioningError(
            _("لا يمكن منح صلاحية ومنعها في الوقت نفسه: %(perms)s")
            % {"perms": "، ".join(sorted(overlap))},
            code="contradiction",
        )
    if user.role == Role.MERCHANT:
        guard_merchant_permissions(user.role, granted)

    before = {
        "granted": sorted(
            f"{p.content_type.app_label}.{p.codename}"
            for p in user.user_permissions.select_related("content_type")
        ),
        "denied": sorted(user.denied_permission_labels()),
        "effective": sorted(effective_permissions(user)),
    }

    user.user_permissions.set(_resolve(granted))
    user.denied_permissions.set(_resolve(denied))
    # The backend caches the denial set on the instance; the row it was read
    # from has just changed underneath it.
    user._maxpay_denied_cache = None

    record_audit(
        action=AuditAction.PERMISSION_CHANGE,
        target=user,
        actor=actor,
        request=http_request,
        before=before,
        after={
            "granted": sorted(granted),
            "denied": sorted(denied),
            "effective": sorted(effective_permissions(user)),
        },
    )
    logger.info(
        "Permissions changed for %s by %s (+%d / -%d)",
        user.email,
        getattr(actor, "pk", None),
        len(granted),
        len(denied),
    )
    return user


def _resolve(labels: set[str]) -> list[Permission]:
    """``app_label.codename`` strings to ``Permission`` rows, silently dropping
    anything that no longer exists — a permission removed by a migration is not
    a reason to refuse the whole form."""
    found: list[Permission] = []
    by_app: dict[str, set[str]] = {}
    for label in labels:
        app_label, _sep, codename = label.partition(".")
        by_app.setdefault(app_label, set()).add(codename)
    for app_label, codenames in by_app.items():
        found.extend(
            Permission.objects.filter(
                content_type__app_label=app_label, codename__in=codenames
            ).select_related("content_type")
        )
    return found


@transaction.atomic
def link_merchant(user: User, merchant, *, actor=None, http_request=None):
    """Point a ``Merchant`` record at the account that signs in for it.

    ``Merchant.user`` is the link and it is a one-to-one, so this both attaches
    the new one and detaches whatever it was pointing at. Passing ``None``
    detaches only, which is how an account is freed before it is re-roled.
    """
    from apps.merchants.models import Merchant

    if merchant is not None and user.role != Role.MERCHANT:
        raise ProvisioningError(
            _("لا يمكن ربط سجل تاجر بحساب ليس دوره «تاجر»."), code="wrong_role"
        )

    previous = Merchant.objects.filter(user=user).first()
    if previous is not None and previous != merchant:
        previous.user = None
        previous.save(update_fields=["user"])

    if merchant is not None:
        merchant.user = user
        merchant.save(update_fields=["user"])

    record_audit(
        action=AuditAction.MERCHANT_CHANGE,
        target=merchant or previous or user,
        actor=actor,
        request=http_request,
        before={"merchant": str(previous) if previous else None, "user": user.email},
        after={"merchant": str(merchant) if merchant else None, "user": user.email},
    )
    return merchant
