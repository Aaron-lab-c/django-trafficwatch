"""Rate limiting as documented in the README: global rule, PATH_RULES, decorators, headers,
observe mode, custom responses, backends."""

import time

import pytest
from conftest import new_client, wait_for_fresh_window
from django.test import Client
from django.utils.translation import gettext_lazy

# -- the global rule ------------------------------------------------------------------


def test_global_limit_headers_and_429(client):
    for i in range(3):
        r = client.get("/")
        assert r.status_code == 200
        assert r["X-RateLimit-Limit"] == "3"
        assert r["X-RateLimit-Remaining"] == str(2 - i)
        assert 1 <= int(r["X-RateLimit-Reset"]) <= 2
    r = client.get("/")
    assert r.status_code == 429
    assert r["Content-Type"] == "application/json"
    body = r.json()
    assert body["detail"] == "Too many requests, please slow down."
    assert body["retry_after"] == int(r["Retry-After"]) >= 1
    assert r["X-RateLimit-Remaining"] == "0"


def test_retry_after_is_honest(client):
    """Waiting exactly Retry-After seconds is enough; the window really is 2 seconds."""
    for _ in range(3):
        client.get("/")
    r = client.get("/")
    assert r.status_code == 429
    time.sleep(int(r["Retry-After"]))
    assert client.get("/").status_code == 200


def test_rejected_requests_are_not_counted(client):
    """Hammering does not extend the block: after the window the client is served again,
    and a slightly-over client keeps getting ``limit`` per window."""
    for _ in range(3):
        client.get("/")
    for _ in range(20):
        assert client.get("/").status_code == 429
    r = client.get("/state/")  # also 429, but Retry-After still fits in the window
    assert int(r["Retry-After"]) <= 2
    time.sleep(int(r["Retry-After"]))
    wait_for_fresh_window()
    assert client.get("/state/").json()["counts"] == [1]


def test_clients_are_independent():
    wait_for_fresh_window()
    a, b = Client(REMOTE_ADDR="203.0.113.1"), Client(REMOTE_ADDR="203.0.113.2")
    for _ in range(3):
        a.get("/")
    assert a.get("/").status_code == 429
    assert b.get("/").status_code == 200


@pytest.mark.django_db
def test_authenticated_users_are_keyed_by_user(client, django_user_model):
    """Same user from two addresses shares one counter (default KEY_FUNC user_or_ip)."""
    wait_for_fresh_window()
    user = django_user_model.objects.create_user("alice", password="pw")
    a, b = Client(REMOTE_ADDR="203.0.113.1"), Client(REMOTE_ADDR="203.0.113.2")
    a.force_login(user)
    b.force_login(user)
    a.get("/")
    a.get("/")
    b.get("/")
    assert b.get("/").status_code == 429
    assert Client(REMOTE_ADDR="203.0.113.1").get("/").status_code == 200  # anonymous: ip key


def test_request_state_is_available_to_views(client):
    data = client.get("/state/").json()
    assert data == {
        "exempt": False,
        "exceeded": False,
        "blocked": False,
        "degraded": False,
        "rules": ["*"],
        "counts": [1],
        "headers": {
            "X-RateLimit-Limit": "3",
            "X-RateLimit-Remaining": "2",
            "X-RateLimit-Reset": data["headers"]["X-RateLimit-Reset"],
        },
    }


# -- PATH_RULES -----------------------------------------------------------------------


def test_path_prefix_rule(client):
    assert client.get("/api/export/").status_code == 200
    assert client.get("/api/export/csv/").status_code == 200  # same prefix, same bucket
    r = client.get("/api/export/")
    assert r.status_code == 429 and r["X-RateLimit-Limit"] == "2"
    assert client.get("/").status_code == 200  # the global bucket is separate


def test_method_scoped_rules_and_several_limits_per_path(client):
    # POST: 2/hour and 3/day. GET: not covered -> global rule.
    assert client.post("/api/login/").status_code == 200
    assert client.post("/api/login/").status_code == 200
    r = client.post("/api/login/")
    assert r.status_code == 429 and r["X-RateLimit-Limit"] == "2"
    for _ in range(3):
        assert client.get("/api/login/").status_code == 200
    assert client.get("/api/login/").status_code == 429


def test_regex_rule(client):
    assert client.get("/api/v1/search/").status_code == 200
    assert client.get("/api/v2/search/").status_code == 429  # same regex rule bucket


def test_longest_prefix_wins_and_global_falls_back(client, tw):
    tw(PATH_RULES={"/api/": {"MAX_REQUESTS": 50}, "/api/export/": {"MAX_REQUESTS": 1}})
    assert client.get("/api/export/").status_code == 200
    assert client.get("/api/export/").status_code == 429
    assert client.get("/api/login/")["X-RateLimit-Limit"] == "50"
    assert client.get("/")["X-RateLimit-Limit"] == "3"


def test_per_rule_block_and_key_func_in_settings(client, tw):
    tw(
        PATH_RULES={
            "/api/export/": {"MAX_REQUESTS": 1, "BLOCK": False, "NAME": "trial"},
            "/api/login/": {"MAX_REQUESTS": 1, "KEY_FUNC": lambda request: "everyone"},
        }
    )
    for _ in range(4):
        assert client.get("/api/export/").status_code == 200  # observed only
    assert client.get("/api/login/").status_code == 200
    assert Client(REMOTE_ADDR="203.0.113.99").get("/api/login/").status_code == 429


def test_malformed_path_rules_are_rejected(tw):
    from django.core.exceptions import ImproperlyConfigured

    tw(PATH_RULES={"/x/": {"LIMIT": 5}})
    with pytest.raises(ImproperlyConfigured, match="unknown rule keys"):
        new_client()


# -- decorators -------------------------------------------------------------------------


def test_stacked_decorators_with_methods(client):
    assert client.post("/otp/").status_code == 200
    assert client.post("/otp/").status_code == 200
    r = client.post("/otp/")
    assert r.status_code == 429 and r["X-RateLimit-Limit"] == "2"
    # GET is only covered by the weekly rule (no methods=), 3 allowed.
    for _ in range(1):
        assert client.get("/otp/").status_code == 200
    assert client.get("/otp/").status_code == 429  # 2 POST + 1 GET = 3 weekly


def test_exempt_decorator(client):
    for _ in range(10):
        r = client.get("/health/")
        assert r.status_code == 200 and "X-RateLimit-Limit" not in r


def test_class_based_view_decorators(client):
    assert client.get("/search/").status_code == 200
    assert client.get("/search/").status_code == 429
    assert client.post("/comments/").status_code == 200
    assert client.post("/comments/").status_code == 429  # method_decorator on post
    for _ in range(3):
        assert client.get("/comments/").status_code == 200  # GET: global rule
    assert client.get("/comments/").status_code == 429


def test_observe_only_rule_and_custom_key_func(client):
    for _ in range(4):
        assert client.get("/observed/").status_code == 200
    assert client.get("/by-key/", HTTP_X_API_KEY="k1").status_code == 200
    assert client.get("/by-key/", HTTP_X_API_KEY="k1").status_code == 429
    assert client.get("/by-key/", HTTP_X_API_KEY="k2").status_code == 200


# -- observe mode, responses, headers ---------------------------------------------------


def test_global_observe_mode(client, tw):
    tw(BLOCK=False)
    for _ in range(6):
        r = client.get("/state/")
        assert r.status_code == 200
    assert r.json()["exceeded"] is True and r.json()["blocked"] is False


def test_custom_status_message_and_lazy_translation(client, tw):
    tw(BLOCK_STATUS=503, BLOCK_MESSAGE=gettext_lazy("Please wait"))
    for _ in range(3):
        client.get("/")
    r = client.get("/")
    assert r.status_code == 503 and r.json()["detail"] == "Please wait"


def test_custom_block_response(client, tw):
    tw(BLOCK_RESPONSE="project.alerts.too_many")
    for _ in range(3):
        client.get("/")
    r = client.get("/")
    assert r.status_code == 429 and r["Content-Type"].startswith("text/html")
    assert b"ip:203.0.113.10 / *" in r.content
    assert "Retry-After" in r and "X-RateLimit-Limit" in r


def test_ietf_headers(client, tw):
    tw(HEADERS_STYLE="ietf")
    r = client.post("/api/login/")
    assert "X-RateLimit-Limit" not in r
    assert r["RateLimit-Policy"] == '"/api/login/#0";q=2;w=3600, "login-daily";q=3;w=86400'
    assert r["RateLimit"].startswith('"/api/login/#0";r=1;t=')
    tw(HEADERS_STYLE="both")
    r = client.get("/")
    assert "X-RateLimit-Limit" in r and "RateLimit" in r
    tw(HEADERS=False)
    assert "RateLimit" not in client.get("/")


def test_reset_as_epoch(client, tw):
    tw(RESET_AS_EPOCH=True)
    now = int(time.time())
    reset = int(client.get("/")["X-RateLimit-Reset"])
    assert now < reset <= now + 3


# -- backends and paths -----------------------------------------------------------------


@pytest.mark.parametrize("backend", ["fixed", "sliding", "redis"])
def test_every_backend_enforces_the_same_limit(tw, backend):
    tw(BACKEND=backend)
    wait_for_fresh_window()
    c = new_client(REMOTE_ADDR="203.0.113.77")
    for _ in range(3):
        assert c.get("/").status_code == 200
    r = c.get("/")
    assert r.status_code == 429 and int(r["Retry-After"]) >= 1


def test_match_path_info_under_script_name(tw):
    tw(MATCH_PATH_INFO=True)
    c = Client(REMOTE_ADDR="203.0.113.78", SCRIPT_NAME="/app")
    assert c.get("/api/export/").status_code == 200
    assert c.get("/api/export/").status_code == 200
    assert c.get("/api/export/").status_code == 429  # matched /api/export/ despite /app
