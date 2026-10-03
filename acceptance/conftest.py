import time

import pytest
from django.core.cache import cache
from django.test import Client
from project import alerts

GLOBAL_WINDOW = 2  # settings.TRAFFICWATCH["WINDOW_SECONDS"]


@pytest.fixture(autouse=True)
def clean_state():
    cache.clear()
    alerts.NOTIFIED.clear()
    alerts.EXCEEDED_TOTAL.clear()
    yield
    cache.clear()


@pytest.fixture
def tw(settings):
    """Merge overrides into TRAFFICWATCH (what a project would put in settings.py)."""

    def _set(**overrides):
        settings.TRAFFICWATCH = {**settings.TRAFFICWATCH, **overrides}

    return _set


def wait_for_fresh_window(window: int = GLOBAL_WINDOW, margin: float = 0.6) -> None:
    """Fixed windows are aligned to the clock. If the current one is about to end, wait for
    the next so a burst of requests lands inside a single window."""
    remaining = window - (time.time() % window)
    if remaining < margin:
        time.sleep(remaining + 0.05)


@pytest.fixture
def client():
    wait_for_fresh_window()
    return Client(REMOTE_ADDR="203.0.113.10")


def new_client(**defaults) -> Client:
    """A client whose middleware chain is built *now*, so settings the middleware reads at
    construction time (BACKEND, CACHE_ALIAS, MIDDLEWARE) are picked up."""
    c = Client(**defaults)
    c.handler = type(c.handler)()
    c.handler.load_middleware()
    return c
