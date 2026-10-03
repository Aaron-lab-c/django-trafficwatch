"""Who is the client, and who is never counted."""

import pytest
from conftest import wait_for_fresh_window
from django.test import Client


def test_forwarded_for_from_an_untrusted_peer_is_ignored(client):
    """A client cannot dodge the limit by sending its own X-Forwarded-For."""
    for i in range(3):
        client.get("/", HTTP_X_FORWARDED_FOR=f"198.51.100.{i}")
    assert client.get("/", HTTP_X_FORWARDED_FOR="198.51.100.9").status_code == 429


def test_forwarded_for_behind_trusted_proxy_identifies_the_real_client():
    wait_for_fresh_window()
    proxy = Client(REMOTE_ADDR="10.0.0.1")  # in TRUSTED_PROXIES
    for _ in range(3):
        proxy.get("/", HTTP_X_FORWARDED_FOR="198.51.100.1")
    assert proxy.get("/", HTTP_X_FORWARDED_FOR="198.51.100.1").status_code == 429
    assert proxy.get("/", HTTP_X_FORWARDED_FOR="198.51.100.2").status_code == 200
    # Client-prepended junk is skipped: the proxy appends the real address on the right.
    r = proxy.get("/state/", HTTP_X_FORWARDED_FOR="1.1.1.1, 198.51.100.3")
    assert r.json()["counts"] == [1]
    assert proxy.get("/state/", HTTP_X_FORWARDED_FOR="2.2.2.2, 198.51.100.3").json()["counts"] == [
        2
    ]


def test_ipv6_clients_share_a_64():
    wait_for_fresh_window()
    a = Client(REMOTE_ADDR="2001:db8:1:2::1")
    b = Client(REMOTE_ADDR="2001:db8:1:2:ffff::9")
    other = Client(REMOTE_ADDR="2001:db8:1:3::1")
    a.get("/")
    a.get("/")
    b.get("/")
    assert b.get("/").status_code == 429
    assert other.get("/").status_code == 200


def test_ipv6_prefix_can_be_disabled(tw):
    tw(IPV6_PREFIX=128)
    wait_for_fresh_window()
    a = Client(REMOTE_ADDR="2001:db8:1:2::1")
    b = Client(REMOTE_ADDR="2001:db8:1:2::2")
    for _ in range(3):
        a.get("/")
    assert a.get("/").status_code == 429
    assert b.get("/").status_code == 200


def test_exempt_paths_and_methods(client):
    for _ in range(10):
        assert client.get("/health/").status_code == 200
        assert client.options("/").status_code == 200
    assert client.get("/").status_code == 200  # nothing was counted


def test_exempt_clients(client):
    internal = Client(REMOTE_ADDR="192.0.2.7")  # in EXEMPT_CLIENTS
    for _ in range(10):
        r = internal.get("/")
        assert r.status_code == 200 and "X-RateLimit-Limit" not in r
    # ... but the allow-list cannot be forged from an untrusted peer.
    for _ in range(3):
        client.get("/", HTTP_X_FORWARDED_FOR="192.0.2.7")
    assert client.get("/", HTTP_X_FORWARDED_FOR="192.0.2.7").status_code == 429


@pytest.mark.django_db
def test_exempt_func_superusers(client, django_user_model):
    admin = django_user_model.objects.create_superuser("root", "r@x.io", "pw")
    client.force_login(admin)
    for _ in range(10):
        assert client.get("/state/").json() == {"exempt": True}
    plain = Client(REMOTE_ADDR="203.0.113.11")
    plain.force_login(django_user_model.objects.create_user("bob", password="pw"))
    for _ in range(3):
        plain.get("/")
    assert plain.get("/").status_code == 429
