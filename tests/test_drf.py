import pytest

pytest.importorskip("rest_framework")

from rest_framework.test import APIClient  # noqa: E402

from django_trafficwatch.drf import TrafficWatchThrottle  # noqa: E402
from tests import urls  # noqa: E402


@pytest.fixture
def throttled_views():
    """Views in tests.urls are declared with ``throttle_classes = []`` (DRF's default when
    REST_FRAMEWORK is unset); switch ours on for the duration of a test."""
    for view in (urls.PlainAPIView, urls.LoginAPIView):
        view.throttle_classes = [TrafficWatchThrottle]
    yield
    for view in (urls.PlainAPIView, urls.LoginAPIView):
        view.throttle_classes = []


@pytest.fixture
def api(settings, throttled_views):
    # Drop the middleware so the throttle is the only thing counting.
    settings.MIDDLEWARE = []
    return APIClient(REMOTE_ADDR="5.5.5.5")


def test_throttle_uses_global_rule(api):
    for _ in range(3):
        assert api.get("/drf/plain/").status_code == 200
    r = api.get("/drf/plain/")
    assert r.status_code == 429
    assert int(r["Retry-After"]) >= 1


def test_throttle_uses_class_decorator_with_methods(api):
    assert api.post("/drf/login/").status_code == 200
    assert api.post("/drf/login/").status_code == 200
    assert api.post("/drf/login/").status_code == 429
    # GET is not covered by the decorator -> global rule, separate counter
    for _ in range(3):
        assert api.get("/drf/login/").status_code == 200
    assert api.get("/drf/login/").status_code == 429


def test_throttle_respects_observe_mode(api, tw):
    tw(BLOCK=False)
    for _ in range(6):
        assert api.get("/drf/plain/").status_code == 200


def test_throttle_defers_to_middleware(throttled_views):
    """With the middleware installed the throttle must not count a second time."""
    client = APIClient(REMOTE_ADDR="6.6.6.6")
    for i in range(3):
        r = client.get("/drf/plain/")
        assert r.status_code == 200
        assert r["X-RateLimit-Remaining"] == str(2 - i)  # would be lower if double counted
    assert client.get("/drf/plain/").status_code == 429
