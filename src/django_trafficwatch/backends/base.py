from __future__ import annotations

from dataclasses import dataclass

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


class BaseBackend:
    """Contract for counters. Implementations must be safe to call concurrently from many
    processes; the built-in ones only use atomic cache ``incr``.

    ``peek`` is optional (used by the DRF throttle and the inspection command) and may return
    ``None`` when a backend cannot answer without mutating state."""

    def __init__(self, cache_alias: str, prefix: str):
        self.cache = caches[cache_alias]
        self.prefix = prefix

    def hit(self, client: str, rule: str, window: int, limit: int) -> HitResult:
        raise NotImplementedError

    def peek(self, client: str, rule: str, window: int, limit: int) -> HitResult | None:
        """Current state without counting a request."""
        return None

    def reset(self, client: str, rule: str, window: int) -> None:
        raise NotImplementedError

    # helper shared by backends
    def _incr(self, key: str, ttl: int) -> int:
        self.cache.add(key, 0, timeout=ttl)
        try:
            return self.cache.incr(key)
        except ValueError:  # expired between add and incr
            self.cache.set(key, 1, timeout=ttl)
            return 1
