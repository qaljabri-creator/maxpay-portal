"""Start-up checks for the B2CORE embed.

Each of these is a mistake that is silent at run time and expensive later: a
cookie the browser quietly drops, a token accepted from the wrong issuer, an
HMAC algorithm that turns a published public key into a signing key.

They fall into two groups, and the split matters:

* **Always** — things that are wrong no matter where the code runs. A cookie
  name collision or a symmetric signing algorithm is a bug in the settings, not
  a property of the environment, so it fails every ``manage.py check``.
* **Deploy only** (``manage.py check --deploy``) — things that are only wrong in
  a real deployment. A developer building the Finance panel has no reason to
  hold B2CORE credentials, and refusing to start without them would just teach
  everyone to silence checks.

A check is also a thing that can be wrong, and two of these were. ``W005`` and
``W006`` warned that the issuer and audience were unverified and told the
operator to set both "once B2CORE confirms the value". When a real token was
finally examined (4 Sep 2026) it turned out to carry no ``aud`` at all, so the
second hint was an instruction to break the product: an operator clearing
warnings before go-live would have refused every client. They are now
``E005`` and ``E006``, the audience one fires on the opposite condition, and
both are errors because either state stops clients signing in or lets the wrong
signer in.

The renumbering is deliberate rather than tidy. ``W006`` meant "you have not set
the audience"; ``E006`` means "you have set it and must not". Anything that had
silenced the old id would otherwise have silenced its opposite.
"""

from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

#: Anything symmetric — or absent — would let a holder of the *public* key mint
#: tokens we accept. Spec §4 verifies signatures against a JWKS, which is only
#: meaningful for asymmetric algorithms.
FORBIDDEN_ALGORITHMS = {"none", "HS256", "HS384", "HS512"}


def _origin_is_wellformed(origin: str) -> bool:
    parts = urlsplit(origin)
    return bool(parts.scheme in {"http", "https"} and parts.netloc and not parts.path)


# ---------------------------------------------------------------------------
# Always
# ---------------------------------------------------------------------------


@register(Tags.security)
def check_b2core_settings(app_configs, **kwargs):
    issues = []

    origin = getattr(settings, "B2CORE_ORIGIN", "")
    if origin and not _origin_is_wellformed(origin):
        issues.append(
            Error(
                f"B2CORE_ORIGIN={origin!r} is not a bare origin.",
                hint="It must be scheme://host[:port] with no path, e.g. "
                "https://portal.example.com — postMessage and CSP both require "
                "that form, and a mismatch there silently blocks the handshake.",
                id="portal.E002",
            )
        )

    algorithms = set(getattr(settings, "B2CORE_JWT_ALGORITHMS", []))
    if not algorithms:
        issues.append(
            Error(
                "B2CORE_JWT_ALGORITHMS is empty; every token would be rejected.",
                id="portal.E003",
            )
        )
    forbidden = algorithms & FORBIDDEN_ALGORITHMS
    if forbidden:
        issues.append(
            Error(
                f"B2CORE_JWT_ALGORITHMS contains {sorted(forbidden)}.",
                hint="Symmetric algorithms and 'none' let anyone holding the public "
                "JWKS key mint tokens this deployment would accept. Use RS*/ES* only.",
                id="portal.E004",
            )
        )

    return issues


@register(Tags.security)
def check_portal_session_cookie(app_configs, **kwargs):
    """The two sessions must stay two sessions.

    The portal cookie is deliberately weaker than the internal one — it has to
    be, to exist at all inside a third-party iframe. That is precisely why it
    must never *be* the internal one.
    """
    issues = []
    portal_name = getattr(settings, "PORTAL_SESSION_COOKIE_NAME", "")
    internal_name = getattr(settings, "SESSION_COOKIE_NAME", "")
    samesite = str(getattr(settings, "PORTAL_SESSION_COOKIE_SAMESITE", "")).lower()
    secure = getattr(settings, "PORTAL_SESSION_COOKIE_SECURE", False)

    if not portal_name:
        issues.append(Error("PORTAL_SESSION_COOKIE_NAME is unset.", id="portal.E010"))
    elif portal_name == internal_name:
        issues.append(
            Error(
                "PORTAL_SESSION_COOKIE_NAME is the same as SESSION_COOKIE_NAME.",
                hint="The client session is SameSite=None because it lives in an "
                "iframe; sharing its name with the internal panel's session would "
                "hand that relaxation to the internal panel too.",
                id="portal.E011",
            )
        )

    if samesite == "none" and not secure:
        issues.append(
            Error(
                "PORTAL_SESSION_COOKIE_SAMESITE='None' requires "
                "PORTAL_SESSION_COOKIE_SECURE=True.",
                hint="Browsers drop a SameSite=None cookie that is not Secure, so no "
                "client would ever hold a session. Serve the portal over HTTPS.",
                id="portal.E012",
            )
        )

    return issues


# ---------------------------------------------------------------------------
# Deploy only — `manage.py check --deploy`
# ---------------------------------------------------------------------------


@register(Tags.security, deploy=True)
def check_b2core_deployment(app_configs, **kwargs):
    issues = []

    missing = ", ".join(
        name
        for name in ("B2CORE_ORIGIN", "B2CORE_JWKS_URL")
        if not getattr(settings, name, "")
    )
    if missing:
        issues.append(
            Error(
                f"B2CORE is not configured: {missing} unset.",
                hint="Client authentication cannot work, and the portal will refuse "
                "to be framed. Set them in the environment — see .env.example.",
                id="portal.E001",
            )
        )

    # An Error, not a Warning, and not the same shape it used to be. Both of
    # these were warnings that read as "you have not finished configuring
    # this yet"; one of them turned out to be advice that breaks the product.
    issuer = str(getattr(settings, "B2CORE_JWT_ISSUER", "") or "")
    origin = str(getattr(settings, "B2CORE_ORIGIN", "") or "")
    if not issuer:
        issues.append(
            Error(
                "B2CORE_JWT_ISSUER is unset, so the 'iss' claim is not verified.",
                hint="Any correctly-signed token is accepted regardless of who "
                "issued it — including one signed by another key in the same "
                "JWKS. Set it to the literal issuer B2CORE mints, trailing "
                "slash included; base.py carries the value.",
                id="portal.E005",
            )
        )
    elif origin and issuer.rstrip("/") == origin.rstrip("/"):
        issues.append(
            Error(
                f"B2CORE_JWT_ISSUER is the portal origin ({origin}).",
                hint="The issuer is B2CORE's auth service, not the site it "
                "frames. They look like they should be the same string and are "
                "not: the issuer is on api.* with a path. No client could "
                "authenticate against this.",
                id="portal.E005",
            )
        )

    # This one is inverted from what it was, and the inversion is the point.
    # It used to warn when the audience was *unset* and tell the operator to
    # "set it once B2CORE confirms the audience value". B2CORE has now been
    # observed: it mints no `aud` at all. The old hint was an instruction to
    # break the integration, and tidying the warning away would have taken the
    # portal down for every client.
    if getattr(settings, "B2CORE_JWT_AUDIENCE", ""):
        issues.append(
            Error(
                "B2CORE_JWT_AUDIENCE is set, and B2CORE sends no 'aud' claim.",
                hint="Setting it turns on PyJWT's audience check, which then "
                "refuses every real token for a claim that is never minted — no "
                "client can sign in. Leave it empty. The issuer check is what "
                "establishes who signed the token.",
                id="portal.E006",
            )
        )

    samesite = str(getattr(settings, "PORTAL_SESSION_COOKIE_SAMESITE", "")).lower()
    if samesite != "none" and getattr(settings, "B2CORE_ORIGIN", ""):
        issues.append(
            Warning(
                f"PORTAL_SESSION_COOKIE_SAMESITE={samesite!r} will not be sent inside "
                "the B2CORE iframe.",
                hint="Only 'None' survives a third-party context. Anything else means "
                "every client request arrives without a session.",
                id="portal.W013",
            )
        )

    if str(getattr(settings, "SESSION_COOKIE_SAMESITE", "")).lower() == "none":
        issues.append(
            Warning(
                "SESSION_COOKIE_SAMESITE is 'None' for the internal panel.",
                hint="The internal session should stay 'Lax'; the embed has its own "
                "cookie precisely so this one does not have to be relaxed.",
                id="portal.W014",
            )
        )

    return issues
