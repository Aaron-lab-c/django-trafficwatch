from django_trafficwatch.checks import check_trafficwatch_settings


def ids(errors):
    return sorted(e.id for e in errors)


def test_clean_settings_pass(settings):
    assert check_trafficwatch_settings(None) == []


def test_unknown_key_warns(tw):
    tw(TYPO=1)
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.W001"]


def test_bad_path_rules_error(tw):
    tw(PATH_RULES={"/x/": {"MAX_REQUESTS": 0}})
    assert "trafficwatch.E003" in ids(check_trafficwatch_settings(None))


def test_bad_dotted_paths_error(tw):
    tw(KEY_FUNC="nope.nothing", BACKEND="nope.Backend", ON_EXCEEDED=42)
    found = ids(check_trafficwatch_settings(None))
    assert found.count("trafficwatch.E004") == 2
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


def test_locmem_in_production_warns(settings):
    settings.DEBUG = False
    assert ids(check_trafficwatch_settings(None)) == ["trafficwatch.W002"]


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
    assert check_trafficwatch_settings(None) == []


def test_manage_check_runs_registered_check(tw):
    from django.core.management import call_command

    tw(PATH_RULES={"/x/": {"MAX_REQUESTS": 0}})
    import pytest
    from django.core.management.base import SystemCheckError

    with pytest.raises(SystemCheckError, match="trafficwatch.E003"):
        call_command("check")
