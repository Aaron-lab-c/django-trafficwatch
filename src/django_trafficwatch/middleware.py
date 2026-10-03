"""Request flow
    process_view        -> skip EXEMPT_PATHS / EXEMPT_METHODS / @trafficwatch_exempt,
                           resolve rules (view decorators > PATH_RULES > global),
                           count against every rule, maybe block
    __call__ (after)    -> attach X-RateLimit-* headers to whatever response came back

The middleware is both sync- and async-capable: under ASGI the response pass runs natively
in the event loop. ``process_view`` itself stays synchronous (cache backends are sync) and
Django adapts it with ``sync_to_async``.
"""

from __future__ import annotations

from asgiref.sync import iscoroutinefunction, markcoroutinefunction

from .conf import tw_settings
from .core import REQUEST_ATTR, TrafficWatch, TrafficWatchState, apply_headers
from .decorators import is_exempt_view, view_rules


class TrafficWatchMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self.watch = TrafficWatch()
        self.backend = self.watch.backend  # kept for introspection / backwards compatibility
        self._is_async = iscoroutinefunction(get_response)
        if self._is_async:
            markcoroutinefunction(self)

    # -- Django hooks ------------------------------------------------------

    def __call__(self, request):
        if self._is_async:
            return self.__acall__(request)
        response = self.get_response(request)
        return apply_headers(response, getattr(request, REQUEST_ATTR, None))

    async def __acall__(self, request):
        response = await self.get_response(request)
        return apply_headers(response, getattr(request, REQUEST_ATTR, None))

    def process_view(self, request, view_func, view_args, view_kwargs):
        if self._is_exempt(request) or is_exempt_view(view_func):
            setattr(request, REQUEST_ATTR, None)
            return None

        rules = view_rules(view_func)
        if rules:
            rules = tuple(r for r in rules if r.applies_to(request.method))
        if not rules:
            rules = tw_settings.rules_for(request.path, request.method)

        state: TrafficWatchState = self.watch.check(request, rules)
        setattr(request, REQUEST_ATTR, state)

        if state.blocked:
            return apply_headers(self.watch.block_response(request, state), state)
        return None

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _is_exempt(request) -> bool:
        if request.method in tw_settings.EXEMPT_METHODS:
            return True
        path = request.path
        return any(path.startswith(p) for p in tw_settings.EXEMPT_PATHS)
