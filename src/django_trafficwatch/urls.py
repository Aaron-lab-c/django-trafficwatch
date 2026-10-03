"""``path("trafficwatch/", include("django_trafficwatch.urls"))`` -> staff-only views:
``recent/`` (HTML) and ``recent.json``."""

from django.urls import path

from .views import recent_violations_json, recent_violations_view

app_name = "trafficwatch"

urlpatterns = [
    path("recent/", recent_violations_view, name="recent"),
    path("recent.json", recent_violations_json, name="recent-json"),
]
