import pytest

from django_trafficwatch.backends.fixed_window import FixedWindowBackend
from django_trafficwatch.backends.sliding_window import SlidingWindowBackend

# -- fixed window ----------------------------------------------------------------


def test_fixed_window_counts_and_resets(frozen):
    b = FixedWindowBackend("default", "t")
    results = [b.hit("c", "*", 10, 2) for _ in range(3)]
    assert [r.count for r in results] == [1, 2, 3]
    assert results[1].exceeded is False
    assert results[2].exceeded and results[2].just_exceeded
    assert results[2].remaining == 0
    assert results[0].reset_in == 10

    frozen["t"] = 1009.0
    assert b.hit("c", "*", 10, 2).reset_in == 1
    frozen["t"] = 1010.0  # next bucket
    assert b.hit("c", "*", 10, 2).count == 1


def test_fixed_window_isolates_clients_and_rules(frozen):
    b = FixedWindowBackend("default", "t")
    b.hit("a", "*", 10, 5)
    assert b.hit("b", "*", 10, 5).count == 1
    assert b.hit("a", "/api/", 10, 5).count == 1
    assert b.hit("a", "*", 10, 5).count == 2


def test_fixed_window_reset_and_peek(frozen):
    b = FixedWindowBackend("default", "t")
    assert b.peek("a", "*", 10, 5).count == 0
    b.hit("a", "*", 10, 5)
    assert b.peek("a", "*", 10, 5).count == 1
    assert b.peek("a", "*", 10, 5).count == 1  # peek does not count
    b.reset("a", "*", 10)
    assert b.hit("a", "*", 10, 5).count == 1


# -- sliding window --------------------------------------------------------------


def test_sliding_window_weights_previous_bucket(frozen):
    b = SlidingWindowBackend("default", "t")
    for _ in range(10):
        b.hit("c", "*", 10, 100)
    # 5s into the next bucket: previous 10 count for half
    frozen["t"] = 1015.0
    assert b.hit("c", "*", 10, 100).count == int(10 * 0.5 + 1)
    # 9s in: previous barely counts
    frozen["t"] = 1019.0
    r = b.hit("c", "*", 10, 100)
    assert r.count == int(10 * 0.1 + 2)
    assert r.reset_in == 1
    # two buckets later: previous bucket is empty
    frozen["t"] = 1030.0
    assert b.hit("c", "*", 10, 100).count == 1


def test_sliding_window_reset_and_peek(frozen):
    b = SlidingWindowBackend("default", "t")
    b.hit("c", "*", 10, 5)
    frozen["t"] = 1012.0
    b.hit("c", "*", 10, 5)
    assert b.peek("c", "*", 10, 5).count == b.peek("c", "*", 10, 5).count
    b.reset("c", "*", 10)
    assert b.hit("c", "*", 10, 5).count == 1


def test_sliding_reset_in_is_time_until_next_accepted_request(frozen):
    """Retry-After must be honest: once it elapses, the next request is accepted."""
    b = SlidingWindowBackend("default", "t")
    # Fill the bucket: limit 5, 6th request exceeds.
    results = [b.hit("c", "*", 10, 5) for _ in range(6)]
    blocked = results[-1]
    assert blocked.exceeded
    # The rejected 6th request is rolled back, so 5 stay stored. We must wait for the
    # roll-over, then until 5 * (1 - e) + 1 <= 5  ->  e >= 0.2  ->  2s into the next bucket.
    assert blocked.reset_in == 12
    assert b.peek("c", "*", 10, 5).count == 5  # the rejected request was not counted
    frozen["t"] = 1000.0 + blocked.reset_in
    assert not b.hit("c", "*", 10, 5).exceeded


@pytest.mark.parametrize(
    "prev_hits, cur_hits, elapsed", [(8, 0, 0.1), (5, 3, 0.5), (12, 1, 0.3), (7, 0, 0.0)]
)
def test_sliding_reset_in_when_previous_bucket_dominates(frozen, prev_hits, cur_hits, elapsed):
    b = SlidingWindowBackend("default", "t")
    limit = 6
    for _ in range(prev_hits):
        b.hit("c", "*", 10, limit)
    frozen["t"] = 1010.0 + elapsed * 10
    last = None
    for _ in range(cur_hits + 1):
        last = b.hit("c", "*", 10, limit)
    assert last.remaining == 0, "parametrization must reach the limit"
    frozen["t"] += last.reset_in
    assert not b.hit("c", "*", 10, limit).exceeded


def test_sliding_reset_in_under_limit_is_window_remainder(frozen):
    b = SlidingWindowBackend("default", "t")
    frozen["t"] = 1003.0
    assert b.hit("c", "*", 10, 100).reset_in == 7


# -- batch API / lockout helpers (shared by every backend) -------------------------


@pytest.mark.parametrize("backend_cls", [FixedWindowBackend, SlidingWindowBackend])
def test_hit_many_and_peek_many(frozen, backend_cls):
    b = backend_cls("default", "t")
    specs = [("c", "a", 10, 2), ("c", "b", 10, 5)]
    for _ in range(3):
        results = b.hit_many(specs)
    assert [r.count for r in results] == [3, 3]
    assert results[0].exceeded and results[0].just_exceeded and not results[1].exceeded
    # The 3rd request was rejected by rule "a": neither rule keeps it (rollback).
    assert [p.count for p in b.peek_many(specs)] == [2, 2]
    again = b.hit_many(specs)
    assert again[0].exceeded and not again[0].just_exceeded  # notified once per window
    assert b.hit_many([]) == [] and b.peek_many([]) == []


@pytest.mark.parametrize("backend_cls", [FixedWindowBackend, SlidingWindowBackend])
def test_observe_only_rules_keep_counting(frozen, backend_cls):
    """A rule with enforce=False (BLOCK=False) never rejects, so nothing is rolled back and
    its counter keeps growing past the limit."""
    b = backend_cls("default", "t")
    for _ in range(5):
        last = b.hit_many([("c", "obs", 10, 2, False)])[0]
    assert last.count == 5 and last.exceeded
    assert b.peek("c", "obs", 10, 2).count == 5


@pytest.mark.parametrize("backend_cls", [FixedWindowBackend, SlidingWindowBackend])
def test_slightly_over_the_limit_client_is_not_starved(frozen, backend_cls):
    """11 evenly spaced requests per minute against 10/min must get ~10 through, not 0."""
    b = backend_cls("default", "t")
    accepted = 0
    for i in range(11 * 5):  # five minutes
        frozen["t"] = 1000.0 + i * (60 / 11)
        if not b.hit("c", "*", 60, 10).exceeded:
            accepted += 1
    assert accepted >= 45, accepted  # fixed: 50, sliding: a little under due to the estimate


def test_fixed_window_retry_after_rounds_up(frozen):
    """Retry-After must be honest: waiting exactly that long lands in the next bucket."""
    b = FixedWindowBackend("default", "t")
    frozen["t"] = 1000.4
    for _ in range(3):
        last = b.hit("c", "*", 10, 2)
    assert last.exceeded and last.reset_in == 10  # 9.6s rounded up, not down to 9
    frozen["t"] += last.reset_in
    assert not b.hit("c", "*", 10, 2).exceeded


def test_lockout_helpers(frozen):
    b = FixedWindowBackend("default", "t")
    assert b.locked_until("c") is None
    assert [b.count_violation("c", 600) for _ in range(3)] == [1, 2, 3]
    assert b.count_violation("other", 600) == 1
    until = b.lock("c", 900)
    assert until == 1900.0 and b.locked_until("c") == 1900.0
    frozen["t"] = 1899.0
    assert b.locked_until("c") == 1900.0
    frozen["t"] = 1900.0
    assert b.locked_until("c") is None
    frozen["t"] = 1000.0
    b.unlock("c")
    assert b.locked_until("c") is None
    frozen["t"] = 1601.0  # violation window rolled over
    assert b.count_violation("c", 600) == 1


# -- redis_lua fallback -----------------------------------------------------------


def test_redis_lua_backend_falls_back_on_non_redis_cache(frozen, caplog, settings):
    from django_trafficwatch.backends.redis_lua import RedisLuaBackend

    if "redis" in settings.CACHES["default"]["BACKEND"].lower():
        pytest.skip("suite is running against Redis")
    import logging

    with caplog.at_level(logging.WARNING, logger="django_trafficwatch"):
        b = RedisLuaBackend("default", "t")
    assert not b.is_native and isinstance(b.fallback, SlidingWindowBackend)
    assert "falling back" in caplog.text
    for _ in range(3):
        last = b.hit("c", "*", 10, 2)
    assert last.count == 3 and last.exceeded
    assert b.peek("c", "*", 10, 2).count == 2  # rejected request rolled back
    assert [r.count for r in b.hit_many([("c", "*", 10, 2), ("c", "x", 10, 2)])] == [3, 1]
    assert b.peek("c", "x", 10, 2).count == 0  # rolled back with the rejected request
    b.reset("c", "*", 10)
    assert b.hit("c", "*", 10, 2).count == 1


def test_raw_redis_client_detection():
    from django.core.cache import caches

    from django_trafficwatch.backends.redis_lua import raw_redis_client

    class FakeDjangoRedis:
        class client:  # noqa: N801
            @staticmethod
            def get_client(write=False):
                return ("django-redis", write)

    assert raw_redis_client(FakeDjangoRedis()) == ("django-redis", True)
    assert raw_redis_client(object()) is None
    if "locmem" in type(caches["default"]).__name__.lower():
        assert raw_redis_client(caches["default"]) is None
