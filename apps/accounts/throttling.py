"""Brute-force protection on the internal login (spec §11) — step 14.

Spec §11 requires 2FA on every internal account, which already makes a stolen
password insufficient. What it does not stop is somebody grinding through
passwords against a known email until one works, learning a valid credential
they can hold until they also get the second factor — and filling the audit log
with thousands of ``login_failed`` entries on the way.

So: a counter, and a lockout.

The backend has a second, unrelated job bolted on for a good reason: it is the
one place every permission check in the project passes through, which makes it
where a *revoked* permission is subtracted. See
:meth:`ThrottledModelBackend.get_all_permissions`.

**Where it sits matters.** This is an authentication *backend*, not a check
inside a view. The two-factor login is ``two_factor``'s wizard and the admin has
its own form; a guard in either would leave the other open, and a middleware
would have to guess which POST was a login attempt. Every one of them ends at
``authenticate()``, and that is here.

**What it counts.** Failures are counted per ``(username, IP)`` pair. Per
username alone would let anyone lock a colleague out of their own account by
guessing at them from anywhere — a denial of service handed to the attacker. Per
IP alone would miss a distributed attempt on one account. The pair is the
narrowest key that still stops the attack this is actually for.

**How it fails.** Open. The counter lives in the cache; if the cache is down,
:func:`is_locked_out` says no and the login proceeds to the password check as it
always did. A rate limiter that takes the desk offline when Redis restarts has
cost more than it saved, and 2FA is still in the way.
"""

import logging

from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.core.cache import cache
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger("maxpay.audit")

#: Cache key prefix, versioned so a change of shape cannot read stale counters.
KEY_PREFIX = "maxpay:login-fail:v1"


class LockedOut(Exception):
    """Raised so the caller can say *why* rather than "wrong password"."""

    message = _("عدد محاولات الدخول تجاوز الحد. حاول بعد قليل.")


def _limit() -> int:
    return int(getattr(settings, "LOGIN_FAILURE_LIMIT", 10))


def _window() -> int:
    return int(getattr(settings, "LOGIN_FAILURE_WINDOW_SECONDS", 15 * 60))


def _key(username: str, ip: str) -> str:
    # Lower-cased because the email field is case-insensitive at login and a
    # counter that resets on a capital letter is not a counter.
    return f"{KEY_PREFIX}:{(username or '').strip().lower()}:{ip or '-'}"


def failures(username: str, ip: str) -> int:
    """How many failures this pair has recorded inside the current window."""
    try:
        return int(cache.get(_key(username, ip), 0))
    except Exception:  # pragma: no cover - cache backends vary in what they raise
        logger.warning("Login throttle unavailable (cache down); allowing.")
        return 0


def record_failure(username: str, ip: str) -> int:
    """Count one failed attempt and return the running total.

    ``add`` then ``incr`` rather than a read-modify-write, so two attempts
    arriving together cannot both read zero. The window is a fixed one: it
    starts at the first failure and the whole counter expires with it, which is
    coarser than a sliding window and enough for a control whose job is to make
    grinding slow rather than to be exact.
    """
    key = _key(username, ip)
    try:
        if cache.add(key, 1, timeout=_window()):
            return 1
        return int(cache.incr(key))
    except Exception:  # pragma: no cover - see above
        logger.warning("Login throttle unavailable (cache down); not counting.")
        return 0


def clear(username: str, ip: str) -> None:
    """Forget this pair's failures. Called on a successful password check."""
    try:
        cache.delete(_key(username, ip))
    except Exception:  # pragma: no cover - see above
        pass


def is_locked_out(username: str, ip: str) -> bool:
    return failures(username, ip) >= _limit()


def merchant_password_login_allowed() -> bool:
    """Whether a merchant may still sign in with a password at all.

    Off by default since B2CORE started framing the merchant panel: merchants
    authenticate over there now, and a door nobody uses is a door nobody
    notices being used. Every merchant account still *has* a password — it is
    what the account was provisioned with — so leaving the door open would
    leave the panel reachable by a credential nobody rotates and nobody
    watches, entirely outside the B2CORE binding that is supposed to be the
    only way in.

    It is a setting rather than a deletion because "B2CORE is down and today's
    queue still has to be worked" is a real Tuesday, and a door built during
    the outage is a door built badly. See MERCHANT_PASSWORD_LOGIN.
    """
    return bool(getattr(settings, "MERCHANT_PASSWORD_LOGIN", False))


def client_ip(request) -> str:
    """The caller's address, by the same rule the portal's limiter uses."""
    from apps.core.services import client_ip as resolve

    return resolve(request) or "-"


class ThrottledModelBackend(ModelBackend):
    """``ModelBackend``, with a lockout in front of the password check.

    Returning ``None`` rather than raising is deliberate: Django tries each
    backend in turn and an exception here would be an error page instead of a
    refusal. What the user is told is decided by the login form, which reads
    :func:`is_locked_out` for a message worth acting on; what an attacker is
    told is nothing they did not already know.

    The password hash is still checked *after* the lockout test rather than
    skipped, in the one case that matters: a locked-out pair never reaches it,
    so a lockout also stops the CPU cost of hashing being used as a load
    amplifier.
    """

    def get_all_permissions(self, user_obj, obj=None):
        """Everything the role grants, minus what has been refused per user.

        Spec §3 says a ``finance_admin`` grants **and revokes** staff and
        merchant permissions. ``user_permissions`` covers granting. It cannot
        cover revoking: Django unions the user's own permissions with every
        group's, and :mod:`apps.accounts.signals` re-attaches the role group on
        every save, so anything the group holds comes straight back. A revoked
        permission is therefore stored as a refusal on the user and subtracted
        here — the one place every ``has_perm`` call in the project passes
        through.

        Subtracting rather than adding is what makes this safe to put in the
        authentication path: the worst a bug here can do is refuse somebody a
        permission they should have, which is visible and complained about.
        Superusers never reach this — ``PermissionsMixin.has_perm`` short
        circuits for them before any backend is consulted — so the deny list
        cannot lock the last administrator out of the system.
        """
        granted = super().get_all_permissions(user_obj, obj)
        if obj is not None or not granted:
            return granted
        denied = getattr(user_obj, "_maxpay_denied_cache", None)
        if denied is None:
            get_labels = getattr(user_obj, "denied_permission_labels", None)
            denied = get_labels() if callable(get_labels) else set()
            user_obj._maxpay_denied_cache = denied
        return granted - denied

    def authenticate(self, request, username=None, password=None, **kwargs):
        identifier = username or kwargs.get(getattr(self, "USERNAME_FIELD", "email"))
        ip = client_ip(request)

        if identifier and is_locked_out(identifier, ip):
            logger.warning(
                "Login refused: %s from %s is locked out after %d failures.",
                identifier,
                ip,
                failures(identifier, ip),
            )
            return None

        user = super().authenticate(request, username=username, password=password, **kwargs)

        if (
            user is not None
            and getattr(user, "role", None) == "merchant"
            and not merchant_password_login_allowed()
        ):
            # The password was right. It is simply not a way into this system
            # any more — see `merchant_password_login_allowed`. Refused *after*
            # the hash check rather than before it, so which accounts are
            # merchants cannot be read off the response time, and logged as a
            # refusal rather than a failure: the counter is for people guessing
            # passwords, and this person was not.
            logger.warning(
                "Merchant password login refused for %s: MERCHANT_PASSWORD_LOGIN is off.",
                identifier,
            )
            clear(identifier, ip)
            return None

        if identifier:
            if user is None:
                total = record_failure(identifier, ip)
                if total == _limit():
                    # Logged once, at the moment it starts, rather than on
                    # every subsequent attempt: a lockout that fills the log is
                    # the same denial of service by another route.
                    logger.warning(
                        "Login lockout: %s from %s reached %d failures.",
                        identifier,
                        ip,
                        total,
                    )
            else:
                clear(identifier, ip)
        return user
