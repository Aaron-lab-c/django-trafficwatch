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


@pytest.fixture
def frozen():
    """Freeze ``time.time()`` for both backends; mutate ``frozen["t"]`` to advance."""
    with mock.patch("django_trafficwatch.backends.fixed_window.time") as ft:
        with mock.patch("django_trafficwatch.backends.sliding_window.time") as st:
            now = {"t": 1000.0}
            ft.time.side_effect = st.time.side_effect = lambda: now["t"]
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
