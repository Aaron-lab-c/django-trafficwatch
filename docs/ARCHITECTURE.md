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
                                │
                                ▼
TrafficWatchMiddleware ──┐
                         ├──> core.TrafficWatch.check(request, rules)
drf.TrafficWatchThrottle ┘        │  for each rule:
                                  │     client = (rule.key_func or KEY_FUNC)(request)
                                  │     backend.hit(client, rule.name, window, limit) -> HitResult
                                  ▼
                            TrafficWatchState (request.trafficwatch)
                                  │
        ├── any rule just_exceeded -> logger.warning(extra=info) + stats.record_violation
        │                            + traffic_exceeded signal + ON_EXCEEDED
        ├── state.blocked           -> BLOCK_RESPONSE or JSON 429, Retry-After
        └── always                  -> X-RateLimit-* headers (strictest rule)
```

| Module | Responsibility |
|---|---|
| `conf.py` | Defaults, lazy access to `settings.TRAFFICWATCH`, cached `RuleSet`, backend alias lookup |
| `rules.py` | `Rule` dataclass, validation of `PATH_RULES`, prefix/regex matching, method filtering |
| `keys.py` | Built-in client identifiers (`client_ip` with trusted-proxy logic, `user_or_ip`), cache-safe key hashing |
| `core.py` | `TrafficWatch` (count, notify, build block response), `TrafficWatchState`, header application |
| `backends/base.py` | `BaseBackend` contract (`hit`, `peek`, `reset`) + `HitResult` + atomic `_incr` helper |
| `backends/fixed_window.py` | Aligned bucket counter — cheapest, exact per bucket |
| `backends/sliding_window.py` | Two-bucket weighted estimate — smooths bursts, honest `Retry-After` |
| `decorators.py` | `@trafficwatch_exempt`, `@trafficwatch_rule(...)` (stackable, CBV-aware) |
| `middleware.py` | Django entry point, sync + async capable |
| `drf.py` | `TrafficWatchThrottle` for Django REST Framework (optional dependency) |
| `stats.py` | Bounded "recent violations" list in the cache |
| `checks.py` / `apps.py` | System checks registered when the app is in `INSTALLED_APPS` |
| `management/commands/trafficwatch_recent.py` | Inspect / clear recent violations |
| `signals.py` | `traffic_exceeded` Django signal |

## Request flow (middleware)

1. `__call__` runs the rest of the stack, then decorates the outgoing response with headers.
   Under ASGI `__acall__` does the same without leaving the event loop.
2. `process_view` (runs after URL resolution, so the view function is known):
   1. Skip if `request.method` is in `EXEMPT_METHODS`, the path starts with an `EXEMPT_PATHS`
      prefix, or the view (or its `view_class`) is `@trafficwatch_exempt`.
   2. Resolve rules: view decorators filtered by method → `PATH_RULES` (regex first, then
      longest prefix; method-filtered) → global rule. Never empty.
   3. For every rule: `client = key_func(request)`, one atomic `incr`.
   4. Store the `TrafficWatchState` on `request.trafficwatch`.
   5. For each rule crossed for the first time (`count == limit + 1`): log, record, signal,
      callback.
   6. If any exceeded rule blocks (per-rule `BLOCK`, else global): return the block response
      with `Retry-After` = the longest wait among blocking rules. The view never runs.

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

`client_ip()` returns `REMOTE_ADDR` unless it belongs to `TRUSTED_PROXIES`. Only then is
`X-Forwarded-For` read, walking from the rightmost hop (closest to us) leftwards and
returning the first hop that is *not* a trusted proxy. A client that sends
`X-Forwarded-For: 1.2.3.4` through a trusted proxy ends up with `1.2.3.4, <real-ip>` and is
keyed on `<real-ip>`.

Identifiers are passed through `safe_key_part()` before reaching the cache: anything with
whitespace / control characters or longer than 120 chars is replaced by a SHA-256 prefix, so
custom `KEY_FUNC`s cannot produce keys Memcached rejects.

## Cache key layout

```
{PREFIX}:{fw|sw}:{rule_name}:{client}:{bucket}
{PREFIX}:recent                                   (list of recent violation dicts)
```
- `rule_name` isolates buckets so `/api/login/` and the global rule don't share counts.
- `bucket = int(now // window)` makes keys self-expiring; TTL = window (+1 window for sliding).

## Sliding window and `Retry-After`

```
estimate = previous_bucket * (1 - elapsed_fraction) + current_bucket
```

When `estimate >= limit`, `reset_in` is the smallest `t` such that a request at `now + t`
would be accepted: either the point inside the current bucket where the previous bucket has
decayed enough, or (if the current bucket alone is already full) the roll-over plus the decay
time of the current bucket. The integer flooring of the estimate is ignored in that solve,
which can only make the wait longer by under a second. Tests assert that a request issued
exactly `reset_in` seconds later is accepted.

## Backend contract

```python
class BaseBackend:
    def hit(client, rule, window, limit) -> HitResult
    def peek(client, rule, window, limit) -> HitResult | None   # optional, read-only
    def reset(client, rule, window) -> None
```
Custom backends (e.g. Redis sorted-set true sliding log, or a DB-backed audit store) only
need `hit`/`reset` and a dotted path in `TRAFFICWATCH["BACKEND"]`.

## Trade-offs
- Fixed window permits up to 2× `limit` across a bucket boundary; sliding reduces that to
  roughly 1×–1.3× depending on traffic shape, at the cost of one extra cache `get`.
- `_incr` is `add` + `incr`; if the key expires between the two the count restarts at 1.
  This can only under-count by one request at a window edge.
- The recent-violations list is a non-atomic read-modify-write; under a flood of simultaneous
  first-crossings an entry may be lost. It is a diagnostic aid; the signal is the audit path.
- `LocMemCache` is per-process; production should use Redis/Memcached.
- Counting happens before the view runs, so a request blocked by auth still consumes quota.
  This is intentional (brute-force protection).
- The backend instance is created when the middleware is instantiated, so changing
  `BACKEND`, `CACHE_ALIAS` or `CACHE_PREFIX` needs a process restart. Everything else is
  read per request.

## Release flow

```
bump __version__  →  update CHANGELOG  →  git tag vX.Y.Z  →  push --tags
                                                 │
                     GitHub Actions publish.yml: test → verify tag == version → build → PyPI
```
PyPI Trusted Publishing is used, so no API token is stored in GitHub.
