from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from django.core.cache import caches


@dataclass(frozen=True)
class HitResult:
    count: int  # requests seen in the current window, including this one
    limit: int
    reset_in: int  # seconds until the client may send another request / gets a fresh window

    @property
    def remaining(self) -> int:
        return max(self.limit - self.count, 0)

    @property
    def exceeded(self) -> bool:
        return self.count > self.limit

    @property
    def just_exceeded(self) -> bool:
        """True only for the first request that crosses the limit."""
        return self.count == self.limit + 1


# (client, rule, window, limit) as handed to ``hit_many`` / ``peek_many``.
HitSpec = tuple[str, str, int, int]


class BaseBackend:
    """Contract for counters. Implementations must be safe to call concurrently from many
    processes; the built-in ones only use atomic cache ``incr``.

    ``peek`` is optional (used by the inspection views) and may return ``None`` when a
    backend cannot answer without mutating state.

    ``hit_many`` / ``peek_many`` evaluate several rules at once; the default implementation
    loops over ``hit`` / ``peek``, a backend with a richer protocol (Redis + Lua) overrides
    them to do it in one round trip.

    Lockout support (``count_violation`` / ``lock`` / ``locked_until`` / ``unlock``) is
    implemented here on top of the plain cache API and works for every backend."""

    def __init__(self, cache_alias: str, prefix: str):
        self.cache: Any = caches[cache_alias]
        self.prefix = prefix

    # -- counters ---------------------------------------------------------------

    def hit(self, client: str, rule: str, window: int, limit: int) -> HitResult:
        raise NotImplementedError

    def peek(self, client: str, rule: str, window: int, limit: int) -> HitResult | None:
        """Current state without counting a request."""
        return None

    def reset(self, client: str, rule: str, window: int) -> None:
        raise NotImplementedError

    def hit_many(self, specs: Sequence[HitSpec]) -> list[HitResult]:
        return [self.hit(*spec) for spec in specs]

    def peek_many(self, specs: Sequence[HitSpec]) -> list[HitResult] | None:
        results = []
        for spec in specs:
            result = self.peek(*spec)
            if result is None:
                return None
            results.append(result)
        return results

    # -- lockout ----------------------------------------------------------------

    def _violations_key(self, client: str, bucket: int) -> str:
        return f"{self.prefix}:viol:{client}:{bucket}"

    def _lock_key(self, client: str) -> str:
        return f"{self.prefix}:lock:{client}"

    def count_violation(self, client: str, window: int) -> int:
        """Record one limit crossing for ``client``; returns the number of crossings in the
        current fixed ``window``."""
        bucket = int(time.time() // window)
        return self._incr(self._violations_key(client, bucket), window + 1)

    def lock(self, client: str, duration: int) -> float:
        """Block ``client`` for ``duration`` seconds; returns the expiry timestamp."""
        until = time.time() + duration
        self.cache.set(self._lock_key(client), until, timeout=duration)
        return until

    def locked_until(self, client: str) -> float | None:
        """Expiry timestamp of an active lockout, else None."""
        until = self.cache.get(self._lock_key(client))
        if until is None:
            return None
        return float(until) if float(until) > time.time() else None

    def unlock(self, client: str) -> None:
        self.cache.delete(self._lock_key(client))

    # -- helpers ----------------------------------------------------------------

    def _incr(self, key: str, ttl: int) -> int:
        self.cache.add(key, 0, timeout=ttl)
        try:
            return int(self.cache.incr(key))
        except ValueError:  # expired between add and incr
            self.cache.set(key, 1, timeout=ttl)
            return 1
