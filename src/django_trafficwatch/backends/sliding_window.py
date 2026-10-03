from __future__ import annotations

import math
import time

from .base import BaseBackend, HitResult


def estimate(previous: int, current: int, elapsed: float) -> int:
    return int(previous * (1 - elapsed) + current)


def seconds_until_allowed(
    previous: int, current: int, elapsed: float, window: int, limit: int
) -> float:
    """Smallest t >= 0 such that a request at now+t would see estimate <= limit.

    Inside the current bucket the estimate for the next request is
    ``previous * (1 - e) + current + 1``; once the bucket rolls, ``current`` becomes
    the decaying previous bucket and the new bucket starts at 1. The int() flooring
    of the estimate is ignored here, which errs on the side of waiting slightly longer."""
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
    previous: int, current: int, elapsed: float, window: int, limit: int
) -> HitResult:
    """Shared by the pure-cache sliding backend and the Redis/Lua backend."""
    est = estimate(previous, current, elapsed)
    if est >= limit:
        wait = seconds_until_allowed(previous, current, elapsed, window, limit)
        reset_in = max(math.ceil(wait), 1)
    else:
        reset_in = max(int((1 - elapsed) * window), 1)
    return HitResult(count=est, limit=limit, reset_in=reset_in)


class SlidingWindowBackend(BaseBackend):
    """Approximate sliding window using the current and previous fixed buckets:

        estimate = prev_count * (1 - elapsed_fraction) + current_count

    Smooths the bucket-edge burst of the fixed window while still only using
    atomic ``incr`` operations, so it works on Redis, Memcached and LocMem alike.

    ``reset_in`` is the number of seconds until *one more* request would be accepted
    under this estimate (so ``Retry-After`` is honest), or until the window rolls over
    when the client is still under the limit."""

    def _key(self, client: str, rule: str, bucket: int) -> str:
        return f"{self.prefix}:sw:{rule}:{client}:{bucket}"

    # Kept as static methods for backwards compatibility with subclasses / tests.
    _estimate = staticmethod(estimate)
    _seconds_until_allowed = staticmethod(seconds_until_allowed)

    def hit(self, client: str, rule: str, window: int, limit: int) -> HitResult:
        now = time.time()
        bucket = int(now // window)
        elapsed = (now - bucket * window) / window  # 0.0 .. 1.0

        current = self._incr(self._key(client, rule, bucket), window * 2 + 1)
        previous = int(self.cache.get(self._key(client, rule, bucket - 1), 0))
        return self._result(previous, current, elapsed, window, limit)

    def peek(self, client: str, rule: str, window: int, limit: int) -> HitResult:
        now = time.time()
        bucket = int(now // window)
        elapsed = (now - bucket * window) / window
        current = int(self.cache.get(self._key(client, rule, bucket), 0))
        previous = int(self.cache.get(self._key(client, rule, bucket - 1), 0))
        return self._result(previous, current, elapsed, window, limit)

    def _result(
        self, previous: int, current: int, elapsed: float, window: int, limit: int
    ) -> HitResult:
        return two_bucket_result(previous, current, elapsed, window, limit)

    def reset(self, client: str, rule: str, window: int) -> None:
        bucket = int(time.time() // window)
        self.cache.delete_many(
            [self._key(client, rule, bucket), self._key(client, rule, bucket - 1)]
        )
