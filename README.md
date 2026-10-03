# django-trafficwatch

[![CI](https://github.com/Aaron-lab-c/django-trafficwatch/actions/workflows/ci.yml/badge.svg)](https://github.com/Aaron-lab-c/django-trafficwatch/actions)
[![PyPI](https://img.shields.io/pypi/v/django-trafficwatch.svg)](https://pypi.org/project/django-trafficwatch/)

Configurable traffic monitoring and rate limiting for Django, as a single middleware.
Set a time window and a request count, globally, per path, per method, or per view.
Observe first, block later.

- Fixed-window counters (default) on top of Django's cache framework (Redis, Memcached, LocMem); sliding-window and single-round-trip Redis variants when the burst at window edges matters
- Rules per path prefix or regex, per HTTP method, several limits on one endpoint (5/min **and** 100/day)
- `@trafficwatch_rule` / `@trafficwatch_exempt` for function and class-based views, stackable
- Observe-only mode (`BLOCK=False`), globally or per rule, to measure before enforcing
- `X-RateLimit-Limit / Remaining / Reset`, the IETF `RateLimit` / `RateLimit-Policy` headers, and an honest `Retry-After`
- Escalating lockout: a client that trips limits repeatedly is locked out for a configurable time
- Fails open: a cache outage is logged and lets traffic through (`request.trafficwatch.degraded`), it never turns into a wall of 500s
- `traffic_exceeded` signal, `ON_EXCEEDED` hook and structured log records for Slack / Sentry / JSON logs / Prometheus
- `X-Forwarded-For` only trusted behind proxies you list: clients cannot forge their identity; IPv6 keyed per /64
- Allow-list by IP / CIDR or by callable (superusers, internal traffic)
- Django REST Framework throttle class sharing the same rules and counters
- Optional single-round-trip Redis backend (one Lua script per request, however many rules apply)
- `manage.py check` validates your configuration and the middleware refuses to start on a bad one; `manage.py trafficwatch_recent` and a staff-only page show who got blocked
- Sync and async (ASGI) capable, no models, no migrations, fully typed (`py.typed`)

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
    "EXEMPT_CLIENTS": ["10.0.0.0/8"],  # monitoring, internal callers
    "EXEMPT_FUNC": lambda request: request.user.is_superuser,
    "TRUSTED_PROXIES": ["10.0.0.0/8"],  # only then is X-Forwarded-For honoured
    "BLOCK": False,  # start in observe-only mode, flip to True when happy
    "BACKEND": "fixed",  # default; "sliding" / "redis" smooth the burst at window edges
    "LOCKOUT": {"VIOLATIONS": 3, "WINDOW_SECONDS": 600, "DURATION_SECONDS": 900},
    "ON_EXCEEDED": "myproject.alerts.notify",
}
```

Run `python manage.py check` to validate the configuration (typos in keys, bad regexes,
unimportable callables, middleware ordering, LocMem in production, ...). The middleware also
validates everything when it is instantiated, so a typo raises `ImproperlyConfigured` at
process start rather than on the first request.

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

On class-based views you can also decorate a single handler, which limits that method only:

```python
@method_decorator(trafficwatch_rule(60, 5), name="post")
class LoginView(View): ...
```

(The middleware looks at the class, `dispatch` and the handler for the request method.)

## Rule resolution

1. `@trafficwatch_rule` decorators on the view (filtered by `methods`).
2. `PATH_RULES`: regex keys (`"re:..."`) first in declaration order, then the longest matching
   prefix. Within the chosen entry only rules whose `METHODS` match apply.
3. The global `WINDOW_SECONDS` / `MAX_REQUESTS`.

Every applicable rule is counted independently; the response headers describe the rule closest
to its limit and `Retry-After` is the longest wait among the rules that blocked.

**Rejected requests are not counted.** A request is counted against every rule first; if any
enforced rule is over, the increments are rolled back. So a client sending 11 requests a
minute against a 10/min limit gets 10 through and one 429 (with either backend), and a request
turned away by the per-minute rule does not eat into the daily one. Rules in observe mode
(`BLOCK=False`) keep counting everything, since those requests are served. `Retry-After` is
rounded *up*, so waiting exactly that long is always enough.

Requests that never reach a view are counted too (`COUNT_UNROUTED`, default `True`): 404s from
the URL resolver and responses produced by a middleware below `TrafficWatchMiddleware` are
matched against `PATH_RULES` / the global rule, so a scanner probing unknown URLs is limited
like everyone else. Responses produced by middleware *above* ours (for example the
`APPEND_SLASH` redirect of `CommonMiddleware`) never reach it; place `TrafficWatchMiddleware`
higher if you need those counted.

Rules are matched against `request.path`. Set `MATCH_PATH_INFO = True` to match
`request.path_info` instead when the project is mounted under a `SCRIPT_NAME` prefix.

## Exemptions

Never counted: `EXEMPT_METHODS` (default `OPTIONS`), `EXEMPT_PATHS` prefixes,
`EXEMPT_CLIENTS` (IPs / CIDR networks, matched against the *resolved* client IP so a client
cannot forge its way in with `X-Forwarded-For`), requests for which `EXEMPT_FUNC(request)`
returns true, and views marked `@trafficwatch_exempt`.

## Escalating lockout

```python
TRAFFICWATCH = {"LOCKOUT": {"VIOLATIONS": 3, "WINDOW_SECONDS": 600, "DURATION_SECONDS": 900}}
```

Every time a client crosses *any* limit for the first time in a window it earns a violation.
After `VIOLATIONS` of them within `WINDOW_SECONDS` the client is blocked for
`DURATION_SECONDS` regardless of rule, with `Retry-After` set to the remaining lock time and
nothing counted in the meantime. The crossing that triggered the lock carries
`info["lockout"]` (`violations`, `threshold`, `locked_until`, `active`) into the signal,
`ON_EXCEEDED`, the log record and `trafficwatch_recent`. Clients are identified with the
global `KEY_FUNC`.

## When the cache is down

With the default `FAIL_OPEN = True` a cache exception (Redis unreachable, Memcached restart)
is logged at `ERROR` on the `django_trafficwatch` logger at most once per
`FAIL_OPEN_LOG_INTERVAL` seconds, the request is allowed and `request.trafficwatch.degraded`
is `True` (no rate-limit headers are emitted). With `FAIL_OPEN = False` every request gets
the block response until the cache recovers; `manage.py check` warns about that
(`trafficwatch.W005`).

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

Both fire **once per client per window**, on the first request that is turned away (an atomic
marker guarantees exactly one event however hard the client hammers). The same event is logged
at `WARNING` on the `django_trafficwatch` logger with the info dict attached as
`record.trafficwatch`, so JSON log handlers get structured fields for free.

Hooks never break a request: an exception in `ON_EXCEEDED` is logged; a raising `KEY_FUNC`
falls back to keying on the client IP; a raising `EXEMPT_FUNC` exempts nothing. The first two
kinds are logged at `ERROR` with the traceback, throttled like the cache-outage log.

A Prometheus counter is a three-line receiver:

```python
from prometheus_client import Counter
from django_trafficwatch import traffic_exceeded

EXCEEDED = Counter("trafficwatch_exceeded_total", "Rate limit crossings", ["rule", "blocked"])

@receiver(traffic_exceeded)
def count_exceeded(sender, info, **kwargs):
    EXCEEDED.labels(rule=info["rule"], blocked=str(info["blocked"])).inc()
```

The last `RECENT_VIOLATIONS` events are kept in the cache:

```bash
python manage.py trafficwatch_recent          # table, newest first
python manage.py trafficwatch_recent --json -n 50
python manage.py trafficwatch_recent --clear
```

The same list is available to staff users over HTTP (403 for everyone else):

```python
urlpatterns = [path("trafficwatch/", include("django_trafficwatch.urls")), ...]
# -> /trafficwatch/recent/  (read-only HTML table)   /trafficwatch/recent.json?limit=50
```

Inside a view or template, `request.trafficwatch` holds the evaluated state
(`.exceeded`, `.blocked`, `.degraded`, `.lockout`, `.results`, `.headers()`); it is `None`
for exempt requests.

## Response headers

`HEADERS_STYLE` selects the header family:

| Style | Headers |
|---|---|
| `"x-ratelimit"` (default) | `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` (seconds, or a Unix timestamp with `RESET_AS_EPOCH = True`) |
| `"ietf"` | `RateLimit-Policy: "login";q=5;w=60, "login-daily";q=50;w=86400` and `RateLimit: "login";r=2;t=37` per [draft-ietf-httpapi-ratelimit-headers](https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/) |
| `"both"` | all of the above |

`Retry-After` is always added to block responses.

## Redis backend

```python
CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": "redis://..."}}
TRAFFICWATCH = {"BACKEND": "redis", ...}
```

`"redis"` evaluates every rule for a client in **one** Lua script (one network round trip per
request instead of two per rule) using the same two-bucket sliding estimate as `"sliding"`.
It works with Django's built-in `RedisCache` and with `django-redis`, and is Redis Cluster
safe (keys are hash-tagged by client). If `CACHE_ALIAS` is not a Redis cache it logs a warning
and behaves exactly like `"sliding"` (`manage.py check` reports `trafficwatch.W006`).

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
| `COUNT_UNROUTED` | `True` | Also count requests that never reach a view (404s, early-middleware responses) |
| `MATCH_PATH_INFO` | `False` | Match paths against `request.path_info` instead of `request.path` |
| `EXEMPT_PATHS` | `["/static/", "/media/"]` | Path prefixes never counted |
| `EXEMPT_METHODS` | `["OPTIONS"]` | HTTP methods never counted |
| `EXEMPT_CLIENTS` | `[]` | IPs / CIDR networks never counted (resolved client IP) |
| `EXEMPT_FUNC` | `None` | Dotted path or callable `(request) -> bool`; true = never counted |
| `BLOCK` | `True` | `False` = log/alert only (per-rule `BLOCK` overrides) |
| `BLOCK_STATUS` | `429` | Status when blocked |
| `BLOCK_MESSAGE` | `"Too many requests…"` | JSON `detail` field (`gettext_lazy` is fine) |
| `BLOCK_RESPONSE` | `None` | Dotted path or callable `(request, info) -> HttpResponse` replacing the JSON body |
| `KEY_FUNC` | `user_or_ip` | Dotted path or callable `(request) -> str` |
| `TRUSTED_PROXIES` | `[]` | IPs / CIDR networks whose `X-Forwarded-For` is trusted |
| `IPV6_PREFIX` | `64` | IPv6 clients are keyed on this prefix length (`128` = full address) |
| `ON_EXCEEDED` | `None` | Dotted path or callable `(request, info)` |
| `BACKEND` | `"fixed"` | `"fixed"` (aligned windows, cheapest, up to 2x the limit across a window edge), `"sliding"` (two-bucket estimate, smooths the edge), `"redis"` (same estimate, one round trip), or dotted path to a `BaseBackend` subclass |
| `CACHE_ALIAS` | `"default"` | Which `CACHES` entry to use |
| `CACHE_PREFIX` | `"tw"` | Key namespace |
| `FAIL_OPEN` | `True` | Allow requests (and mark `degraded`) when the cache raises; `False` blocks them |
| `FAIL_OPEN_LOG_INTERVAL` | `60` | Seconds between outage log records |
| `LOCKOUT` | `None` | `{"VIOLATIONS": N, "WINDOW_SECONDS": W, "DURATION_SECONDS": D}` escalating lockout |
| `HEADERS` | `True` | Emit rate-limit headers |
| `HEADERS_STYLE` | `"x-ratelimit"` | `"x-ratelimit"`, `"ietf"` or `"both"` |
| `RESET_AS_EPOCH` | `False` | `X-RateLimit-Reset` as a Unix timestamp instead of seconds |
| `RECENT_VIOLATIONS` | `100` | How many violations to keep for `trafficwatch_recent` / the staff views (`0` disables) |

## Security notes

- By default the client is keyed on `REMOTE_ADDR` (or the authenticated user). `X-Forwarded-For`
  is **ignored** unless the direct peer is in `TRUSTED_PROXIES`; then the rightmost address not
  belonging to a trusted proxy is used, so a client cannot prepend a fake address.
- IPv6 clients are keyed on their /64 (`IPV6_PREFIX`), because one subscriber typically owns a
  whole /64 and could otherwise get a fresh counter per request by rotating addresses.
- `EXEMPT_CLIENTS` is matched against the same resolved address, never against a raw header.
- Counting happens before the view runs, so requests rejected by authentication still consume
  quota. That is what makes brute-force protection work.
- `LocMemCache` is per process. Use Redis or Memcached in production (`manage.py check` warns
  when `DEBUG` is off). `FileBasedCache` and `DatabaseCache` have a non-atomic `incr` and lose
  counts under concurrency (`trafficwatch.W007`); `DummyCache` never limits anything (`E014`).
- Deploy with `manage.py check --deploy --fail-level WARNING` so a misconfiguration stops the
  rollout; the middleware itself refuses to start on invalid values (`MAX_REQUESTS=0`, a bad
  dotted path, ...).

## Development

```bash
pip install -e ".[dev,typing]"
pytest                                        # LocMem
TW_REDIS_URL=redis://localhost:6379/1 pytest  # the same suite against Redis + Redis-only tests
ruff check . && ruff format --check src tests
mypy --strict src
```

## Release

1. Bump `__version__` in `src/django_trafficwatch/__init__.py` and update `CHANGELOG.md`.
2. `git tag v0.4.0 && git push origin v0.4.0`
3. The `publish.yml` workflow runs tests, checks the tag matches the version, builds, and
   uploads to PyPI via Trusted Publishing.

One-time setup: on PyPI → your project → *Publishing* → add a GitHub publisher with
owner `Aaron-lab-c`, repo `django-trafficwatch`, workflow `publish.yml`, environment `pypi`.
Then create an environment named `pypi` in the GitHub repo settings.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for design details.
Planned work is tracked in [docs/ROADMAP.md](docs/ROADMAP.md).

## License
MIT
