"""apirouter.budget — per-principal usage ledger + spend cap."""

from apirouter.budget import Budget


def test_unlimited_when_no_cap():
    b = Budget({})
    b.record("alice", cost=999.0)
    assert not b.exceeded("alice")


def test_exceeded_after_reaching_cap():
    b = Budget({"alice": 10.0})
    assert not b.exceeded("alice")
    b.record("alice", cost=4.0)
    assert not b.exceeded("alice")       # spent 4 < 10
    b.record("alice", cost=6.0)
    assert b.exceeded("alice")           # spent 10 >= 10


def test_record_ignores_none_principal():
    b = Budget({"alice": 1.0})
    b.record(None, cost=5.0)
    assert b.spent("alice") == 0.0


def test_tracks_calls_and_tokens():
    b = Budget({"alice": 5.0})
    b.record("alice", input_tokens=10, output_tokens=4, cost=1.0)
    b.record("alice", input_tokens=6, output_tokens=2, cost=0.5)
    assert b.spent("alice") == 1.5


def test_report():
    b = Budget({"alice": 5.0, "carol": 2.0})  # carol capped, no usage yet
    b.record("alice", input_tokens=10, output_tokens=4, cost=1.5)
    b.record("bob", input_tokens=3, output_tokens=1, cost=0.2)  # bob uncapped
    rep = {r["principal"]: r for r in b.report()}

    assert rep["alice"]["calls"] == 1
    assert rep["alice"]["input_tokens"] == 10 and rep["alice"]["output_tokens"] == 4
    assert rep["alice"]["cost_usd"] == 1.5
    assert rep["alice"]["budget_usd"] == 5.0 and rep["alice"]["remaining_usd"] == 3.5
    assert rep["bob"]["budget_usd"] is None and rep["bob"]["remaining_usd"] is None
    assert rep["carol"]["calls"] == 0 and rep["carol"]["remaining_usd"] == 2.0  # capped, unused
