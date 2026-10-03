import contextlib
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import Client


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def tw(settings):
    """Return a helper that merges overrides into TRAFFICWATCH."""

    def _set(**overrides):
        settings.TRAFFICWATCH = {**settings.TRAFFICWATCH, **overrides}

    return _set


FROZEN_MODULES = (
    "django_trafficwatch.backends.base.time",
    "django_trafficwatch.backends.fixed_window.time",
    "django_trafficwatch.backends.sliding_window.time",
    "django_trafficwatch.backends.redis_lua.time",
    "django_trafficwatch.core.time",
)


@pytest.fixture
def frozen():
    """Freeze ``time.time()`` for the backends and core; mutate ``frozen["t"]`` to advance."""
    now = {"t": 1000.0}
    with contextlib.ExitStack() as stack:
        for target in FROZEN_MODULES:
            patched = stack.enter_context(mock.patch(target))
            patched.time.side_effect = lambda: now["t"]
        yield now


@pytest.fixture
def client():
    return Client(REMOTE_ADDR="1.1.1.1")


def fresh_client(**defaults):
    """A test client with a *new* middleware chain, so settings that the middleware reads at
    construction time (BACKEND) are picked up."""
    c = Client(**defaults)
    c.handler = type(c.handler)()
    c.handler.load_middleware()
    return c
