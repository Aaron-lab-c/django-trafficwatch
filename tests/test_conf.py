import pytest

from django_trafficwatch.backends.fixed_window import FixedWindowBackend
from django_trafficwatch.backends.sliding_window import SlidingWindowBackend
from django_trafficwatch.conf import tw_settings
from django_trafficwatch.keys import user_or_ip


def test_defaults_and_overrides(tw):
    assert tw_settings.MAX_REQUESTS == 3
    assert tw_settings.BLOCK is True
    assert tw_settings.TRUSTED_PROXIES == []
    assert tw_settings.EXEMPT_METHODS == ["OPTIONS"]
    from django_trafficwatch.conf import DEFAULTS

    assert "/favicon.ico" in DEFAULTS["EXEMPT_PATHS"]  # tests/settings.py overrides the list
    tw(BLOCK=False)
    assert tw_settings.BLOCK is False


def test_unknown_setting_raises():
    with pytest.raises(AttributeError):
        _ = tw_settings.NOPE


def test_dotted_paths_are_imported(tw):
    assert tw_settings.KEY_FUNC is user_or_ip
    tw(KEY_FUNC=lambda r: "x")
    assert tw_settings.KEY_FUNC(None) == "x"
    assert tw_settings.BLOCK_RESPONSE is None
    tw(BLOCK_RESPONSE="django.http.JsonResponse")
    from django.http import JsonResponse

    assert tw_settings.BLOCK_RESPONSE is JsonResponse


def test_backend_aliases(tw):
    assert tw_settings.backend_class() is FixedWindowBackend
    tw(BACKEND="sliding")
    assert tw_settings.backend_class() is SlidingWindowBackend
    tw(BACKEND="django_trafficwatch.backends.fixed_window.FixedWindowBackend")
    assert tw_settings.backend_class() is FixedWindowBackend


def test_redis_alias(tw):
    from django_trafficwatch.backends.redis_lua import RedisLuaBackend

    tw(BACKEND="redis")
    assert tw_settings.backend_class() is RedisLuaBackend


def test_lockout_is_parsed_and_cached(tw):
    assert tw_settings.lockout is None
    tw(LOCKOUT={"VIOLATIONS": 2, "WINDOW_SECONDS": 600, "DURATION_SECONDS": 900})
    cfg = tw_settings.lockout
    assert (cfg.violations, cfg.window, cfg.duration) == (2, 600, 900)
    assert tw_settings.lockout is cfg


@pytest.mark.parametrize(
    "bad, message",
    [
        ({"PATH_RULES": {"/x/": {"MAX_REQUESTS": 0}}}, "PATH_RULES"),
        ({"KEY_FUNC": "nope.nothing"}, "KEY_FUNC cannot be imported"),
        ({"ON_EXCEEDED": 42}, "ON_EXCEEDED must be callable"),
        ({"BACKEND": "nope.Backend"}, "BACKEND cannot be imported"),
        ({"TRUSTED_PROXIES": ["x"]}, "TRUSTED_PROXIES entry"),
        ({"EXEMPT_CLIENTS": "10.0.0.1"}, "EXEMPT_CLIENTS must be a list"),
        ({"LOCKOUT": {"VIOLATIONS": 1}}, "LOCKOUT"),
        ({"HEADERS_STYLE": "nope"}, "HEADERS_STYLE"),
        ({"IPV6_PREFIX": 0}, "IPV6_PREFIX"),
        ({"MAX_REQUEST": 5}, "unknown keys \\['MAX_REQUEST'\\]"),
    ],
)
def test_validate_fails_fast(tw, bad, message):
    from django.core.exceptions import ImproperlyConfigured

    tw(**bad)
    with pytest.raises(ImproperlyConfigured, match=message):
        tw_settings.validate()


def test_validate_passes_on_defaults():
    tw_settings.validate()
