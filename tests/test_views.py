import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.test import Client, RequestFactory

from django_trafficwatch.views import recent_violations_json, recent_violations_view

pytestmark = pytest.mark.django_db


def violate(n=4):
    c = Client(REMOTE_ADDR="1.1.1.1")
    for _ in range(n):
        c.get("/")


def get(view, user, query=""):
    request = RequestFactory().get(f"/trafficwatch/recent/{query}", REMOTE_ADDR="10.10.10.10")
    request.user = user
    return view(request)


@pytest.fixture
def staff():
    return User.objects.create_user("staff", password="pw", is_staff=True)


@pytest.mark.parametrize("view", [recent_violations_json, recent_violations_view])
def test_views_require_staff(view):
    assert get(view, AnonymousUser()).status_code == 403
    assert get(view, User.objects.create_user("plain", password="pw")).status_code == 403
    inactive = User.objects.create_user("gone", password="pw", is_staff=True, is_active=False)
    assert get(view, inactive).status_code == 403


def test_recent_json(staff):
    import json

    r = get(recent_violations_json, staff)
    assert r.status_code == 200 and json.loads(r.content) == {"violations": []}
    violate()
    r = get(recent_violations_json, staff, "?limit=5")
    rows = json.loads(r.content)["violations"]
    assert len(rows) == 1
    assert rows[0]["client"] == "ip:1.1.1.1" and rows[0]["rule"] == "*" and "when" in rows[0]
    assert r["Cache-Control"].startswith("max-age=0")


def test_recent_html(staff):
    violate()
    r = get(recent_violations_view, staff)
    assert r.status_code == 200
    body = r.content.decode()
    assert "ip:1.1.1.1" in body and "4/3" in body and "<table>" in body


def test_recent_html_empty(staff):
    body = get(recent_violations_view, staff).content.decode()
    assert "No violations recorded." in body


def test_limit_param_is_sanitised(staff):
    assert get(recent_violations_json, staff, "?limit=abc").status_code == 200
    assert get(recent_violations_json, staff, "?limit=-5").status_code == 200


def test_mounted_urls_are_exempt_from_counting():
    """``include("django_trafficwatch.urls")`` works and the views are never counted."""
    c = Client(REMOTE_ADDR="10.10.10.10")
    for _ in range(10):
        r = c.get("/trafficwatch/recent.json")
        assert r.status_code == 403 and "X-RateLimit-Limit" not in r
        r = c.get("/trafficwatch/recent/")
        assert r.status_code == 403 and "X-RateLimit-Limit" not in r
