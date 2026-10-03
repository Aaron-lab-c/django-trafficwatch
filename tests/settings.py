import os

SECRET_KEY = "test"
DEBUG = True
ROOT_URLCONF = "tests.urls"
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_trafficwatch",
]
MIDDLEWARE = ["django_trafficwatch.middleware.TrafficWatchMiddleware"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
# ``TW_REDIS_URL=redis://localhost:6379/1 pytest`` runs the whole suite against Redis
# (the CI ``test-redis`` job does this); otherwise LocMem.
_REDIS_URL = os.environ.get("TW_REDIS_URL")
if _REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": _REDIS_URL,
        }
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
TRAFFICWATCH = {"WINDOW_SECONDS": 60, "MAX_REQUESTS": 3, "EXEMPT_PATHS": ["/health/"]}
USE_TZ = True
ALLOWED_HOSTS = ["*"]
