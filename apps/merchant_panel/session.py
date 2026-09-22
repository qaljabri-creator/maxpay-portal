"""The merchant's embed session: what it stores, and who it will open for.

B2CORE frames the merchant panel and authenticates the person on the other side
of it. What arrives here is the same signed token the client portal receives —
the same handshake, the same :func:`~apps.portal.b2core.verify_token`, the same
JWKS. What is *different* is what a verified subject entitles the caller to.

**The binding is the whole guard.** B2CORE's token carries no client type;
checked against a real one on 4 Sep 2026, it has ``sub``, ``email``,
``first_name``, ``last_name`` and nothing that says "this is a merchant". The
menu item is restricted to the merchant client type on B2CORE's side, and that
restriction is a *convenience for the operator*, not a control we can verify —
anyone who can obtain a token from B2CORE can post it here. So the only thing
standing between an ordinary client and the merchant panel is this: the subject
must equal the ``b2core_id`` of a merchant record Finance typed in. No match, no
session. ``apps.merchant_panel.tests.test_embed`` states that as a test with an
ordinary client's perfectly valid token.

That is a change of meaning for ``Merchant.b2core_id``, and it is worth naming
rather than discovering: until now the field was reference data for manual
reconciliation, explicitly documented as never to be promoted into an
authorisation decision. It now is one. Which means a typo in it is no longer a
reconciliation nuisance — it is a merchant who cannot sign in, or, if it is
typed to match somebody else's subject, the wrong person's queue. The field's
own docstring says so now too.

**A session is not a licence to keep it.** Everything the binding asserts is
re-checked on every request, not only at the door: the merchant is still active,
still unarchived, still wired to a login account, and that account is still an
active merchant account. Suspending a merchant has to take effect on their next
click, not on their next login.
"""

import logging
import secrets

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.merchants.models import Merchant

logger = logging.getLogger("maxpay.b2core")

SESSION_MERCHANT_ID = "merchant_id"
SESSION_USER_ID = "merchant_user_id"
SESSION_SUBJECT = "b2core_sub"
SESSION_EXPIRES_AT = "b2core_exp"
SESSION_CSRF = "merchant_csrf"

#: Fallback when ``MERCHANT_SESSION_MAX_SECONDS`` is absent. An embed session is
#: never allowed to outlive this, however long-lived the token was.
MAX_SESSION_SECONDS = 60 * 60 * 8


class MerchantEmbedRefused(Exception):
    """No session, and a reason the merchant can act on.

    ``code`` is what the embed branches on; ``message`` is what it shows. Both
    are deliberately specific — the caller has already proved which B2CORE
    subject they are, so "you are not linked to a merchant" tells them nothing
    they could not work out, and telling them nothing at all leaves somebody
    staring at a blank frame with no idea who to ask.
    """

    def __init__(self, code: str, message, *, status: int = 403):
        super().__init__(str(message))
        self.code = code
        self.message = message
        self.status = status


def max_session_seconds() -> int:
    return int(getattr(settings, "MERCHANT_SESSION_MAX_SECONDS", MAX_SESSION_SECONDS))


def store(request):
    """The merchant session store, or ``None`` if the middleware is absent."""
    return getattr(request, "merchant_session", None)


# ---------------------------------------------------------------------------
# Who a subject is allowed to be
# ---------------------------------------------------------------------------


def resolve_merchant(subject: str) -> Merchant:
    """The merchant this verified subject *is*, or refuse and say why.

    Four refusals, each with its own code, and the order they are tested in is
    the order that says the most: an unmatched subject is not a merchant at all,
    which is a different thing from a merchant who has been stood down.
    """
    subject = (subject or "").strip()
    merchant = (
        Merchant.objects.select_related("user").filter(b2core_id=subject).first()
        if subject
        else None
    )

    if merchant is None:
        # The ordinary case and the dangerous one at once: a perfectly valid
        # B2CORE token belonging to somebody who is simply not a merchant.
        raise MerchantEmbedRefused(
            "not_a_merchant",
            _("هذا الحساب غير مرتبط بأي تاجر في MaxPay. لوحة التاجر متاحة لحسابات التجار فقط."),
        )

    if merchant.is_archived:
        raise MerchantEmbedRefused(
            "merchant_archived",
            _("تمّت أرشفة حساب التاجر. راجع المالية."),
        )

    if not merchant.is_active:
        raise MerchantEmbedRefused(
            "merchant_suspended",
            _("حساب التاجر موقوف حاليًا. راجع المالية."),
        )

    user = merchant.user
    if user is None or not user.is_active or getattr(user, "role", None) != "merchant":
        # A merchant record Finance registered before its login account existed,
        # or one whose account was disabled. The panel is rendered *as* that
        # account — every move it records names a user — so there is nothing to
        # open without one.
        raise MerchantEmbedRefused(
            "no_login_account",
            _("هذا التاجر بلا حساب دخول فعّال. راجع المالية."),
        )

    return merchant


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def start(request, merchant: Merchant, *, subject: str, token_expires_at=None) -> Merchant:
    """Establish an embed session for a merchant already resolved from a token."""
    session = store(request)
    if session is None:
        raise RuntimeError("MerchantSessionMiddleware is not installed.")

    # Guard against session fixation: whoever held this session id before does
    # not get to keep it now that it carries an identity.
    session.cycle_key()

    now = int(timezone.now().timestamp())
    ceiling = now + max_session_seconds()
    expires_at = min(int(token_expires_at or ceiling), ceiling)

    session[SESSION_MERCHANT_ID] = merchant.pk
    session[SESSION_USER_ID] = merchant.user_id
    session[SESSION_SUBJECT] = subject
    session[SESSION_EXPIRES_AT] = expires_at
    # The panel posts forms, and Django's CSRF cookie is SameSite=Lax and so
    # never arrives inside the frame. This is what stands in for it: a token a
    # cross-site page cannot read, echoed back on every unsafe request. See
    # apps.merchant_panel.embed_auth.
    session[SESSION_CSRF] = secrets.token_urlsafe(32)

    session.set_expiry(max(0, expires_at - now))
    return merchant


def get_merchant(request) -> Merchant | None:
    """The merchant this session belongs to, or ``None``.

    Everything :func:`resolve_merchant` established at the door is established
    again here, because all of it can change between two clicks. A session whose
    ground has moved is ended rather than merely refused, so the frame goes back
    to B2CORE for a token instead of holding a key that will never work again.
    """
    session = store(request)
    if session is None:
        return None

    merchant_id = session.get(SESSION_MERCHANT_ID)
    if not merchant_id:
        return None

    expires_at = session.get(SESSION_EXPIRES_AT)
    if expires_at and int(timezone.now().timestamp()) >= int(expires_at):
        end(request)
        return None

    try:
        # Re-run the whole door test against the stored *subject*, not the
        # stored row id. A recycled primary key, a merchant re-pointed at
        # another B2CORE account, a suspension, an archive, a disabled login:
        # any of them and this session stops here.
        merchant = resolve_merchant(session.get(SESSION_SUBJECT) or "")
    except MerchantEmbedRefused as exc:
        logger.info("Merchant embed session ended: %s", exc.code)
        end(request)
        return None

    if merchant.pk != merchant_id or merchant.user_id != session.get(SESSION_USER_ID):
        # The subject still resolves, but to somebody else than the session was
        # opened for. Nothing legitimate produces this.
        logger.warning(
            "Merchant embed session no longer matches its subject; ending it."
        )
        end(request)
        return None

    return merchant


def end(request) -> None:
    session = store(request)
    if session is not None:
        session.flush()


def csrf_token(request) -> str:
    session = store(request)
    return session.get(SESSION_CSRF, "") if session is not None else ""


def expires_at(request):
    session = store(request)
    return session.get(SESSION_EXPIRES_AT) if session is not None else None
