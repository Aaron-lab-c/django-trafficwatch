import os

import pytest

from django_trafficwatch.checks import check_trafficwatch_settings


def ids(errors):
    """Ids of everything above INFO (the LocMem I001 info is tested on its own)."""
    return sorted(e.id for e in errors if e.level > 20)


def test_clean_settings_pass(settings):
    assert ids(check_trafficwatch_settings(None)) == []


def test_unknown_key_errors(tw):
    tw(MAX_REQUEST=1)
    found = check_trafficwatch_settings(None)
    assert ids(found) == ["trafficwatch.E016"]
    assert "MAX_REQUEST" in found[0].msg and "MAX_REQUESTS" in (found[0].hint or "")


def test_bad_path_rules_error(tw):
    tw(PATH_RULES={"/x/": {"MAX_REQUESTS": 0}})
    assert "trafficwatch.E003" in ids(check_trafficwatch_settings(None))


def test_bad_dotted_paths_error(tw):
    tw(KEY_FUNC="nope.nothing", BACKEND="nope.Backend", ON_EXCEEDED=42, EXEMPT_FUNC="x.y")
    found = ids(check_trafficwatch_settings(None))
    assert found.count("trafficwatch.E004") == 3
    assert "trafficwatch.E005" in found


def test_bad_trusted_proxy_error(tw):
    tw(TRUSTED_PROXIES=["10.0.0.1", "not-an-ip"])
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.E006"]


def test_exempt_lists_must_be_lists(tw):
    tw(EXEMPT_PATHS="/static/")
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.E007"]


def test_missing_cache_alias_error(tw):
    tw(CACHE_ALIAS="ratelimit")
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.E008"]


locmem_only = pytest.mark.skipif(
    bool(os.environ.get("TW_REDIS_URL")), reason="suite is running against Redis"
)


@locmem_only
def test_locmem_in_production_warns(settings):
    settings.DEBUG = False
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.W002"]


@locmem_only
def test_locmem_in_debug_is_an_info(settings):
    settings.DEBUG = True
    found = check_trafficwatch_settings(None)
    assert [m.id for m in found] == ["trafficwatch.I001"]
    assert found[0].level == 20  # INFO: visible, never fails `check`
    assert "trafficwatch_recent" in found[0].msg


def test_middleware_missing_warns(settings):
    settings.MIDDLEWARE = []
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.W003"]


def test_middleware_before_auth_warns(settings):
    settings.MIDDLEWARE = [
        "django_trafficwatch.middleware.TrafficWatchMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
    ]
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.W004"]
    settings.MIDDLEWARE = list(reversed(settings.MIDDLEWARE))
    assert ids(check_trafficwatch_settings(None)) == []


def test_manage_check_runs_registered_check(tw):
    from django.core.management import call_command

    tw(PATH_RULES={"/x/": {"MAX_REQUESTS": 0}})
    import pytest
    from django.core.management.base import SystemCheckError

    with pytest.raises(SystemCheckError, match="trafficwatch.E003"):
        call_command("check")


def test_bad_exempt_clients_error(tw):
    tw(EXEMPT_CLIENTS=["10.0.0.0/8", "nope"])
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.E009"]


@pytest.mark.parametrize("prefix", [0, 129, "64", True])
def test_bad_ipv6_prefix_error(tw, prefix):
    tw(IPV6_PREFIX=prefix)
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.E010"]


@pytest.mark.parametrize(
    "lockout",
    [
        {"VIOLATIONS": 3},
        {"VIOLATIONS": 0, "WINDOW_SECONDS": 60, "DURATION_SECONDS": 60},
        {"VIOLATIONS": 3, "WINDOW_SECONDS": 60, "DURATION_SECONDS": 60, "EXTRA": 1},
        5,
    ],
)
def test_bad_lockout_error(tw, lockout):
    tw(LOCKOUT=lockout)
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.E011"]


def test_good_lockout_passes(tw):
    tw(LOCKOUT={"VIOLATIONS": 3, "WINDOW_SECONDS": 60, "DURATION_SECONDS": 60})
    assert ids(check_trafficwatch_settings(None)) == []


def test_bad_headers_style_error(tw):
    tw(HEADERS_STYLE="rfc")
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.E012"]


def test_non_boolean_flags_error(tw):
    tw(FAIL_OPEN="yes", MATCH_PATH_INFO=1)
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.E013", "trafficwatch.E013"]


def test_fail_closed_warns(tw):
    tw(FAIL_OPEN=False)
    found = check_trafficwatch_settings(None)
    assert ids(found) == ["trafficwatch.W005"]
    (w005,) = [m for m in found if m.id == "trafficwatch.W005"]
    assert "degraded" in w005.hint


@locmem_only
def test_redis_backend_on_non_redis_cache_warns(tw):
    tw(BACKEND="redis")
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.W006"]


def test_log_interval_zero_is_allowed(tw):
    tw(FAIL_OPEN_LOG_INTERVAL=0)
    assert ids(check_trafficwatch_settings(None)) == []
    tw(FAIL_OPEN_LOG_INTERVAL=-1)
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.E002"]


@locmem_only
@pytest.mark.parametrize(
    "backend, expected",
    [
        ("django.core.cache.backends.filebased.FileBasedCache", "trafficwatch.W007"),
        ("django.core.cache.backends.db.DatabaseCache", "trafficwatch.W007"),
        ("django.core.cache.backends.dummy.DummyCache", "trafficwatch.E014"),
    ],
)
def test_non_atomic_caches_are_reported(settings, backend, expected):
    settings.CACHES = {"default": {"BACKEND": backend, "LOCATION": "/tmp/x"}}
    assert ids(check_trafficwatch_settings(None)) == [expected]


def test_zero_or_negative_limits_error_once(tw):
    tw(MAX_REQUESTS=0)
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.E002"]
    tw(MAX_REQUESTS=-1, WINDOW_SECONDS=0)
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.E002", "trafficwatch.E002"]


def test_bad_status_and_recent_error(tw):
    tw(BLOCK_STATUS=42, RECENT_VIOLATIONS=-1)
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.E015", "trafficwatch.E015"]
