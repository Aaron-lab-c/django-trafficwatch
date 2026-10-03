# Changelog

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
