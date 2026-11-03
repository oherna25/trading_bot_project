import json
from datetime import datetime, timezone

import pytest

from bot.agent import parse_proposal, propose_order, run_agent_cycle
from bot.deployments import create_deployment, run_deployments
from bot.journal import Journal
from bot.optionbook import open_option_positions
from bot.shapes import Bar, ChainSnapshot, OperatorProfile, OptionContract, SellCandidate, SellLeg, StrategyDeployment
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
SLUG_ENV = {"BASETEN_API_KEY": "key-1", "BASETEN_REASON_MODEL_ID": "meta-models/muse-spark-open-1026"}
DEPLOYMENT = StrategyDeployment(id="d1", strategy_id="cash_secured_put", symbols=("SPY",))


def _candidate(strike, annualized):
    return SellCandidate(
        symbol="SPY", strategy_id="cash_secured_put", action="SELL_PUT",
        legs=(SellLeg("sell", "put", strike),), expiry="2026-11-13", dte=17,
        credit=1.0, collateral_per_contract=9000.0, annualized_return=annualized,
        delta=-0.2, snapshot_id="s1", spot=100.0,
    )


def _pick_stub(index, rationale="defined risk first"):
    def stub(url, headers, payload, timeout):
        assert headers["Authorization"] == "Bearer key-1"
        assert payload["model"] == "meta-models/muse-spark-open-1026"
        return {"choices": [{"message": {"content": json.dumps({"index": index, "rationale": rationale})}}]}

    return stub


def test_parse_proposal_open_skip_and_rejects():
    assert parse_proposal({"index": 1, "rationale": "x"}, 3, "baseten", "m").candidate_index == 1
    assert parse_proposal('{"index": -1, "rationale": "skip"}', 3, "baseten", "m").action == "skip"
    with pytest.raises(ValueError):
        parse_proposal({"index": 5, "rationale": "x"}, 3, "baseten", "m")
    with pytest.raises(ValueError):
        parse_proposal({"index": 0, "rationale": " "}, 3, "baseten", "m")


def test_propose_order_without_baseten_keeps_deterministic_order():
    journal = Journal()
    ordered, proposal = propose_order([_candidate(90.0, 0.5), _candidate(95.0, 0.9)], DEPLOYMENT, journal, NOW, {})
    assert [item.legs[0].strike for item in ordered] == [95.0, 90.0]
    assert proposal is None
    assert any(entry.kind == "agent_fallback" for entry in journal.entries())


def test_propose_order_baseten_pick_reorders_and_journals():
    journal = Journal()
    ordered, proposal = propose_order(
        [_candidate(90.0, 0.5), _candidate(95.0, 0.9)], DEPLOYMENT, journal, NOW, SLUG_ENV, _pick_stub(1)
    )
    assert proposal is not None and proposal.candidate_index == 1
    assert ordered[0].legs[0].strike == 90.0
    entry = [item for item in journal.entries() if item.kind == "agent_proposal"][0]
    assert entry.payload["model_id"] == "meta-models/muse-spark-open-1026"
    assert entry.payload["backend"] == "baseten"


def test_propose_order_skip_returns_no_candidates():
    journal = Journal()
    ordered, proposal = propose_order(
        [_candidate(90.0, 0.5), _candidate(95.0, 0.9)], DEPLOYMENT, journal, NOW, SLUG_ENV, _pick_stub(-1, "event risk")
    )
    assert ordered == []
    assert proposal is not None and proposal.action == "skip"


def test_propose_order_invalid_pick_falls_back_to_deterministic():
    journal = Journal()
    ordered, proposal = propose_order(
        [_candidate(90.0, 0.5), _candidate(95.0, 0.9)], DEPLOYMENT, journal, NOW, SLUG_ENV, _pick_stub(9)
    )
    assert proposal is None
    assert ordered[0].legs[0].strike == 95.0
    assert any(entry.kind == "agent_proposal_rejected" for entry in journal.entries())


def _contract(strike, delta, bid, ask):
    return OptionContract(strike=strike, expiry="2026-11-13", call_or_put="put", open_interest=500.0, delta=delta, bid=bid, ask=ask, iv=0.30)


def _store():
    store = SnapshotStore()
    journal = Journal()
    store.save_profile(OperatorProfile("moderate", 100000.0, "intermediate", True))
    store.save(ChainSnapshot("SPY", NOW, "test", 1.0, (_contract(95.0, -0.20, 1.20, 1.30), _contract(90.0, -0.25, 1.00, 1.10))))
    store.save_bar("SPY", Bar("1d", NOW, 99.0, 101.0, 98.0, 100.0, 1000.0))
    return store, journal


def test_deployment_opens_baseten_pick_through_risk_gate():
    store, journal = _store()
    create_deployment(store, "cash_secured_put", ["SPY"], capital_cap=50000.0)
    text = run_deployments(store, journal, NOW, env=SLUG_ENV, http_post=_pick_stub(1))
    positions = open_option_positions(journal)
    assert len(positions) == 1
    assert "[agent baseten:" in text
    assert any(entry.kind == "agent_proposal" for entry in journal.entries())
    assert any(entry.kind == "fill" for entry in journal.entries())


def test_agent_cycle_journals_steps_and_run():
    store, journal = _store()
    create_deployment(store, "cash_secured_put", ["SPY"], capital_cap=50000.0)
    text = run_agent_cycle(store, journal, NOW, "SPY")
    assert "agent cycle:" in text
    assert "observe:" in text and "verify:" in text
    kinds = [entry.kind for entry in journal.entries()]
    assert kinds.count("agent_step") == 3
    assert "agent_run" in kinds
