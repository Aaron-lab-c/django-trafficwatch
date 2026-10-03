"""Middleware-level tests for the 0.3.0 roadmap items: fail-open, exemptions, fail-fast,
escalating lockout, header styles, MATCH_PATH_INFO, lazy BLOCK_MESSAGE, method_decorator."""

import logging
import time
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, RequestFactory
from django.utils.translation import gettext_lazy

from django_trafficwatch import core, traffic_exceeded
from django_trafficwatch.backends.fixed_window import FixedWindowBackend
from tests.conftest import fresh_client

# -- fail-open -----------------------------------------------------------------------


class BrokenBackend(FixedWindowBackend):
    def hit_many(self, specs):
        raise ConnectionError("redis down")

    def locked_until(self, client):
        raise ConnectionError("redis down")


@pytest.fixture
def broken(tw):
    tw(BACKEND="tests.test_features.BrokenBackend")
    core._outage_logged_at = 0.0
    return fresh_client(REMOTE_ADDR="7.7.7.7")


def test_fail_open_allows_and_marks_degraded(broken, caplog):
    with caplog.at_level(logging.ERROR, logger="django_trafficwatch"):
        for _ in range(10):
            r = broken.get("/state/")
            assert r.status_code == 200
            assert "X-RateLimit-Limit" not in r
    assert r.json() == {
        "exceeded": False,
        "blocked": False,
        "degraded": True,
        "rules": [],
        "counts": [],
    }
    # Logged once (FAIL_OPEN_LOG_INTERVAL), not once per request.
    outage = [rec for rec in caplog.records if "unavailable" in rec.getMessage()]
    assert len(outage) == 1 and outage[0].exc_info is not None


def test_fail_open_log_interval_elapses(broken, tw, caplog):
    tw(BACKEND="tests.test_features.BrokenBackend", FAIL_OPEN_LOG_INTERVAL=30)
    with caplog.at_level(logging.ERROR, logger="django_trafficwatch"):
        broken.get("/")
        broken.get("/")
        with mock.patch("django_trafficwatch.core.time") as t:
            t.time.return_value = time.time() + 31
            broken.get("/")
    assert sum("unavailable" in rec.getMessage() for rec in caplog.records) == 2


def test_fail_closed_blocks(broken, tw):
    tw(BACKEND="tests.test_features.BrokenBackend", FAIL_OPEN=False)
    r = broken.get("/")
    assert r.status_code == 429
    assert r["Retry-After"] == "60"  # the smallest window among the rules
    assert r.json()["retry_after"] == 60


def test_fail_closed_custom_response_gets_degraded_info(broken, tw):
    seen = {}

    def custom(request, info):
        seen.update(info)
        from django.http import HttpResponse

        return HttpResponse("nope", status=503)

    tw(BACKEND="tests.test_features.BrokenBackend", FAIL_OPEN=False, BLOCK_RESPONSE=custom)
    assert broken.get("/").status_code == 503
    assert seen["degraded"] is True and seen["rule"] is None and seen["client"] == "ip:7.7.7.7"


def test_key_func_errors_are_not_swallowed(client, tw):
    def boom(request):
        raise RuntimeError("bad key func")

    tw(KEY_FUNC=boom)
    with pytest.raises(RuntimeError):
        client.get("/")


# -- exemptions ----------------------------------------------------------------------


def test_exempt_clients(client, tw):
    tw(EXEMPT_CLIENTS=["1.1.1.0/24"])
    for _ in range(10):
        r = client.get("/")
        assert r.status_code == 200 and "X-RateLimit-Limit" not in r
    other = Client(REMOTE_ADDR="2.2.2.2")
    for _ in range(3):
        other.get("/")
    assert other.get("/").status_code == 429


def test_exempt_func(client, tw):
    tw(EXEMPT_FUNC=lambda request: request.headers.get("X-Internal") == "1")
    for _ in range(10):
        assert client.get("/", HTTP_X_INTERNAL="1").status_code == 200
    for _ in range(3):
        client.get("/")
    assert client.get("/").status_code == 429


def test_exempt_func_dotted_path(client, tw):
    tw(EXEMPT_FUNC="tests.test_features.always_exempt")
    for _ in range(10):
        assert client.get("/").status_code == 200


def always_exempt(request):
    return True


# -- fail fast -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"PATH_RULES": {"/x/": {"MAX_REQUESTS": 0}}},
        {"KEY_FUNC": "nope.nothing"},
        {"BACKEND": "nope.Backend"},
        {"LOCKOUT": {"VIOLATIONS": 1}},
    ],
)
def test_middleware_fails_at_startup_on_bad_config(tw, bad):
    tw(**bad)
    with pytest.raises(ImproperlyConfigured):
        fresh_client()


# -- escalating lockout --------------------------------------------------------------


@pytest.fixture
def lockout(tw):
    tw(
        MAX_REQUESTS=1,
        PATH_RULES={"/api/login/": {"MAX_REQUESTS": 1, "WINDOW_SECONDS": 60}},
        LOCKOUT={"VIOLATIONS": 2, "WINDOW_SECONDS": 600, "DURATION_SECONDS": 900},
    )


def test_lockout_after_n_violations(client, lockout, frozen):
    infos = []
    receiver = lambda sender, **kw: infos.append(kw["info"])  # noqa: E731
    traffic_exceeded.connect(receiver, weak=False)
    try:
        # violation 1: global rule on "/"
        assert client.get("/").status_code == 200
        r = client.get("/")
        assert r.status_code == 429 and r["Retry-After"] == "20"  # bucket ends at t=1020
        assert infos[-1]["lockout"] == {
            "violations": 1,
            "threshold": 2,
            "window": 600,
            "duration": 900,
            "locked_until": None,
            "active": False,
        }
        # violation 2: a different rule, same client -> locked for 900s
        assert client.get("/api/login/").status_code == 200
        r = client.get("/api/login/")
        assert r.status_code == 429
        assert r["Retry-After"] == "900"
        assert infos[-1]["lockout"]["active"] is True
        assert infos[-1]["lockout"]["locked_until"] == 1000.0 + 900
        assert infos[-1]["blocked"] is True
        # While locked: everything is blocked, nothing is counted, no new notifications.
        frozen["t"] = 1000.0 + 61  # the per-rule windows have rolled over
        r = client.get("/state/")
        assert r.status_code == 429 and r["Retry-After"] == str(900 - 61)
        assert r.json()["retry_after"] == 900 - 61
        assert len(infos) == 2
        # After the lock expires the client is served again.
        frozen["t"] = 1000.0 + 901
        assert client.get("/state/").status_code == 200
    finally:
        traffic_exceeded.disconnect(receiver)


def test_lockout_is_per_client(lockout, frozen):
    a, b = Client(REMOTE_ADDR="1.1.1.1"), Client(REMOTE_ADDR="2.2.2.2")
    for _ in range(2):
        a.get("/")
        a.get("/api/login/")
    assert a.get("/health/").status_code == 200  # exempt paths stay exempt
    assert a.get("/decorated/exempt/").status_code == 200
    assert a.get("/").status_code == 429
    assert b.get("/").status_code == 200


def test_lockout_violation_window(lockout, frozen):
    c = Client(REMOTE_ADDR="3.3.3.3")
    c.get("/")
    c.get("/")  # violation 1
    frozen["t"] = 1000.0 + 601  # violation window has passed
    c.get("/")
    c.get("/")  # violation 1 again, not 2
    frozen["t"] += 61
    assert c.get("/").status_code == 200


def test_lockout_recorded_and_shown_by_command(client, lockout, frozen):
    from io import StringIO

    from django.core.management import call_command

    for _ in range(2):
        client.get("/")
        client.get("/api/login/")
    out = StringIO()
    call_command("trafficwatch_recent", stdout=out)
    text = out.getvalue()
    assert "LOCKED" in text and "1/2" in text


def test_lockout_info_reaches_custom_block_response(client, lockout, frozen, tw):
    seen = []

    def custom(request, info):
        seen.append(info)
        from django.http import HttpResponse

        return HttpResponse("locked", status=429)

    tw(BLOCK_RESPONSE=custom)
    for _ in range(2):
        client.get("/")
        client.get("/api/login/")
    assert client.get("/state/").status_code == 429
    info = seen[-1]
    assert info["rule"] == "lockout" and info["lockout"]["active"] is True
    assert info["retry_after"] == 900 and info["count"] is None


# -- headers -------------------------------------------------------------------------


def test_ietf_headers(client, tw, frozen):
    tw(HEADERS_STYLE="ietf")
    r = client.get("/decorated/stacked/")
    assert "X-RateLimit-Limit" not in r
    assert r["RateLimit-Policy"] == '"stacked-minute";q=2;w=60, "stacked-hour";q=3;w=3600'
    assert r["RateLimit"] == '"stacked-minute";r=1;t=20'  # minute bucket ends at t=1020
    client.get("/decorated/stacked/")
    r = client.get("/decorated/stacked/")
    assert r.status_code == 429
    assert r["RateLimit"].startswith('"stacked-minute";r=0;t=')
    assert r["Retry-After"] == r["RateLimit"].rsplit("t=", 1)[1]


def test_both_header_styles(client, tw):
    tw(HEADERS_STYLE="both")
    r = client.get("/")
    assert r["X-RateLimit-Limit"] == "3" and r["RateLimit-Policy"] == '"*";q=3;w=60'


def test_reset_as_epoch(client, tw):
    tw(RESET_AS_EPOCH=True)
    before = int(time.time())
    r = client.get("/")
    reset = int(r["X-RateLimit-Reset"])
    assert before + 1 <= reset <= before + 61


def test_ietf_headers_while_locked(client, tw, frozen):
    tw(MAX_REQUESTS=1, LOCKOUT={"VIOLATIONS": 1, "WINDOW_SECONDS": 60, "DURATION_SECONDS": 300})
    tw(HEADERS_STYLE="ietf")
    client.get("/")
    client.get("/")
    r = client.get("/")
    assert r.status_code == 429 and r["RateLimit"] == '"lockout";r=0;t=300'


def test_sf_string_escaping():
    assert core._sf_string('a"b\\c') == '"a\\"b\\\\c"'
    assert core._sf_string("é\n") == '"??"'


# -- MATCH_PATH_INFO -----------------------------------------------------------------


def test_match_path_info_under_script_name(tw):
    tw(PATH_RULES={"/api/login/": {"MAX_REQUESTS": 1}}, EXEMPT_PATHS=["/health/"])
    mounted = Client(REMOTE_ADDR="8.8.8.8", SCRIPT_NAME="/app")
    # request.path is /app/api/login/ -> prefix does not match, global rule (3) applies
    assert mounted.get("/api/login/").status_code == 200
    assert mounted.get("/api/login/").status_code == 200
    tw(MATCH_PATH_INFO=True)
    mounted = Client(REMOTE_ADDR="9.9.9.9", SCRIPT_NAME="/app")
    assert mounted.get("/api/login/").status_code == 200
    assert mounted.get("/api/login/").status_code == 429
    for _ in range(10):
        assert mounted.get("/health/").status_code == 200


# -- lazy BLOCK_MESSAGE --------------------------------------------------------------


def test_lazy_block_message(client, tw):
    tw(BLOCK_MESSAGE=gettext_lazy("Slow down"))
    for _ in range(3):
        client.get("/")
    r = client.get("/")
    assert r.status_code == 429 and r.json()["detail"] == "Slow down"


# -- method_decorator on class-based views -------------------------------------------


def test_method_decorator_on_handler(client):
    assert client.post("/cbv/method/").status_code == 200
    assert client.post("/cbv/method/").status_code == 429
    for _ in range(3):  # GET is not decorated -> global rule
        assert client.get("/cbv/method/").status_code == 200
    assert client.get("/cbv/method/").status_code == 429


def test_method_decorator_views_do_not_share_counters(client):
    assert client.post("/cbv/method/").status_code == 200
    assert client.post("/cbv/method2/").status_code == 200
    assert client.post("/cbv/method2/").status_code == 429


def test_method_decorator_on_dispatch(client):
    assert client.get("/cbv/dispatch/").status_code == 200
    r = client.get("/cbv/dispatch/")
    assert r.status_code == 429
    for _ in range(5):
        assert client.get("/cbv/dispatch-exempt/").status_code == 200


def test_method_decorator_rule_names():
    from django_trafficwatch.decorators import view_rules
    from tests import urls

    names = [r.name for r in view_rules(urls.MethodDecoratedView.as_view(), "POST")]
    assert names == ["view:tests.urls.MethodDecoratedView.post"]
    names = [r.name for r in view_rules(urls.DispatchDecoratedView.as_view(), "GET")]
    assert names == ["view:tests.urls.DispatchDecoratedView.dispatch"]
    assert view_rules(urls.MethodDecoratedView.as_view(), "GET") == ()


# -- hit_many is used (one batch per request) ----------------------------------------


def test_check_uses_hit_many(client):
    with mock.patch.object(
        FixedWindowBackend, "hit_many", autospec=True, side_effect=FixedWindowBackend.hit_many
    ) as hm:
        client.get("/decorated/stacked/")
    assert hm.call_count == 1
    specs = hm.call_args.args[1]
    assert [s[1] for s in specs] == ["stacked-minute", "stacked-hour"]


def test_state_info_for_request_factory():
    """``TrafficWatchState.info`` works for an empty state (used by block_response)."""
    state = core.TrafficWatchState(results=(), degraded=True, fallback_retry_after=7)
    req = RequestFactory().get("/x/", REMOTE_ADDR="1.2.3.4")
    info = state.info(req)
    assert info["retry_after"] == 7 and info["client"] == "ip:1.2.3.4" and info["degraded"]
    assert state.headers() == {} and state.strictest is None
