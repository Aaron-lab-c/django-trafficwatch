"""Production-safety behaviour: cache outages, lockout, unrouted requests, bad hooks and
bad configuration."""

import logging
import time

import pytest
from conftest import new_client, wait_for_fresh_window
from django.core.exceptions import ImproperlyConfigured
from django.test import Client
from project import alerts

# -- cache outage ---------------------------------------------------------------------


def test_fail_open_when_redis_is_unreachable(tw, caplog):
    tw(CACHE_ALIAS="unreachable")  # redis://127.0.0.1:1 -> connection refused
    c = new_client(REMOTE_ADDR="203.0.113.20")
    with caplog.at_level(logging.ERROR, logger="django_trafficwatch"):
        for _ in range(10):
            r = c.get("/state/")
            assert r.status_code == 200
            assert "X-RateLimit-Limit" not in r
    assert r.json()["degraded"] is True and r.json()["blocked"] is False
    outage = [rec for rec in caplog.records if "unavailable" in rec.getMessage()]
    assert len(outage) == 1  # throttled, with traceback
    assert outage[0].exc_info is not None


def test_fail_closed_when_configured(tw):
    tw(CACHE_ALIAS="unreachable", FAIL_OPEN=False)
    c = new_client(REMOTE_ADDR="203.0.113.21")
    r = c.get("/")
    assert r.status_code == 429 and r["Retry-After"] == "2"


# -- escalating lockout -----------------------------------------------------------------


def test_lockout(tw):
    tw(LOCKOUT={"VIOLATIONS": 2, "WINDOW_SECONDS": 600, "DURATION_SECONDS": 2})
    wait_for_fresh_window()
    c = Client(REMOTE_ADDR="203.0.113.30")
    # violation 1: the global rule
    for _ in range(3):
        c.get("/")
    assert c.get("/").status_code == 429
    assert alerts.NOTIFIED[-1]["lockout"]["violations"] == 1
    assert alerts.NOTIFIED[-1]["lockout"]["active"] is False
    # violation 2: a different rule -> locked for 2 seconds. Retry-After is the longer of
    # the hourly rule's own wait and the lock, so it is large here...
    c.get("/api/export/")
    c.get("/api/export/")
    r = c.get("/api/export/")
    assert r.status_code == 429 and int(r["Retry-After"]) > 2
    assert alerts.NOTIFIED[-1]["lockout"]["active"] is True
    # ...but on a path the client has never used, only the lock speaks: nothing is counted.
    r = c.get("/search/")
    assert r.status_code == 429 and r["Retry-After"] == "2"
    assert c.get("/health/").status_code == 200  # truly exempt paths stay exempt
    assert Client(REMOTE_ADDR="203.0.113.31").get("/search/").status_code == 200
    time.sleep(2.1)
    assert c.get("/search/").status_code == 200  # lock expired, /search/ quota untouched


# -- unrouted requests ------------------------------------------------------------------


def test_404s_count_and_can_be_blocked(client):
    for i in range(3):
        r = client.get(f"/no-such-page-{i}/")
        assert r.status_code == 404 and r["X-RateLimit-Remaining"] == str(2 - i)
    assert client.get("/no-such-page-9/").status_code == 429
    assert client.get("/").status_code == 429  # same global bucket


def test_missing_favicon_is_free(client):
    for _ in range(10):
        r = client.get("/favicon.ico")
        assert r.status_code == 404 and "X-RateLimit-Limit" not in r
    assert client.get("/").status_code == 200


def test_append_slash_redirects_are_counted(client):
    for _ in range(3):
        assert client.get("/api/export").status_code == 301
    assert client.get("/api/export").status_code == 429


def test_count_unrouted_can_be_turned_off(client, tw):
    tw(COUNT_UNROUTED=False)
    for _ in range(10):
        assert client.get("/nope/").status_code == 404
    assert client.get("/").status_code == 200


# -- bad hooks never 500 ----------------------------------------------------------------


def test_broken_key_func_falls_back_to_ip(client, tw, caplog):
    def boom(request):
        raise RuntimeError("identity service down")

    tw(KEY_FUNC=boom)
    with caplog.at_level(logging.ERROR, logger="django_trafficwatch"):
        for _ in range(3):
            assert client.get("/").status_code == 200
        assert client.get("/").status_code == 429
    assert any("KEY_FUNC" in rec.getMessage() for rec in caplog.records)


def test_broken_exempt_func_exempts_nobody(client, tw):
    tw(EXEMPT_FUNC=lambda request: 1 / 0)
    for _ in range(3):
        assert client.get("/").status_code == 200
    assert client.get("/").status_code == 429


# -- configuration mistakes fail at startup ----------------------------------------------


@pytest.mark.parametrize(
    "bad, message",
    [
        ({"MAX_REQUEST": 10}, "unknown keys"),
        ({"MAX_REQUESTS": 0}, "MAX_REQUESTS"),
        ({"WINDOW_SECONDS": -5}, "WINDOW_SECONDS"),
        ({"KEY_FUNC": "project.nowhere.func"}, "KEY_FUNC cannot be imported"),
        ({"BACKEND": "project.nowhere.Backend"}, "BACKEND cannot be imported"),
        ({"TRUSTED_PROXIES": ["not-an-ip"]}, "TRUSTED_PROXIES"),
        ({"EXEMPT_CLIENTS": ["10.0.0.0/99"]}, "EXEMPT_CLIENTS"),
        ({"LOCKOUT": {"VIOLATIONS": 3}}, "LOCKOUT"),
        ({"HEADERS_STYLE": "rfc"}, "HEADERS_STYLE"),
        ({"BLOCK_STATUS": 42}, "BLOCK_STATUS"),
        ({"FAIL_OPEN": "yes"}, "FAIL_OPEN must be a boolean"),
        ({"PATH_RULES": {"re:(": {}}}, "invalid regex"),
    ],
)
def test_misconfiguration_refuses_to_start(tw, bad, message):
    tw(**bad)
    with pytest.raises(ImproperlyConfigured, match=message):
        new_client()
