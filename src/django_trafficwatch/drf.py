"""Django REST Framework integration.

``TrafficWatchThrottle`` reuses the same backend, rule resolution and notifications as the
middleware, so a project can protect plain Django views with the middleware and DRF views
with a throttle class, both configured from ``TRAFFICWATCH``::

    REST_FRAMEWORK = {
        "DEFAULT_THROTTLE_CLASSES": ["django_trafficwatch.drf.TrafficWatchThrottle"],
    }

    @trafficwatch_rule(60, 5, methods=["POST"])
    class LoginView(APIView): ...

If the middleware is installed as well it has already counted the request by the time the
throttle runs, so the throttle simply defers to it and nothing is counted twice.
"""

from __future__ import annotations

from typing import Any

from rest_framework.throttling import BaseThrottle

from .conf import tw_settings
from .core import REQUEST_ATTR, TrafficWatch, TrafficWatchState, is_exempt_request
from .decorators import class_rules, is_exempt_class

_watch: TrafficWatch | None = None


def _shared_watch() -> TrafficWatch:
    global _watch
    if _watch is None:
        tw_settings.validate()
        _watch = TrafficWatch()
    return _watch


class TrafficWatchThrottle(BaseThrottle):  # type: ignore[misc]
    state: TrafficWatchState | None = None

    def allow_request(self, request: Any, view: Any) -> bool:
        django_request = getattr(request, "_request", request)
        method = request.method or "GET"

        # Middleware already evaluated (and would have blocked) this request.
        if hasattr(django_request, REQUEST_ATTR):
            self.state = getattr(django_request, REQUEST_ATTR)
            return True

        view_cls = view if isinstance(view, type) else type(view)
        if is_exempt_request(django_request) or is_exempt_class(view_cls, method):
            return True

        rules = class_rules(view_cls, method)
        if rules:
            rules = tuple(r for r in rules if r.applies_to(method))
        if not rules:
            rules = tw_settings.rules_for(tw_settings.match_path(django_request), method)

        self.state = _shared_watch().check(django_request, rules)
        setattr(django_request, REQUEST_ATTR, self.state)
        return not self.state.blocked

    def wait(self) -> float | None:
        if self.state is None:
            return None
        return self.state.retry_after
