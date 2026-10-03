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
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
TRAFFICWATCH = {"WINDOW_SECONDS": 60, "MAX_REQUESTS": 3, "EXEMPT_PATHS": ["/health/"]}
USE_TZ = True
ALLOWED_HOSTS = ["*"]
