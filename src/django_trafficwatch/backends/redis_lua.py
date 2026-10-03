"""Redis backend evaluating every rule for a client in a single round trip.

Selected with ``TRAFFICWATCH["BACKEND"] = "redis"``. It talks to the raw ``redis-py`` client
behind Django's ``RedisCache`` (or ``django-redis``) and runs one Lua script that increments
the current bucket and reads the previous one for *all* rules at once, so a request guarded
by three rules costs one network round trip instead of six. The arithmetic is the same
two-bucket sliding estimate as ``SlidingWindowBackend`` (see there for ``Retry-After``).

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

from .base import BaseBackend, HitResult, HitSpec
from .sliding_window import SlidingWindowBackend, two_bucket_result

logger = logging.getLogger("django_trafficwatch")

# KEYS: current_1, previous_1, current_2, previous_2, ...   ARGV: ttl_1, ttl_2, ...
# Returns [current_1, previous_1, current_2, previous_2, ...]
HIT_SCRIPT = """
local out = {}
local n = #KEYS / 2
for i = 1, n do
  local cur = KEYS[2 * i - 1]
  local prev = KEYS[2 * i]
  local c = redis.call('INCR', cur)
  if c == 1 then
    redis.call('EXPIRE', cur, tonumber(ARGV[i]))
  end
  local p = redis.call('GET', prev)
  out[#out + 1] = c
  out[#out + 1] = tonumber(p) or 0
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

    # -- single-rule API (delegates to the batch one) ------------------------------------

    def hit(self, client: str, rule: str, window: int, limit: int) -> HitResult:
        return self.hit_many([(client, rule, window, limit)])[0]

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
        self.redis.delete(self._key(client, rule, bucket), self._key(client, rule, bucket - 1))

    # -- batch API ---------------------------------------------------------------------

    def hit_many(self, specs: Sequence[HitSpec]) -> list[HitResult]:
        if self.fallback is not None:
            return self.fallback.hit_many(specs)
        now = time.time()
        results: dict[int, HitResult] = {}
        indexed = sorted(enumerate(specs), key=lambda item: item[1][0])
        # One script call per distinct client keeps every key in a single cluster slot.
        for _client, group in groupby(indexed, key=lambda item: item[1][0]):
            items = list(group)
            keys: list[str] = []
            ttls: list[int] = []
            for _, (client, rule, window, _limit) in items:
                bucket = int(now // window)
                keys += [self._key(client, rule, bucket), self._key(client, rule, bucket - 1)]
                ttls.append(window * 2 + 1)
            raw = self._script(keys=keys, args=ttls)
            for n, (index, (_c, _r, window, limit)) in enumerate(items):
                current, previous = int(raw[2 * n]), int(raw[2 * n + 1])
                bucket = int(now // window)
                elapsed = (now - bucket * window) / window
                results[index] = two_bucket_result(previous, current, elapsed, window, limit)
        return [results[i] for i in range(len(specs))]

    def peek_many(self, specs: Sequence[HitSpec]) -> list[HitResult] | None:
        if self.fallback is not None:
            return self.fallback.peek_many(specs)
        if not specs:
            return []
        now = time.time()
        keys: list[str] = []
        for client, rule, window, _limit in specs:
            bucket = int(now // window)
            keys += [self._key(client, rule, bucket), self._key(client, rule, bucket - 1)]
        assert self.redis is not None
        raw = self.redis.mget(keys)
        out = []
        for n, (_client, _rule, window, limit) in enumerate(specs):
            current = int(raw[2 * n] or 0)
            previous = int(raw[2 * n + 1] or 0)
            bucket = int(now // window)
            elapsed = (now - bucket * window) / window
            out.append(two_bucket_result(previous, current, elapsed, window, limit))
        return out
