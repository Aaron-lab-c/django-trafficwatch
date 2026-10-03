from django.urls import include, path

from . import views

urlpatterns = [
    path("", views.ok),
    path("async/", views.async_ok),
    path("state/", views.state),
    path("health/", views.healthcheck),
    path("api/export/", views.ok),
    path("api/export/csv/", views.ok),
    path("api/login/", views.ok),
    path("api/v1/search/", views.ok),
    path("api/v2/search/", views.ok),
    path("otp/", views.send_otp),
    path("search/", views.SearchView.as_view()),
    path("comments/", views.CommentView.as_view()),
    path("observed/", views.observed),
    path("by-key/", views.by_api_key),
    path("drf/plain/", views.PlainAPIView.as_view()),
    path("drf/login/", views.LoginAPIView.as_view()),
    path("trafficwatch/", include("django_trafficwatch.urls")),
]
