"""Tests that need a real Redis: ``TW_REDIS_URL=redis://localhost:6379/1 pytest``.
The CI ``test-redis`` job sets it; locally they are skipped when unset."""

import os
import threading

import pytest
from django.core.cache import caches

from django_trafficwatch.backends.fixed_window import FixedWindowBackend
from django_trafficwatch.backends.redis_lua import RedisLuaBackend
from django_trafficwatch.backends.sliding_window import SlidingWindowBackend
from tests.conftest import fresh_client

pytestmark = pytest.mark.skipif(not os.environ.get("TW_REDIS_URL"), reason="TW_REDIS_URL unset")


def test_suite_runs_against_redis():
    assert type(caches["default"]).__name__ == "RedisCache"


@pytest.mark.parametrize("backend_cls", [FixedWindowBackend, SlidingWindowBackend, RedisLuaBackend])
def test_incr_is_atomic_across_threads(backend_cls):
    """N threads x M hits must end at exactly N*M: no lost updates."""
    b = backend_cls("default", "t")
    threads, per_thread = 8, 50
    counts = []
    lock = threading.Lock()

    def worker():
        for _ in range(per_thread):
            r = b.hit("c", "*", 3600, 10**6)
            with lock:
                counts.append(r.count)

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    assert max(counts) == threads * per_thread
    assert sorted(counts) == list(range(1, threads * per_thread + 1))


def test_redis_lua_backend_is_native():
    b = RedisLuaBackend("default", "t")
    assert b.is_native and b.fallback is None


def test_redis_lua_single_round_trip(frozen):
    b = RedisLuaBackend("default", "t")
    specs = [("c", "a", 10, 5), ("c", "b", 60, 100)]
    for _ in range(5):
        results = b.hit_many(specs)
    assert [r.count for r in results] == [5, 5]
    r = b.hit_many(specs)
    assert r[0].exceeded and r[0].just_exceeded and not r[1].exceeded
    assert [p.count for p in b.peek_many(specs)] == [5, 5]  # rejected -> rolled back
    r = b.hit_many(specs)
    assert r[0].exceeded and not r[0].just_exceeded  # marker: notified once
    # previous bucket decays like the sliding estimate
    frozen["t"] = 1015.0
    assert b.hit("c", "a", 10, 5).count == int(5 * 0.5 + 1)
    b.reset("c", "a", 10)
    assert b.hit("c", "a", 10, 5).count == 1


def test_redis_lua_keys_are_hash_tagged():
    b = RedisLuaBackend("default", "t")
    assert b._key("ip:1.2.3.4", "*", 7) == "t:rl:{ip:1.2.3.4}:*:7"


def test_redis_lua_through_middleware(tw):
    tw(BACKEND="redis")
    c = fresh_client(REMOTE_ADDR="3.3.3.3")
    backend = c.handler._middleware_chain.backend
    assert isinstance(backend, RedisLuaBackend) and backend.is_native
    for _ in range(3):
        assert c.get("/").status_code == 200
    assert c.get("/").status_code == 429


def test_redis_lua_mixed_clients_per_rule(tw):
    """Rules with different key funcs run one script per client."""
    b = RedisLuaBackend("default", "t")
    results = b.hit_many([("a", "r1", 10, 5), ("b", "r2", 10, 5), ("a", "r3", 10, 5)])
    assert [r.count for r in results] == [1, 1, 1]
    assert b.hit("a", "r3", 10, 5).count == 2
