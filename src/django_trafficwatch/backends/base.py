from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Union

from django.core.cache import caches


@dataclass(frozen=True)
class HitResult:
    count: int  # requests attempted in the current window, including this one
    limit: int
    reset_in: int  # seconds until the client may send another request / gets a fresh window
    first: bool | None = None  # True on the first rejection of this window (see below)

    @property
    def remaining(self) -> int:
        return max(self.limit - self.count, 0)

    @property
    def exceeded(self) -> bool:
        return self.count > self.limit

    @property
    def just_exceeded(self) -> bool:
        """True only once per window: the first request that is turned away. Backends set
        ``first`` from an atomic marker; a backend that does not is approximated by
        ``count == limit + 1``."""
        if self.first is not None:
            return self.first
        return self.count == self.limit + 1


# (client, rule, window, limit[, enforce]) as handed to ``hit_many`` / ``peek_many``.
# ``enforce`` (default True) says whether exceeding this rule rejects the request; rules in
# observe-only mode pass False so their counters keep counting served requests.
HitSpec = Union[tuple[str, str, int, int], tuple[str, str, int, int, bool]]


def spec_parts(spec: HitSpec) -> tuple[str, str, int, int, bool]:
    client, rule, window, limit = spec[0], spec[1], spec[2], spec[3]
    enforce = bool(spec[4]) if len(spec) > 4 else True
    return client, rule, window, limit, enforce


def ceil_seconds(seconds: float) -> int:
    """``Retry-After`` must never be short: round up and never report 0."""
    return max(math.ceil(seconds), 1)


class BaseBackend:
    """Contract for counters. Implementations must be safe to call concurrently from many
    processes; the built-in ones only use atomic cache ``incr`` / ``decr`` / ``add``.

    Semantics of the built-in backends: **rejected requests are not counted**. A request is
    counted against every rule first; if any *enforced* rule is exceeded the increments are
    rolled back, so a client that keeps retrying does not starve itself and a request blocked
    by one rule does not consume the quota of the others. The reported ``count`` still
    includes the attempt (``limit + 1`` when turned away) so ``exceeded`` / ``remaining`` read
    naturally. ``HitResult.first`` is set from an atomic one-per-window marker.

    ``peek`` is optional (used by the inspection views) and may return ``None`` when a
    backend cannot answer without mutating state.

    ``hit_many`` / ``peek_many`` evaluate several rules at once; the Redis backend does it in
    one round trip. A third-party backend may implement only ``hit`` / ``reset``: the default
    ``hit_many`` loops over ``hit`` (without rollback) and adds the first-crossing marker.

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
        results = []
        for spec in specs:
            client, rule, window, limit, _enforce = spec_parts(spec)
            result = self.hit(client, rule, window, limit)
            if result.first is None and result.exceeded:
                result = replace(result, first=self.first_crossing(client, rule, window))
            results.append(result)
        return results

    def peek_many(self, specs: Sequence[HitSpec]) -> list[HitResult] | None:
        results = []
        for spec in specs:
            client, rule, window, limit, _enforce = spec_parts(spec)
            result = self.peek(client, rule, window, limit)
            if result is None:
                return None
            results.append(result)
        return results

    # -- first-crossing marker ----------------------------------------------------

    def _marker_key(self, client: str, rule: str) -> str:
        return f"{self.prefix}:x:{rule}:{client}"

    def first_crossing(self, client: str, rule: str, ttl: int) -> bool:
        """True the first time it is called for ``client``/``rule`` within ``ttl`` seconds
        (atomic ``add``). Drives the once-per-window notification."""
        return bool(self.cache.add(self._marker_key(client, rule), 1, timeout=max(ttl, 1)))

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

    def _decr(self, key: str) -> None:
        """Undo one ``_incr`` (rollback of a rejected request). A key that expired in
        between simply stays gone."""
        try:
            self.cache.decr(key)
        except ValueError:
            pass
