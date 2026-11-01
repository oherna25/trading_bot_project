from datetime import datetime, timedelta, timezone

from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OptionContract
from bot.store import SnapshotStore
from bot.strategy import evaluate_strategy, is_strategy_request, parse_strategy

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)


def _contract(**over):
    base = dict(strike=100.0, expiry="2026-11-20", call_or_put="call",
                open_interest=2000.0, gamma=0.05, delta=0.6, bid=2.4, ask=2.6, price=2.5)
    base.update(over)
    return OptionContract(**base)


def _seeded() -> SnapshotStore:
    store = SnapshotStore()
    store.save(ChainSnapshot(underlying="SPY", timestamp_utc=NOW, source_name="tradier_sandbox",
                             latency_ms=5.0, contracts_list=(_contract(),)))
    store.save_bar("SPY", Bar(interval="1d", timestamp_utc=NOW, open=100.0, high=103.0,
                              low=99.0, close=102.0, volume=1200.0))
    later = NOW + timedelta(days=1)
    store.save(ChainSnapshot(underlying="SPY", timestamp_utc=later, source_name="tradier_sandbox",
                             latency_ms=5.0, contracts_list=(_contract(open_interest=2100.0),)))
    store.save_bar("SPY", Bar(interval="1d", timestamp_utc=later, open=102.0, high=105.0,
                              low=101.0, close=104.0, volume=1300.0))
    return store


def test_is_strategy_request():
    assert is_strategy_request("my strategy: long SPY")
    assert not is_strategy_request("trade ideas")


def test_parse_strategy_direction_and_filters():
    spec = parse_strategy("my strategy: long SPY 7-30 DTE flip", "SPY", _seeded())
    assert spec.underlying == "SPY"
    assert spec.direction == "long"
    assert (spec.dte_min, spec.dte_max) == (7, 30)
    assert spec.use_flip is True


def test_evaluate_strategy_reports_pnl_and_benchmark():
    store = _seeded()
    journal = Journal()
    spec = parse_strategy("my strategy: long SPY flip", "SPY", store)
    text = evaluate_strategy(spec, store, journal, NOW + timedelta(days=1))
    assert "strategy feedback SPY" in text
    assert "fills" in text
    assert any(entry.kind == "strategy_evaluated" for entry in journal.entries())
