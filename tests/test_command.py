import json
from io import StringIO

from django.core.management import call_command


def violate(client, n=4):
    for _ in range(n):
        client.get("/")


def test_recent_command_empty():
    out = StringIO()
    call_command("trafficwatch_recent", stdout=out)
    assert "No violations recorded." in out.getvalue()


def test_recent_command_table(client):
    violate(client)
    out = StringIO()
    call_command("trafficwatch_recent", stdout=out)
    text = out.getvalue()
    assert "ip:1.1.1.1" in text and "4/3" in text and "GET" in text


def test_recent_command_json_and_limit(client):
    violate(client)
    out = StringIO()
    call_command("trafficwatch_recent", "--json", "-n", "1", stdout=out)
    rows = json.loads(out.getvalue())
    assert len(rows) == 1 and rows[0]["rule"] == "*"


def test_recent_command_clear(client):
    violate(client)
    out = StringIO()
    call_command("trafficwatch_recent", "--clear", stdout=out)
    assert "Cleared" in out.getvalue()
    out = StringIO()
    call_command("trafficwatch_recent", stdout=out)
    assert "No violations recorded." in out.getvalue()


def test_recent_command_explains_locmem(settings):
    if "locmem" not in settings.CACHES["default"]["BACKEND"].lower():
        import pytest

        pytest.skip("suite is running against Redis")
    out, err = StringIO(), StringIO()
    call_command("trafficwatch_recent", stdout=out, stderr=err)
    assert "No violations recorded." in out.getvalue()
    assert "LocMemCache" in err.getvalue() and "own process" in err.getvalue()
