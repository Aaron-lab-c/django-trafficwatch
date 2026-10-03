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
    # Current bucket is full (6 > 5) so we must wait for the roll-over, then until
    # 6 * (1 - e) + 1 <= 5  ->  e >= 1/3  ->  ~3.34s into the next bucket.
    assert blocked.reset_in == 14
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
