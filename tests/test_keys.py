from types import SimpleNamespace

import pytest
from django.test import RequestFactory

from django_trafficwatch.keys import (
    client_ip,
    is_exempt_client,
    normalise_ip,
    resolve_client_ip,
    safe_key_part,
    user_or_ip,
)


def test_forwarded_for_ignored_by_default():
    """A client must not be able to pick its own identity by sending X-Forwarded-For."""
    req = RequestFactory().get("/", REMOTE_ADDR="9.9.9.9", HTTP_X_FORWARDED_FOR="1.2.3.4")
    assert client_ip(req) == "9.9.9.9"


def test_forwarded_for_honoured_behind_trusted_proxy(tw):
    tw(TRUSTED_PROXIES=["10.0.0.0/8"])
    req = RequestFactory().get(
        "/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="1.2.3.4, 10.0.0.7"
    )
    # 10.0.0.7 is another trusted hop, 1.2.3.4 is the first untrusted one.
    assert client_ip(req) == "1.2.3.4"


def test_forwarded_for_spoofed_prefix_is_ignored(tw):
    """Client sends its own XFF; the proxy appends the real address. We take the real one."""
    tw(TRUSTED_PROXIES=["10.0.0.1"])
    req = RequestFactory().get(
        "/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="8.8.8.8, 203.0.113.5"
    )
    assert client_ip(req) == "203.0.113.5"


def test_forwarded_for_from_untrusted_peer_is_ignored(tw):
    tw(TRUSTED_PROXIES=["10.0.0.1"])
    req = RequestFactory().get("/", REMOTE_ADDR="9.9.9.9", HTTP_X_FORWARDED_FOR="1.2.3.4")
    assert client_ip(req) == "9.9.9.9"


def test_trusted_proxy_without_header_falls_back_to_peer(tw):
    tw(TRUSTED_PROXIES=["10.0.0.1"])
    req = RequestFactory().get("/", REMOTE_ADDR="10.0.0.1")
    assert client_ip(req) == "10.0.0.1"


def test_trusted_proxy_ipv6(tw):
    tw(TRUSTED_PROXIES=["fd00::/8"])
    req = RequestFactory().get("/", REMOTE_ADDR="fd00::1", HTTP_X_FORWARDED_FOR="2001:db8::9")
    assert resolve_client_ip(req) == "2001:db8::9"
    assert client_ip(req) == "2001:db8::/64"  # keyed on the /64 by default


# -- IPv6 prefix keying --------------------------------------------------------------


def test_ipv6_clients_share_their_64():
    a = RequestFactory().get("/", REMOTE_ADDR="2001:db8:1:2:aaaa::1")
    b = RequestFactory().get("/", REMOTE_ADDR="2001:db8:1:2:ffff:ffff:ffff:ffff")
    c = RequestFactory().get("/", REMOTE_ADDR="2001:db8:1:3::1")
    assert client_ip(a) == client_ip(b) == "2001:db8:1:2::/64"
    assert client_ip(c) == "2001:db8:1:3::/64"


def test_ipv6_prefix_is_configurable(tw):
    req = RequestFactory().get("/", REMOTE_ADDR="2001:db8:1:2:aaaa::1")
    tw(IPV6_PREFIX=48)
    assert client_ip(req) == "2001:db8:1::/48"
    tw(IPV6_PREFIX=128)
    assert client_ip(req) == "2001:db8:1:2:aaaa::1"


def test_ipv4_and_mapped_ipv4_untouched():
    assert client_ip(RequestFactory().get("/", REMOTE_ADDR="203.0.113.7")) == "203.0.113.7"
    mapped = RequestFactory().get("/", REMOTE_ADDR="::ffff:203.0.113.7")
    assert client_ip(mapped) == "203.0.113.7"
    assert normalise_ip("not-an-ip", 64) == "not-an-ip"


# -- exempt clients ------------------------------------------------------------------


def test_exempt_clients_match_resolved_ip(tw):
    tw(EXEMPT_CLIENTS=["192.0.2.0/24", "2001:db8::1"])
    assert is_exempt_client(RequestFactory().get("/", REMOTE_ADDR="192.0.2.9"))
    assert is_exempt_client(RequestFactory().get("/", REMOTE_ADDR="2001:db8::1"))
    assert not is_exempt_client(RequestFactory().get("/", REMOTE_ADDR="192.0.3.9"))
    assert not is_exempt_client(RequestFactory().get("/", REMOTE_ADDR="2001:db8::2"))


def test_exempt_clients_cannot_be_forged_via_forwarded_for(tw):
    tw(EXEMPT_CLIENTS=["192.0.2.0/24"])
    req = RequestFactory().get("/", REMOTE_ADDR="9.9.9.9", HTTP_X_FORWARDED_FOR="192.0.2.1")
    assert not is_exempt_client(req)
    tw(EXEMPT_CLIENTS=["192.0.2.0/24"], TRUSTED_PROXIES=["9.9.9.9"])
    assert is_exempt_client(req)


def test_client_ip_falls_back_to_remote_addr():
    req = RequestFactory().get("/", REMOTE_ADDR="9.9.9.9")
    assert client_ip(req) == "9.9.9.9"


def test_user_or_ip():
    req = RequestFactory().get("/", REMOTE_ADDR="9.9.9.9")
    assert user_or_ip(req) == "ip:9.9.9.9"
    req.user = SimpleNamespace(is_authenticated=True, pk=42)
    assert user_or_ip(req) == "user:42"
    req.user = SimpleNamespace(is_authenticated=False, pk=None)
    assert user_or_ip(req) == "ip:9.9.9.9"


@pytest.mark.parametrize("value", ["ip:1.2.3.4", "user:42", "api-key_ABC.def"])
def test_safe_key_part_keeps_plain_values(value):
    assert safe_key_part(value) == value


@pytest.mark.parametrize("value", ["has space", "tab\tchar", "ctrl\x01", "x" * 300])
def test_safe_key_part_hashes_unsafe_values(value):
    out = safe_key_part(value)
    assert out.startswith("h:") and len(out) == 34
    assert " " not in out
    assert safe_key_part(value) == out  # deterministic
