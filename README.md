# django-trafficwatch

[![CI](https://github.com/Aaron-lab-c/django-trafficwatch/actions/workflows/ci.yml/badge.svg)](https://github.com/Aaron-lab-c/django-trafficwatch/actions)
[![PyPI](https://img.shields.io/pypi/v/django-trafficwatch.svg)](https://pypi.org/project/django-trafficwatch/)

Configurable traffic monitoring and rate limiting for Django, as a single middleware.
Set a time window and a request count, globally, per path, per method, or per view.
Observe first, block later.

- Fixed-window and sliding-window counters on top of Django's cache framework (Redis, Memcached, LocMem)
- Rules per path prefix or regex, per HTTP method, several limits on one endpoint (5/min **and** 100/day)
- `@trafficwatch_rule` / `@trafficwatch_exempt` for function and class-based views, stackable
- Observe-only mode (`BLOCK=False`), globally or per rule, to measure before enforcing
- `X-RateLimit-Limit / Remaining / Reset` and an honest `Retry-After`
- `traffic_exceeded` signal, `ON_EXCEEDED` hook and structured log records for Slack / Sentry / JSON logs
- `X-Forwarded-For` only trusted behind proxies you list: clients cannot forge their identity
- Django REST Framework throttle class sharing the same rules and counters
- `manage.py check` validates your configuration, `manage.py trafficwatch_recent` shows who got blocked
- Sync and async (ASGI) capable, no models, no migrations

## Install

```bash
pip install django-trafficwatch
```

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "django_trafficwatch",  # optional: enables system checks and the management command
]

MIDDLEWARE = [
    # ...
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_trafficwatch.middleware.TrafficWatchMiddleware",  # after auth so users can be keyed
]

TRAFFICWATCH = {
    "WINDOW_SECONDS": 60,
    "MAX_REQUESTS": 100,
    "PATH_RULES": {
        # one rule
        "/api/export/": {"WINDOW_SECONDS": 3600, "MAX_REQUESTS": 10},
        # POST only, two limits at once
        "/api/login/": [
            {"WINDOW_SECONDS": 300, "MAX_REQUESTS": 5, "METHODS": ["POST"]},
            {"WINDOW_SECONDS": 86400, "MAX_REQUESTS": 50, "METHODS": ["POST"], "NAME": "login-daily"},
        ],
        # regex key
        r"re:^/api/v\d+/search/": {"MAX_REQUESTS": 30},
    },
    "EXEMPT_PATHS": ["/static/", "/media/", "/health/"],
    "TRUSTED_PROXIES": ["10.0.0.0/8"],  # only then is X-Forwarded-For honoured
    "BLOCK": False,  # start in observe-only mode, flip to True when happy
    "BACKEND": "sliding",  # or "fixed" (default)
    "ON_EXCEEDED": "myproject.alerts.notify",
}
```

Run `python manage.py check` to validate the configuration (typos in keys, bad regexes,
unimportable callables, middleware ordering, LocMem in production, ...).

## Per-view control

```python
from django_trafficwatch import trafficwatch_exempt, trafficwatch_rule

@trafficwatch_rule(window_seconds=60, max_requests=3, methods=["POST"])
@trafficwatch_rule(86400, 20, name="otp-daily")          # stack for several limits
def send_otp(request): ...

@trafficwatch_exempt
def healthcheck(request): ...

@trafficwatch_rule(60, 10)                                # works on class-based views too
class SearchView(View): ...
```

`@trafficwatch_rule` also accepts `name=`, `block=` (per-rule observe mode) and `key_func=`.

## Rule resolution

1. `@trafficwatch_rule` decorators on the view (filtered by `methods`).
2. `PATH_RULES`: regex keys (`"re:..."`) first in declaration order, then the longest matching
   prefix. Within the chosen entry only rules whose `METHODS` match apply.
3. The global `WINDOW_SECONDS` / `MAX_REQUESTS`.

Every applicable rule is counted independently; the response headers describe the rule closest
to its limit and `Retry-After` is the longest wait among the rules that blocked.

## Alerts and observability

```python
# myproject/alerts.py
def notify(request, info):
    # info: client, path, method, rule, count, limit, window, reset_in, blocked
    slack.post(f"{info['client']} exceeded {info['limit']}/{info['window']}s on {info['path']}")
```

or with the signal:

```python
from django_trafficwatch import traffic_exceeded

@receiver(traffic_exceeded)
def on_exceeded(sender, request, info, **kwargs): ...
```

Both fire **once per client per window**, on the first request that crosses the limit. The
same event is logged at `WARNING` on the `django_trafficwatch` logger with the info dict attached
as `record.trafficwatch`, so JSON log handlers get structured fields for free.

The last `RECENT_VIOLATIONS` events are kept in the cache:

```bash
python manage.py trafficwatch_recent          # table, newest first
python manage.py trafficwatch_recent --json -n 50
python manage.py trafficwatch_recent --clear
```

Inside a view or template, `request.trafficwatch` holds the evaluated state
(`.exceeded`, `.blocked`, `.results`, `.headers()`); it is `None` for exempt requests.

## Django REST Framework

```python
REST_FRAMEWORK = {
    "DEFAULT_THROTTLE_CLASSES": ["django_trafficwatch.drf.TrafficWatchThrottle"],
}

@trafficwatch_rule(60, 5, methods=["POST"])
class LoginView(APIView): ...
```

The throttle resolves rules exactly like the middleware (class decorators, `PATH_RULES`,
global) and uses the same counters. If the middleware is installed too, the throttle defers
to it so nothing is counted twice. Use the middleware when you want the `X-RateLimit-*`
headers.

## Custom block response

```python
def too_many(request, info):
    return render(request, "429.html", {"retry_after": info["retry_after"]}, status=429)

TRAFFICWATCH = {"BLOCK_RESPONSE": "myproject.views.too_many", ...}
```

`Retry-After` and the `X-RateLimit-*` headers are added to whatever you return.

## All settings

| Key | Default | Meaning |
|---|---|---|
| `WINDOW_SECONDS` | `60` | Global window length |
| `MAX_REQUESTS` | `100` | Global limit per window |
| `PATH_RULES` | `{}` | `{prefix or "re:regex": rule or [rules]}`; rule keys `WINDOW_SECONDS`, `MAX_REQUESTS`, `METHODS`, `NAME`, `BLOCK`, `KEY_FUNC` |
| `EXEMPT_PATHS` | `["/static/", "/media/"]` | Path prefixes never counted |
| `EXEMPT_METHODS` | `["OPTIONS"]` | HTTP methods never counted |
| `BLOCK` | `True` | `False` = log/alert only (per-rule `BLOCK` overrides) |
| `BLOCK_STATUS` | `429` | Status when blocked |
| `BLOCK_MESSAGE` | `"Too many requests…"` | JSON `detail` field |
| `BLOCK_RESPONSE` | `None` | Dotted path or callable `(request, info) -> HttpResponse` replacing the JSON body |
| `KEY_FUNC` | `user_or_ip` | Dotted path or callable `(request) -> str` |
| `TRUSTED_PROXIES` | `[]` | IPs / CIDR networks whose `X-Forwarded-For` is trusted |
| `ON_EXCEEDED` | `None` | Dotted path or callable `(request, info)` |
| `BACKEND` | `"fixed"` | `"fixed"`, `"sliding"`, or dotted path to a `BaseBackend` subclass |
| `CACHE_ALIAS` | `"default"` | Which `CACHES` entry to use |
| `CACHE_PREFIX` | `"tw"` | Key namespace |
| `HEADERS` | `True` | Emit `X-RateLimit-*` headers |
| `RECENT_VIOLATIONS` | `100` | How many violations to keep for `trafficwatch_recent` (`0` disables) |

## Security notes

- By default the client is keyed on `REMOTE_ADDR` (or the authenticated user). `X-Forwarded-For`
  is **ignored** unless the direct peer is in `TRUSTED_PROXIES`; then the rightmost address not
  belonging to a trusted proxy is used, so a client cannot prepend a fake address.
- Counting happens before the view runs, so requests rejected by authentication still consume
  quota. That is what makes brute-force protection work.
- `LocMemCache` is per process. Use Redis or Memcached in production (`manage.py check` warns).

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check src tests
```

## Release

1. Bump `__version__` in `src/django_trafficwatch/__init__.py` and update `CHANGELOG.md`.
2. `git tag v0.2.0 && git push origin v0.2.0`
3. The `publish.yml` workflow runs tests, checks the tag matches the version, builds, and
   uploads to PyPI via Trusted Publishing.

One-time setup: on PyPI → your project → *Publishing* → add a GitHub publisher with
owner `Aaron-lab-c`, repo `django-trafficwatch`, workflow `publish.yml`, environment `pypi`.
Then create an environment named `pypi` in the GitHub repo settings.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for design details.
Planned work is tracked in [docs/ROADMAP.md](docs/ROADMAP.md).

## License
MIT
