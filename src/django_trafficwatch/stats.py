"""A small, bounded log of recent limit violations kept in the cache.

This is deliberately simple: one list under ``{CACHE_PREFIX}:recent`` capped at
``RECENT_VIOLATIONS`` entries. The read-modify-write is not atomic, so under heavy
concurrent violations an entry may occasionally be lost; it is a diagnostic aid, not an
audit log. For a durable record use the ``traffic_exceeded`` signal.
"""

from __future__ import annotations

import time
from typing import Any

from django.core.cache import caches

from .conf import tw_settings

RETENTION_SECONDS = 7 * 24 * 3600


def _cache() -> Any:
    return caches[tw_settings.CACHE_ALIAS]


def _key() -> str:
    return f"{tw_settings.CACHE_PREFIX}:recent"


def record_violation(info: dict[str, Any]) -> None:
    keep = int(tw_settings.RECENT_VIOLATIONS or 0)
    if keep <= 0:
        return
    cache = _cache()
    entries = cache.get(_key()) or []
    entries.append({**info, "at": time.time()})
    if len(entries) > keep:
        entries = entries[-keep:]
    cache.set(_key(), entries, timeout=RETENTION_SECONDS)


def recent_violations(limit: int | None = None) -> list[dict[str, Any]]:
    """Most recent violations, newest first."""
    entries = list(_cache().get(_key()) or [])
    entries.reverse()
    return entries[:limit] if limit else entries


def clear_recent_violations() -> None:
    _cache().delete(_key())
