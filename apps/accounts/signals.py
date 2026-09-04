"""Keeps group membership, audit entries and login metadata in sync."""

import logging

from django.contrib.auth.models import Group
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.signals import post_migrate, post_save
from django.dispatch import receiver

from apps.core.choices import AuditAction
from apps.core.services import client_ip, record_audit

from .permissions import ROLE_PERMISSIONS, post_migrate_sync

logger = logging.getLogger("maxpay.audit")

ROLE_GROUP_NAMES = set(ROLE_PERMISSIONS)

post_migrate.connect(post_migrate_sync, dispatch_uid="maxpay.accounts.sync_role_groups")


@receiver(post_save, sender="accounts.User", dispatch_uid="maxpay.accounts.sync_user_role_group")
def sync_user_role_group(sender, instance, created, raw=False, **kwargs):
    """Mirror ``User.role`` into group membership.

    Only the three role groups are touched, so any extra group a
    ``finance_admin`` has attached to a user survives.
    """
    if raw:
        return

    try:
        target = Group.objects.get(name=instance.role)
    except Group.DoesNotExist:
        logger.warning(
            "Role group %r does not exist yet; run `manage.py bootstrap_roles`.",
            instance.role,
        )
        target = None

    current_role_groups = instance.groups.filter(name__in=ROLE_GROUP_NAMES)
    stale = [g for g in current_role_groups if target is None or g.pk != target.pk]
    if stale:
        instance.groups.remove(*stale)
    if target is not None and not instance.groups.filter(pk=target.pk).exists():
        instance.groups.add(target)


@receiver(user_logged_in, dispatch_uid="maxpay.accounts.on_login")
def on_login(sender, request, user, **kwargs):
    ip = client_ip(request)
    if ip and getattr(user, "last_login_ip", None) != ip:
        type(user).objects.filter(pk=user.pk).update(last_login_ip=ip)
    record_audit(
        action=AuditAction.LOGIN,
        target=user,
        actor=user,
        request=request,
        after={"event": "login", "two_factor_verified": bool(getattr(user, "is_verified", lambda: False)())},
    )


@receiver(user_login_failed, dispatch_uid="maxpay.accounts.on_login_failed")
def on_login_failed(sender, credentials, request=None, **kwargs):
    # Never log the submitted password, and only log the username so a failed
    # attempt can be traced without storing a secret.
    record_audit(
        action=AuditAction.LOGIN_FAILED,
        target_type="accounts.User",
        target_id=credentials.get("username", ""),
        request=request,
        after={"event": "login_failed"},
    )


@receiver(user_logged_out, dispatch_uid="maxpay.accounts.on_logout")
def on_logout(sender, request, user, **kwargs):
    if user is None:
        return
    logger.info("logout user=%s ip=%s", user.pk, client_ip(request))
