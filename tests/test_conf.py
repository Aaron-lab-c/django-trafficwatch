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
