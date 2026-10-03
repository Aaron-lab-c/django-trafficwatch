"""Redis backend evaluating every rule for a client in a single round trip.

Selected with ``TRAFFICWATCH["BACKEND"] = "redis"``. It talks to the raw ``redis-py`` client
behind Django's ``RedisCache`` (or ``django-redis``) and runs one Lua script that, for *all*
rules at once, increments the current bucket, reads the previous one, decides admission,
rolls the increments back when the request is rejected and sets the once-per-window
notification marker. A request guarded by three rules costs one network round trip. The
arithmetic is the same two-bucket sliding estimate as ``SlidingWindowBackend`` (see there
for ``Retry-After`` and the "rejected requests are not counted" rule).

Keys are hash-tagged with the client (``tw:rl:{ip:1.2.3.4}:<rule>:<bucket>``) so that all
keys touched by one script call live in the same Redis Cluster slot.

Degrades gracefully: when ``CACHE_ALIAS`` is not a Redis cache (LocMem in tests, Memcached)
a warning is logged once and every call is delegated to ``SlidingWindowBackend``, which has
identical semantics. ``manage.py check`` reports this as ``trafficwatch.W006``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from itertools import groupby
from typing import Any

from .base import BaseBackend, HitResult, HitSpec, spec_parts
from .sliding_window import SlidingWindowBackend, two_bucket_result

logger = logging.getLogger("django_trafficwatch")

# KEYS: cur_1, prev_1, marker_1, cur_2, prev_2, marker_2, ...
# ARGV: n, then per rule: ttl, weight (1 - elapsed), limit, enforce ("1"/"0"), marker_ttl
# Returns: [rejected, cur_1, prev_1, first_1, cur_2, prev_2, first_2, ...]
HIT_SCRIPT = """
local n = tonumber(ARGV[1])
local cur, prev, exceeded = {}, {}, {}
local rejected = false
for i = 1, n do
  local a = (i - 1) * 5
  local ttl, w, limit = tonumber(ARGV[a + 2]), tonumber(ARGV[a + 3]), tonumber(ARGV[a + 4])
  local enforce = ARGV[a + 5] == '1'
  local ck, pk = KEYS[3 * i - 2], KEYS[3 * i - 1]
  local c = redis.call('INCR', ck)
  if c == 1 then redis.call('EXPIRE', ck, ttl) end
  local p = tonumber(redis.call('GET', pk) or '0')
  cur[i], prev[i] = c, p
  exceeded[i] = math.floor(p * w + c) > limit
  if exceeded[i] and enforce then rejected = true end
end
local out = { rejected and 1 or 0 }
for i = 1, n do
  if rejected then redis.call('DECR', KEYS[3 * i - 2]) end
  local first = 0
  if exceeded[i] then
    local mttl = tonumber(ARGV[(i - 1) * 5 + 6])
    if redis.call('SET', KEYS[3 * i], '1', 'NX', 'EX', mttl) then first = 1 end
  end
  out[#out + 1] = cur[i]
  out[#out + 1] = prev[i]
  out[#out + 1] = first
end
return out
"""


def raw_redis_client(cache: Any) -> Any | None:
    """The underlying redis-py client of a Django cache, or None if it is not Redis."""
    # django.core.cache.backends.redis.RedisCache
    inner = getattr(cache, "_cache", None)
    get_client = getattr(inner, "get_client", None)
    if callable(get_client) and type(inner).__name__ == "RedisCacheClient":
        return get_client(None, write=True)
    # django-redis: cache.client.get_client(write=True)
    client_wrapper = getattr(cache, "client", None)
    get_client = getattr(client_wrapper, "get_client", None)
    if callable(get_client):
        try:
            return get_client(write=True)
        except TypeError:
            return None
    return None


class RedisLuaBackend(BaseBackend):
    def __init__(self, cache_alias: str, prefix: str):
        super().__init__(cache_alias, prefix)
        self.redis = raw_redis_client(self.cache)
        self.fallback: SlidingWindowBackend | None = None
        self._script: Any = None
        if self.redis is None:
            logger.warning(
                "TRAFFICWATCH BACKEND='redis' but cache %r is not a Redis cache; "
                "falling back to the sliding-window backend.",
                cache_alias,
            )
            self.fallback = SlidingWindowBackend(cache_alias, prefix)
        else:
            self._script = self.redis.register_script(HIT_SCRIPT)

    @property
    def is_native(self) -> bool:
        return self.fallback is None

    def _key(self, client: str, rule: str, bucket: int) -> str:
        return f"{self.prefix}:rl:{{{client}}}:{rule}:{bucket}"

    def _marker_key(self, client: str, rule: str) -> str:
        return f"{self.prefix}:rl:{{{client}}}:{rule}:x"

    # -- single-rule API (delegates to the batch one) ------------------------------------

    def hit(self, client: str, rule: str, window: int, limit: int) -> HitResult:
        return self.hit_many([(client, rule, window, limit, True)])[0]

    def peek(self, client: str, rule: str, window: int, limit: int) -> HitResult:
        results = self.peek_many([(client, rule, window, limit)])
        assert results is not None
        return results[0]

    def reset(self, client: str, rule: str, window: int) -> None:
        if self.fallback is not None:
            self.fallback.reset(client, rule, window)
            return
        assert self.redis is not None
        bucket = int(time.time() // window)
        self.redis.delete(
            self._key(client, rule, bucket),
            self._key(client, rule, bucket - 1),
            self._marker_key(client, rule),
        )

    # -- batch API ---------------------------------------------------------------------

    def hit_many(self, specs: Sequence[HitSpec]) -> list[HitResult]:
        if self.fallback is not None:
            return self.fallback.hit_many(specs)
        now = time.time()
        parsed = [spec_parts(spec) for spec in specs]
        results: dict[int, HitResult] = {}
        indexed = sorted(enumerate(parsed), key=lambda item: item[1][0])
        # One script call per distinct client keeps every key in a single cluster slot.
        for _client, group in groupby(indexed, key=lambda item: item[1][0]):
            items = list(group)
            keys: list[str] = []
            args: list[Any] = [len(items)]
            elapsed_by_index: dict[int, float] = {}
            for index, (client, rule, window, limit, enforce) in items:
                bucket = int(now // window)
                elapsed = (now - bucket * window) / window
                elapsed_by_index[index] = elapsed
                keys += [
                    self._key(client, rule, bucket),
                    self._key(client, rule, bucket - 1),
                    self._marker_key(client, rule),
                ]
                # repr() round-trips the double exactly, so Lua sees the same weight.
                args += [window * 2 + 1, repr(1 - elapsed), limit, "1" if enforce else "0", window]
            raw = self._script(keys=keys, args=args)
            rejected = bool(int(raw[0]))
            for n, (index, (_c, _r, window, limit, _e)) in enumerate(items):
                current, previous = int(raw[3 * n + 1]), int(raw[3 * n + 2])
                first = bool(int(raw[3 * n + 3]))
                result = two_bucket_result(
                    previous, current, elapsed_by_index[index], window, limit, counted=not rejected
                )
                results[index] = HitResult(result.count, result.limit, result.reset_in, first)
        return [results[i] for i in range(len(specs))]

    def peek_many(self, specs: Sequence[HitSpec]) -> list[HitResult] | None:
        if self.fallback is not None:
            return self.fallback.peek_many(specs)
        if not specs:
            return []
        assert self.redis is not None
        now = time.time()
        parsed = [spec_parts(spec) for spec in specs]
        keys: list[str] = []
        for client, rule, window, _limit, _enforce in parsed:
            bucket = int(now // window)
            keys += [self._key(client, rule, bucket), self._key(client, rule, bucket - 1)]
        raw = self.redis.mget(keys)
        out = []
        for n, (_client, _rule, window, limit, _enforce) in enumerate(parsed):
            current = int(raw[2 * n] or 0)
            previous = int(raw[2 * n + 1] or 0)
            bucket = int(now // window)
            elapsed = (now - bucket * window) / window
            out.append(two_bucket_result(previous, current, elapsed, window, limit))
        return out
