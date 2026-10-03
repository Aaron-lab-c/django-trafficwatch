import pytest

from django_trafficwatch import Rule, trafficwatch_rule
from django_trafficwatch.conf import tw_settings
from django_trafficwatch.decorators import view_rules
from django_trafficwatch.rules import RuleConfigError, RuleSet


def rs(path_rules, window=60, limit=3):
    return RuleSet(path_rules, window, limit)


def test_global_rule_when_nothing_matches():
    (rule,) = rs({}).for_request("/anything/", "GET")
    assert rule == Rule(name="*", window=60, limit=3)


def test_longest_prefix_wins():
    r = rs(
        {
            "/api/": {"MAX_REQUESTS": 50},
            "/api/login/": {"WINDOW_SECONDS": 300, "MAX_REQUESTS": 5},
        }
    )
    assert [str(x) for x in r.for_request("/api/login/sso/", "GET")] == ["/api/login/: 5/300s"]
    assert [str(x) for x in r.for_request("/api/users/", "GET")] == ["/api/: 50/60s"]
    assert r.for_request("/other/", "GET")[0].name == "*"


def test_regex_rules_take_precedence_over_prefixes():
    r = rs(
        {
            "/api/": {"MAX_REQUESTS": 50},
            r"re:^/api/v\d+/export/": {"WINDOW_SECONDS": 3600, "MAX_REQUESTS": 10},
        }
    )
    (rule,) = r.for_request("/api/v2/export/", "GET")
    assert (rule.window, rule.limit) == (3600, 10)
    (rule,) = r.for_request("/api/export/", "GET")  # no version segment -> prefix rule
    assert rule.limit == 50


def test_multiple_rules_per_entry_get_indexed_names():
    r = rs({"/api/login/": [{"MAX_REQUESTS": 5}, {"WINDOW_SECONDS": 86400, "MAX_REQUESTS": 100}]})
    rules = r.for_request("/api/login/", "POST")
    assert [x.name for x in rules] == ["/api/login/#0", "/api/login/#1"]
    assert [(x.window, x.limit) for x in rules] == [(60, 5), (86400, 100)]


def test_explicit_rule_name():
    r = rs({"/x/": {"NAME": "exports", "MAX_REQUESTS": 1}})
    assert r.for_request("/x/", "GET")[0].name == "exports"


def test_method_filter_falls_back_to_global():
    r = rs({"/api/login/": {"MAX_REQUESTS": 1, "METHODS": ["post"]}})
    assert r.for_request("/api/login/", "POST")[0].limit == 1
    assert r.for_request("/api/login/", "GET")[0].name == "*"


def test_method_filter_keeps_only_applicable_rules_in_entry():
    r = rs(
        {
            "/api/": [
                {"MAX_REQUESTS": 1, "METHODS": ["POST"], "NAME": "writes"},
                {"MAX_REQUESTS": 100, "NAME": "all"},
            ]
        }
    )
    assert [x.name for x in r.for_request("/api/", "POST")] == ["writes", "all"]
    assert [x.name for x in r.for_request("/api/", "GET")] == ["all"]


def test_per_rule_block_and_key_func():
    key = lambda request: "k"  # noqa: E731
    r = rs({"/x/": {"BLOCK": False, "KEY_FUNC": key}})
    (rule,) = r.for_request("/x/", "GET")
    assert rule.block is False and rule.key_func is key


def test_key_func_dotted_path_is_imported():
    r = rs({"/x/": {"KEY_FUNC": "django_trafficwatch.keys.client_ip"}})
    from django_trafficwatch.keys import client_ip

    assert r.for_request("/x/", "GET")[0].key_func is client_ip


@pytest.mark.parametrize(
    "bad, message",
    [
        ({"/x/": {"MAX_REQUESTS": 0}}, "positive integer"),
        ({"/x/": {"WINDOW_SECONDS": -1}}, "positive integer"),
        ({"/x/": {"MAX_REQUESTS": True}}, "positive integer"),
        ({"/x/": {"LIMIT": 5}}, "unknown rule keys"),
        ({"/x/": {"METHODS": "POST"}}, "METHODS must be a list"),
        ({"/x/": {"METHODS": []}}, "must not be empty"),
        ({"/x/": {"BLOCK": "no"}}, "BLOCK must be a boolean"),
        ({"/x/": []}, "empty rule list"),
        ({"/x/": 5}, "rule must be a dict"),
        ({"": {}}, "non-empty string"),
        ({"re:(": {}}, "invalid regex"),
    ],
)
def test_malformed_rules_raise(bad, message):
    with pytest.raises(RuleConfigError, match=message):
        rs(bad)


def test_settings_ruleset_is_cached_and_invalidated(tw):
    first = tw_settings.ruleset
    assert tw_settings.ruleset is first
    tw(PATH_RULES={"/x/": {"MAX_REQUESTS": 1}})
    assert tw_settings.ruleset is not first
    assert tw_settings.rules_for("/x/")[0].limit == 1


def test_legacy_rule_for(tw):
    tw(PATH_RULES={"/api/login/": {"WINDOW_SECONDS": 300, "MAX_REQUESTS": 5}})
    assert tw_settings.rule_for("/api/login/sso/") == (300, 5, "/api/login/")
    assert tw_settings.rule_for("/other/") == (60, 3, "*")


def test_decorator_stacking_preserves_declaration_order():
    @trafficwatch_rule(60, 2)
    @trafficwatch_rule(3600, 10)
    def view(request): ...

    rules = view_rules(view)
    assert [(r.window, r.limit) for r in rules] == [(60, 2), (3600, 10)]
    assert rules[0].name.startswith("view:") and rules[0].name.endswith("view#1")
    assert rules[1].name.endswith(".view")


def test_decorator_rejects_non_positive():
    with pytest.raises(ValueError):
        trafficwatch_rule(0, 1)
