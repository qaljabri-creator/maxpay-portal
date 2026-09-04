"""The role → permission matrix, and the code that syncs it into Django groups.

Spec §3: all merchant and staff permissions are granted and revoked by
``finance_admin``. This module defines the *baseline* each role receives; a
``finance_admin`` can then add or remove individual permissions per user through
the admin. Re-running :func:`sync_role_groups` resets the group baseline but
never touches the per-user overrides.

Every role's group is named after the role, so ``User.role`` and group
membership stay in lockstep — see :mod:`apps.accounts.signals`.
"""

import logging

from django.apps import apps as django_apps
from django.contrib.auth.models import Group, Permission
from django.db import transaction

logger = logging.getLogger("maxpay.audit")

ALL = "*"

#: Apps whose permissions this matrix governs.
MANAGED_APP_LABELS = ["core", "accounts", "merchants", "rates", "transactions"]

#: Permissions the OTP plugins define. finance_admin needs them to reset a
#: locked-out user's second factor (spec §3: manages all other users).
OTP_PERMISSIONS = {
    "otp_totp.view_totpdevice",
    "otp_totp.delete_totpdevice",
    "otp_static.view_staticdevice",
    "otp_static.delete_staticdevice",
    "otp_static.view_statictoken",
    "otp_static.delete_statictoken",
}

#: Permissions that exist because Django generates them, but that nobody should
#: ever hold. Requests, messages, attachments and clients are business records:
#: they are cancelled or deactivated, never deleted. Spec §11 requires the audit
#: trail to stay intact, and a deleted row breaks it.
GLOBAL_DENY = {
    "transactions.delete_request",
    "transactions.delete_message",
    "transactions.delete_attachment",
    "accounts.delete_client",
    "accounts.delete_user",
    "core.delete_systemsettings",
    "merchants.delete_merchant",
    "merchants.delete_paymentmethod",
    "merchants.delete_wallet",
}

#: Spec §2 — the anonymity guarantee, expressed as permissions a merchant must
#: never hold under any circumstance. :func:`sync_role_groups` refuses to build
#: a merchant group containing any of these, so a future edit to the matrix
#: cannot quietly break client anonymity.
MERCHANT_FORBIDDEN = {
    "accounts.view_client",
    "accounts.add_client",
    "accounts.change_client",
    "accounts.view_client_pii",
    "accounts.view_client_identity",
    "transactions.view_all_requests",
    "core.view_auditlog",
    "accounts.view_user",
    "accounts.manage_internal_users",
    "accounts.manage_permissions",
}

FINANCE_STAFF_PERMISSIONS = {
    # The request queue (spec §9)
    "transactions.view_request",
    "transactions.change_request",
    "transactions.view_all_requests",
    "transactions.route_request",
    "transactions.approve_request",
    "transactions.reject_request",
    # Distinct from rejecting: abandoned, not failed (Finance review, 24 Aug
    # 2026). Staff-level for the same reason rejecting is — the desk that works
    # the queue is the desk that ends a request nobody needs any more.
    "transactions.cancel_request",
    # Correcting a request to what actually arrived (Finance review, 24 Aug).
    "transactions.change_request_amount",
    "transactions.credit_request",
    "transactions.close_request",
    # Threads — Finance supervises every conversation (spec §9)
    "transactions.view_message",
    "transactions.add_message",
    "transactions.view_attachment",
    "transactions.add_attachment",
    # Full request detail includes client identity (spec §9)
    "accounts.view_client",
    "accounts.view_client_pii",
    "accounts.view_client_identity",
    # Reference data is read-only for staff; managing it is admin-only (spec §3)
    "merchants.view_merchant",
    "merchants.view_merchantmethod",
    "merchants.view_wallet",
    "merchants.view_paymentmethod",
    "rates.view_exchangerate",
    "core.view_systemsettings",
    # Audit log viewer (spec §9)
    "core.view_auditlog",
    "accounts.view_audit_log",
}

MERCHANT_PERMISSIONS = {
    # Only requests routed to them; the object-level scoping lives in the
    # merchant queryset, not here (spec §8).
    "transactions.view_request",
    "transactions.confirm_request",
    "transactions.reject_request",
    # Handing a request back to Finance rather than rejecting it: the merchant
    # is saying "not me", not "no" (Finance review, 24 Aug 2026).
    "transactions.return_request",
    # The merchant is who watches the money land, so they are who can say it
    # was $60 and not $100. Every edit is audited and Finance sees it on the
    # request, which is what makes handing this to a third party safe.
    "transactions.change_request_amount",
    # Thread participation (spec §8)
    "transactions.view_message",
    "transactions.add_message",
    "transactions.view_attachment",
    "transactions.add_attachment",
    # Read-only view of own wallets and their active status (spec §8)
    "merchants.view_wallet",
    "merchants.view_merchantmethod",
    "merchants.view_paymentmethod",
}

#: role → permission set, or ``ALL`` for every managed permission.
ROLE_PERMISSIONS = {
    "finance_admin": ALL,
    "finance_staff": FINANCE_STAFF_PERMISSIONS,
    "merchant": MERCHANT_PERMISSIONS,
}


class PermissionMatrixError(Exception):
    """Raised when the matrix cannot be applied safely."""


def _managed_permission_qs():
    return Permission.objects.filter(
        content_type__app_label__in=MANAGED_APP_LABELS
    ).select_related("content_type")


def _label(permission: Permission) -> str:
    return f"{permission.content_type.app_label}.{permission.codename}"


def expected_permissions(role: str) -> set[str]:
    """The permission labels a role's group should hold, deny-list applied."""
    spec = ROLE_PERMISSIONS[role]
    if spec is ALL:
        granted = {_label(p) for p in _managed_permission_qs()} | set(OTP_PERMISSIONS)
    else:
        granted = set(spec)
    return granted - GLOBAL_DENY


def _resolve(labels: set[str]) -> tuple[list[Permission], set[str]]:
    """Turn ``app_label.codename`` strings into Permission rows."""
    wanted = {}
    for label in labels:
        app_label, _, codename = label.partition(".")
        wanted.setdefault(app_label, set()).add(codename)

    found: list[Permission] = []
    for app_label, codenames in wanted.items():
        found.extend(
            Permission.objects.filter(
                content_type__app_label=app_label, codename__in=codenames
            ).select_related("content_type")
        )
    missing = labels - {_label(p) for p in found}
    return found, missing


@transaction.atomic
def sync_role_groups(*, strict: bool = True) -> dict[str, int]:
    """Create the role groups and reset each one to its baseline.

    Returns ``{role: permission_count}``.

    ``strict=False`` makes the function a no-op when some permissions do not
    exist yet. That is what the ``post_migrate`` hook uses: the hook fires once
    per app, and only the firing that happens after the last managed app has
    created its permissions does any work.
    """
    results: dict[str, int] = {}

    for role in ROLE_PERMISSIONS:
        labels = expected_permissions(role)

        if role == "merchant":
            leaked = labels & MERCHANT_FORBIDDEN
            if leaked:
                raise PermissionMatrixError(
                    "Client anonymity (spec §2) would be broken: the merchant "
                    f"role must never hold {sorted(leaked)}."
                )

        permissions, missing = _resolve(labels)
        if missing:
            if not strict:
                logger.debug(
                    "Skipping role sync; %d permission(s) not created yet.", len(missing)
                )
                return {}
            raise PermissionMatrixError(
                f"Unknown permission(s) for role {role!r}: {sorted(missing)}. "
                "Run migrations first, or fix the matrix."
            )

        group, _created = Group.objects.get_or_create(name=role)
        group.permissions.set(permissions)
        results[role] = len(permissions)

    logger.info("Role groups synced: %s", results)
    return results


def post_migrate_sync(sender, **kwargs):
    """``post_migrate`` receiver — keeps the groups current after every migrate."""
    if not django_apps.ready:
        return
    try:
        sync_role_groups(strict=False)
    except PermissionMatrixError:
        # A genuine matrix error must not abort `migrate`; `bootstrap_roles`
        # reports it properly.
        logger.exception("Role group sync failed during post_migrate.")


def assert_merchant_anonymity(user) -> None:
    """Raise if a merchant account somehow holds an identity permission.

    Cheap enough to call from any merchant-scoped view or serializer as a
    belt-and-braces check on top of the group matrix (spec §2).
    """
    if getattr(user, "role", None) != "merchant":
        return
    held = {label for label in MERCHANT_FORBIDDEN if user.has_perm(label)}
    if held:
        raise PermissionMatrixError(
            f"Merchant account {user.pk} holds forbidden permissions: {sorted(held)}."
        )
