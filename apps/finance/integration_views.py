"""Finance panel — the B2CORE integration, read-only (spec §4, §9).

The screen exists for one question, asked by whoever is holding the phone when a
client says the portal will not load: *is the B2CORE side of this working, and
what is it pointed at?* Until now the only answers were the server's environment
and its log file, neither of which Finance can reach.

Two rules shape the whole module.

**It is read-only, and not by convention.** There is no form here, no POST, and
no route that writes — the same way the audit viewer is read-only, and for a
related reason. This configuration lives in the environment because changing it
is a deployment act: it decides who may frame the portal and whose tokens are
believed, it must be reviewed and rolled back like code, and it must be
identical across every worker. A text box on a web page is none of those things.
So the screen reports and refuses to edit, and says as much on its face rather
than leaving an operator hunting for a save button that was never there.

**It never touches the network.** Everything shown is either a setting already
in memory or an observation :mod:`apps.portal.b2core.jwks` recorded while
serving real traffic. A status page that probes on load is one that can be
refreshed until it takes the endpoint down, and it would end up reporting on its
own probe rather than on the path clients actually travel.
"""

from django.conf import settings
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from apps.portal.b2core import jwks

from .mixins import FinancePanelMixin


def _setting(name: str) -> str:
    return str(getattr(settings, name, "") or "")


class B2CoreIntegrationView(FinancePanelMixin, TemplateView):
    """Current B2CORE settings and what the key lookup has been doing.

    Behind the Finance role and nothing further. Every value on the page is a
    public identifier — an endpoint URL that serves public keys, the issuer and
    audience strings that appear in every token — and none of it is a
    credential; the signing keys are B2CORE's and are never held here. Set
    ``permission_required`` with :class:`FinanceWriteMixin`'s sibling gate if a
    deployment would rather keep it to ``finance_admin``, but weigh that against
    who is actually fielding "تعذّر الاتصال" at four in the afternoon.
    """

    template_name = "finance/b2core_integration.html"
    nav_section = "integration"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        origin = _setting("B2CORE_ORIGIN")
        jwks_url = _setting("B2CORE_JWKS_URL")
        issuer = _setting("B2CORE_JWT_ISSUER")
        audience = _setting("B2CORE_JWT_AUDIENCE")

        context["b2core"] = {
            "origin": origin,
            "jwks_url": jwks_url,
            "issuer": issuer,
            "audience": audience,
            "algorithms": list(getattr(settings, "B2CORE_JWT_ALGORITHMS", [])),
            "leeway_seconds": getattr(settings, "B2CORE_JWT_LEEWAY_SECONDS", 30),
            # Shown beside the status because the status cannot be read without
            # it: a success recorded four minutes ago against a ten-minute cache
            # says nothing at all about whether B2CORE is reachable right now.
            "cache_seconds": getattr(settings, "B2CORE_JWKS_CACHE_SECONDS", 600),
            "timeout_seconds": getattr(settings, "B2CORE_JWKS_TIMEOUT_SECONDS", 5),
        }

        # The handshake needs both to work at all: no URL and no token can be
        # verified, no origin and the portal refuses to be framed.
        context["b2core_configured"] = bool(origin and jwks_url)

        # The same two gaps `apps.portal.checks` raises under `check --deploy`,
        # said here to the people who would notice. They are not errors — a
        # correctly-signed token is still required — but each one widens *whose*
        # correctly-signed token this deployment will believe, which is not a
        # thing to discover from a checklist nobody ran.
        gaps = []
        if not issuer:
            gaps.append(
                _(
                    "لم يُضبط issuer: يُقبل أي رمز موقّع توقيعًا صحيحًا مهما كانت الجهة "
                    "التي أصدرته."
                )
            )
        if not audience:
            gaps.append(
                _(
                    "لم يُضبط audience: يُقبل رمز أصدره B2CORE لجهة أخرى غير هذه البوابة."
                )
            )
        context["b2core_gaps"] = gaps

        context["jwks_status"] = jwks.status()
        return context
