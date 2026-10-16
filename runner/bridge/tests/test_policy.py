import pytest

from bridge.policy import Policy, PolicyError, PolicyRule, fallback_verdict, render_input


def test_render_input_per_tool():
    assert render_input("Bash", {"command": "git status", "description": "x"}) == "git status"
    assert render_input("Edit", {"file_path": "/w/a.py", "old_string": "a", "new_string": "b"}) == "/w/a.py"
    assert render_input("Grep", {"pattern": "foo", "path": "/w"}) == "foo /w"
    assert render_input("mcp__x__y", {"b": 1, "a": "z"}) == '{"a": "z", "b": 1}'
    assert render_input("Bash", "raw") == "raw"


def test_evaluate_order_deny_allow_ask():
    policy = Policy.from_dict(
        {
            "allow": [{"tool": "Read"}, {"tool": "Bash", "match": r"^git (status|diff)"}],
            "ask": [{"tool": "*"}],
            "deny": [{"tool": "Bash", "match": r"rm -rf"}],
        }
    )
    assert policy.evaluate("Bash", {"command": "git status"}) == "allow"
    assert policy.evaluate("Bash", {"command": "git status && rm -rf /"}) == "deny"
    assert policy.evaluate("Read", {"file_path": "/etc/passwd"}) == "allow"
    assert policy.evaluate("Write", {"file_path": "/w/x"}) == "ask"


def test_unmatched_is_none_and_fallback_by_mode():
    policy = Policy()
    assert policy.evaluate("Bash", {"command": "ls"}) is None
    assert fallback_verdict("default", "Bash") == "ask"
    assert fallback_verdict("acceptEdits", "Edit") == "allow"
    assert fallback_verdict("acceptEdits", "Bash") == "ask"
    assert fallback_verdict("bypassPermissions", "Bash") == "allow"
    assert fallback_verdict("plan", "Write") == "deny"
    assert fallback_verdict("plan", "Read") == "ask"


def test_allow_exact_adds_anchored_rule():
    policy = Policy()
    rule = policy.allow_exact("Bash", {"command": "ls -la (x)"})
    assert rule in policy.allow
    assert policy.evaluate("Bash", {"command": "ls -la (x)"}) == "allow"
    assert policy.evaluate("Bash", {"command": "ls -la (x) && rm"}) is None
    policy.allow_exact("Bash", {"command": "ls -la (x)"})
    assert len(policy.allow) == 1


def test_invalid_policy_rejected():
    with pytest.raises(PolicyError):
        Policy.from_dict({"allow": [{"tool": "Bash", "match": "("}]})
    with pytest.raises(PolicyError):
        Policy.from_dict({"allow": [{"tool": ""}]})
    with pytest.raises(PolicyError):
        Policy.from_dict({"bogus": []})
    with pytest.raises(PolicyError):
        PolicyRule.from_dict(42)


def test_round_trip():
    raw = {"allow": [{"tool": "Read"}], "ask": [], "deny": [{"tool": "Bash", "match": "sudo"}]}
    assert Policy.from_dict(raw).to_dict() == raw


def test_auto_mode_allows_unmatched_calls_but_policy_still_wins():
    from bridge.policy import Policy, fallback_verdict

    assert fallback_verdict("auto", "Bash") == "allow" and fallback_verdict("auto", "Write") == "allow"
    policy = Policy.from_dict({"deny": [{"tool": "Bash", "match": "rm -rf"}], "ask": [{"tool": "Bash", "match": "^git push"}]})
    assert policy.evaluate("Bash", {"command": "rm -rf /"}) == "deny"
    assert policy.evaluate("Bash", {"command": "git push origin main"}) == "ask"
    assert policy.evaluate("Bash", {"command": "ls"}) is None          # → fallback → allow in auto


def test_sdk_mode_mapping():
    from bridge.claude_provider import sdk_permission_mode

    assert sdk_permission_mode("auto") == "default" and sdk_permission_mode("plan") == "plan"
