"""Response security headers for every surface (spec §11).

One middleware, because there is exactly one question — *what may this page do,
and who may frame it?* — and answering it in two places is how two answers
diverge.

Three surfaces, three answers:

**The client portal** (``PORTAL_URL_PREFIX``) is framed by B2CORE, so spec §11's
``frame-ancestors`` is set to that origin and nothing else. Django's
``XFrameOptionsMiddleware`` would send ``X-Frame-Options: DENY`` alongside and
break the embed, so the header is stripped here — CSP is what browsers honour
for a *named* ancestor, and ``X-Frame-Options`` has no equivalent form.

**The internal panels** get the strictest policy in the project. They are
same-origin pages that load one stylesheet and one script from us and make no
third-party request at all, so ``default-src 'self'`` with ``script-src 'self'``
costs nothing and takes inline script off the table permanently. ``style-src``
keeps ``'unsafe-inline'`` because the templates carry ``style=""`` attributes
for row animation delays and the two-factor pages ship a ``<style>`` block;
that is a real weakening and it is worth naming, but a style attribute cannot
execute and the directive that matters here is ``script-src``.

**The Django admin** is exempt from ``script-src``. Its widgets ship inline
handlers we do not control, and a policy that breaks the admin is a policy
somebody switches off. It keeps the framing and transport headers.

This middleware must stay **early** in ``MIDDLEWARE``: the response phase runs
in reverse, so an early entry is the last to touch the headers, which is the
only position from which it can strip an ``X-Frame-Options`` that has already
been set.
"""

from django.conf import settings

#: Directives shared by every non-portal surface.
BASE_POLICY = (
    "default-src 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "frame-src 'none'",
    # Style attributes and one <style> block on the two-factor pages. See the
    # module docstring: this is the one relaxation, and it is not script.
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
)

#: Added for the panels, where we own every script on the page.
STRICT_SCRIPT = ("script-src 'self'",)

#: Added for the admin, whose own widgets we do not control.
ADMIN_SCRIPT = ("script-src 'self' 'unsafe-inline'",)


def portal_prefix() -> str:
    return getattr(settings, "PORTAL_URL_PREFIX", "/portal/")


def admin_prefix() -> str:
    return getattr(settings, "ADMIN_URL_PREFIX", "/admin/")


class SecurityHeadersMiddleware:
    """Sets the response policy for whichever surface answered the request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if request.path.startswith(portal_prefix()):
            return self._portal(response)
        return self._internal(request, response)

    # -- the embedded client portal ---------------------------------------

    @staticmethod
    def _portal(response):
        origin = getattr(settings, "B2CORE_ORIGIN", "")
        if not origin:
            # Not configured to be framed by anyone, so it is framed by nobody.
            response.headers.setdefault(
                "Content-Security-Policy", "frame-ancestors 'none'"
            )
            return response

        if "Content-Security-Policy" in response.headers:
            # A response that already declares a policy knows better than this
            # one does: the attachment view serves a client-uploaded file under
            # `default-src 'none'; sandbox`, which the page policy below would
            # silently widen back out.
            response.headers.pop("X-Frame-Options", None)
            return response

        response.headers["Content-Security-Policy"] = "; ".join(
            (
                f"frame-ancestors {origin}",
                # The embed ships one stylesheet and one script, both from our
                # own origin. Nothing else — no CDN, no inline handler.
                "default-src 'self'",
                "base-uri 'none'",
                "form-action 'self'",
                "object-src 'none'",
                f"connect-src 'self' {origin}",
                "img-src 'self' data:",
            )
        )
        response.headers.pop("X-Frame-Options", None)
        return response

    # -- the internal panels and the admin --------------------------------

    @staticmethod
    def _internal(request, response):
        if "Content-Security-Policy" in response.headers:
            # Same rule as the portal branch: a view that set its own policy —
            # the signed attachment views — is stricter than this one and stays.
            return response

        script = (
            ADMIN_SCRIPT
            if request.path.startswith(admin_prefix())
            else STRICT_SCRIPT
        )
        response.headers["Content-Security-Policy"] = "; ".join(
            (*BASE_POLICY, *script)
        )
        # Spec §11 has no line for these two, but both are free and both close
        # a class of attack the panels would otherwise be open to: a stale tab
        # keeping a reference to a window it opened, and a browser guessing a
        # content type we already declared.
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response
