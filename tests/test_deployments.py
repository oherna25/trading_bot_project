from datetime import datetime, timedelta, timezone

import pytest

from bot.deployments import (
    STRATEGY_REGISTRY,
    create_deployment,
    deployments_response,
    deployments_text,
    run_deployments,
    strategy_trackrecord_text,
)
from bot.journal import Journal
from bot.optionbook import closed_option_trades, open_option_positions
from bot.shapes import Bar, ChainSnapshot, Holding, OperatorProfile, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
EXPIRY = "2026-11-13"


def _contract(strike, side, delta, bid, ask, expiry=EXPIRY):
    return OptionContract(
        strike=strike, expiry=expiry, call_or_put=side,
        open_interest=500.0, delta=delta, bid=bid, ask=ask, iv=0.30,
    )


def _store_with(contracts, close=100.0):
    store = SnapshotStore()
    journal = Journal()
    store.save_profile(OperatorProfile("moderate", 100000.0, "intermediate", True))
    store.save(ChainSnapshot("SPY", NOW, "test", 1.0, tuple(contracts)))
    store.save_bar("SPY", Bar("1d", NOW, 99.0, 101.0, 98.0, close, 1000.0))
    return store, journal


def _put_store(bid=1.20, ask=1.30):
    return _store_with([_contract(95.0, "put", -0.20, bid, ask)])


def test_registry_has_premium_selling_strategies():
    assert set(STRATEGY_REGISTRY) == {
        "cash_secured_put", "covered_call", "put_credit_spread", "call_credit_spread",
    }


def test_deploy_chat_creates_persisted_deployment():
    store, journal = _put_store()
    reply = deployments_response("deploy cash_secured_put on SPY paper capital 50000", store, journal, NOW)
    assert "deployment d1 created" in reply
    assert store.list_deployments()[0].strategy_id == "cash_secured_put"
    assert any(entry.kind == "deployment_created" for entry in journal.entries())


def test_create_deployment_rejects_unknown_strategy():
    store, journal = _put_store()
    with pytest.raises(ValueError, match="unknown strategy"):
        create_deployment(store, "momentum_magic", ["SPY"])


def test_run_opens_cash_secured_put_with_collateral():
    store, journal = _put_store()
    create_deployment(store, "cash_secured_put", ["SPY"], capital_cap=50000.0)
    text = run_deployments(store, journal, NOW)
    assert "sold 1 SPY SELL_PUT" in text
    positions = open_option_positions(journal)
    assert len(positions) == 1
    assert positions[0].collateral_per_contract == 9500.0
    assert positions[0].entry_credit == 1.20
    fills = [entry for entry in journal.entries() if entry.kind == "fill"]
    assert fills[-1].payload["deployment_id"] == "d1"
    assert fills[-1].payload["strategy_id"] == "cash_secured_put"


def test_run_is_idempotent_same_day_and_while_open():
    store, journal = _put_store()
    create_deployment(store, "cash_secured_put", ["SPY"], capital_cap=50000.0)
    run_deployments(store, journal, NOW)
    run_deployments(store, journal, NOW + timedelta(days=1))
    assert len(open_option_positions(journal)) == 1


def test_capital_target_blocks_second_large_position():
    store, journal = _put_store()
    create_deployment(store, "cash_secured_put", ["SPY"], capital_cap=10000.0)
    run_deployments(store, journal, NOW)
    assert open_option_positions(journal) == []


def test_profit_target_closes_short_put():
    store, journal = _put_store()
    create_deployment(store, "cash_secured_put", ["SPY"], capital_cap=50000.0)
    run_deployments(store, journal, NOW)
    day2 = NOW + timedelta(days=1)
    store.save(ChainSnapshot("SPY", day2, "test", 1.0, (_contract(95.0, "put", -0.15, 0.40, 0.50),)))
    text = run_deployments(store, journal, day2)
    assert "profit_target" in text
    assert open_option_positions(journal) == []
    closed = closed_option_trades(journal)
    assert closed[0]["pnl"] == pytest.approx(70.0)
    assert "win rate 100%" in strategy_trackrecord_text(journal)


def test_close_dte_closes_position():
    store, journal = _put_store()
    deployment = create_deployment(store, "cash_secured_put", ["SPY"], capital_cap=50000.0)
    run_deployments(store, journal, NOW)
    near = datetime(2026, 11, 10, 14, 30, tzinfo=timezone.utc)
    store.save(ChainSnapshot("SPY", near, "test", 1.0, (_contract(95.0, "put", -0.20, 1.10, 1.20),)))
    store.save_bar("SPY", Bar("1d", near, 99.0, 101.0, 98.0, 100.0, 1000.0))
    text = run_deployments(store, journal, near)
    assert "close_dte" in text
    assert deployment.close_dte == 5


def test_expiry_settlement_and_assignment_journal():
    store, journal = _put_store()
    create_deployment(store, "cash_secured_put", ["SPY"], capital_cap=50000.0)
    run_deployments(store, journal, NOW)
    after = datetime(2026, 11, 14, 14, 30, tzinfo=timezone.utc)
    store.save_bar("SPY", Bar("1d", after, 89.0, 91.0, 88.0, 90.0, 1000.0))
    text = run_deployments(store, journal, after)
    assert "settled SPY" in text
    assert open_option_positions(journal) == []
    assert closed_option_trades(journal)[0]["pnl"] == pytest.approx(-380.0)
    assert any(entry.kind == "assignment" for entry in journal.entries())


def test_put_credit_spread_uses_defined_risk_collateral():
    store, journal = _store_with(
        [
            _contract(95.0, "put", -0.20, 1.20, 1.30),
            _contract(90.0, "put", -0.10, 0.50, 0.60),
        ]
    )
    create_deployment(store, "put_credit_spread", ["SPY"], capital_cap=50000.0)
    run_deployments(store, journal, NOW)
    positions = open_option_positions(journal)
    assert len(positions) == 1
    assert positions[0].collateral_per_contract == pytest.approx(440.0)
    assert len(positions[0].legs) == 2


def test_covered_call_requires_shares():
    contracts = [_contract(105.0, "call", 0.20, 1.20, 1.30)]
    store, journal = _store_with(contracts)
    create_deployment(store, "covered_call", ["SPY"], capital_cap=50000.0)
    run_deployments(store, journal, NOW)
    assert open_option_positions(journal) == []
    store.save_holdings([Holding("SPY", "stock", "long", qty=100.0)])
    text = run_deployments(store, journal, NOW)
    assert "SELL_CALL" in text
    assert len(open_option_positions(journal)) == 1


def test_no_profile_no_trade():
    store = SnapshotStore()
    journal = Journal()
    store.save(ChainSnapshot("SPY", NOW, "test", 1.0, (_contract(95.0, "put", -0.20, 1.20, 1.30),)))
    store.save_bar("SPY", Bar("1d", NOW, 99.0, 101.0, 98.0, 100.0, 1000.0))
    create_deployment(store, "cash_secured_put", ["SPY"], capital_cap=50000.0)
    run_deployments(store, journal, NOW)
    assert open_option_positions(journal) == []
    assert any(entry.payload.get("reason") == "profile with options_ok required" for entry in journal.entries())


def test_feedback_pauses_after_three_losing_closes():
    store, journal = _put_store()
    deployment = create_deployment(store, "cash_secured_put", ["SPY"], capital_cap=10000.0)
    for index in range(3):
        key = f"{deployment.id}|SPY|2026-12-18|sell:put:95:{index}"
        journal.append(
            NOW, "fill",
            {
                "option_position": True, "trade_action": "open", "position_action": "SELL_PUT",
                "deployment_id": deployment.id, "strategy_id": "cash_secured_put", "symbol": "SPY",
                "contracts": 1.0, "entry_credit": 1.0, "collateral_per_contract": 9500.0,
                "position_key": key, "expiry": "2026-12-18",
                "legs": [{"side": "sell", "call_or_put": "put", "strike": 95.0}],
                "opened_date": "2026-10-27", "venue": "paper",
            },
        )
        journal.append(
            NOW, "fill",
            {
                "option_position": True, "trade_action": "close", "deployment_id": deployment.id,
                "strategy_id": "cash_secured_put", "symbol": "SPY", "contracts": 1.0,
                "exit_debit": 3.0, "position_key": key, "expiry": "2026-12-18",
                "legs": [{"side": "sell", "call_or_put": "put", "strike": 95.0}],
                "venue": "paper",
            },
        )
    run_deployments(store, journal, NOW)
    updated = store.read_deployment(deployment.id)
    assert updated is not None and updated.status == "paused"
    assert any(entry.kind == "strategy_feedback" for entry in journal.entries())


def test_deployments_html_shows_utilization_and_positions():
    from bot.deployments import deployments_html

    store, journal = _put_store()
    create_deployment(store, "cash_secured_put", ["SPY"], capital_cap=50000.0)
    run_deployments(store, journal, NOW)
    body = deployments_html(store, journal, NOW)
    assert "strategy deployments" in body
    assert "collateral deployed" in body
    assert "sell put 95" in body
    assert 'href="/deployments/page"' in body
    assert 'class="table-scroll"' in body
    assert "var(--good)" in body


def test_deployments_page_route_and_empty_state_link_to_demo():
    from bot.muse import route_http
    from bot.shapes import default_operator_config

    store, journal = _put_store()
    response = route_http("GET", "/deployments/page", {}, store, journal, default_operator_config(), NOW)
    assert response.status == 200
    body = response.body.decode("utf-8")
    assert "no deployments yet" in body
    assert "Open the demo chat" in body


def test_plain_english_deploy_maps_to_put_selling():
    store, journal = _put_store()
    reply = deployments_response("deploy sell puts on SPY paper capital 40000", store, journal, NOW)
    assert "cash_secured_put" in reply
    assert store.list_deployments()[0].capital_cap == 40000.0


def test_live_deployment_uses_armed_venue(monkeypatch):
    import bot.autonomy as autonomy
    from bot.deployments import run_deployments
    from bot.venue import LiveAck

    store, journal = _put_store()
    deployment = create_deployment(store, "cash_secured_put", ["SPY"], mode="live", capital_cap=50000.0)

    class FakeVenue:
        def place_order(self, intent, reference_price):
            return LiveAck(order_id="ord-1", status="filled", fill_price=reference_price, quantity=1)

    monkeypatch.setattr(autonomy, "live_enabled", True)
    text = run_deployments(store, journal, NOW, venue=FakeVenue())
    assert "sold 1 SPY SELL_PUT" in text
    assert len(open_option_positions(journal)) == 1
    assert open_option_positions(journal)[0].venue == "live"
    assert deployment.mode == "live"


def test_scheduled_session_runs_deployments(tmp_path):
    from bot.feeds import default_context
    from bot.schedule import run_session_core

    store, journal = _put_store()
    create_deployment(store, "cash_secured_put", ["SPY"], capital_cap=50000.0)
    run_session_core("SPY", store, journal, str(tmp_path), NOW, context_factory=default_context)
    assert any(entry.kind == "deployment_run" for entry in journal.entries())


def test_deployments_text_reports_utilization():
    store, journal = _put_store()
    create_deployment(store, "cash_secured_put", ["SPY"], capital_cap=50000.0)
    run_deployments(store, journal, NOW)
    text = deployments_text(store, journal)
    assert "utilization 19%" in text
    assert "deployed 9500" in text


def test_deployment_fill_stays_out_of_stock_books():
    from bot.research_paper import open_trade_size, paper_pnl_text
    from bot.trackrecord import closed_trades, trackrecord_text

    store, journal = _put_store()
    create_deployment(store, "cash_secured_put", ["SPY"], capital_cap=50000.0)
    run_deployments(store, journal, NOW)
    assert open_option_positions(journal)
    assert open_trade_size(journal, "SPY") == 0.0
    assert closed_trades(journal) == []
    assert "no paper fills" in trackrecord_text(journal, store)
    assert "size 0" not in paper_pnl_text(journal, store)
