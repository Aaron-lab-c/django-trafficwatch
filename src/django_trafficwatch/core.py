"""Framework-agnostic enforcement logic shared by the middleware and the DRF throttle.

``TrafficWatch.check()`` evaluates a set of rules for a request and returns a
``TrafficWatchState``; notification (log, signal, callback, recent-violations store) and
building the block response live here too so both entry points behave identically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.http import HttpResponse, JsonResponse

from . import stats
from .backends.base import BaseBackend, HitResult
from .conf import tw_settings
from .keys import safe_key_part
from .rules import Rule
from .signals import traffic_exceeded

logger = logging.getLogger("django_trafficwatch")

REQUEST_ATTR = "trafficwatch"


@dataclass(frozen=True)
class RuleResult:
    rule: Rule
    client: str
    result: HitResult

    @property
    def blocks(self) -> bool:
        """Exceeded *and* configured to block (per-rule BLOCK beats the global one)."""
        block = self.rule.block if self.rule.block is not None else tw_settings.BLOCK
        return bool(block and self.result.exceeded)

    def info(self, request) -> dict:
        """Serializable summary handed to the signal, ON_EXCEEDED and the recent store."""
        return {
            "client": self.client,
            "path": request.path,
            "method": request.method,
            "rule": self.rule.name,
            "count": self.result.count,
            "limit": self.result.limit,
            "window": self.rule.window,
            "reset_in": self.result.reset_in,
            "blocked": self.blocks,
        }


@dataclass(frozen=True)
class TrafficWatchState:
    """Everything the middleware learnt about one request. Exposed as ``request.trafficwatch``
    so views, templates and log filters can read it."""

    results: tuple[RuleResult, ...]

    @property
    def strictest(self) -> RuleResult:
        """The rule closest to (or furthest past) its limit; drives the X-RateLimit headers."""
        return min(
            self.results,
            key=lambda r: (
                r.result.remaining,
                -(r.result.count - r.result.limit),
                -r.result.reset_in,
            ),
        )

    @property
    def exceeded(self) -> bool:
        return any(r.result.exceeded for r in self.results)

    @property
    def blocked(self) -> bool:
        return any(r.blocks for r in self.results)

    @property
    def retry_after(self) -> int:
        """Seconds until every blocking rule would accept a request again."""
        waits = [r.result.reset_in for r in self.results if r.blocks]
        return max(waits) if waits else self.strictest.result.reset_in

    def headers(self) -> dict[str, str]:
        s = self.strictest.result
        return {
            "X-RateLimit-Limit": str(s.limit),
            "X-RateLimit-Remaining": str(s.remaining),
            "X-RateLimit-Reset": str(s.reset_in),
        }


class TrafficWatch:
    def __init__(self, backend: BaseBackend | None = None):
        self.backend = backend or tw_settings.backend_class()(
            tw_settings.CACHE_ALIAS, tw_settings.CACHE_PREFIX
        )

    # -- evaluation ----------------------------------------------------------

    def check(self, request, rules: tuple[Rule, ...]) -> TrafficWatchState:
        """Count this request against every rule, fire notifications for rules that were
        just crossed and return the combined state. Does not build a response."""
        default_key_func = tw_settings.KEY_FUNC
        results = []
        for rule in rules:
            client = (rule.key_func or default_key_func)(request)
            hit = self.backend.hit(
                safe_key_part(client), safe_key_part(rule.name), rule.window, rule.limit
            )
            results.append(RuleResult(rule=rule, client=client, result=hit))

        state = TrafficWatchState(results=tuple(results))
        for rr in state.results:
            if rr.result.just_exceeded:
                self.notify(request, rr)
        return state

    def peek(self, request, rules: tuple[Rule, ...]) -> TrafficWatchState | None:
        """Read-only view of the counters (no request is counted). None if the backend
        cannot answer."""
        default_key_func = tw_settings.KEY_FUNC
        results = []
        for rule in rules:
            client = (rule.key_func or default_key_func)(request)
            hit = self.backend.peek(
                safe_key_part(client), safe_key_part(rule.name), rule.window, rule.limit
            )
            if hit is None:
                return None
            results.append(RuleResult(rule=rule, client=client, result=hit))
        return TrafficWatchState(results=tuple(results))

    # -- side effects --------------------------------------------------------

    def notify(self, request, rr: RuleResult) -> None:
        info = rr.info(request)
        logger.warning(
            "Traffic limit exceeded: rule=%(rule)s client=%(client)s %(method)s %(path)s "
            "count=%(count)s limit=%(limit)s/%(window)ss blocked=%(blocked)s",
            info,
            extra={"trafficwatch": info},
        )
        stats.record_violation(info)
        traffic_exceeded.send(sender=self.__class__, request=request, info=info)
        callback = tw_settings.ON_EXCEEDED
        if callback:
            try:
                callback(request, info)
            except Exception:  # a broken alert hook must never break the request
                logger.exception("TRAFFICWATCH ON_EXCEEDED callback failed")

    def block_response(self, request, state: TrafficWatchState) -> HttpResponse:
        retry_after = state.retry_after
        strictest = min((r for r in state.results if r.blocks), key=lambda r: -r.result.reset_in)
        info = strictest.info(request)
        info["retry_after"] = retry_after

        custom = tw_settings.BLOCK_RESPONSE
        if custom:
            response = custom(request, info)
        else:
            response = JsonResponse(
                {"detail": tw_settings.BLOCK_MESSAGE, "retry_after": retry_after},
                status=tw_settings.BLOCK_STATUS,
            )
        response["Retry-After"] = str(retry_after)
        return response


def apply_headers(response: HttpResponse, state: TrafficWatchState | None) -> HttpResponse:
    if state is not None and tw_settings.HEADERS:
        for name, value in state.headers().items():
            response[name] = value
    return response
