"""Request flow
    __init__            -> validate TRAFFICWATCH eagerly (typos fail at process start)
    process_view        -> skip EXEMPT_* / @trafficwatch_exempt,
                           resolve rules (view decorators > PATH_RULES > global),
                           count against every rule (one batch), maybe block
    __call__ (after)    -> if no view ran (404 from the resolver, a response short-circuited
                           by an earlier middleware) count the request against PATH_RULES /
                           the global rule now (COUNT_UNROUTED) and maybe replace the
                           response; attach rate-limit headers to whatever goes out

The middleware is both sync- and async-capable: under ASGI the response pass runs natively
in the event loop. ``process_view`` itself stays synchronous (cache backends are sync) and
Django adapts it with ``sync_to_async``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from asgiref.sync import iscoroutinefunction, markcoroutinefunction, sync_to_async
from django.http import HttpRequest, HttpResponse

from .conf import tw_settings
from .core import REQUEST_ATTR, TrafficWatch, TrafficWatchState, apply_headers, is_exempt_request
from .decorators import is_exempt_view, view_rules


class TrafficWatchMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response: Callable[[HttpRequest], Any]):
        self.get_response = get_response
        tw_settings.validate()  # raises ImproperlyConfigured: fail fast, not on first request
        self.watch = TrafficWatch()
        self.backend = self.watch.backend  # kept for introspection / backwards compatibility
        self._is_async = iscoroutinefunction(get_response)
        if self._is_async:
            markcoroutinefunction(self)

    # -- Django hooks ------------------------------------------------------

    def __call__(self, request: HttpRequest) -> HttpResponse | Awaitable[HttpResponse]:
        if self._is_async:
            return self.__acall__(request)
        response: HttpResponse = self.get_response(request)
        if not hasattr(request, REQUEST_ATTR):
            response = self._late_check(request) or response
        return apply_headers(response, getattr(request, REQUEST_ATTR, None))

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        response: HttpResponse = await self.get_response(request)
        if not hasattr(request, REQUEST_ATTR):
            response = await sync_to_async(self._late_check)(request) or response
        return apply_headers(response, getattr(request, REQUEST_ATTR, None))

    def process_view(
        self,
        request: HttpRequest,
        view_func: Callable[..., Any],
        view_args: tuple[Any, ...],
        view_kwargs: dict[str, Any],
    ) -> HttpResponse | None:
        method = request.method or "GET"
        if self._is_exempt(request) or is_exempt_view(view_func, method):
            setattr(request, REQUEST_ATTR, None)
            return None

        rules = view_rules(view_func, method)
        if rules:
            rules = tuple(r for r in rules if r.applies_to(method))
        if not rules:
            rules = tw_settings.rules_for(tw_settings.match_path(request), method)

        state: TrafficWatchState = self.watch.check(request, rules)
        setattr(request, REQUEST_ATTR, state)

        if state.blocked:
            return apply_headers(self.watch.block_response(request, state), state)
        return None

    def _late_check(self, request: HttpRequest) -> HttpResponse | None:
        """``process_view`` never ran: the URL did not resolve (404) or an earlier
        middleware answered the request itself. Count it against PATH_RULES / the global
        rule so scanners probing unknown URLs are limited too; return a block response
        when the client is already over."""
        if not tw_settings.COUNT_UNROUTED or self._is_exempt(request):
            setattr(request, REQUEST_ATTR, None)
            return None
        method = request.method or "GET"
        rules = tw_settings.rules_for(tw_settings.match_path(request), method)
        state = self.watch.check(request, rules)
        setattr(request, REQUEST_ATTR, state)
        if state.blocked:
            return self.watch.block_response(request, state)
        return None

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _is_exempt(request: HttpRequest) -> bool:
        return is_exempt_request(request)
