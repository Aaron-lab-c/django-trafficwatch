# Changelog

## 0.5.0 — unreleased

### Changed
- **Unknown top-level `TRAFFICWATCH` keys refuse to start.** A typo such as `MAX_REQUEST` used
  to produce only `W001` while the middleware ran with the default value; it now raises
  `ImproperlyConfigured` at startup and `manage.py check` reports `E016`, consistent with
  unknown keys inside a rule.
- **`/favicon.ico` is in the default `EXEMPT_PATHS`.** With `COUNT_UNROUTED` a missing favicon
  was charged to the visitor's global quota. Projects overriding `EXEMPT_PATHS` should add it.
- `LocMemCache` is now reported under `DEBUG=True` too, as `Info` (`I001`); `W002` is unchanged
  for `DEBUG=False`. `trafficwatch_recent` explains why it sees nothing on LocMem.

### Fixed
- README: `CommonMiddleware`'s `APPEND_SLASH` redirect *is* counted (Django turns the 404 into
  a 301 only in the response phase); the earlier text claimed the opposite. The 404 side
  effect of `COUNT_UNROUTED` on browser-initiated requests is documented.

### Added
- `acceptance/`: a black-box acceptance suite (a README-configured project driven through
  HTTP) that CI runs against the built wheel, on LocMem and on Redis.

### Removed
- `trafficwatch.W001` (replaced by `E016`).

## 0.4.0 — unreleased

Fixes from a load / behaviour test of 0.3.0.

### Changed (behaviour)
- **Rejected requests are no longer counted.** All built-in backends increment every rule,
  then roll the increments back when an enforced rule is over. Previously the sliding window
  counted its own rejections, so a client slightly over the limit (11/min against 10/min)
  pushed its estimate up with every retry and was starved almost completely; it now gets 10
  through. A request blocked by one rule no longer consumes the quota of the others (e.g. the
  daily rule). Observe-only rules (`BLOCK=False`) still count everything.
- **`Retry-After` / `X-RateLimit-Reset` round up** instead of down; waiting exactly that long
  is always enough (the fixed window was 1 s short).
- **Once-per-window notifications use an atomic marker** (`HitResult.first`) instead of
  `count == limit + 1`, so the signal / `ON_EXCEEDED` fire exactly once even when the sliding
  estimate jumps over `limit + 1` or a client hammers.
- **Unrouted requests are counted** (`COUNT_UNROUTED`, default `True`): 404s from the URL
  resolver and responses from middleware below `TrafficWatchMiddleware` are matched against
  `PATH_RULES` / the global rule and can be blocked. Previously a scanner hitting unknown URLs
  was never counted. Set `COUNT_UNROUTED = False` for the old behaviour.
- **A raising `KEY_FUNC` / `EXEMPT_FUNC` no longer 500s the request**: logged at `ERROR`
  (throttled like the cache-outage log); the key func falls back to the client IP, the exempt
  func exempts nothing.

### Added
- Startup validation (`ImproperlyConfigured`) and `manage.py check` now cover
  `WINDOW_SECONDS` / `MAX_REQUESTS` (0 or negative used to start fine and block everything),
  `BLOCK_STATUS`, `RECENT_VIOLATIONS`, `FAIL_OPEN_LOG_INTERVAL`, the boolean flags and the
  exempt lists (`E002`, `E013`, `E015`).
- `trafficwatch.W007`: `FileBasedCache` / `DatabaseCache` have a non-atomic `incr` and lose
  counts under concurrency. `trafficwatch.E014`: `DummyCache` never limits anything.
- `HitSpec` may carry a 5th element `enforce`; `BaseBackend.first_crossing()` and `_decr()`
  helpers for custom backends.

### Removed
- Nothing. `HitResult.just_exceeded` still works for backends that do not set `first`.

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
