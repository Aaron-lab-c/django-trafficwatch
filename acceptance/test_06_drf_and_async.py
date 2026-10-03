"""Django REST Framework throttle and the ASGI path."""

import pytest
from conftest import new_client, wait_for_fresh_window
from django.test import AsyncClient
from rest_framework.test import APIClient


def test_throttle_defers_to_the_middleware_when_both_are_installed():
    wait_for_fresh_window()
    api = APIClient(REMOTE_ADDR="203.0.113.60")
    for i in range(3):
        r = api.get("/drf/plain/")
        assert r.status_code == 200
        assert r["X-RateLimit-Remaining"] == str(2 - i)  # counted once, not twice
    assert api.get("/drf/plain/").status_code == 429


def test_throttle_alone(settings):
    """Without the middleware the throttle enforces the same rules and counters."""
    settings.MIDDLEWARE = [m for m in settings.MIDDLEWARE if "trafficwatch" not in m]
    wait_for_fresh_window()
    api = APIClient(REMOTE_ADDR="203.0.113.61")
    api.handler = type(api.handler)()
    api.handler.load_middleware()
    for _ in range(3):
        assert api.get("/drf/plain/").status_code == 200
    r = api.get("/drf/plain/")
    assert r.status_code == 429 and int(r["Retry-After"]) >= 1
    assert "X-RateLimit-Limit" not in r  # headers are the middleware's job
    # Class decorator with methods=: POST 2/day, GET falls back to the global rule.
    assert api.post("/drf/login/").status_code == 200
    assert api.post("/drf/login/").status_code == 200
    assert api.post("/drf/login/").status_code == 429


@pytest.mark.asyncio
async def test_asgi_stack():
    wait_for_fresh_window()
    c = AsyncClient(REMOTE_ADDR="203.0.113.70")
    for i in range(3):
        r = await c.get("/async/")
        assert r.status_code == 200 and r["X-RateLimit-Remaining"] == str(2 - i)
    r = await c.get("/async/")
    assert r.status_code == 429 and "Retry-After" in r
    assert (await c.get("/no-such/")).status_code == 429  # unrouted requests under ASGI too


def test_sync_client_after_async_is_unaffected():
    """Sanity: the per-test cache reset keeps suites independent."""
    assert new_client(REMOTE_ADDR="203.0.113.71").get("/").status_code == 200
