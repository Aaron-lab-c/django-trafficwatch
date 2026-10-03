# Changelog

## 0.3.0 — unreleased

### Security / production safety
- **Fail-open on cache outage.** A cache exception (Redis down) no longer turns every request
  into a 500. With `FAIL_OPEN` (default `True`) the request is allowed and
  `request.trafficwatch.degraded` is set; the outage is logged at `ERROR` once per
  `FAIL_OPEN_LOG_INTERVAL` seconds. `FAIL_OPEN=False` returns the block response instead
  (`manage.py check` warns, `trafficwatch.W005`).
- **IPv6 /64 keying.** IPv6 clients are keyed on their `IPV6_PREFIX` (default 64) network, so a
  client rotating addresses inside its /64 shares one counter. IPv4 is untouched.
- **Allow-list and conditional exemption.** `EXEMPT_CLIENTS` (IPs / CIDR, matched against the
  resolved client IP) and `EXEMPT_FUNC` (dotted path or callable `(request) -> bool`).
- **Fail fast on bad configuration.** `TrafficWatchMiddleware.__init__` (and the DRF throttle on
  first use) validates `TRAFFICWATCH` and raises `ImproperlyConfigured`, so a typo fails at
  process start instead of on the first request.

### Added
- **Escalating lockout.** `LOCKOUT = {"VIOLATIONS": 3, "WINDOW_SECONDS": 600,
  "DURATION_SECONDS": 900}` blocks a client for the duration after N first-crossings within the
  window, regardless of rule. Surfaced in `Retry-After`, `info["lockout"]`, the signal,
  `request.trafficwatch.lockout` and `trafficwatch_recent`.
- **Redis backend.** `BACKEND = "redis"` evaluates all rules for a client in a single Lua script
  (one round trip per request) on Django's `RedisCache` or `django-redis`; Redis Cluster safe.
  Falls back to the sliding-window backend when the cache is not Redis (`trafficwatch.W006`).
- **Standard rate-limit headers.** `HEADERS_STYLE = "x-ratelimit" | "ietf" | "both"`; `ietf`
  emits `RateLimit-Policy` / `RateLimit` per draft-ietf-httpapi-ratelimit-headers.
  `RESET_AS_EPOCH` emits `X-RateLimit-Reset` as a Unix timestamp.
- Staff-only inspection views: `include("django_trafficwatch.urls")` gives `recent/` (HTML
  table) and `recent.json`. README shows a Prometheus counter built on the signal.
- `method_decorator(trafficwatch_rule(...), name="post")` / `name="dispatch"` on class-based
  views now works (previously invisible to the middleware); auto-generated rule names are
  derived from the class and handler so two views never share a counter by accident.
- `MATCH_PATH_INFO` matches `EXEMPT_PATHS` / `PATH_RULES` against `request.path_info` for
  deployments under `SCRIPT_NAME`.
- `BaseBackend.hit_many()` / `peek_many()` batch API (default loops over `hit` / `peek`) and
  lockout helpers (`count_violation`, `lock`, `locked_until`, `unlock`).
- `py.typed` shipped; `mypy --strict src` runs in CI.
- CI job running the whole suite against a Redis service, plus a threaded test proving `incr`
  atomicity on Redis.
- New system checks: `E009` (EXEMPT_CLIENTS), `E010` (IPV6_PREFIX), `E011` (LOCKOUT),
  `E012` (HEADERS_STYLE), `E013` (boolean flags), `W005` (FAIL_OPEN=False), `W006` (BACKEND
  "redis" on a non-Redis cache).

### Changed
- `BLOCK_MESSAGE` is passed through `str()` so a `gettext_lazy` message serialises.
- `info` dicts gained `lockout` and `degraded`; `trafficwatch_recent` shows a `lock` column.
- `TrafficWatchState.strictest` is `None` (and `headers()` empty) when nothing was counted.
- `core.TrafficWatch.check()` calls `backend.hit_many()` once instead of `hit()` per rule.
- `stats.record_violation` failures are logged instead of breaking the request.

### Breaking
- `keys.client_ip()` returns the /64 network for IPv6 clients (`IPV6_PREFIX=128` restores the
  old behaviour). Use `keys.resolve_client_ip()` for the raw address.

## 0.2.0 — unreleased

### Security
- `X-Forwarded-For` is no longer trusted by default. It is honoured only when `REMOTE_ADDR`
  is in the new `TRUSTED_PROXIES` setting (IPs or CIDR networks), taking the rightmost
  untrusted hop. Previously any client could choose its own rate-limit identity.
- Client identifiers and rule names are hashed when they contain whitespace / control
  characters or exceed 120 chars, so custom `KEY_FUNC`s cannot produce cache keys Memcached
  rejects.

### Added
- Rules per HTTP method (`METHODS`), several rules per `PATH_RULES` entry (list), regex keys
  (`"re:^/api/v\d+/"`), explicit rule `NAME`, per-rule `BLOCK` and `KEY_FUNC`.
- `@trafficwatch_rule` is stackable, accepts `methods=`, `name=`, `block=`, `key_func=`, and
  works on class-based views. `@trafficwatch_exempt` works on class-based views.
- `EXEMPT_METHODS` (default `["OPTIONS"]`).
- `BLOCK_RESPONSE` callable to render a custom 429 (HTML, templated JSON, ...).
- Django REST Framework throttle: `django_trafficwatch.drf.TrafficWatchThrottle`, sharing rules
  and counters with the middleware and deferring to it when both are installed.
- `request.trafficwatch` exposes the evaluated `TrafficWatchState` to views and templates.
- System checks (`manage.py check`, ids `trafficwatch.E001`–`E008`, `W001`–`W004`) when
  `django_trafficwatch` is in `INSTALLED_APPS`.
- `manage.py trafficwatch_recent [--json] [-n N] [--clear]` listing the last
  `RECENT_VIOLATIONS` limit violations kept in the cache.
- `BaseBackend.peek()` for read-only inspection of counters.
- Log records carry the info dict as `record.trafficwatch`; `info` gained `method` and `blocked`.
- Middleware is `async_capable`; the response pass runs natively under ASGI.
- CI matrix now covers Django 4.2, 5.2, 6.0 and 6.1.

### Changed
- Sliding-window `reset_in` / `Retry-After` is now the time until the next request would be
  accepted, instead of the time until the current bucket ends.
- Compiled `PATH_RULES` are cached and rebuilt on `setting_changed`; malformed entries raise
  `RuleConfigError` at first use (and are reported by `manage.py check`).
- Rule names of per-view rules use `__qualname__` (`view:app.views.Cls.method`).

## 0.1.0 — unreleased
- Initial release: fixed / sliding window backends, per-path and per-view rules,
  observe-only mode, `X-RateLimit-*` headers, `traffic_exceeded` signal, `ON_EXCEEDED` hook.
