"""System checks (``python manage.py check``) for the TRAFFICWATCH setting.

IDs: trafficwatch.E0xx are misconfigurations that would fail at request time;
trafficwatch.W0xx are things that work but probably not as intended."""

from __future__ import annotations

import ipaddress

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register
from django.utils.module_loading import import_string

from .conf import _IMPORTABLE, DEFAULTS, tw_settings
from .rules import RuleConfigError, RuleSet

MIDDLEWARE_PATH = "django_trafficwatch.middleware.TrafficWatchMiddleware"
AUTH_MIDDLEWARE = "django.contrib.auth.middleware.AuthenticationMiddleware"


@register(Tags.security)
def check_trafficwatch_settings(app_configs, **kwargs):
    errors: list = []
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

    for name in ("WINDOW_SECONDS", "MAX_REQUESTS"):
        value = user.get(name, DEFAULTS[name])
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            errors.append(
                Error(f"TRAFFICWATCH[{name!r}] must be a positive integer.", id="trafficwatch.E002")
            )

    try:
        RuleSet(
            user.get("PATH_RULES", {}),
            user.get("WINDOW_SECONDS", DEFAULTS["WINDOW_SECONDS"]) or 1,
            user.get("MAX_REQUESTS", DEFAULTS["MAX_REQUESTS"]) or 1,
        )
    except (RuleConfigError, ImportError, TypeError) as exc:
        errors.append(Error(f"Invalid TRAFFICWATCH PATH_RULES: {exc}", id="trafficwatch.E003"))

    for name in _IMPORTABLE:
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

    try:
        tw_settings.backend_class()
    except ImportError as exc:
        errors.append(
            Error(f"TRAFFICWATCH['BACKEND'] cannot be imported: {exc}", id="trafficwatch.E005")
        )

    for entry in user.get("TRUSTED_PROXIES", []):
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            errors.append(
                Error(
                    f"TRAFFICWATCH['TRUSTED_PROXIES'] entry {entry!r} is not an IP or CIDR "
                    "network.",
                    id="trafficwatch.E006",
                )
            )

    for name in ("EXEMPT_PATHS", "EXEMPT_METHODS"):
        value = user.get(name, DEFAULTS[name])
        if isinstance(value, str) or not all(isinstance(p, str) for p in value):
            errors.append(
                Error(f"TRAFFICWATCH[{name!r}] must be a list of strings.", id="trafficwatch.E007")
            )

    alias = user.get("CACHE_ALIAS", DEFAULTS["CACHE_ALIAS"])
    caches = getattr(settings, "CACHES", {})
    if alias not in caches:
        errors.append(
            Error(
                f"TRAFFICWATCH['CACHE_ALIAS'] {alias!r} is not defined in CACHES.",
                id="trafficwatch.E008",
            )
        )
    elif "locmem" in caches[alias].get("BACKEND", "").lower() and not settings.DEBUG:
        errors.append(
            Warning(
                "TRAFFICWATCH uses LocMemCache, which is per-process: limits are not shared "
                "between workers or hosts.",
                hint="Point CACHE_ALIAS at a Redis or Memcached cache in production.",
                id="trafficwatch.W002",
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
