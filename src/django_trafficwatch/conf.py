"""Settings access. Everything is read lazily from ``settings.TRAFFICWATCH`` so that
``override_settings`` works in tests and values can be changed without restart. The compiled
``RuleSet`` is cached and rebuilt whenever ``TRAFFICWATCH`` changes (``setting_changed``)."""

from __future__ import annotations

from django.conf import settings as django_settings
from django.core.signals import setting_changed
from django.utils.module_loading import import_string

from .rules import Rule, RuleSet

BUILTIN_BACKENDS = {
    "fixed": "django_trafficwatch.backends.fixed_window.FixedWindowBackend",
    "sliding": "django_trafficwatch.backends.sliding_window.SlidingWindowBackend",
}

DEFAULTS: dict = {
    # Global rule: at most MAX_REQUESTS per WINDOW_SECONDS.
    "WINDOW_SECONDS": 60,
    "MAX_REQUESTS": 100,
    # Per-path rules overriding the global one. See rules.py for the full format.
    # {"/api/login/": {"WINDOW_SECONDS": 300, "MAX_REQUESTS": 5, "METHODS": ["POST"]}}
    "PATH_RULES": {},
    # Path prefixes that are never counted.
    "EXEMPT_PATHS": ["/static/", "/media/"],
    # HTTP methods that are never counted (CORS preflight, health probes).
    "EXEMPT_METHODS": ["OPTIONS"],
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
    # Callable or dotted path: (request, info) -> None, run once per exceeded window.
    "ON_EXCEEDED": None,
    # "fixed", "sliding" or a dotted path to a Backend subclass.
    "BACKEND": "fixed",
    "CACHE_ALIAS": "default",
    "CACHE_PREFIX": "tw",
    # Attach X-RateLimit-* headers to responses.
    "HEADERS": True,
    # Keep the last N limit violations in the cache for inspection
    # (``python manage.py trafficwatch_recent``). 0 disables.
    "RECENT_VIOLATIONS": 100,
}

_IMPORTABLE = {"KEY_FUNC", "ON_EXCEEDED", "BLOCK_RESPONSE"}


class TrafficWatchSettings:
    def __init__(self):
        self._ruleset: RuleSet | None = None
        setting_changed.connect(self._on_setting_changed)

    def _on_setting_changed(self, *, setting, **kwargs):
        if setting == "TRAFFICWATCH":
            self._ruleset = None

    def _user(self) -> dict:
        return getattr(django_settings, "TRAFFICWATCH", {}) or {}

    def __getattr__(self, name: str):
        if name.startswith("_") or name not in DEFAULTS:
            raise AttributeError(f"Unknown TRAFFICWATCH setting: {name}")
        value = self._user().get(name, DEFAULTS[name])
        if name in _IMPORTABLE and isinstance(value, str):
            value = import_string(value)
        return value

    def backend_class(self):
        path = self.BACKEND
        path = BUILTIN_BACKENDS.get(path, path)
        return import_string(path)

    @property
    def ruleset(self) -> RuleSet:
        if self._ruleset is None:
            self._ruleset = RuleSet(self.PATH_RULES, self.WINDOW_SECONDS, self.MAX_REQUESTS)
        return self._ruleset

    def rules_for(self, path: str, method: str = "GET") -> tuple[Rule, ...]:
        """Applicable rules for a request path and method (never empty)."""
        return self.ruleset.for_request(path, method)

    def rule_for(self, path: str) -> tuple[int, int, str]:
        """Backwards-compatible single-rule lookup: ``(window, limit, name)`` of the first
        applicable rule for ``path`` (any method)."""
        rule = self.rules_for(path)[0]
        return rule.window, rule.limit, rule.name


tw_settings = TrafficWatchSettings()
