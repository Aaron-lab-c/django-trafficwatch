"""Staff-only inspection views for the recent-violations store.

Mount them with::

    urlpatterns = [path("trafficwatch/", include("django_trafficwatch.urls")), ...]

which gives ``trafficwatch/recent/`` (a self-contained, read-only HTML table; it needs no
``TEMPLATES`` configuration) and ``trafficwatch/recent.json``. Both require an active staff
user and answer 403 otherwise. The views are exempt from counting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from django.http import HttpRequest, HttpResponse, HttpResponseForbidden, JsonResponse
from django.template import Context, Engine
from django.views.decorators.cache import never_cache

from . import stats
from .conf import tw_settings
from .decorators import trafficwatch_exempt

DEFAULT_LIMIT = 100

_TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Traffic Watch: recent violations</title>
<style>
body{font:14px/1.4 system-ui,sans-serif;margin:2rem;color:#222}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.35rem .6rem;
border-bottom:1px solid #ddd;white-space:nowrap}th{background:#f4f4f4}
td.path{white-space:normal;word-break:break-all}.lock{color:#b00;font-weight:600}
</style></head><body>
<h1>Traffic Watch: recent violations</h1>
<p>{{ rows|length }} of the last {{ keep }} first-crossings, newest first.
<a href="recent.json">JSON</a></p>
{% if rows %}<table><thead><tr><th>when (UTC)</th><th>client</th><th>rule</th>
<th>method</th><th>count/limit</th><th>window</th><th>blocked</th><th>lockout</th>
<th>path</th></tr></thead><tbody>
{% for r in rows %}<tr><td>{{ r.when }}</td><td>{{ r.client }}</td><td>{{ r.rule }}</td>
<td>{{ r.method }}</td><td>{{ r.count }}/{{ r.limit }}</td><td>{{ r.window }}s</td>
<td>{{ r.blocked|yesno:"yes,no" }}</td>
<td>{% if r.lockout.active %}<span class="lock">locked</span>{% elif r.lockout %}
{{ r.lockout.violations }}/{{ r.lockout.threshold }}{% endif %}</td>
<td class="path">{{ r.path }}</td></tr>
{% endfor %}</tbody></table>{% else %}<p>No violations recorded.</p>{% endif %}
</body></html>"""


def _is_staff(request: HttpRequest) -> bool:
    user = getattr(request, "user", None)
    return bool(
        user is not None and getattr(user, "is_active", False) and getattr(user, "is_staff", False)
    )


def _limit(request: HttpRequest) -> int:
    try:
        return max(1, min(int(request.GET.get("limit", DEFAULT_LIMIT)), 10_000))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def _rows(limit: int) -> list[dict[str, Any]]:
    rows = []
    for row in stats.recent_violations(limit):
        when = datetime.fromtimestamp(float(row.get("at", 0)), tz=timezone.utc)
        rows.append({**row, "when": when.strftime("%Y-%m-%d %H:%M:%S")})
    return rows


@trafficwatch_exempt
@never_cache
def recent_violations_json(request: HttpRequest) -> HttpResponse:
    """``GET ?limit=N`` -> ``{"violations": [...]}`` (newest first). Staff only."""
    if not _is_staff(request):
        return JsonResponse({"detail": "Staff only."}, status=403)
    return JsonResponse({"violations": _rows(_limit(request))}, json_dumps_params={"default": str})


@trafficwatch_exempt
@never_cache
def recent_violations_view(request: HttpRequest) -> HttpResponse:
    """Read-only HTML table of the recent violations. Staff only."""
    if not _is_staff(request):
        return HttpResponseForbidden("Staff only.")
    # A private Engine: works without TEMPLATES and cannot be affected by project loaders.
    template = Engine(autoescape=True).from_string(_TEMPLATE)
    context = Context({"rows": _rows(_limit(request)), "keep": tw_settings.RECENT_VIOLATIONS})
    return HttpResponse(template.render(context))
