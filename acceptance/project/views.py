from django.http import HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from rest_framework.response import Response
from rest_framework.views import APIView

from django_trafficwatch import trafficwatch_exempt, trafficwatch_rule


def ok(request):
    return HttpResponse("ok")


async def async_ok(request):
    return HttpResponse("async ok")


def state(request):
    """What a template or view can read from ``request.trafficwatch``."""
    tw = request.trafficwatch
    if tw is None:
        return JsonResponse({"exempt": True})
    return JsonResponse(
        {
            "exempt": False,
            "exceeded": tw.exceeded,
            "blocked": tw.blocked,
            "degraded": tw.degraded,
            "rules": [r.rule.name for r in tw.results],
            "counts": [r.result.count for r in tw.results],
            "headers": tw.headers(),
        }
    )


@trafficwatch_rule(window_seconds=86400, max_requests=2, methods=["POST"])
@trafficwatch_rule(86400 * 7, 3, name="otp-weekly")
def send_otp(request):
    return HttpResponse("otp sent")


@trafficwatch_exempt
def healthcheck(request):
    return HttpResponse("healthy")


@trafficwatch_rule(86400, 1)
class SearchView(View):
    def get(self, request):
        return HttpResponse("search")


@method_decorator(trafficwatch_rule(86400, 1), name="post")
class CommentView(View):
    def get(self, request):
        return HttpResponse("comments")

    def post(self, request):
        return HttpResponse("comment posted")


@trafficwatch_rule(86400, 1, block=False, name="observe-me")
def observed(request):
    return HttpResponse("observed")


@trafficwatch_rule(86400, 1, key_func=lambda request: request.headers.get("X-API-Key", "anon"))
def by_api_key(request):
    return HttpResponse("keyed")


class PlainAPIView(APIView):
    def get(self, request):
        return Response({"ok": True})


@trafficwatch_rule(86400, 2, methods=["POST"], name="drf-login")
class LoginAPIView(APIView):
    def post(self, request):
        return Response({"ok": True})

    def get(self, request):
        return Response({"ok": True})
