from __future__ import annotations

import math
import time
from collections.abc import Sequence

from .base import BaseBackend, HitResult, HitSpec, ceil_seconds, spec_parts


def estimate(previous: int, current: int, elapsed: float) -> int:
    # Written as ``previous * weight + current`` with weight = 1 - elapsed so that the Redis
    # Lua script, given the same ``weight`` double, computes the identical integer.
    return math.floor(previous * (1 - elapsed) + current)


def seconds_until_allowed(
    previous: int, current: int, elapsed: float, window: int, limit: int
) -> float:
    """Smallest t >= 0 such that a request at now+t would be accepted, given ``current``
    *accepted* requests in this bucket and ``previous`` in the last one.

    Inside the current bucket the estimate for the next request is
    ``previous * (1 - e) + current + 1``; once the bucket rolls, ``current`` becomes
    the decaying previous bucket and the new bucket starts at 1. The flooring of the
    estimate is ignored here, which errs on the side of waiting slightly longer."""
    remaining_in_bucket = (1 - elapsed) * window
    if current + 1 <= limit:
        # The next request can fit in this bucket once ``previous`` has decayed enough.
        if previous <= 0:
            return 0.0
        needed = 1 - (limit - current - 1) / previous  # elapsed fraction required
        return max(needed - elapsed, 0.0) * window
    # Current bucket is full: wait for the next one, where ``current`` decays.
    needed = 1 - (limit - 1) / current
    return remaining_in_bucket + max(needed, 0.0) * window


def two_bucket_result(
    previous: int, current: int, elapsed: float, window: int, limit: int, *, counted: bool = True
) -> HitResult:
    """Shared by the pure-cache sliding backend and the Redis/Lua backend.

    ``current`` includes this request. With ``counted=False`` the request was rejected and
    rolled back, so ``reset_in`` is computed from the stored value (``current - 1``)."""
    est = estimate(previous, current, elapsed)
    stored = current if counted else current - 1
    if est >= limit:
        wait = seconds_until_allowed(previous, stored, elapsed, window, limit)
        reset_in = ceil_seconds(wait)
    else:
        reset_in = ceil_seconds((1 - elapsed) * window)
    return HitResult(count=est, limit=limit, reset_in=reset_in)


class SlidingWindowBackend(BaseBackend):
    """Approximate sliding window using the current and previous fixed buckets:

        estimate = prev_count * (1 - elapsed_fraction) + current_count

    Smooths the bucket-edge burst of the fixed window while still only using
    atomic ``incr`` operations, so it works on Redis, Memcached and LocMem alike.

    Only accepted requests stay counted (a rejected request is rolled back), otherwise a
    client slightly over the limit would push its own estimate up with every retry and be
    starved almost completely. ``reset_in`` is the number of seconds until *one more*
    request would be accepted (so ``Retry-After`` is honest), or until the window rolls
    over when the client is still under the limit."""

    def _key(self, client: str, rule: str, bucket: int) -> str:
        return f"{self.prefix}:sw:{rule}:{client}:{bucket}"

    # Kept as static methods for backwards compatibility with subclasses / tests.
    _estimate = staticmethod(estimate)
    _seconds_until_allowed = staticmethod(seconds_until_allowed)

    def hit(self, client: str, rule: str, window: int, limit: int) -> HitResult:
        return self.hit_many([(client, rule, window, limit, True)])[0]

    def hit_many(self, specs: Sequence[HitSpec]) -> list[HitResult]:
        now = time.time()
        counted: list[tuple[str, int, int, float, bool]] = []
        for spec in specs:
            client, rule, window, _limit, enforce = spec_parts(spec)
            bucket = int(now // window)
            elapsed = (now - bucket * window) / window  # 0.0 .. 1.0
            key = self._key(client, rule, bucket)
            current = self._incr(key, window * 2 + 1)
            previous = int(self.cache.get(self._key(client, rule, bucket - 1), 0))
            counted.append((key, current, previous, elapsed, enforce))

        rejected = False
        for spec, (_key, current, previous, elapsed, enforce) in zip(specs, counted):
            _c, _r, window, limit, _e = spec_parts(spec)
            if enforce and estimate(previous, current, elapsed) > limit:
                rejected = True
                break

        results = []
        for spec, (key, current, previous, elapsed, _enforce) in zip(specs, counted):
            client, rule, window, limit, _e = spec_parts(spec)
            if rejected:
                self._decr(key)
            result = two_bucket_result(
                previous, current, elapsed, window, limit, counted=not rejected
            )
            first = self.first_crossing(client, rule, window) if result.exceeded else False
            results.append(HitResult(result.count, result.limit, result.reset_in, first))
        return results

    def peek(self, client: str, rule: str, window: int, limit: int) -> HitResult:
        now = time.time()
        bucket = int(now // window)
        elapsed = (now - bucket * window) / window
        current = int(self.cache.get(self._key(client, rule, bucket), 0))
        previous = int(self.cache.get(self._key(client, rule, bucket - 1), 0))
        return two_bucket_result(previous, current, elapsed, window, limit)

    def _result(
        self, previous: int, current: int, elapsed: float, window: int, limit: int
    ) -> HitResult:
        return two_bucket_result(previous, current, elapsed, window, limit)

    def reset(self, client: str, rule: str, window: int) -> None:
        bucket = int(time.time() // window)
        self.cache.delete_many(
            [
                self._key(client, rule, bucket),
                self._key(client, rule, bucket - 1),
                self._marker_key(client, rule),
            ]
        )
