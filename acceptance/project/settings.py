"""A small but realistic project configured the way the README recommends."""

import os

SECRET_KEY = "acceptance"
DEBUG = True
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

_REDIS_URL = os.environ.get("TW_REDIS_URL")
CACHES = {
    "default": (
        {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": _REDIS_URL}
        if _REDIS_URL
        else {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    ),
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
    },
    "EXEMPT_PATHS": ["/static/", "/media/", "/favicon.ico", "/health/"],
    "EXEMPT_CLIENTS": ["192.0.2.0/24"],
    "EXEMPT_FUNC": "project.alerts.is_superuser",
    "TRUSTED_PROXIES": ["10.0.0.0/8"],
    "BLOCK": True,
    "ON_EXCEEDED": "project.alerts.notify",
}
