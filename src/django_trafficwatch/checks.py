"""System checks (``python manage.py check``) for the TRAFFICWATCH setting.

IDs: trafficwatch.E0xx are misconfigurations that would fail at request time;
trafficwatch.W0xx are things that work but probably not as intended."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.checks import CheckMessage, Error, Tags, Warning, register
from django.utils.module_loading import import_string

from .conf import (
    _IMPORTABLE,
    BOOLEAN_SETTINGS,
    DEFAULTS,
    HEADER_STYLES,
    parse_lockout,
    parse_networks,
    tw_settings,
)
from .rules import RuleConfigError, RuleSet

MIDDLEWARE_PATH = "django_trafficwatch.middleware.TrafficWatchMiddleware"
AUTH_MIDDLEWARE = "django.contrib.auth.middleware.AuthenticationMiddleware"


def _is_positive_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


@register(Tags.security)
def check_trafficwatch_settings(app_configs: Any, **kwargs: Any) -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    user = getattr(settings, "TRAFFICWATCH", {}) or {}

    if not isinstance(user, dict):
        return [Error("TRAFFICWATCH must be a dict.", id="trafficwatch.E001")]

    unknown = sorted(set(user) - set(DEFAULTS))
    if unknown:
        errors.append(
            Warning(
                f"Unknown TRAFFICWATCH keys: {unknown}.",
                hint=f"Known keys: {sorted(DEFAULTS)}",
                id="trafficwatch.W001",
            )
        )

    for name in ("WINDOW_SECONDS", "MAX_REQUESTS", "FAIL_OPEN_LOG_INTERVAL"):
        value = user.get(name, DEFAULTS[name])
        if not _is_positive_int(value) and not (name == "FAIL_OPEN_LOG_INTERVAL" and value == 0):
            errors.append(
                Error(f"TRAFFICWATCH[{name!r}] must be a positive integer.", id="trafficwatch.E002")
            )

    def _sane(name: str) -> int:  # E002 already covers a bad value; don't double-report
        value = user.get(name, DEFAULTS[name])
        return int(value) if _is_positive_int(value) else 1

    try:
        RuleSet(user.get("PATH_RULES", {}), _sane("WINDOW_SECONDS"), _sane("MAX_REQUESTS"))
    except (RuleConfigError, ImportError, TypeError) as exc:
        errors.append(Error(f"Invalid TRAFFICWATCH PATH_RULES: {exc}", id="trafficwatch.E003"))

    for name in sorted(_IMPORTABLE):
        value = user.get(name)
        if isinstance(value, str):
            try:
                import_string(value)
            except ImportError as exc:
                errors.append(
                    Error(
                        f"TRAFFICWATCH[{name!r}] cannot be imported: {exc}", id="trafficwatch.E004"
                    )
                )
        elif value is not None and not callable(value):
            errors.append(
                Error(
                    f"TRAFFICWATCH[{name!r}] must be callable or a dotted path.",
                    id="trafficwatch.E004",
                )
            )

    backend_cls = None
    try:
        backend_cls = tw_settings.backend_class()
    except ImportError as exc:
        errors.append(
            Error(f"TRAFFICWATCH['BACKEND'] cannot be imported: {exc}", id="trafficwatch.E005")
        )

    try:
        parse_networks(user.get("TRUSTED_PROXIES", []), "TRUSTED_PROXIES")
    except RuleConfigError as exc:
        errors.append(Error(f"TRAFFICWATCH['{exc}", id="trafficwatch.E006"))

    for name in ("EXEMPT_PATHS", "EXEMPT_METHODS"):
        value = user.get(name, DEFAULTS[name])
        if isinstance(value, str) or not all(isinstance(p, str) for p in value):
            errors.append(
                Error(f"TRAFFICWATCH[{name!r}] must be a list of strings.", id="trafficwatch.E007")
            )

    alias = user.get("CACHE_ALIAS", DEFAULTS["CACHE_ALIAS"])
    caches = getattr(settings, "CACHES", {})
    cache_backend = caches.get(alias, {}).get("BACKEND", "").lower() if alias in caches else ""
    if alias not in caches:
        errors.append(
            Error(
                f"TRAFFICWATCH['CACHE_ALIAS'] {alias!r} is not defined in CACHES.",
                id="trafficwatch.E008",
            )
        )
    elif "locmem" in cache_backend and not settings.DEBUG:
        errors.append(
            Warning(
                "TRAFFICWATCH uses LocMemCache, which is per-process: limits are not shared "
                "between workers or hosts.",
                hint="Point CACHE_ALIAS at a Redis or Memcached cache in production.",
                id="trafficwatch.W002",
            )
        )
    elif "dummycache" in cache_backend:
        errors.append(
            Error(
                "TRAFFICWATCH uses DummyCache: nothing is ever counted and no limit is enforced.",
                hint="Point CACHE_ALIAS at a Redis or Memcached cache.",
                id="trafficwatch.E014",
            )
        )
    elif "filebased" in cache_backend or ".db.databasecache" in cache_backend:
        errors.append(
            Warning(
                f"TRAFFICWATCH uses {caches[alias].get('BACKEND')}, whose incr() is a "
                "non-atomic get/set: concurrent requests lose counts and limits are not "
                "reliably enforced (file locking errors also trigger fail-open).",
                hint="Point CACHE_ALIAS at a Redis or Memcached cache.",
                id="trafficwatch.W007",
            )
        )

    try:
        parse_networks(user.get("EXEMPT_CLIENTS", []), "EXEMPT_CLIENTS")
    except RuleConfigError as exc:
        errors.append(Error(f"TRAFFICWATCH['{exc}", id="trafficwatch.E009"))

    prefix = user.get("IPV6_PREFIX", DEFAULTS["IPV6_PREFIX"])
    if not _is_positive_int(prefix) or prefix > 128:
        errors.append(
            Error(
                "TRAFFICWATCH['IPV6_PREFIX'] must be an integer between 1 and 128.",
                id="trafficwatch.E010",
            )
        )

    try:
        parse_lockout(user.get("LOCKOUT"))
    except RuleConfigError as exc:
        errors.append(Error(f"TRAFFICWATCH['{exc}", id="trafficwatch.E011"))

    status = user.get("BLOCK_STATUS", DEFAULTS["BLOCK_STATUS"])
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        errors.append(
            Error(
                "TRAFFICWATCH['BLOCK_STATUS'] must be an HTTP status code.", id="trafficwatch.E015"
            )
        )
    recent = user.get("RECENT_VIOLATIONS", DEFAULTS["RECENT_VIOLATIONS"])
    if isinstance(recent, bool) or not isinstance(recent, int) or recent < 0:
        errors.append(
            Error(
                "TRAFFICWATCH['RECENT_VIOLATIONS'] must be a non-negative integer.",
                id="trafficwatch.E015",
            )
        )

    style = user.get("HEADERS_STYLE", DEFAULTS["HEADERS_STYLE"])
    if style not in HEADER_STYLES:
        errors.append(
            Error(
                f"TRAFFICWATCH['HEADERS_STYLE'] must be one of {list(HEADER_STYLES)}.",
                id="trafficwatch.E012",
            )
        )

    for name in BOOLEAN_SETTINGS:
        if not isinstance(user.get(name, DEFAULTS[name]), bool):
            errors.append(
                Error(f"TRAFFICWATCH[{name!r}] must be a boolean.", id="trafficwatch.E013")
            )

    if user.get("FAIL_OPEN", DEFAULTS["FAIL_OPEN"]) is False:
        errors.append(
            Warning(
                "TRAFFICWATCH['FAIL_OPEN'] is False: every request is rejected with the block "
                "response while the cache is unreachable.",
                hint="Leave FAIL_OPEN=True to let requests through (request.trafficwatch."
                "degraded is set and the outage is logged on the django_trafficwatch logger).",
                id="trafficwatch.W005",
            )
        )

    if (
        user.get("BACKEND") == "redis"
        and backend_cls is not None
        and alias in caches
        and "redis" not in cache_backend
    ):
        errors.append(
            Warning(
                f"TRAFFICWATCH['BACKEND'] is 'redis' but CACHES[{alias!r}] is "
                f"{caches[alias].get('BACKEND')!r}; the sliding-window backend will be used.",
                hint="Point CACHE_ALIAS at django.core.cache.backends.redis.RedisCache or "
                "django_redis.cache.RedisCache.",
                id="trafficwatch.W006",
            )
        )

    middleware = list(getattr(settings, "MIDDLEWARE", []))
    if MIDDLEWARE_PATH not in middleware:
        errors.append(
            Warning(
                "TrafficWatchMiddleware is not in MIDDLEWARE; TRAFFICWATCH settings have no effect "
                "unless you use the DRF throttle.",
                id="trafficwatch.W003",
            )
        )
    elif AUTH_MIDDLEWARE in middleware and middleware.index(MIDDLEWARE_PATH) < middleware.index(
        AUTH_MIDDLEWARE
    ):
        key_func = user.get("KEY_FUNC", DEFAULTS["KEY_FUNC"])
        if key_func == DEFAULTS["KEY_FUNC"]:
            errors.append(
                Warning(
                    "TrafficWatchMiddleware runs before AuthenticationMiddleware, so the default "
                    "KEY_FUNC (user_or_ip) will never see request.user and keys on IP only.",
                    hint="Move TrafficWatchMiddleware below AuthenticationMiddleware.",
                    id="trafficwatch.W004",
                )
            )

    return errors
