"""Rule model and matching.

A *rule* is "at most ``limit`` requests per ``window`` seconds", optionally restricted to
some HTTP methods. Rules come from three places, in priority order:

1. ``@trafficwatch_rule(...)`` on the view (may be stacked for several limits),
2. ``TRAFFICWATCH["PATH_RULES"]`` matched against the request path,
3. the global ``WINDOW_SECONDS`` / ``MAX_REQUESTS`` pair.

``PATH_RULES`` keys are path prefixes, or regular expressions when wrapped as
``"re:<pattern>"``. Values are either one rule dict or a list of rule dicts so that a single
endpoint can carry several limits (e.g. 5/minute *and* 100/day)::

    PATH_RULES = {
        "/api/login/": [
            {"WINDOW_SECONDS": 60, "MAX_REQUESTS": 5, "METHODS": ["POST"]},
            {"WINDOW_SECONDS": 86400, "MAX_REQUESTS": 100, "NAME": "login-daily"},
        ],
        "re:^/api/v\\d+/export/": {"WINDOW_SECONDS": 3600, "MAX_REQUESTS": 10},
    }

Matching: regex keys are tried first in declaration order (first match wins); otherwise the
longest matching prefix wins. Within the selected entry only rules whose ``METHODS`` include
the request method apply. If none apply the global rule is used.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from django.utils.module_loading import import_string

REGEX_PREFIX = "re:"

_KNOWN_KEYS = {"WINDOW_SECONDS", "MAX_REQUESTS", "METHODS", "NAME", "BLOCK", "KEY_FUNC"}


class RuleConfigError(ValueError):
    """Raised for a malformed PATH_RULES entry. Also reported by the system check."""


@dataclass(frozen=True)
class Rule:
    name: str
    window: int
    limit: int
    methods: frozenset[str] | None = None  # None -> every method
    block: bool | None = None  # None -> fall back to the global BLOCK setting
    key_func: Callable[..., str] | None = None  # None -> global KEY_FUNC

    def applies_to(self, method: str) -> bool:
        return self.methods is None or method.upper() in self.methods

    def __str__(self) -> str:
        return f"{self.name}: {self.limit}/{self.window}s"


def _positive_int(value: Any, field: str, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuleConfigError(f"{where}: {field} must be a positive integer, got {value!r}")
    return value


def rule_from_dict(
    raw: Mapping[str, Any],
    *,
    name: str,
    default_window: int,
    default_limit: int,
    where: str = "PATH_RULES",
) -> Rule:
    if not isinstance(raw, Mapping):
        raise RuleConfigError(f"{where}: rule must be a dict, got {type(raw).__name__}")
    unknown = set(raw) - _KNOWN_KEYS
    if unknown:
        raise RuleConfigError(f"{where}: unknown rule keys {sorted(unknown)}")

    window = _positive_int(raw.get("WINDOW_SECONDS", default_window), "WINDOW_SECONDS", where)
    limit = _positive_int(raw.get("MAX_REQUESTS", default_limit), "MAX_REQUESTS", where)

    methods = raw.get("METHODS")
    if methods is not None:
        if isinstance(methods, str) or not isinstance(methods, Iterable):
            raise RuleConfigError(f"{where}: METHODS must be a list of HTTP methods")
        methods = frozenset(m.upper() for m in methods)
        if not methods:
            raise RuleConfigError(f"{where}: METHODS must not be empty")

    block = raw.get("BLOCK")
    if block is not None and not isinstance(block, bool):
        raise RuleConfigError(f"{where}: BLOCK must be a boolean")

    key_func = raw.get("KEY_FUNC")
    if isinstance(key_func, str):
        key_func = import_string(key_func)
    if key_func is not None and not callable(key_func):
        raise RuleConfigError(f"{where}: KEY_FUNC must be callable or a dotted path")

    return Rule(
        name=str(raw.get("NAME", name)),
        window=window,
        limit=limit,
        methods=methods,
        block=block,
        key_func=key_func,
    )


def rules_from_entry(
    key: str, value: Any, *, default_window: int, default_limit: int
) -> tuple[Rule, ...]:
    """Normalise one ``PATH_RULES`` entry (dict or list of dicts) into Rule objects."""
    entries = value if isinstance(value, (list, tuple)) else [value]
    if not entries:
        raise RuleConfigError(f"PATH_RULES[{key!r}]: empty rule list")
    rules = []
    for i, raw in enumerate(entries):
        name = key if len(entries) == 1 else f"{key}#{i}"
        rules.append(
            rule_from_dict(
                raw,
                name=name,
                default_window=default_window,
                default_limit=default_limit,
                where=f"PATH_RULES[{key!r}]",
            )
        )
    return tuple(rules)


@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


class RuleSet:
    """Compiled view of ``PATH_RULES`` plus the global rule. Built by ``conf.tw_settings``
    and cached until ``TRAFFICWATCH`` changes."""

    def __init__(self, path_rules: Mapping[str, Any], default_window: int, default_limit: int):
        if not isinstance(path_rules, Mapping):
            raise RuleConfigError("PATH_RULES must be a dict")
        default_window = _positive_int(default_window, "WINDOW_SECONDS", "TRAFFICWATCH")
        default_limit = _positive_int(default_limit, "MAX_REQUESTS", "TRAFFICWATCH")
        self.global_rule = Rule(name="*", window=default_window, limit=default_limit)
        self._regex: list[tuple[re.Pattern[str], tuple[Rule, ...]]] = []
        self._prefix: list[tuple[str, tuple[Rule, ...]]] = []
        for key, value in path_rules.items():
            if not isinstance(key, str) or not key:
                raise RuleConfigError(f"PATH_RULES: key must be a non-empty string, got {key!r}")
            rules = rules_from_entry(
                key, value, default_window=default_window, default_limit=default_limit
            )
            if key.startswith(REGEX_PREFIX):
                try:
                    compiled = _compile(key[len(REGEX_PREFIX) :])
                except re.error as exc:
                    raise RuleConfigError(f"PATH_RULES[{key!r}]: invalid regex: {exc}") from exc
                self._regex.append((compiled, rules))
            else:
                self._prefix.append((key, rules))
        # Longest prefix first so the first hit wins.
        self._prefix.sort(key=lambda item: len(item[0]), reverse=True)

    def match(self, path: str) -> tuple[Rule, ...] | None:
        """Rules configured for ``path`` (before method filtering), or None."""
        for pattern, rules in self._regex:
            if pattern.search(path):
                return rules
        for prefix, rules in self._prefix:
            if path.startswith(prefix):
                return rules
        return None

    def for_request(self, path: str, method: str) -> tuple[Rule, ...]:
        """Rules that apply to this path *and* method. Never empty: falls back to global."""
        matched = self.match(path)
        if matched:
            applicable = tuple(r for r in matched if r.applies_to(method))
            if applicable:
                return applicable
        return (self.global_rule,)
