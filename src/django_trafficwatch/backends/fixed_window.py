from __future__ import annotations

import time
from collections.abc import Sequence

from .base import BaseBackend, HitResult, HitSpec, ceil_seconds, spec_parts


class FixedWindowBackend(BaseBackend):
    """Counts requests in aligned buckets of ``window`` seconds. Cheapest and atomic
    on every Django cache backend, but allows up to 2x the limit across a bucket edge.

    Rejected requests are rolled back (see ``BaseBackend``), so a client sending 11/min
    against a 10/min limit gets 10 through and one 429 per minute, and ``Retry-After`` is
    the (rounded-up) time to the next bucket."""

    def _key(self, client: str, rule: str, bucket: int) -> str:
        return f"{self.prefix}:fw:{rule}:{client}:{bucket}"

    @staticmethod
    def _reset_in(now: float, bucket: int, window: int) -> int:
        return ceil_seconds((bucket + 1) * window - now)

    def hit(self, client: str, rule: str, window: int, limit: int) -> HitResult:
        return self.hit_many([(client, rule, window, limit, True)])[0]

    def hit_many(self, specs: Sequence[HitSpec]) -> list[HitResult]:
        now = time.time()
        counted: list[tuple[str, int, HitResult, bool]] = []  # key, reset_in, result, enforce
        for spec in specs:
            client, rule, window, limit, enforce = spec_parts(spec)
            bucket = int(now // window)
            key = self._key(client, rule, bucket)
            count = self._incr(key, window + 1)
            reset_in = self._reset_in(now, bucket, window)
            counted.append((key, reset_in, HitResult(count, limit, reset_in), enforce))

        rejected = any(r.exceeded and enforce for _k, _t, r, enforce in counted)
        results = []
        for spec, (key, reset_in, result, _enforce) in zip(specs, counted):
            client, rule, _w, _l, _e = spec_parts(spec)
            if rejected:
                self._decr(key)
            first = self.first_crossing(client, rule, reset_in) if result.exceeded else False
            results.append(HitResult(result.count, result.limit, reset_in, first))
        return results

    def peek(self, client: str, rule: str, window: int, limit: int) -> HitResult:
        now = time.time()
        bucket = int(now // window)
        count = int(self.cache.get(self._key(client, rule, bucket), 0))
        return HitResult(count=count, limit=limit, reset_in=self._reset_in(now, bucket, window))

    def reset(self, client: str, rule: str, window: int) -> None:
        bucket = int(time.time() // window)
        self.cache.delete_many([self._key(client, rule, bucket), self._marker_key(client, rule)])
