"""Whose address a request is from, and how many proxies are believed.

``client_ip`` feeds the internal login lockout and the portal's rate limiter,
both of which count per address. It used to take the left-most entry of
``X-Forwarded-For`` — the one the caller writes — so a caller could name a new
address on every attempt and never be counted twice.
"""

from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.accounts import throttling
from apps.core.services import client_ip

CLIENT = "203.0.113.7"
SPOOFED = "198.51.100.1"
NGINX = "10.0.0.2"
BALANCER = "10.0.0.3"


def request_from(remote=NGINX, forwarded=None):
    headers = {"REMOTE_ADDR": remote}
    if forwarded is not None:
        headers["HTTP_X_FORWARDED_FOR"] = forwarded
    return RequestFactory().get("/", **headers)


class NoTrustedProxyTests(SimpleTestCase):
    """The default: nothing in front of us is believed."""

    def test_the_default_is_zero(self):
        from django.conf import settings

        self.assertEqual(settings.TRUSTED_PROXY_COUNT, 0)

    def test_remote_addr_is_the_client(self):
        self.assertEqual(client_ip(request_from(remote=CLIENT)), CLIENT)

    def test_the_header_is_ignored_entirely(self):
        request = request_from(remote=CLIENT, forwarded=f"{SPOOFED}, {NGINX}")
        self.assertEqual(client_ip(request), CLIENT)


@override_settings(TRUSTED_PROXY_COUNT=1)
class OneProxyTests(SimpleTestCase):
    """nginx in front: it appends the address it saw, and that is the client."""

    def test_the_right_most_entry_is_the_client(self):
        request = request_from(forwarded=f"{SPOOFED}, {CLIENT}")
        self.assertEqual(client_ip(request), CLIENT)

    def test_a_forged_left_entry_is_not_believed(self):
        """The attack the old left-most rule allowed."""
        request = request_from(forwarded=f"{SPOOFED}, {CLIENT}")
        self.assertNotEqual(client_ip(request), SPOOFED)

    def test_a_header_nginx_overwrote_works_the_same(self):
        """``proxy_set_header X-Forwarded-For $remote_addr`` leaves one entry."""
        self.assertEqual(client_ip(request_from(forwarded=CLIENT)), CLIENT)

    def test_no_header_falls_back_to_remote_addr(self):
        self.assertEqual(client_ip(request_from()), NGINX)

    def test_a_non_address_falls_back_to_remote_addr(self):
        for junk in ("not-an-ip", "unknown", "1.2.3.4.5", "<script>"):
            with self.subTest(junk=junk):
                self.assertEqual(client_ip(request_from(forwarded=junk)), NGINX)

    def test_ipv6_is_an_address(self):
        self.assertEqual(
            client_ip(request_from(forwarded="2001:db8::1")), "2001:db8::1"
        )

    def test_spaces_and_empty_hops_are_not_hops(self):
        request = request_from(forwarded=f" {SPOOFED} ,, {CLIENT} ")
        self.assertEqual(client_ip(request), CLIENT)


@override_settings(TRUSTED_PROXY_COUNT=2)
class TwoProxyTests(SimpleTestCase):
    """A balancer in front of nginx: the client is second from the right."""

    def test_the_second_from_the_right_is_the_client(self):
        request = request_from(forwarded=f"{SPOOFED}, {CLIENT}, {BALANCER}")
        self.assertEqual(client_ip(request), CLIENT)

    def test_a_header_too_short_for_the_proxies_is_not_believed(self):
        """One entry where two proxies should each have added one: it did not
        come the way we were told requests come."""
        self.assertEqual(client_ip(request_from(forwarded=CLIENT)), NGINX)


class MisconfiguredCountTests(SimpleTestCase):
    @override_settings(TRUSTED_PROXY_COUNT=-1)
    def test_a_negative_count_trusts_nothing(self):
        request = request_from(remote=CLIENT, forwarded=SPOOFED)
        self.assertEqual(client_ip(request), CLIENT)


class TheLimitersUseItTests(SimpleTestCase):
    """The two counters that made this matter read the same rule."""

    def test_the_login_lockout_cannot_be_dodged_by_rotating_the_header(self):
        seen = {
            throttling.client_ip(request_from(remote=CLIENT, forwarded=f"10.9.9.{n}"))
            for n in range(20)
        }
        self.assertEqual(seen, {CLIENT})

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_nor_behind_a_proxy(self):
        seen = {
            throttling.client_ip(request_from(forwarded=f"10.9.9.{n}, {CLIENT}"))
            for n in range(20)
        }
        self.assertEqual(seen, {CLIENT})
