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

from rest_framework.throttling import BaseThrottle

from .conf import tw_settings
from .core import REQUEST_ATTR, TrafficWatch, TrafficWatchState
from .decorators import RULES_ATTR, is_exempt_view

_watch: TrafficWatch | None = None


def _shared_watch() -> TrafficWatch:
    global _watch
    if _watch is None:
        _watch = TrafficWatch()
    return _watch


class TrafficWatchThrottle(BaseThrottle):
    state: TrafficWatchState | None = None

    def allow_request(self, request, view) -> bool:
        django_request = getattr(request, "_request", request)

        # Middleware already evaluated (and would have blocked) this request.
        if hasattr(django_request, REQUEST_ATTR):
            self.state = getattr(django_request, REQUEST_ATTR)
            return True

        view_cls = view if isinstance(view, type) else type(view)
        if is_exempt_view(view_cls):
            return True

        rules = tuple(getattr(view_cls, RULES_ATTR, ()))
        if rules:
            rules = tuple(r for r in rules if r.applies_to(request.method))
        if not rules:
            rules = tw_settings.rules_for(django_request.path, request.method)

        self.state = _shared_watch().check(django_request, rules)
        setattr(django_request, REQUEST_ATTR, self.state)
        return not self.state.blocked

    def wait(self):
        if self.state is None:
            return None
        return self.state.retry_after
