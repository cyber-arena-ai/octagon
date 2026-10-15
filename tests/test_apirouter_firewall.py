"""apirouter.firewall — egress allow/deny rule engine."""

from apirouter.firewall import Firewall


def test_default_deny():
    fw = Firewall(default="deny")
    assert not fw.decide("example.com").allowed


def test_default_allow():
    fw = Firewall(default="allow")
    assert fw.decide("example.com").allowed


def test_allow_domain_glob():
    fw = Firewall(default="deny", allow_domains=["*.ppapi.ai", "api.anthropic.com"])
    assert fw.decide("app-us.ppapi.ai").allowed
    assert fw.decide("api.anthropic.com").allowed
    assert not fw.decide("evil.com").allowed


def test_deny_wins_over_allow():
    fw = Firewall(default="allow", allow_domains=["*"], deny_domains=["*.evil.com"])
    d = fw.decide("c2.evil.com")
    assert not d.allowed
    assert d.reason.startswith("deny:domain:")


def test_cidr_allow_and_deny():
    fw = Firewall(default="deny", allow_cidrs=["10.0.0.0/8"], deny_cidrs=["10.6.6.0/24"])
    assert fw.decide("host", ip="10.1.2.3").allowed
    assert not fw.decide("host", ip="10.6.6.9").allowed   # deny cidr wins
    assert not fw.decide("host", ip="192.168.1.1").allowed  # default deny


def test_case_insensitive_domain():
    fw = Firewall(default="deny", allow_domains=["API.Anthropic.com"])
    assert fw.decide("api.anthropic.com").allowed


def test_reason_strings():
    fw = Firewall(default="deny", allow_domains=["ok.com"])
    assert fw.decide("ok.com").reason == "allow:domain:ok.com"
    assert fw.decide("no.com").reason == "default:deny"
