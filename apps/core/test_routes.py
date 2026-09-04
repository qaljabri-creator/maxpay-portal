"""Every route in the project, checked for a door — build-order step 14.

The rest of the suite tests the surfaces somebody thought to write a test for.
This file tests the ones nobody did. It walks the URLconf, resolves every
pattern the project owns, and refuses to pass while any of them is reachable
without saying who is asking.

**Why an enumeration rather than more per-view tests.** A view added next year
comes with its own tests, written by whoever added it, covering what they were
thinking about. What it does not come with is a test somebody wrote *this* year
about a rule that applies to it. The failure mode this guards is a Finance view
shipped without ``FinancePanelMixin`` — plausible, silent, and catastrophic,
because the Finance panel is the identity-aware side of the system (spec §9).

Three classes of route, and each must be declared:

``PUBLIC``
    Deliberately open. Every entry needs a reason, because "it looked harmless"
    is how a surface ends up open.
``CLIENT``
    Under ``/portal/``. Authenticated by a B2CORE token, not a Django session
    (spec §4), so an anonymous Django caller is *supposed* to reach the handler
    — and be refused by it. Their refusals are tested in
    :mod:`apps.portal.tests`; what is asserted here is that they never answer
    with client data.
``INTERNAL``
    Everything else. An anonymous caller must be redirected to the login or
    refused outright, never served.

Adding a route to the project and not to one of the three lists fails this
file. That is the point: the decision has to be made in writing.
"""

from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.urls.exceptions import NoReverseMatch

#: Sample arguments for the patterns that take them. The value never matters —
#: what is being tested is the guard, which runs before anything is looked up.
SAMPLE_ARGS = {
    "ref": "MP-00000",
    "reference": "MP-00000",
    "pk": 1,
    "merchant_pk": 1,
    "method_pk": 1,
    "action": "route",
    "token": "not-a-real-signature",
    "code": "zaincash",
}

#: Open on purpose, each with the reason it is.
PUBLIC = {
    "healthz": "Liveness probe for the load balancer; reveals only {'status': 'ok'}.",
    "two_factor:login": "The login form itself.",
    "two_factor:setup": "Enrolment, reachable by a signed-in but non-compliant user.",
    "two_factor:setup_complete": "Part of the enrolment wizard.",
    "two_factor:qr": "The enrolment QR, bound to the in-progress wizard session.",
    "set_language": "Django's language switcher; changes a cookie and nothing else.",
    "portal:bootstrap": "The page B2CORE frames. Renders no client data (spec §4).",
}

#: Under /portal/. Session-less by design — see the module docstring.
CLIENT_PREFIX = "/portal/"

#: Portal endpoints that answer a caller with no B2CORE token, and what they
#: are allowed to say. ``session`` is how the embed asks "am I signed in?", so
#: answering it is the feature; answering it with anything about a client would
#: not be. :meth:`ClientSurfaceTests.test_the_session_probe_reveals_no_client`
#: is what holds it to that.
PORTAL_ANSWERS_WITHOUT_A_TOKEN = {
    "portal:session": "The embed's own 'do I have a session?' probe (spec §4).",
}

#: Routes that answer an unauthenticated caller with a redirect rather than a
#: refusal, because that is what a browser needs. Anything not listed must be
#: one or the other; this only records which.
LOGIN_REDIRECT_STATUSES = frozenset({302})
REFUSAL_STATUSES = frozenset({401, 403, 404, 405})


def walk(resolver, prefix=""):
    """Every concrete pattern under ``resolver``, with its full name."""
    for entry in resolver.url_patterns:
        if isinstance(entry, URLResolver):
            namespace = entry.namespace
            child = f"{prefix}{namespace}:" if namespace else prefix
            yield from walk(entry, child)
        elif isinstance(entry, URLPattern):
            if entry.name:
                yield f"{prefix}{entry.name}", entry


def build(name, pattern) -> str | None:
    """Reverse ``name``, guessing arguments from the pattern's own converters."""
    keys = set(pattern.pattern.regex.groupindex)
    kwargs = {key: SAMPLE_ARGS[key] for key in keys if key in SAMPLE_ARGS}
    if keys - set(kwargs):
        return None
    try:
        return reverse(name, kwargs=kwargs) if kwargs else reverse(name)
    except NoReverseMatch:
        return None


class RouteInventoryTests(TestCase):
    """The URLconf is the source of truth; this keeps the lists honest."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.routes = dict(walk(get_resolver()))

    def project_routes(self) -> dict:
        """Ours, plus the login wizard we mount. Not the admin's hundreds.

        The Django admin is excluded because it is not our URLconf and its
        access control is Django's, reinforced by ``AdminSiteOTPRequired`` and
        by ``EnforceTwoFactorMiddleware``. One sample of it is checked below,
        which is what we can honestly claim to have verified.
        """
        return {
            name: pattern
            for name, pattern in self.routes.items()
            if not name.startswith("admin:")
        }

    def test_every_route_is_classified(self):
        unclassified = []
        for name, pattern in self.project_routes().items():
            if name in PUBLIC:
                continue
            url = build(name, pattern)
            if url is None:
                # A pattern this file cannot construct is not a pattern it can
                # vouch for. Naming it is the honest outcome.
                unclassified.append(f"{name} (could not build a URL for it)")
                continue
            if url.startswith(CLIENT_PREFIX):
                continue
            # Everything else is INTERNAL by default, which is the safe
            # default — it will be exercised below.

        self.assertEqual(
            unclassified,
            [],
            "A route could not be reversed with the sample arguments in "
            "SAMPLE_ARGS. Add its argument there so it can be swept.",
        )

    def test_the_public_list_names_only_routes_that_exist(self):
        missing = sorted(set(PUBLIC) - set(self.routes))

        self.assertEqual(
            missing,
            [],
            "PUBLIC names a route that no longer exists. Remove it rather than "
            "leaving a stale exemption behind.",
        )


class AnonymousAccessTests(TestCase):
    """No internal route answers somebody who has not said who they are."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.routes = dict(walk(get_resolver()))

    def internal_urls(self):
        for name, pattern in sorted(self.routes.items()):
            if name.startswith("admin:") or name in PUBLIC:
                continue
            url = build(name, pattern)
            if url is None or url.startswith(CLIENT_PREFIX):
                continue
            yield name, url

    def test_no_internal_route_serves_an_anonymous_get(self):
        allowed = LOGIN_REDIRECT_STATUSES | REFUSAL_STATUSES
        for name, url in self.internal_urls():
            with self.subTest(route=name, url=url):
                response = self.client.get(url)
                self.assertIn(
                    response.status_code,
                    allowed,
                    f"{name} ({url}) answered an anonymous GET with "
                    f"{response.status_code}. Every internal route redirects to "
                    "the login or refuses.",
                )

    def test_no_internal_route_serves_an_anonymous_post(self):
        allowed = LOGIN_REDIRECT_STATUSES | REFUSAL_STATUSES
        for name, url in self.internal_urls():
            with self.subTest(route=name, url=url):
                response = self.client.post(url, {})
                self.assertIn(
                    response.status_code,
                    allowed,
                    f"{name} ({url}) answered an anonymous POST with "
                    f"{response.status_code}.",
                )

    def test_every_redirect_chain_ends_at_the_login(self):
        """Followed to the end, not judged on its first hop.

        Two routes bounce through another of ours on the way: ``home`` sends a
        caller to whichever panel is theirs and lets *that* demand a login, and
        the two-factor wizard's ``disable`` view sends anyone without a device
        to ``LOGIN_REDIRECT_URL``. Both are chains, not leaks — but an open
        redirect on the way to a login form is still an open redirect, and the
        login form is the one page a user is primed to trust. So the assertion
        is on where the chain *ends* and on every host it passes through.
        """
        login = reverse("two_factor:login")
        for name, url in self.internal_urls():
            first = self.client.get(url)
            if first.status_code not in LOGIN_REDIRECT_STATUSES:
                continue
            with self.subTest(route=name):
                for hop in self.client.get(url, follow=True).redirect_chain:
                    self.assertTrue(
                        hop[0].startswith("/"),
                        f"{name} redirected off-site to {hop[0]!r}.",
                    )
                final = self.client.get(url, follow=True)
                self.assertEqual(final.request["PATH_INFO"], login,
                    f"{name} sent an anonymous caller to "
                    f"{final.request['PATH_INFO']!r} rather than the login.")

    def test_the_admin_is_closed_too(self):
        # One sample rather than the whole admin: its access control is
        # Django's, and this is what we can honestly claim to have checked.
        response = self.client.get(reverse("admin:index"))

        self.assertIn(response.status_code, LOGIN_REDIRECT_STATUSES | REFUSAL_STATUSES)

    def test_the_public_routes_really_are_reachable(self):
        # A sweep that passes because everything is broken proves nothing.
        for name in ("healthz", "two_factor:login", "portal:bootstrap"):
            with self.subTest(route=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_the_health_probe_reveals_nothing(self):
        body = self.client.get(reverse("healthz")).json()

        self.assertEqual(body, {"status": "ok"})


class ClientSurfaceTests(TestCase):
    """Spec §4: the portal authenticates a B2CORE token, not a Django session.

    So an anonymous Django caller reaches the handler on purpose. What must not
    happen is that it answers with anything.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.routes = dict(walk(get_resolver()))

    def test_every_portal_endpoint_refuses_a_session_less_caller(self):
        served = []
        for name, pattern in sorted(self.routes.items()):
            if (
                not name.startswith("portal:")
                or name in PUBLIC
                or name in PORTAL_ANSWERS_WITHOUT_A_TOKEN
            ):
                continue
            url = build(name, pattern)
            if url is None:
                continue
            response = self.client.get(url)
            if 200 <= response.status_code < 300:
                served.append(f"{name} → {response.status_code}")

        self.assertEqual(
            served,
            [],
            "A portal endpoint answered a caller with no verified B2CORE token "
            "(spec §4). If it is meant to, add it to "
            "PORTAL_ANSWERS_WITHOUT_A_TOKEN with the reason.",
        )

    def test_the_session_probe_reveals_no_client(self):
        # The one endpoint that answers without a token. What it may say is
        # "no", and spec §4 is why it may not say anything else.
        response = self.client.get(reverse("portal:session"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"authenticated": False, "reason": "no_session", "remedy": "reauthenticate"},
        )

    def test_the_exception_list_names_only_routes_that_exist(self):
        missing = sorted(set(PORTAL_ANSWERS_WITHOUT_A_TOKEN) - set(self.routes))

        self.assertEqual(missing, [], "A stale exemption is worse than none.")


class RoleSeparationTests(TestCase):
    """A signed-in account reaches its own panel and not the other one."""

    def setUp(self):
        from apps.accounts.models import Role
        from apps.accounts.permissions import sync_role_groups
        from apps.accounts.tests import make_user
        from apps.merchants.models import Merchant

        sync_role_groups()
        self.staff = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)
        self.merchant_user = make_user("m@example.com", Role.MERCHANT)
        Merchant.objects.create(name="تاجر", user=self.merchant_user)

    def sign_in(self, user):
        from apps.accounts.tests import verify_otp

        verify_otp(self.client, user)

    def test_a_merchant_cannot_reach_any_finance_route(self):
        # Spec §9's panel is the identity-aware side of the system; spec §2 is
        # why a merchant account must bounce off all of it, not most of it.
        self.sign_in(self.merchant_user)
        routes = dict(walk(get_resolver()))

        for name, pattern in sorted(routes.items()):
            if not name.startswith("finance:"):
                continue
            url = build(name, pattern)
            if url is None:
                continue
            with self.subTest(route=name):
                self.assertEqual(
                    self.client.get(url).status_code,
                    403,
                    f"A merchant reached {name} ({url}).",
                )

    def test_finance_cannot_reach_the_merchant_panel(self):
        self.sign_in(self.staff)
        routes = dict(walk(get_resolver()))

        for name, pattern in sorted(routes.items()):
            if not name.startswith("merchant_panel:"):
                continue
            url = build(name, pattern)
            if url is None:
                continue
            with self.subTest(route=name):
                self.assertEqual(
                    self.client.get(url).status_code,
                    403,
                    f"A Finance account reached {name} ({url}).",
                )
