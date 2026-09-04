"""Portal request plumbing: the client on the request, and the portal language.

The iframe headers spec §11 asks for used to live here too. They moved to
:mod:`apps.core.middleware` in build-order step 14, when the internal panels
grew a policy of their own: "who may frame this, and what may it load" is one
question with three answers, and answering it in two modules is how two answers
diverge. What stayed here is what is genuinely portal-specific — the client on
the request, and the language B2CORE announced.
"""

from django.conf import settings
from django.utils import translation
from django.utils.functional import SimpleLazyObject

from . import session


def portal_prefix() -> str:
    return getattr(settings, "PORTAL_URL_PREFIX", "/portal/")


class ClientSessionMiddleware:
    """Attaches ``request.portal_client`` — the B2CORE client, or ``None``.

    Lazy, so a request that never looks at it costs no query. Kept entirely
    separate from ``request.user``: an internal staff session and a client
    session are different things and must never be confused for one another.
    Use :func:`apps.portal.session.current_client` to read it — the lazy wrapper
    around ``None`` is not itself ``None``.

    It also settles the language for portal requests. ``LocaleMiddleware``
    decides from the ``django_language`` cookie, which is ``SameSite=Lax`` and
    so never reaches us inside the iframe; what B2CORE announced over
    ``embed-language-change`` lives in the portal session instead.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.portal_client = SimpleLazyObject(lambda: session.get_client(request))

        if not request.path.startswith(portal_prefix()):
            return self.get_response(request)

        language = session.normalise_language(session.get_language(request))
        if not language:
            return self.get_response(request)

        with translation.override(language):
            request.LANGUAGE_CODE = language
            response = self.get_response(request)
        response.headers.setdefault("Content-Language", language)
        return response
