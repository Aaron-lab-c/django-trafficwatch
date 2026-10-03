"""Installation and setup: what a user checks right after ``pip install``."""

import os
import pathlib

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError

import django_trafficwatch


def test_public_api_is_importable():
    from django_trafficwatch import Rule, traffic_exceeded, trafficwatch_exempt, trafficwatch_rule

    assert callable(trafficwatch_rule) and callable(trafficwatch_exempt)
    assert Rule.__name__ == "Rule" and hasattr(traffic_exceeded, "connect")
    assert django_trafficwatch.__version__.count(".") == 2


def test_package_is_typed_and_ships_its_modules():
    pkg = pathlib.Path(django_trafficwatch.__file__).parent
    assert (pkg / "py.typed").exists()
    for name in ("middleware.py", "drf.py", "urls.py", "views.py", "backends/redis_lua.py"):
        assert (pkg / name).exists(), name
    if os.environ.get("TW_ACCEPTANCE_INSTALLED"):
        # CI runs this suite against the built wheel from outside the repo.
        assert "site-packages" in str(pkg), pkg


def test_readme_configuration_passes_manage_py_check():
    call_command("check")  # raises SystemCheckError on any Error-level message


def test_manage_py_check_catches_a_typo(settings):
    settings.TRAFFICWATCH = {**settings.TRAFFICWATCH, "MAX_REQUEST": 10}
    with pytest.raises(SystemCheckError, match="trafficwatch.E016"):
        call_command("check")


def test_management_command_is_discovered():
    from io import StringIO

    out = StringIO()
    call_command("trafficwatch_recent", stdout=out, stderr=StringIO())
    assert "No violations recorded." in out.getvalue()
