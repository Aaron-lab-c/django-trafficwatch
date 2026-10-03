"""Settings access. Everything is read lazily from ``settings.TRAFFICWATCH`` so that
``override_settings`` works in tests and values can be changed without restart. The compiled
``RuleSet`` (and the parsed ``LOCKOUT``) are cached and rebuilt whenever ``TRAFFICWATCH``
changes (``setting_changed``).

``validate()`` performs every import / compilation eagerly; the middleware calls it at
construction so a typo fails at process start instead of on the first request."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.utils.module_loading import import_string

from .rules import Rule, RuleConfigError, RuleSet

BUILTIN_BACKENDS = {
    "fixed": "django_trafficwatch.backends.fixed_window.FixedWindowBackend",
    "sliding": "django_trafficwatch.backends.sliding_window.SlidingWindowBackend",
    "redis": "django_trafficwatch.backends.redis_lua.RedisLuaBackend",
}

HEADER_STYLES = ("x-ratelimit", "ietf", "both")

DEFAULTS: dict[str, Any] = {
    # Global rule: at most MAX_REQUESTS per WINDOW_SECONDS.
    "WINDOW_SECONDS": 60,
    "MAX_REQUESTS": 100,
    # Per-path rules overriding the global one. See rules.py for the full format.
    # {"/api/login/": {"WINDOW_SECONDS": 300, "MAX_REQUESTS": 5, "METHODS": ["POST"]}}
    "PATH_RULES": {},
    # Also count requests that never reach a view: 404s from the URL resolver and responses
    # produced by middleware above this one (redirects, CSRF failures). They are matched
    # against PATH_RULES / the global rule. False restores the pre-0.4 behaviour.
    "COUNT_UNROUTED": True,
    # Match EXEMPT_PATHS / PATH_RULES against request.path_info (path without SCRIPT_NAME)
    # instead of request.path. Useful when the project is mounted under a URL prefix.
    "MATCH_PATH_INFO": False,
    # Path prefixes that are never counted.
    "EXEMPT_PATHS": ["/static/", "/media/"],
    # HTTP methods that are never counted (CORS preflight, health probes).
    "EXEMPT_METHODS": ["OPTIONS"],
    # Client IPs / CIDR networks that are never counted (matched against the resolved
    # client IP, i.e. after the trusted-proxy logic).
    "EXEMPT_CLIENTS": [],
    # Callable or dotted path: (request) -> bool; True exempts the request (e.g. superusers).
    "EXEMPT_FUNC": None,
    # Block (return BLOCK_STATUS) or only observe.
    "BLOCK": True,
    "BLOCK_STATUS": 429,
    "BLOCK_MESSAGE": "Too many requests, please slow down.",
    # Optional callable or dotted path: (request, info) -> HttpResponse, replaces the
    # default JSON 429 body (e.g. render an HTML template). Retry-After is still added.
    "BLOCK_RESPONSE": None,
    # Callable or dotted path: (request) -> str identifying the client.
    "KEY_FUNC": "django_trafficwatch.keys.user_or_ip",
    # Proxies whose X-Forwarded-For header may be trusted (IPs or CIDR networks).
    # Empty (default) = ignore X-Forwarded-For and key on REMOTE_ADDR.
    "TRUSTED_PROXIES": [],
    # IPv6 clients are keyed on this prefix length (a /64 is one subscriber in practice,
    # and addresses inside it rotate freely). 128 keys on the full address. IPv4 untouched.
    "IPV6_PREFIX": 64,
    # Callable or dotted path: (request, info) -> None, run once per exceeded window.
    "ON_EXCEEDED": None,
    # "fixed", "sliding", "redis" or a dotted path to a Backend subclass.
    "BACKEND": "fixed",
    "CACHE_ALIAS": "default",
    "CACHE_PREFIX": "tw",
    # When the cache backend raises (Redis down): True lets the request through and marks
    # ``request.trafficwatch.degraded``; False returns the block response.
    "FAIL_OPEN": True,
    # Log a cache outage at most once per this many seconds.
    "FAIL_OPEN_LOG_INTERVAL": 60,
    # Escalating lockout: after VIOLATIONS first-crossings within WINDOW_SECONDS the client
    # is blocked for DURATION_SECONDS regardless of rule. None disables.
    # {"VIOLATIONS": 3, "WINDOW_SECONDS": 600, "DURATION_SECONDS": 900}
    "LOCKOUT": None,
    # Attach rate-limit headers to responses.
    "HEADERS": True,
    # "x-ratelimit" (X-RateLimit-Limit/Remaining/Reset), "ietf" (RateLimit-Policy / RateLimit
    # per draft-ietf-httpapi-ratelimit-headers) or "both".
    "HEADERS_STYLE": "x-ratelimit",
    # Emit X-RateLimit-Reset as a Unix timestamp instead of seconds from now.
    "RESET_AS_EPOCH": False,
    # Keep the last N limit violations in the cache for inspection
    # (``python manage.py trafficwatch_recent`` / the staff-only views). 0 disables.
    "RECENT_VIOLATIONS": 100,
}

_IMPORTABLE = {"KEY_FUNC", "ON_EXCEEDED", "BLOCK_RESPONSE", "EXEMPT_FUNC"}

BOOLEAN_SETTINGS = (
    "BLOCK",
    "HEADERS",
    "FAIL_OPEN",
    "RESET_AS_EPOCH",
    "MATCH_PATH_INFO",
    "COUNT_UNROUTED",
)

_LOCKOUT_KEYS = {"VIOLATIONS", "WINDOW_SECONDS", "DURATION_SECONDS"}


@dataclass(frozen=True)
class LockoutConfig:
    violations: int
    window: int
    duration: int


def parse_lockout(raw: Any) -> LockoutConfig | None:
    """Validate ``TRAFFICWATCH["LOCKOUT"]``; raises ``RuleConfigError`` when malformed."""
    if raw is None or raw is False:
        return None
    if not isinstance(raw, Mapping):
        raise RuleConfigError("LOCKOUT must be a dict or None")
    unknown = set(raw) - _LOCKOUT_KEYS
    missing = _LOCKOUT_KEYS - set(raw)
    if unknown or missing:
        raise RuleConfigError(
            f"LOCKOUT must have exactly the keys {sorted(_LOCKOUT_KEYS)} "
            f"(unknown: {sorted(unknown)}, missing: {sorted(missing)})"
        )
    values = {}
    for key in _LOCKOUT_KEYS:
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuleConfigError(f"LOCKOUT[{key!r}] must be a positive integer, got {value!r}")
        values[key] = value
    return LockoutConfig(
        violations=values["VIOLATIONS"],
        window=values["WINDOW_SECONDS"],
        duration=values["DURATION_SECONDS"],
    )


def parse_networks(raw: Any, where: str) -> tuple[Any, ...]:
    """Parse a list of IPs / CIDR strings; raises ``RuleConfigError`` on a bad entry."""
    if isinstance(raw, str) or not hasattr(raw, "__iter__"):
        raise RuleConfigError(f"{where} must be a list of IPs or CIDR networks")
    networks = []
    for entry in raw:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise RuleConfigError(f"{where} entry {entry!r} is not an IP or CIDR network") from exc
    return tuple(networks)


class TrafficWatchSettings:
    def __init__(self) -> None:
        self._ruleset: RuleSet | None = None
        self._lockout: LockoutConfig | None = None
        self._lockout_parsed = False
        setting_changed.connect(self._on_setting_changed)

    def _on_setting_changed(self, *, setting: str, **kwargs: Any) -> None:
        if setting == "TRAFFICWATCH":
            self._ruleset = None
            self._lockout = None
            self._lockout_parsed = False

    def _user(self) -> dict[str, Any]:
        return getattr(django_settings, "TRAFFICWATCH", {}) or {}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name not in DEFAULTS:
            raise AttributeError(f"Unknown TRAFFICWATCH setting: {name}")
        value = self._user().get(name, DEFAULTS[name])
        if name in _IMPORTABLE and isinstance(value, str):
            value = import_string(value)
        return value

    def backend_class(self) -> type:
        path = self.BACKEND
        path = BUILTIN_BACKENDS.get(path, path)
        cls = import_string(path)
        assert isinstance(cls, type)
        return cls

    def match_path(self, request: Any) -> str:
        """The path EXEMPT_PATHS and PATH_RULES are matched against."""
        path: str = request.path_info if self.MATCH_PATH_INFO else request.path
        return path

    @property
    def ruleset(self) -> RuleSet:
        if self._ruleset is None:
            self._ruleset = RuleSet(self.PATH_RULES, self.WINDOW_SECONDS, self.MAX_REQUESTS)
        return self._ruleset

    @property
    def lockout(self) -> LockoutConfig | None:
        if not self._lockout_parsed:
            self._lockout = parse_lockout(self.LOCKOUT)
            self._lockout_parsed = True
        return self._lockout

    def rules_for(self, path: str, method: str = "GET") -> tuple[Rule, ...]:
        """Applicable rules for a request path and method (never empty)."""
        return self.ruleset.for_request(path, method)

    def rule_for(self, path: str) -> tuple[int, int, str]:
        """Backwards-compatible single-rule lookup: ``(window, limit, name)`` of the first
        applicable rule for ``path`` (any method)."""
        rule = self.rules_for(path)[0]
        return rule.window, rule.limit, rule.name

    def validate(self) -> None:
        """Eagerly compile / import everything so misconfiguration fails at startup.
        Raises ``ImproperlyConfigured``. ``manage.py check`` reports the same problems
        (and more) with ids instead of raising."""
        problems = []
        for name in ("WINDOW_SECONDS", "MAX_REQUESTS"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                problems.append(f"{name} must be a positive integer, got {value!r}")
        status = self.BLOCK_STATUS
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            problems.append(f"BLOCK_STATUS must be an HTTP status code, got {status!r}")
        for name in ("RECENT_VIOLATIONS", "FAIL_OPEN_LOG_INTERVAL"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                problems.append(f"{name} must be a non-negative integer, got {value!r}")
        for name in BOOLEAN_SETTINGS:
            if not isinstance(getattr(self, name), bool):
                problems.append(f"{name} must be a boolean")
        for name in ("EXEMPT_PATHS", "EXEMPT_METHODS"):
            value = getattr(self, name)
            if isinstance(value, str) or not all(isinstance(p, str) for p in value):
                problems.append(f"{name} must be a list of strings")
        try:
            _ = self.ruleset
        except (RuleConfigError, ImportError, TypeError) as exc:
            problems.append(f"PATH_RULES: {exc}")
        for name in sorted(_IMPORTABLE):
            try:
                value = getattr(self, name)
            except ImportError as exc:
                problems.append(f"{name} cannot be imported: {exc}")
                continue
            if value is not None and not callable(value):
                problems.append(f"{name} must be callable or a dotted path")
        try:
            self.backend_class()
        except ImportError as exc:
            problems.append(f"BACKEND cannot be imported: {exc}")
        for name in ("TRUSTED_PROXIES", "EXEMPT_CLIENTS"):
            try:
                parse_networks(getattr(self, name), name)
            except RuleConfigError as exc:
                problems.append(str(exc))
        try:
            _ = self.lockout
        except RuleConfigError as exc:
            problems.append(str(exc))
        if self.HEADERS_STYLE not in HEADER_STYLES:
            problems.append(f"HEADERS_STYLE must be one of {HEADER_STYLES}")
        prefix = self.IPV6_PREFIX
        if isinstance(prefix, bool) or not isinstance(prefix, int) or not 1 <= prefix <= 128:
            problems.append("IPV6_PREFIX must be an integer between 1 and 128")
        if problems:
            raise ImproperlyConfigured("Invalid TRAFFICWATCH settings: " + "; ".join(problems))


tw_settings = TrafficWatchSettings()
