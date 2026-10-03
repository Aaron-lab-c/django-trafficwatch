"""Alerts, logging, the recent-violations store, command and staff views."""

import json
import logging
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client
from project import alerts


def test_callback_signal_and_log_fire_once_per_window(client, caplog):
    with caplog.at_level(logging.WARNING, logger="django_trafficwatch"):
        for _ in range(8):
            client.get("/")
    assert len(alerts.NOTIFIED) == 1
    info = alerts.NOTIFIED[0]
    assert info["client"] == "ip:203.0.113.10" and info["rule"] == "*"
    assert info["count"] == 4 and info["limit"] == 3 and info["window"] == 2
    assert info["method"] == "GET" and info["path"] == "/" and info["blocked"] is True
    assert info["lockout"] is None and info["degraded"] is False
    # Prometheus-style counter fed by the signal (README example)
    assert alerts.EXCEEDED_TOTAL == {("*", True): 1}
    records = [r for r in caplog.records if "Traffic limit exceeded" in r.getMessage()]
    assert len(records) == 1 and records[0].trafficwatch == info


def test_broken_alert_hook_does_not_break_requests(client, tw):
    def boom(request, info):
        raise RuntimeError("pager down")

    tw(ON_EXCEEDED=boom)
    for _ in range(3):
        client.get("/")
    assert client.get("/").status_code == 429


def test_recent_violations_command(client):
    for _ in range(4):
        client.get("/")
    out = StringIO()
    call_command("trafficwatch_recent", stdout=out)
    text = out.getvalue()
    assert "ip:203.0.113.10" in text and "4/3" in text and "GET" in text

    out = StringIO()
    call_command("trafficwatch_recent", "--json", "-n", "5", stdout=out)
    rows = json.loads(out.getvalue())
    assert len(rows) == 1 and rows[0]["rule"] == "*" and "at" in rows[0]

    call_command("trafficwatch_recent", "--clear", stdout=StringIO())
    out = StringIO()
    call_command("trafficwatch_recent", stdout=out, stderr=StringIO())
    assert "No violations recorded." in out.getvalue()


def test_recent_violations_can_be_disabled(client, tw):
    tw(RECENT_VIOLATIONS=0)
    for _ in range(4):
        client.get("/")
    out = StringIO()
    call_command("trafficwatch_recent", stdout=out, stderr=StringIO())
    assert "No violations recorded." in out.getvalue()


@pytest.mark.django_db
def test_staff_views(client, django_user_model):
    for _ in range(4):
        client.get("/")

    anon = Client(REMOTE_ADDR="203.0.113.50")
    assert anon.get("/trafficwatch/recent.json").status_code == 403
    assert anon.get("/trafficwatch/recent/").status_code == 403

    staff = Client(REMOTE_ADDR="203.0.113.51")
    staff.force_login(django_user_model.objects.create_user("ops", password="pw", is_staff=True))
    data = staff.get("/trafficwatch/recent.json?limit=10").json()
    assert data["violations"][0]["client"] == "ip:203.0.113.10"
    html = staff.get("/trafficwatch/recent/").content.decode()
    assert "ip:203.0.113.10" in html and "4/3" in html
    # The inspection views themselves are never counted.
    for _ in range(10):
        r = staff.get("/trafficwatch/recent.json")
        assert r.status_code == 200 and "X-RateLimit-Limit" not in r
