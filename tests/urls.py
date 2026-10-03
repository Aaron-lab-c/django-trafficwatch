from django.http import HttpResponse, JsonResponse
from django.urls import path
from django.views import View

from django_trafficwatch import trafficwatch_exempt, trafficwatch_rule


def ok(request):
    return HttpResponse("ok")


async def async_ok(request):
    return HttpResponse("async ok")


def echo_state(request):
    """Expose request.trafficwatch so tests can assert on what the middleware recorded."""
    state = request.trafficwatch
    return JsonResponse(
        {
            "exceeded": state.exceeded,
            "blocked": state.blocked,
            "rules": [r.rule.name for r in state.results],
            "counts": [r.result.count for r in state.results],
        }
    )


@trafficwatch_exempt
def exempt_view(request):
    return HttpResponse("exempt")


@trafficwatch_rule(window_seconds=60, max_requests=1)
def strict_view(request):
    return HttpResponse("strict")


@trafficwatch_rule(60, 2, name="stacked-minute")
@trafficwatch_rule(3600, 3, name="stacked-hour")
def stacked_view(request):
    return HttpResponse("stacked")


@trafficwatch_rule(60, 1, methods=["POST"])
def post_only_view(request):
    return HttpResponse("post-only")


@trafficwatch_rule(60, 1)
class StrictClassView(View):
    def get(self, request):
        return HttpResponse("cbv")


@trafficwatch_exempt
class ExemptClassView(View):
    def get(self, request):
        return HttpResponse("cbv exempt")


urlpatterns = [
    path("", ok),
    path("async/", async_ok),
    path("state/", echo_state),
    path("health/", ok),
    path("api/login/", ok),
    path("api/login/sso/", ok),
    path("api/v1/export/", ok),
    path("api/v2/export/", ok),
    path("decorated/exempt/", exempt_view),
    path("decorated/strict/", strict_view),
    path("decorated/stacked/", stacked_view),
    path("decorated/post-only/", post_only_view),
    path("cbv/strict/", StrictClassView.as_view()),
    path("cbv/exempt/", ExemptClassView.as_view()),
]

try:
    from rest_framework.views import APIView
except ImportError:  # DRF is optional
    pass
else:
    from rest_framework.response import Response

    class PlainAPIView(APIView):
        throttle_classes = []

        def get(self, request):
            return Response({"ok": True})

    @trafficwatch_rule(60, 2, methods=["POST"], name="drf-login")
    class LoginAPIView(APIView):
        throttle_classes = []

        def post(self, request):
            return Response({"ok": True})

        def get(self, request):
            return Response({"ok": True})

    urlpatterns += [
        path("drf/plain/", PlainAPIView.as_view()),
        path("drf/login/", LoginAPIView.as_view()),
    ]
