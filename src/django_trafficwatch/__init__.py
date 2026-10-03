"""django-trafficwatch: configurable traffic monitoring / rate limiting middleware for Django."""

__version__ = "0.3.0"

from .decorators import trafficwatch_exempt, trafficwatch_rule  # noqa: F401
from .rules import Rule  # noqa: F401
from .signals import traffic_exceeded  # noqa: F401

__all__ = ["Rule", "trafficwatch_exempt", "trafficwatch_rule", "traffic_exceeded", "__version__"]
