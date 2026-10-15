"""apirouter.config — YAML load, ${ENV} expansion, schema."""

import pytest

from apirouter import config as cfgmod

RAW = {
    "listen": {"gateway": "0.0.0.0:9090", "proxy": "127.0.0.1:3130"},
    "upstream": {"base_url": "https://up.example", "api_key": "${TEST_UPSTREAM_KEY}"},
    "pricing": {"deepseek-v4-flash": {"input": 0.27, "output": 1.10}},
    "firewall": {"default": "deny", "allow": {"domains": ["*.example"]}},
    "metering": {"jsonl_path": "/tmp/x.jsonl", "stdout": False},
}


def test_from_dict_full(monkeypatch):
    monkeypatch.setenv("TEST_UPSTREAM_KEY", "sk-real")
    cfg = cfgmod.from_dict(RAW)

    assert cfg.listen.gateway_host == "0.0.0.0" and cfg.listen.gateway_port == 9090
    assert cfg.listen.proxy_host == "127.0.0.1" and cfg.listen.proxy_port == 3130
    assert cfg.upstream.base_url == "https://up.example"
    assert cfg.upstream.api_key == "sk-real"
    assert cfg.pricing["deepseek-v4-flash"]["input"] == 0.27
    assert cfg.firewall.default == "deny" and "*.example" in cfg.firewall.allow_domains
    assert cfg.metering.stdout is False


def test_missing_env_var_errors(monkeypatch):
    monkeypatch.delenv("TEST_UPSTREAM_KEY", raising=False)
    with pytest.raises(ValueError, match="TEST_UPSTREAM_KEY"):
        cfgmod.from_dict(RAW)


def test_base_url_required():
    with pytest.raises(ValueError, match="base_url"):
        cfgmod.from_dict({"upstream": {}})


def test_api_key_required():
    """No keyless mode: the router forwards with the upstream's key, and a client's
    token is a billing principal rather than a provider credential — so an upstream
    without a key has nothing valid to send."""
    with pytest.raises(ValueError, match="api_key is required"):
        cfgmod.from_dict({"upstream": {"base_url": "https://up.example"}})


def test_principals_parsed():
    cfg = cfgmod.from_dict({
        "upstream": {"base_url": "https://up.example", "api_key": "K"},
        "principals": [{"id": "alice", "token": "a-tok"}, {"id": "bob", "token": "b-tok"}],
    })
    assert cfg.principal_for("a-tok") == "alice"
    assert cfg.principal_for("b-tok") == "bob"
    assert cfg.principal_for("nope") is None
    assert cfg.principal_for(None) is None


def test_relative_jsonl_path_resolved_to_config_dir():
    cfg = cfgmod.from_dict(
        {"upstream": {"base_url": "x", "api_key": "K"}, "metering": {"jsonl_path": "usage.jsonl"}},
        base_dir="/data",
    )
    assert cfg.metering.jsonl_path == "/data/usage.jsonl"


def test_absolute_jsonl_path_left_alone():
    cfg = cfgmod.from_dict(
        {"upstream": {"base_url": "x", "api_key": "K"}, "metering": {"jsonl_path": "/var/log/u.jsonl"}},
        base_dir="/data",
    )
    assert cfg.metering.jsonl_path == "/var/log/u.jsonl"


def test_budgets_parsed():
    cfg = cfgmod.from_dict({
        "upstream": {"base_url": "https://up.example", "api_key": "K"},
        "principals": [{"id": "alice", "token": "a", "budget_usd": 5},
                       {"id": "bob", "token": "b"}],
    })
    assert cfg.budgets == {"alice": 5.0}   # bob omitted → unlimited
