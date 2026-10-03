"""Hooks a project would write: an alert callback, an exemption predicate and a
Prometheus-style counter fed by the signal (as shown in the README)."""

from collections import Counter

from django.dispatch import receiver

from django_trafficwatch import traffic_exceeded

NOTIFIED: list = []  # every ON_EXCEEDED call (reset by the test fixture)
EXCEEDED_TOTAL: Counter = Counter()  # (rule, blocked) -> count, like a Prometheus counter


def notify(request, info):
    NOTIFIED.append(info)


def is_superuser(request):
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_authenticated and user.is_superuser)


@receiver(traffic_exceeded)
def count_exceeded(sender, info, **kwargs):
    EXCEEDED_TOTAL[(info["rule"], info["blocked"])] += 1


def too_many(request, info):
    """A custom BLOCK_RESPONSE rendering HTML."""
    from django.http import HttpResponse

    return HttpResponse(
        f"<h1>Slow down</h1><p>{info['client']} / {info['rule']} / retry in "
        f"{info['retry_after']}s</p>",
        status=429,
        content_type="text/html",
    )
