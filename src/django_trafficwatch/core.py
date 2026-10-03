"""Framework-agnostic enforcement logic shared by the middleware and the DRF throttle.

``TrafficWatch.check()`` evaluates a set of rules for a request and returns a
``TrafficWatchState``; notification (log, signal, callback, recent-violations store),
escalating lockout, fail-open handling and building the block response live here too so
both entry points behave identically.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any

from django.http import HttpResponse, JsonResponse

from . import stats
from .backends.base import BaseBackend, HitResult
from .conf import tw_settings
from .keys import client_ip, is_exempt_client, safe_key_part
from .rules import Rule
from .signals import traffic_exceeded

logger = logging.getLogger("django_trafficwatch")

REQUEST_ATTR = "trafficwatch"
LOCKOUT_RULE_NAME = "lockout"

_outage_lock = threading.Lock()
_outage_logged_at: dict[str, float] = {}


def _log_throttled(kind: str, message: str, *args: Any) -> None:
    """Log ``message`` at ERROR with the traceback at most once per
    FAIL_OPEN_LOG_INTERVAL seconds per ``kind`` (cache outage, broken KEY_FUNC, ...)."""
    now = time.time()
    interval = float(tw_settings.FAIL_OPEN_LOG_INTERVAL or 0)
    with _outage_lock:
        should_log = now - _outage_logged_at.get(kind, 0.0) >= interval
        if should_log:
            _outage_logged_at[kind] = now
    if should_log:
        logger.error(message, *args, exc_info=True)


def _reset_log_throttle() -> None:  # for tests
    _outage_logged_at.clear()


def client_for(request: Any, key_func: Any) -> str:
    """Run a KEY_FUNC. A raising key func is logged (throttled) and the request falls back
    to the client IP so it is still limited instead of failing with a 500."""
    try:
        return str(key_func(request))
    except Exception:
        _log_throttled(
            "key_func", "TRAFFICWATCH KEY_FUNC %r raised; keying on the client IP.", key_func
        )
        return f"ip:{client_ip(request)}"


def _sf_string(value: str) -> str:
    """Quote a value as an HTTP Structured Field string (RFC 8941)."""
    printable = "".join(ch if " " <= ch <= "~" else "?" for ch in value)
    return '"' + printable.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True)
class LockoutState:
    """Escalating-lockout bookkeeping for one request (``request.trafficwatch.lockout``)."""

    violations: int  # crossings seen in the lockout window, including this one
    threshold: int  # LOCKOUT["VIOLATIONS"]
    window: int
    duration: int
    locked_until: float | None  # epoch seconds, None when not locked

    @property
    def active(self) -> bool:
        return self.locked_until is not None and self.locked_until > time.time()

    @property
    def retry_after(self) -> int:
        if self.locked_until is None:
            return 0
        return max(math.ceil(self.locked_until - time.time()), 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "violations": self.violations,
            "threshold": self.threshold,
            "window": self.window,
            "duration": self.duration,
            "locked_until": self.locked_until,
            "active": self.active,
        }


def rule_enforces(rule: Rule) -> bool:
    """Per-rule BLOCK beats the global one."""
    return bool(rule.block if rule.block is not None else tw_settings.BLOCK)


@dataclass(frozen=True)
class RuleResult:
    rule: Rule
    client: str
    result: HitResult

    @property
    def blocks(self) -> bool:
        """Exceeded *and* configured to block (per-rule BLOCK beats the global one)."""
        return rule_enforces(self.rule) and self.result.exceeded

    def info(
        self, request: Any, *, lockout: LockoutState | None = None, degraded: bool = False
    ) -> dict[str, Any]:
        """Serializable summary handed to the signal, ON_EXCEEDED and the recent store."""
        locked = lockout is not None and lockout.active
        return {
            "client": self.client,
            "path": request.path,
            "method": request.method,
            "rule": self.rule.name,
            "count": self.result.count,
            "limit": self.result.limit,
            "window": self.rule.window,
            "reset_in": self.result.reset_in,
            "blocked": self.blocks or locked,
            "lockout": lockout.as_dict() if lockout is not None else None,
            "degraded": degraded,
        }


@dataclass(frozen=True)
class TrafficWatchState:
    """Everything the middleware learnt about one request. Exposed as ``request.trafficwatch``
    so views, templates and log filters can read it.

    ``results`` is empty when the request was not counted: during a cache outage
    (``degraded``) or while the client is locked out (``lockout.active``)."""

    results: tuple[RuleResult, ...]
    degraded: bool = False
    lockout: LockoutState | None = None
    fallback_retry_after: int = 1  # Retry-After when no rule result is available

    @property
    def strictest(self) -> RuleResult | None:
        """The rule closest to (or furthest past) its limit; drives the rate-limit headers."""
        if not self.results:
            return None
        return min(
            self.results,
            key=lambda r: (
                r.result.remaining,
                -(r.result.count - r.result.limit),
                -r.result.reset_in,
            ),
        )

    @property
    def locked(self) -> bool:
        return self.lockout is not None and self.lockout.active

    @property
    def exceeded(self) -> bool:
        return self.locked or any(r.result.exceeded for r in self.results)

    @property
    def blocked(self) -> bool:
        if self.degraded and not tw_settings.FAIL_OPEN:
            return True
        return self.locked or any(r.blocks for r in self.results)

    @property
    def retry_after(self) -> int:
        """Seconds until every blocking rule (and the lockout) would accept a request again."""
        waits = [r.result.reset_in for r in self.results if r.blocks]
        if self.lockout is not None and self.lockout.active:
            waits.append(self.lockout.retry_after)
        if waits:
            return max(waits)
        strictest = self.strictest
        return strictest.result.reset_in if strictest else self.fallback_retry_after

    def info(self, request: Any) -> dict[str, Any]:
        """Info dict for the block response: the strictest blocking rule, or a rule-less
        summary when the request was rejected by the lockout / fail-closed outage."""
        blocking = [r for r in self.results if r.blocks]
        if blocking:
            strictest = max(blocking, key=lambda r: r.result.reset_in)
            info = strictest.info(request, lockout=self.lockout, degraded=self.degraded)
        else:
            client = client_for(request, tw_settings.KEY_FUNC)
            info = {
                "client": client,
                "path": request.path,
                "method": request.method,
                "rule": LOCKOUT_RULE_NAME if self.locked else None,
                "count": None,
                "limit": None,
                "window": None,
                "reset_in": self.retry_after,
                "blocked": True,
                "lockout": self.lockout.as_dict() if self.lockout is not None else None,
                "degraded": self.degraded,
            }
        info["retry_after"] = self.retry_after
        return info

    def headers(self) -> dict[str, str]:
        style = tw_settings.HEADERS_STYLE
        headers: dict[str, str] = {}
        strictest = self.strictest
        if strictest is None:
            if self.locked and style in ("ietf", "both"):
                headers["RateLimit"] = f"{_sf_string(LOCKOUT_RULE_NAME)};r=0;t={self.retry_after}"
            return headers
        s = strictest.result
        if style in ("x-ratelimit", "both"):
            reset = s.reset_in
            if tw_settings.RESET_AS_EPOCH:
                reset = int(time.time()) + reset
            headers["X-RateLimit-Limit"] = str(s.limit)
            headers["X-RateLimit-Remaining"] = str(s.remaining)
            headers["X-RateLimit-Reset"] = str(reset)
        if style in ("ietf", "both"):
            headers["RateLimit-Policy"] = ", ".join(
                f"{_sf_string(r.rule.name)};q={r.rule.limit};w={r.rule.window}"
                for r in self.results
            )
            name, remaining, reset_in = strictest.rule.name, s.remaining, s.reset_in
            if self.locked:
                name, remaining, reset_in = LOCKOUT_RULE_NAME, 0, self.retry_after
            headers["RateLimit"] = f"{_sf_string(name)};r={remaining};t={reset_in}"
        return headers


def is_exempt_request(request: Any) -> bool:
    """EXEMPT_METHODS / EXEMPT_PATHS / EXEMPT_CLIENTS / EXEMPT_FUNC (view decorators are
    checked separately by the caller, which knows the view)."""
    if request.method in tw_settings.EXEMPT_METHODS:
        return True
    path = tw_settings.match_path(request)
    if any(path.startswith(p) for p in tw_settings.EXEMPT_PATHS):
        return True
    if is_exempt_client(request):
        return True
    func = tw_settings.EXEMPT_FUNC
    if func is None:
        return False
    try:
        return bool(func(request))
    except Exception:  # a broken exemption hook must not 500 the site: count the request
        _log_throttled("exempt_func", "TRAFFICWATCH EXEMPT_FUNC %r raised; not exempting.", func)
        return False


class TrafficWatch:
    def __init__(self, backend: BaseBackend | None = None):
        self.backend: BaseBackend = backend or tw_settings.backend_class()(
            tw_settings.CACHE_ALIAS, tw_settings.CACHE_PREFIX
        )

    # -- evaluation ----------------------------------------------------------

    def check(self, request: Any, rules: tuple[Rule, ...]) -> TrafficWatchState:
        """Count this request against every rule, fire notifications for rules that were
        just crossed and return the combined state. Does not build a response."""
        default_key_func = tw_settings.KEY_FUNC
        lockout_cfg = tw_settings.lockout
        clients = [client_for(request, rule.key_func or default_key_func) for rule in rules]
        lock_client = safe_key_part(client_for(request, default_key_func)) if lockout_cfg else None

        try:
            if lockout_cfg is not None and lock_client is not None:
                until = self.backend.locked_until(lock_client)
                if until is not None:
                    active = LockoutState(
                        violations=lockout_cfg.violations,
                        threshold=lockout_cfg.violations,
                        window=lockout_cfg.window,
                        duration=lockout_cfg.duration,
                        locked_until=until,
                    )
                    return TrafficWatchState(results=(), lockout=active)

            hits = self.backend.hit_many(
                [
                    (
                        safe_key_part(client),
                        safe_key_part(rule.name),
                        rule.window,
                        rule.limit,
                        rule_enforces(rule),
                    )
                    for client, rule in zip(clients, rules)
                ]
            )
            results = tuple(
                RuleResult(rule=rule, client=client, result=hit)
                for rule, client, hit in zip(rules, clients, hits)
            )
            crossed = [rr for rr in results if rr.result.just_exceeded]

            lockout: LockoutState | None = None
            if lockout_cfg is not None and lock_client is not None and crossed:
                violations = self.backend.count_violation(lock_client, lockout_cfg.window)
                until = None
                if violations >= lockout_cfg.violations:
                    until = self.backend.lock(lock_client, lockout_cfg.duration)
                lockout = LockoutState(
                    violations=violations,
                    threshold=lockout_cfg.violations,
                    window=lockout_cfg.window,
                    duration=lockout_cfg.duration,
                    locked_until=until,
                )
        except Exception:
            return self._degraded(rules)

        state = TrafficWatchState(results=results, lockout=lockout)
        for rr in crossed:
            self.notify(request, rr, state)
        return state

    def peek(self, request: Any, rules: tuple[Rule, ...]) -> TrafficWatchState | None:
        """Read-only view of the counters (no request is counted). None if the backend
        cannot answer."""
        default_key_func = tw_settings.KEY_FUNC
        clients = [client_for(request, rule.key_func or default_key_func) for rule in rules]
        try:
            hits = self.backend.peek_many(
                [
                    (safe_key_part(client), safe_key_part(rule.name), rule.window, rule.limit)
                    for client, rule in zip(clients, rules)
                ]
            )
        except Exception:
            return None
        if hits is None:
            return None
        return TrafficWatchState(
            results=tuple(
                RuleResult(rule=rule, client=client, result=hit)
                for rule, client, hit in zip(rules, clients, hits)
            )
        )

    def _degraded(self, rules: tuple[Rule, ...]) -> TrafficWatchState:
        """The cache raised: log (rate limited) and return an empty, degraded state."""
        _log_throttled(
            "cache",
            "TrafficWatch cache backend unavailable; %s requests until it recovers (FAIL_OPEN=%s).",
            "allowing" if tw_settings.FAIL_OPEN else "blocking",
            tw_settings.FAIL_OPEN,
        )
        retry = min((rule.window for rule in rules), default=60)
        return TrafficWatchState(results=(), degraded=True, fallback_retry_after=retry)

    # -- side effects --------------------------------------------------------

    def notify(self, request: Any, rr: RuleResult, state: TrafficWatchState | None = None) -> None:
        lockout = state.lockout if state is not None else None
        info = rr.info(request, lockout=lockout)
        logger.warning(
            "Traffic limit exceeded: rule=%(rule)s client=%(client)s %(method)s %(path)s "
            "count=%(count)s limit=%(limit)s/%(window)ss blocked=%(blocked)s",
            info,
            extra={"trafficwatch": info},
        )
        try:
            stats.record_violation(info)
        except Exception:  # the cache may be flapping; never break the request
            logger.exception("TrafficWatch could not record the violation")
        traffic_exceeded.send(sender=self.__class__, request=request, info=info)
        callback = tw_settings.ON_EXCEEDED
        if callback:
            try:
                callback(request, info)
            except Exception:  # a broken alert hook must never break the request
                logger.exception("TRAFFICWATCH ON_EXCEEDED callback failed")

    def block_response(self, request: Any, state: TrafficWatchState) -> HttpResponse:
        retry_after = state.retry_after
        info = state.info(request)

        custom = tw_settings.BLOCK_RESPONSE
        response: HttpResponse
        if custom:
            response = custom(request, info)
        else:
            response = JsonResponse(
                # str() so a gettext_lazy BLOCK_MESSAGE serialises in the active language.
                {"detail": str(tw_settings.BLOCK_MESSAGE), "retry_after": retry_after},
                status=tw_settings.BLOCK_STATUS,
            )
        response["Retry-After"] = str(retry_after)
        return response


def apply_headers(response: HttpResponse, state: TrafficWatchState | None) -> HttpResponse:
    if state is not None and tw_settings.HEADERS:
        for name, value in state.headers().items():
            response[name] = value
    return response
