# Roadmap

Prioritised backlog. Each item should land with tests, a CHANGELOG entry and
README/ARCHITECTURE updates. Run the suite with `pytest` (and
`TW_REDIS_URL=redis://localhost:6379/1 pytest` for the Redis path), lint with
`ruff check . && ruff format --check src tests`, type-check with `mypy --strict src`.

## Done in 0.3.0

- P1.1 Fail-open on cache outage (`FAIL_OPEN`, `FAIL_OPEN_LOG_INTERVAL`, `state.degraded`, W005).
- P1.2 IPv6 /64 keying (`IPV6_PREFIX`).
- P1.3 Allow-list and conditional exemption (`EXEMPT_CLIENTS`, `EXEMPT_FUNC`, E009).
- P1.4 Fail fast on bad configuration (`tw_settings.validate()` in the middleware constructor).
- P2.5 Escalating lockout (`LOCKOUT`, `info["lockout"]`, `Retry-After`, `trafficwatch_recent`).
- P2.6 Redis integration tests in CI (`test-redis` job, threaded `incr` atomicity test).
- P2.7 Single round-trip Redis backend (`BACKEND="redis"`, `backends/redis_lua.py`, W006).
- P2.8 Standard rate-limit headers (`HEADERS_STYLE`, `RESET_AS_EPOCH`).
- P3.9 Staff-only `recent/` + `recent.json` views; Prometheus example in the README.
- P3.10 `BLOCK_MESSAGE` with `gettext_lazy`.
- P3.11 `method_decorator(trafficwatch_rule(...), name="post")` on CBVs is now supported
  (class, `dispatch` and the request-method handler are all inspected).
- P3.12 `MATCH_PATH_INFO`.
- P3.13 `py.typed` shipped, `mypy --strict src` in CI.

## Done in 0.4.0 (behaviour test follow-ups)

- Rejected requests are rolled back (no sliding-window starvation, no cross-rule quota loss).
- `Retry-After` rounds up; once-per-window notification via an atomic marker.
- `COUNT_UNROUTED`: 404s and early-middleware responses are counted.
- Numeric / boolean settings validated at startup; `W007` / `E014` for non-atomic or dummy
  caches; raising `KEY_FUNC` / `EXEMPT_FUNC` degrade instead of 500.

## Done in 0.5.0

- Unknown top-level keys fail at startup (`E016`); LocMem reported under `DEBUG` (`I001`);
  `/favicon.ico` exempt by default; `APPEND_SLASH` / 404 side effects documented.

## Open

1. **Exact sliding log for Redis.** An optional `"redis-log"` mode using a sorted set per
   client/rule, for deployments that need exact counts and accept the memory profile (one
   member per request in the window). Must not count rejected requests, and must still fire
   the first-crossing notification exactly once.
2. **Admin site integration.** Register the inspection view on `admin.site` (link in the admin
   index) instead of a standalone URL include; needs `django.contrib.admin` detection so the
   package keeps working without it.
3. **Lockout notifications.** A dedicated `lockout_started` signal (today the lock rides on the
   crossing's `traffic_exceeded` payload as `info["lockout"]`).
4. **Per-rule lockout identity.** Allow `LOCKOUT["KEY_FUNC"]` for projects whose rules key on
   API keys rather than users / IPs.
5. **Admission for third-party backends.** Let a `hit()`-only backend opt into rollback by
   implementing `uncount()`, so custom stores get the "rejected requests are not counted"
   semantics too.
6. (Low value) native async `process_view` using the cache `a*` API; Django's cache backends
   are sync underneath, so measure before merging.

## Non-goals for now

- Host-level bandwidth accounting (what django-traffic-monitor does) is a different tool.
- A persistent DB audit log: use the `traffic_exceeded` signal from the host project.
