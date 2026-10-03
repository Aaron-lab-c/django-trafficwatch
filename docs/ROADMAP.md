# Roadmap

Prioritised backlog after the 0.2.0 refactor. Each item should land with tests, a CHANGELOG
entry and README/ARCHITECTURE updates. Run the suite with `pytest`, lint with
`ruff check . && ruff format --check src tests`.

## P1 — production safety

1. **Fail-open on cache outage.** `cache.add/incr/get` raising (Redis down) currently turns
   every request into a 500. Add `FAIL_OPEN` (default `True`): catch backend exceptions in
   `core.TrafficWatch.check`, log once per N seconds, allow the request and mark the state as
   `degraded`; with `FAIL_OPEN=False` return the block response. Expose `degraded` on
   `TrafficWatchState` and in a system-check hint.
2. **IPv6 /64 keying.** Add `IPV6_PREFIX` (default `64`) in `keys.client_ip`; a client that
   rotates addresses inside its /64 must share one counter. Keep IPv4 untouched.
3. **Allow-list and conditional exemption.** `EXEMPT_CLIENTS` (IPs / CIDR, matched against the
   resolved client IP) and `EXEMPT_FUNC` (dotted path or callable `(request) -> bool`, e.g.
   superusers). Both validated by `checks.py`.
4. **Fail fast on bad configuration.** Build `tw_settings.ruleset` and import all
   `_IMPORTABLE` callables in `TrafficWatchMiddleware.__init__` so a typo fails at process
   start instead of on the first request.

## P2 — capability gaps

5. **Escalating lockout.** `LOCKOUT = {"VIOLATIONS": 3, "WINDOW_SECONDS": 600,
   "DURATION_SECONDS": 900}`: after N first-crossings within the window, block the client for
   the duration regardless of rule. Implement with an extra counter + a `locked` key via the
   existing backend; surface in headers (`Retry-After`), `info["lockout"]`, signal and
   `trafficwatch_recent`.
6. **Redis integration tests in CI.** Add a job with a `redis` service, settings using
   `django.core.cache.backends.redis.RedisCache`, run the whole suite against it, plus a
   threaded test proving `incr` atomicity.
7. **Single round-trip Redis backend.** `backends/redis_lua.py`: one Lua script evaluating all
   rules for a client (exact sliding log with a sorted set or the two-bucket estimate),
   selected via `BACKEND="redis"`. Must degrade gracefully when the cache is not Redis.
8. **Standard rate-limit headers.** `HEADERS_STYLE = "x-ratelimit" | "ietf" | "both"`; `ietf`
   emits `RateLimit-Policy` / `RateLimit` per draft-ietf-httpapi-ratelimit-headers. Option
   `RESET_AS_EPOCH` for `X-RateLimit-Reset`.

## P3 — polish

9. Admin integration: a read-only admin view (and/or `trafficwatch/recent.json` endpoint,
   staff-only) listing recent violations; optional Prometheus counter example via the signal.
10. `BLOCK_MESSAGE` with `gettext_lazy`: wrap in `str()` before `JsonResponse`.
11. Document (or detect and warn) that `method_decorator(trafficwatch_rule(...), name="post")`
    on a CBV method is invisible to the middleware; decorate the class instead.
12. `MATCH_PATH_INFO` option so `PATH_RULES` match `request.path_info` under `SCRIPT_NAME`
    deployments.
13. Ship `py.typed` and run `mypy --strict` on `src` in CI.
14. (Low value) native async `process_view` using the cache `a*` API; Django's cache backends
    are sync underneath, so measure before merging.

## Non-goals for now

- Host-level bandwidth accounting (what django-traffic-monitor does) is a different tool.
- A persistent DB audit log: use the `traffic_exceeded` signal from the host project.
