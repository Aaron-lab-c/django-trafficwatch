"""A small but realistic project configured the way the README recommends."""

import os

SECRET_KEY = "acceptance"
DEBUG = os.environ.get("TW_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "project.urls"
USE_TZ = True

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "rest_framework",
    "django_trafficwatch",  # system checks + management command
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_trafficwatch.middleware.TrafficWatchMiddleware",  # after auth, as documented
]

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True}]

# TW_REDIS_URL=redis://host:6379/1 or TW_MEMCACHED=host:11211 switch the counter store;
# the default is LocMem.
_REDIS_URL = os.environ.get("TW_REDIS_URL")
_MEMCACHED = os.environ.get("TW_MEMCACHED")
if _REDIS_URL:
    _DEFAULT_CACHE = {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": _REDIS_URL,
    }
elif _MEMCACHED:
    _DEFAULT_CACHE = {
        "BACKEND": "django.core.cache.backends.memcached.PyMemcacheCache",
        "LOCATION": _MEMCACHED,
    }
else:
    _DEFAULT_CACHE = {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
CACHES = {
    "default": _DEFAULT_CACHE,
    # A Redis that is not there: used to exercise FAIL_OPEN.
    "unreachable": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": "redis://127.0.0.1:1/0",
    },
}

REST_FRAMEWORK = {
    "DEFAULT_THROTTLE_CLASSES": ["django_trafficwatch.drf.TrafficWatchThrottle"],
}

TRAFFICWATCH = {
    # Short global window so tests can wait it out; per-path rules use long windows.
    "WINDOW_SECONDS": 2,
    "MAX_REQUESTS": 3,
    "PATH_RULES": {
        "/api/export/": {"WINDOW_SECONDS": 3600, "MAX_REQUESTS": 2},
        "/api/login/": [
            {"WINDOW_SECONDS": 3600, "MAX_REQUESTS": 2, "METHODS": ["POST"]},
            {
                "WINDOW_SECONDS": 86400,
                "MAX_REQUESTS": 3,
                "METHODS": ["POST"],
                "NAME": "login-daily",
            },
        ],
        r"re:^/api/v\d+/search/": {"WINDOW_SECONDS": 3600, "MAX_REQUESTS": 1},
        # Used by acceptance/deploy (multi-worker tests): a generous hourly budget whose size
        # the harness controls, and a practically unlimited path for throughput runs.
        "/bench/": {
            "WINDOW_SECONDS": 3600,
            "MAX_REQUESTS": int(os.environ.get("TW_BENCH_LIMIT", "50")),
            "NAME": "bench",
        },
        "/bench-open/": {"WINDOW_SECONDS": 3600, "MAX_REQUESTS": 10**9, "NAME": "bench-open"},
    },
    "EXEMPT_PATHS": ["/static/", "/media/", "/favicon.ico", "/health/"],
    "EXEMPT_CLIENTS": ["192.0.2.0/24"],
    "EXEMPT_FUNC": "project.alerts.is_superuser",
    # The deploy harness talks to a real server on 127.0.0.1 and picks client identities
    # through X-Forwarded-For, so loopback is a trusted proxy only in that mode.
    "TRUSTED_PROXIES": ["10.0.0.0/8"] + (["127.0.0.1"] if os.environ.get("TW_DEPLOY") else []),
    "BLOCK": True,
    "BACKEND": os.environ.get("TW_BACKEND", "fixed"),
    "ON_EXCEEDED": "project.alerts.notify",
}
