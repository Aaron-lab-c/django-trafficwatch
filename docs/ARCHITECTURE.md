# Architecture

## Goals
1. One middleware, zero models, works on any Django cache backend.
2. Rules are data (settings) and can be overridden per path, per method or per view.
3. Safe by default: observe-only mode, alert hooks never break a request, forged
   `X-Forwarded-For` cannot change a client's identity.
4. Multi-process / multi-host safe when backed by Redis or Memcached.
5. Same semantics whether a request enters through the middleware or the DRF throttle.

## Components

```
settings.TRAFFICWATCH ──> conf.tw_settings ──> rules.RuleSet   (compiled, cached, rebuilt on setting_changed)
                                │               conf.LockoutConfig
                                │  validate() at middleware construction -> ImproperlyConfigured
                                ▼
TrafficWatchMiddleware ──┐
                         ├──> core.is_exempt_request  (EXEMPT_METHODS/PATHS/CLIENTS/FUNC)
drf.TrafficWatchThrottle ┘
                         └──> core.TrafficWatch.check(request, rules)
                                  │  lockout? backend.locked_until(client) -> blocked, nothing counted
                                  │  clients = [(rule.key_func or KEY_FUNC)(request) ...]
                                  │  backend.hit_many([(client, rule.name, window, limit) ...])
                                  │  crossings -> backend.count_violation / backend.lock
                                  │  backend raised -> degraded state (FAIL_OPEN decides)
                                  ▼
                            TrafficWatchState (request.trafficwatch)
                                  │
        ├── any rule just_exceeded -> logger.warning(extra=info) + stats.record_violation
        │                            + traffic_exceeded signal + ON_EXCEEDED   (info["lockout"])
        ├── state.blocked           -> BLOCK_RESPONSE or JSON 429, Retry-After
        └── always                  -> X-RateLimit-* and/or RateLimit headers (strictest rule)
```

| Module | Responsibility |
|---|---|
| `conf.py` | Defaults, lazy access to `settings.TRAFFICWATCH`, cached `RuleSet` / `LockoutConfig`, backend alias lookup, eager `validate()` |
| `rules.py` | `Rule` dataclass, validation of `PATH_RULES`, prefix/regex matching, method filtering |
| `keys.py` | Built-in client identifiers (`client_ip` with trusted-proxy logic and IPv6 prefix, `user_or_ip`), `EXEMPT_CLIENTS` matching, cache-safe key hashing |
| `core.py` | `TrafficWatch` (count, lockout, fail-open, notify, build block response), `TrafficWatchState`, `LockoutState`, header rendering, `is_exempt_request` |
| `backends/base.py` | `BaseBackend` contract (`hit`, `peek`, `reset`, `hit_many`, `peek_many`, lockout helpers) + `HitResult` + atomic `_incr` helper |
| `backends/fixed_window.py` | Aligned bucket counter — cheapest, exact per bucket |
| `backends/sliding_window.py` | Two-bucket weighted estimate — smooths bursts, honest `Retry-After` |
| `backends/redis_lua.py` | Same estimate, all rules in one Lua script on the raw redis-py client; falls back to sliding when the cache is not Redis |
| `decorators.py` | `@trafficwatch_exempt`, `@trafficwatch_rule(...)` (stackable, CBV- and `method_decorator`-aware) |
| `middleware.py` | Django entry point, sync + async capable, validates settings at construction |
| `drf.py` | `TrafficWatchThrottle` for Django REST Framework (optional dependency) |
| `stats.py` | Bounded "recent violations" list in the cache |
| `views.py` / `urls.py` | Staff-only `recent/` (HTML) and `recent.json` inspection endpoints |
| `checks.py` / `apps.py` | System checks registered when the app is in `INSTALLED_APPS` |
| `management/commands/trafficwatch_recent.py` | Inspect / clear recent violations |
| `signals.py` | `traffic_exceeded` Django signal |

## Request flow (middleware)

1. `__call__` runs the rest of the stack. If `process_view` never ran (the URL did not
   resolve, or a middleware below ours answered) and `COUNT_UNROUTED` is on, the request is
   counted now against `PATH_RULES` / the global rule and the response is replaced by the
   block response when the client is already over. Then the outgoing response gets the
   headers. Under ASGI `__acall__` does the same (the late check runs via `sync_to_async`).
2. `process_view` (runs after URL resolution, so the view function is known):
   1. Skip if `request.method` is in `EXEMPT_METHODS`, the match path (`request.path`, or
      `request.path_info` with `MATCH_PATH_INFO`) starts with an `EXEMPT_PATHS` prefix, the
      resolved client IP is in `EXEMPT_CLIENTS`, `EXEMPT_FUNC(request)` is true, or the view
      (its `view_class`, `dispatch` or the handler for the method) is `@trafficwatch_exempt`.
   2. Resolve rules: view decorators (class, `dispatch`, method handler) filtered by method →
      `PATH_RULES` (regex first, then longest prefix; method-filtered) → global rule. Never
      empty.
   3. If `LOCKOUT` is configured and the client is locked: an empty state with
      `lockout.active`; nothing is counted.
   4. Otherwise `client = key_func(request)` per rule and one `backend.hit_many()` call with
      an *enforce* flag per rule (per-rule `BLOCK`, else global). The backend increments
      every rule, and if any enforced rule is exceeded it rolls all increments back: the
      rejected request is not counted anywhere. The Redis backend does this in one script.
      The returned `count` still includes the attempt, so a rejected request reads
      `limit + 1`.
   5. "Crossed for the first time" is an atomic `add` of a per-client/rule marker with the
      window as TTL (`HitResult.first`), so the notification fires exactly once per window
      however many rejections follow. If `LOCKOUT` is configured one violation is counted
      per such request; at the threshold the client is locked.
   6. Store the `TrafficWatchState` on `request.trafficwatch`.
   7. For each crossed rule: log, record, signal, callback (info carries `lockout`).
   8. If any exceeded rule blocks (per-rule `BLOCK`, else global), the client is locked, or
      the backend failed and `FAIL_OPEN` is `False`: return the block response with
      `Retry-After` = the longest wait among blocking rules and the lockout. The view never
      runs.

   Steps 3–5 run inside one `try`: any exception from the cache produces a *degraded* state
   (no results, `degraded=True`), logged at most once per `FAIL_OPEN_LOG_INTERVAL`. A raising
   `KEY_FUNC` is logged the same way and the request is keyed on the client IP; a raising
   `EXEMPT_FUNC` is logged and exempts nothing. Neither turns into a 500.

`process_view` stays synchronous because Django's cache API is synchronous; Django wraps it
with `sync_to_async` in an async stack. The response pass is native in both modes.

## Rule semantics

- Rules are counted independently. `/api/login/` with `[5/min, 100/day]` keeps two counters.
- Headers describe the *strictest* rule: lowest `remaining`, then the one furthest over its
  limit, then the longest `reset_in`.
- A per-rule `BLOCK: False` observes that rule even when the global `BLOCK` is `True`,
  letting you trial a new limit on a live endpoint.
- A per-rule `KEY_FUNC` lets one rule key on e.g. an API key while others key on the user.

## Client identity and proxies

`resolve_client_ip()` returns `REMOTE_ADDR` unless it belongs to `TRUSTED_PROXIES`. Only then
is `X-Forwarded-For` read, walking from the rightmost hop (closest to us) leftwards and
returning the first hop that is *not* a trusted proxy. A client that sends
`X-Forwarded-For: 1.2.3.4` through a trusted proxy ends up with `1.2.3.4, <real-ip>` and is
keyed on `<real-ip>`. `EXEMPT_CLIENTS` is matched against this resolved address.

`client_ip()` additionally collapses IPv6 addresses to their `IPV6_PREFIX` network
(`2001:db8:1:2::/64`), IPv4-mapped addresses to the IPv4, and leaves IPv4 alone, so a
subscriber rotating through its /64 keeps one counter.

Identifiers are passed through `safe_key_part()` before reaching the cache: anything with
whitespace / control characters or longer than 120 chars is replaced by a SHA-256 prefix, so
custom `KEY_FUNC`s cannot produce keys Memcached rejects.

## Cache key layout

```
{PREFIX}:{fw|sw}:{rule_name}:{client}:{bucket}    fixed / sliding counters (Django cache keys)
{PREFIX}:x:{rule_name}:{client}                   first-crossing marker, TTL = window
{PREFIX}:rl:{{client}}:{rule_name}:{bucket}        redis backend (raw keys, hash-tagged by client)
{PREFIX}:rl:{{client}}:{rule_name}:x               redis backend first-crossing marker
{PREFIX}:viol:{client}:{bucket}                   lockout violation counter (fixed window)
{PREFIX}:lock:{client}                            active lockout, value = expiry timestamp
{PREFIX}:recent                                   list of recent violation dicts
```
- `rule_name` isolates buckets so `/api/login/` and the global rule don't share counts.
- `bucket = int(now // window)` makes keys self-expiring; TTL = window (+1 window for sliding).
- Clients and rule names pass through `safe_key_part()` first.

## Escalating lockout

`LOCKOUT = {"VIOLATIONS": N, "WINDOW_SECONDS": W, "DURATION_SECONDS": D}`. A *violation* is a
request that crosses at least one rule for the first time in that rule's window (the same
event that fires the signal), counted once per request in a fixed window of `W` seconds keyed
on the global `KEY_FUNC` identity. When the counter reaches `N` (`>=`, so a client that keeps
violating after the lock expires is re-locked immediately) a `lock` key with TTL `D` is
written. Locked clients get the block response with `Retry-After` = remaining lock time and
no counters are touched, so they cannot keep themselves locked by retrying; the lock simply
expires. `request.trafficwatch.lockout` and `info["lockout"]` expose
`violations / threshold / locked_until / active`.

## Fail-open

Every backend call in `check()` is wrapped. On an exception the request gets a
`TrafficWatchState(results=(), degraded=True)`: `blocked` is `False` with `FAIL_OPEN=True`
(default) and `True` otherwise (`Retry-After` = the smallest window among the rules). The
outage is logged with the traceback at `ERROR`, rate limited to one record per
`FAIL_OPEN_LOG_INTERVAL` seconds process-wide. `stats.record_violation` is wrapped separately
so a flapping cache cannot break a request that was counted successfully.

## Redis backend

`RedisLuaBackend` obtains the redis-py client behind Django's `RedisCache`
(`cache._cache.get_client(None, write=True)`) or `django-redis` (`cache.client.get_client`).
`hit_many` groups the specs by client and runs one `EVALSHA` per client: the script `INCR`s
each current bucket (setting the TTL on creation), `GET`s each previous bucket, computes the
estimate with the same `weight = 1 - elapsed` double Python uses (passed as `repr`, so both
sides floor the identical number), rolls every increment back when an enforced rule is
over, and `SET NX EX`s the first-crossing marker. Python then applies `two_bucket_result()`,
the exact function the sliding backend uses, so semantics, tests and `Retry-After` are
identical. Keys are hash-tagged with the client
so Redis Cluster accepts the multi-key script. Lockout and the recent list keep using the
Django cache API. When the cache is not Redis the constructor logs once and delegates
everything to a `SlidingWindowBackend`.

## Headers

`HEADERS_STYLE="x-ratelimit"` emits the de-facto `X-RateLimit-Limit/Remaining/Reset` for the
strictest rule (`Reset` as epoch seconds with `RESET_AS_EPOCH`). `"ietf"` emits
`RateLimit-Policy` listing every applicable rule as `"name";q=limit;w=window` and `RateLimit`
as `"name";r=remaining;t=seconds` for the strictest rule (or `"lockout";r=0;t=…` while locked),
per draft-ietf-httpapi-ratelimit-headers; names are quoted as RFC 8941 strings. `"both"`
emits all of them.

## Admission, rollback and `Retry-After`

Only accepted requests stay counted. Every built-in backend does `incr` on each rule, decides
admission from the new values, and `decr`s them all when an enforced rule is over. With
atomic `incr`/`decr` this admits exactly `limit` requests per window under concurrency (two
racing requests both see the over-limit value and both roll back). Counting rejected requests
instead, as 0.3 did, made the sliding estimate self-reinforcing: a client at 1.1× the limit
pushed its own estimate up with each retry and got almost nothing through.

```
estimate = previous_bucket * (1 - elapsed_fraction) + current_bucket
```

When `estimate >= limit`, `reset_in` is the smallest `t` such that a request at `now + t`
would be accepted given the *stored* (accepted) counts: either the point inside the current
bucket where the previous bucket has decayed enough, or (if the current bucket alone is
already full) the roll-over plus the decay time of the current bucket. The integer flooring
of the estimate is ignored in that solve, which can only make the wait longer by under a
second. All `reset_in` values are rounded up (`ceil`) and never 0, so a client that waits
exactly `Retry-After` is served. Tests assert that for both backends.

## Backend contract

```python
class BaseBackend:
    def hit(client, rule, window, limit) -> HitResult
    def peek(client, rule, window, limit) -> HitResult | None   # optional, read-only
    def reset(client, rule, window) -> None
    def hit_many(specs) -> list[HitResult]                     # default: loop over hit()
    def peek_many(specs) -> list[HitResult] | None             # default: loop over peek()
    # lockout, implemented on the plain cache API, overridable:
    def count_violation(client, window) -> int
    def lock(client, duration) -> float
    def locked_until(client) -> float | None
    def unlock(client) -> None
```
Custom backends (e.g. a Redis sorted-set exact sliding log, or a DB-backed audit store) only
need `hit`/`reset` and a dotted path in `TRAFFICWATCH["BACKEND"]`.

## Trade-offs
- Fixed window permits up to 2× `limit` across a bucket boundary; sliding reduces that to
  roughly 1×–1.3× depending on traffic shape, at the cost of one extra cache `get`.
- `_incr` is `add` + `incr`; if the key expires between the two the count restarts at 1.
  This can only under-count by one request at a window edge. The rollback `decr` of a
  rejected request costs one extra cache round trip per rule on rejections only.
- Not counting rejected requests means a hammering client is served exactly `limit` per
  window instead of (as in 0.3) locking itself out further. Operators who want escalation
  use `LOCKOUT`.
- A third-party backend that only implements `hit()` keeps "count everything" semantics;
  the default `hit_many` cannot roll back what it does not know about.
- Caches whose `incr` is a non-atomic get/set (`FileBasedCache`, `DatabaseCache`) lose counts
  under concurrency; `manage.py check` warns (`W007`).
- The recent-violations list is a non-atomic read-modify-write; under a flood of simultaneous
  first-crossings an entry may be lost. It is a diagnostic aid; the signal is the audit path.
- `LocMemCache` is per-process; production should use Redis/Memcached.
- Counting happens before the view runs, so a request blocked by auth still consumes quota.
  This is intentional (brute-force protection).
- The backend instance is created when the middleware is instantiated, so changing
  `BACKEND`, `CACHE_ALIAS` or `CACHE_PREFIX` needs a process restart. Everything else is
  read per request.
- Lockout violations are keyed on the global `KEY_FUNC` identity even when the crossed rule
  has its own `KEY_FUNC`; a per-rule key is usually a coarser or finer view of the same
  client, and one lockout identity keeps the escalation predictable.
- The Redis backend keeps the two-bucket estimate rather than an exact sorted-set log: the
  log costs one member per request (unbounded under attack) and would either stop counting
  blocked requests or keep a hammering client blocked forever. The estimate is bounded and
  matches the other backends exactly.

## Test layout

- `tests/`: unit and integration tests against the source tree, with frozen time, direct
  backend access and internal helpers. Fast and exhaustive.
- `acceptance/`: black-box tests from the package user's point of view: a real project
  (`acceptance/project/`) configured per the README, driven through HTTP with real time
  (2-second global window) and only public API. CI runs it against the built wheel from a
  directory outside the repo, on LocMem and on Redis. Add a scenario here whenever a
  README promise changes.

## Release flow

```
bump __version__  →  update CHANGELOG  →  git tag vX.Y.Z  →  push --tags
                                                 │
                     GitHub Actions publish.yml: test → verify tag == version → build → PyPI
```
PyPI Trusted Publishing is used, so no API token is stored in GitHub.
