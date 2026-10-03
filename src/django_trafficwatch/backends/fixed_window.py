from __future__ import annotations

import time

from .base import BaseBackend, HitResult


class FixedWindowBackend(BaseBackend):
    """Counts requests in aligned buckets of ``window`` seconds. Cheapest and atomic
    on every Django cache backend, but allows up to 2x the limit across a bucket edge."""

    def _key(self, client: str, rule: str, bucket: int) -> str:
        return f"{self.prefix}:fw:{rule}:{client}:{bucket}"

    @staticmethod
    def _reset_in(now: float, bucket: int, window: int) -> int:
        return max(int((bucket + 1) * window - now), 1)

    def hit(self, client: str, rule: str, window: int, limit: int) -> HitResult:
        now = time.time()
        bucket = int(now // window)
        count = self._incr(self._key(client, rule, bucket), window + 1)
        return HitResult(count=count, limit=limit, reset_in=self._reset_in(now, bucket, window))

    def peek(self, client: str, rule: str, window: int, limit: int) -> HitResult:
        now = time.time()
        bucket = int(now // window)
        count = int(self.cache.get(self._key(client, rule, bucket), 0))
        return HitResult(count=count, limit=limit, reset_in=self._reset_in(now, bucket, window))

    def reset(self, client: str, rule: str, window: int) -> None:
        bucket = int(time.time() // window)
        self.cache.delete(self._key(client, rule, bucket))
