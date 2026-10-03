from django.apps import AppConfig


class TrafficWatchConfig(AppConfig):
    """Optional. Add ``"django_trafficwatch"`` to ``INSTALLED_APPS`` to get the system checks
    (``manage.py check``) and the ``trafficwatch_recent`` management command. The middleware
    itself works without it."""

    name = "django_trafficwatch"
    verbose_name = "Traffic Watch"

    def ready(self):
        from . import checks  # noqa: F401  (registers the system checks)
