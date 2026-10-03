import logging

import pytest
from django.http import HttpResponse
from django.test import AsyncClient, Client

from django_trafficwatch import traffic_exceeded
from django_trafficwatch.backends.sliding_window import SlidingWindowBackend
from tests.conftest import fresh_client

# -- basic enforcement -------------------------------------------------------------


def test_allows_until_limit_then_blocks(client):
    for i in range(3):
        r = client.get("/")
        assert r.status_code == 200
        assert r["X-RateLimit-Limit"] == "3"
        assert r["X-RateLimit-Remaining"] == str(2 - i)
    r = client.get("/")
    assert r.status_code == 429
    assert r.json()["detail"].startswith("Too many")
    assert r.json()["retry_after"] == int(r["Retry-After"])
    assert r["Retry-After"] == r["X-RateLimit-Reset"]
    assert r["X-RateLimit-Remaining"] == "0"


def test_observe_only_mode(client, tw):
    tw(BLOCK=False)
    for _ in range(5):
        r = client.get("/state/")
        assert r.status_code == 200
    assert r.json() == {"exceeded": True, "blocked": False, "rules": ["*"], "counts": [5]}


def test_custom_status_and_message(client, tw):
    tw(BLOCK_STATUS=503, BLOCK_MESSAGE="nope")
    for _ in range(3):
        client.get("/")
    r = client.get("/")
    assert r.status_code == 503 and r.json()["detail"] == "nope"


def test_custom_block_response(client, tw):
    def html(request, info):
        return HttpResponse(f"slow down {info['client']} ({info['rule']})", status=429)

    tw(BLOCK_RESPONSE=html)
    for _ in range(3):
        client.get("/")
    r = client.get("/")
    assert r.status_code == 429
    assert r.content == b"slow down ip:1.1.1.1 (*)"
    assert "Retry-After" in r and "X-RateLimit-Limit" in r


def test_clients_are_independent():
    a, b = Client(REMOTE_ADDR="1.1.1.1"), Client(REMOTE_ADDR="2.2.2.2")
    for _ in range(3):
        a.get("/")
    assert a.get("/").status_code == 429
    assert b.get("/").status_code == 200


def test_custom_key_func(client, tw):
    tw(KEY_FUNC=lambda r: r.headers.get("X-API-Key", "anon"))
    for _ in range(3):
        client.get("/", HTTP_X_API_KEY="k1")
    assert client.get("/", HTTP_X_API_KEY="k1").status_code == 429
    assert client.get("/", HTTP_X_API_KEY="k2").status_code == 200


def test_forged_forwarded_for_cannot_bypass_limit(client):
    for i in range(4):
        r = client.get("/", HTTP_X_FORWARDED_FOR=f"10.0.0.{i}")
    assert r.status_code == 429


def test_headers_can_be_disabled(client, tw):
    tw(HEADERS=False)
    assert "X-RateLimit-Limit" not in client.get("/")


# -- exemptions ----------------------------------------------------------------------


def test_exempt_path_not_counted(client):
    for _ in range(10):
        r = client.get("/health/")
        assert r.status_code == 200
        assert "X-RateLimit-Limit" not in r


def test_exempt_method_not_counted(client):
    for _ in range(10):
        assert client.options("/").status_code == 200
    assert client.get("/").status_code == 200  # counter untouched


def test_exempt_decorator(client):
    for _ in range(10):
        assert client.get("/decorated/exempt/").status_code == 200
        assert client.get("/cbv/exempt/").status_code == 200


# -- rule resolution -----------------------------------------------------------------


def test_rule_decorator_overrides_everything(client, tw):
    tw(PATH_RULES={"/decorated/": {"MAX_REQUESTS": 50}})
    assert client.get("/decorated/strict/").status_code == 200
    assert client.get("/decorated/strict/").status_code == 429


def test_rule_decorator_on_class_based_view(client):
    assert client.get("/cbv/strict/").status_code == 200
    assert client.get("/cbv/strict/").status_code == 429


def test_stacked_decorators_enforce_both_limits(client):
    # minute: 2, hour: 3 -> 3rd request trips the minute rule
    for _ in range(2):
        r = client.get("/decorated/stacked/")
        assert r.status_code == 200
    r = client.get("/decorated/stacked/")
    assert r.status_code == 429
    # Headers describe the strictest rule.
    assert r["X-RateLimit-Limit"] == "2"


def test_stacked_headers_follow_strictest_rule(client):
    r = client.get("/decorated/stacked/")
    # after 1 request: minute 1/2 (remaining 1), hour 1/3 (remaining 2) -> minute is strictest
    assert (r["X-RateLimit-Limit"], r["X-RateLimit-Remaining"]) == ("2", "1")


def test_method_scoped_decorator_falls_back_to_other_rules_for_other_methods(client):
    assert client.post("/decorated/post-only/").status_code == 200
    assert client.post("/decorated/post-only/").status_code == 429
    for _ in range(3):  # GET is governed by the global rule (3/min)
        assert client.get("/decorated/post-only/").status_code == 200
    assert client.get("/decorated/post-only/").status_code == 429


def test_path_rules(client, tw):
    tw(PATH_RULES={"/api/login/": {"WINDOW_SECONDS": 300, "MAX_REQUESTS": 1}})
    assert client.get("/api/login/").status_code == 200
    assert client.get("/api/login/sso/").status_code == 429  # same prefix bucket
    assert client.get("/").status_code == 200  # global bucket untouched


def test_path_rules_method_filter(client, tw):
    tw(PATH_RULES={"/api/login/": {"MAX_REQUESTS": 1, "METHODS": ["POST"]}})
    assert client.post("/api/login/").status_code == 200
    assert client.post("/api/login/").status_code == 429
    assert client.get("/api/login/").status_code == 200  # global rule, separate counter


def test_path_rules_regex(client, tw):
    tw(PATH_RULES={r"re:^/api/v\d+/export/": {"MAX_REQUESTS": 1}})
    assert client.get("/api/v1/export/").status_code == 200
    assert client.get("/api/v2/export/").status_code == 429  # same regex rule bucket


def test_multiple_rules_per_path(client, tw):
    tw(
        PATH_RULES={
            "/api/login/": [
                {"MAX_REQUESTS": 2, "NAME": "burst"},
                {"WINDOW_SECONDS": 3600, "MAX_REQUESTS": 10, "NAME": "hourly"},
            ]
        }
    )
    r = client.get("/api/login/")
    assert r.status_code == 200
    client.get("/api/login/")
    r = client.get("/api/login/")
    assert r.status_code == 429


def test_per_rule_block_override(client, tw):
    """A rule with BLOCK=False only observes even though the global BLOCK is True."""
    tw(PATH_RULES={"/api/login/": {"MAX_REQUESTS": 1, "BLOCK": False}})
    for _ in range(4):
        assert client.get("/api/login/").status_code == 200


def test_per_rule_key_func(client, tw):
    tw(PATH_RULES={"/api/login/": {"MAX_REQUESTS": 1, "KEY_FUNC": lambda r: "everyone"}})
    other = Client(REMOTE_ADDR="2.2.2.2")
    assert client.get("/api/login/").status_code == 200
    assert other.get("/api/login/").status_code == 429  # shared bucket


# -- notifications -----------------------------------------------------------------


def test_callback_and_signal_fire_once(client, tw, caplog):
    calls, signals = [], []
    tw(ON_EXCEEDED=lambda req, info: calls.append(info))
    receiver = lambda sender, **kw: signals.append(kw["info"])  # noqa: E731
    traffic_exceeded.connect(receiver, weak=False)
    try:
        with caplog.at_level(logging.WARNING, logger="django_trafficwatch"):
            for _ in range(6):
                client.get("/")
    finally:
        traffic_exceeded.disconnect(receiver)
    assert len(calls) == 1 and len(signals) == 1
    info = calls[0]
    assert info["count"] == 4 and info["limit"] == 3 and info["client"] == "ip:1.1.1.1"
    assert info["method"] == "GET" and info["path"] == "/" and info["blocked"] is True
    assert "Traffic limit exceeded" in caplog.text
    assert caplog.records[0].trafficwatch == info  # structured field for JSON log handlers


def test_broken_callback_does_not_break_request(client, tw):
    def boom(req, info):
        raise RuntimeError("alert service down")

    tw(ON_EXCEEDED=boom)
    for _ in range(3):
        client.get("/")
    assert client.get("/").status_code == 429


def test_recent_violations_recorded(client, tw):
    from django_trafficwatch import stats

    for _ in range(4):
        client.get("/")
    rows = stats.recent_violations()
    assert len(rows) == 1
    assert rows[0]["client"] == "ip:1.1.1.1" and "at" in rows[0]
    stats.clear_recent_violations()
    assert stats.recent_violations() == []


def test_recent_violations_can_be_disabled(client, tw):
    from django_trafficwatch import stats

    tw(RECENT_VIOLATIONS=0)
    for _ in range(4):
        client.get("/")
    assert stats.recent_violations() == []


# -- backends / async ------------------------------------------------------------------


def test_sliding_backend_through_middleware(tw):
    tw(BACKEND="sliding")
    c = fresh_client(REMOTE_ADDR="3.3.3.3")
    assert isinstance(c.handler._middleware_chain.backend, SlidingWindowBackend)
    for _ in range(3):
        assert c.get("/").status_code == 200
    assert c.get("/").status_code == 429


@pytest.mark.asyncio
async def test_async_stack():
    c = AsyncClient(REMOTE_ADDR="4.4.4.4")
    for i in range(3):
        r = await c.get("/async/")
        assert r.status_code == 200
        assert r["X-RateLimit-Remaining"] == str(2 - i)
    r = await c.get("/async/")
    assert r.status_code == 429
    assert "Retry-After" in r


def test_request_state_is_exposed(client):
    r = client.get("/state/")
    assert r.json() == {"exceeded": False, "blocked": False, "rules": ["*"], "counts": [1]}
