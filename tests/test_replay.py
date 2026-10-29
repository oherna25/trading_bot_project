from datetime import datetime, timedelta, timezone

from bot.feeds import FEED_REGISTRY, ingest_chain
from bot.journal import Journal
from bot.replay import paper_evidence_from_journal, replay_week
from bot.shapes import Bar, ChainSnapshot, OptionContract
from bot.store import SnapshotStore

DAY = datetime(2026, 10, 19, 14, 30, tzinfo=timezone.utc)


def _snapshot(day_offset: int) -> ChainSnapshot:
    return ChainSnapshot(
        underlying="TEST",
        timestamp_utc=DAY + timedelta(days=day_offset),
        source_name="marketdata_eod",
        latency_ms=5.0,
        contracts_list=(
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=100.0, gamma=0.05),
        ),
    )


def test_replay_processes_snapshots_in_time_order():
    journal = replay_week([_snapshot(2), _snapshot(0), _snapshot(1)], Journal())
    stamps = [entry.timestamp_utc for entry in journal.entries()]
    assert stamps == [DAY, DAY + timedelta(days=1), DAY + timedelta(days=2)]
    assert [entry.kind for entry in journal.entries()] == ["replay_step"] * 3


def _flippable_snapshot(day_offset: int) -> ChainSnapshot:
    return ChainSnapshot(
        underlying="TEST",
        timestamp_utc=DAY + timedelta(days=day_offset),
        source_name="marketdata_eod",
        latency_ms=5.0,
        contracts_list=(
            OptionContract(strike=90.0, expiry="2026-11-20", call_or_put="call", open_interest=1000.0, gamma=0.05),
            OptionContract(strike=110.0, expiry="2026-11-20", call_or_put="put", open_interest=2000.0, gamma=0.03),
        ),
    )


def _bar(day_offset: int, close: float) -> Bar:
    return Bar(interval="1d", timestamp_utc=DAY + timedelta(days=day_offset), open=close,
               high=close, low=close, close=close, volume=1000.0)


def test_replay_with_bars_runs_signal_to_fill_backtest():
    journal = replay_week(
        [_snapshot(2), _flippable_snapshot(0), _flippable_snapshot(1), _flippable_snapshot(2)],
        Journal(),
        bars=[_bar(0, 100.0), _bar(1, 108.0), _bar(2, 110.0)],
    )
    kinds = [e.kind for e in journal.entries()]
    assert kinds.count("replay_step") == 4
    assert "signal" in kinds
    assert "intent_proposed" in kinds
    assert "fill" in kinds
    assert "backtest_step" in kinds
    assert "halt" in kinds


def test_paper_evidence_generates_from_backtest_journal():
    journal = replay_week(
        [_flippable_snapshot(0), _flippable_snapshot(1), _flippable_snapshot(2)],
        Journal(),
        bars=[_bar(0, 100.0), _bar(1, 108.0), _bar(2, 110.0)],
    )
    evidence = paper_evidence_from_journal(journal, clean_days_target=2, drawdown_bound=500.0)
    assert evidence.clean_days_target == 2
    assert evidence.clean_days == 1
    assert evidence.max_drawdown == 8000.0
    assert evidence.halt_drills_passed is False


def test_replay_twice_produces_identical_journals():
    week = [_snapshot(offset) for offset in range(5)]
    first = replay_week(list(week), Journal()).entries()
    second = replay_week(list(week), Journal()).entries()
    assert first == second


def test_ingest_stamps_source_and_registers_snapshot():
    store = SnapshotStore()
    journal = Journal()
    snapshot = ingest_chain("tradier_sandbox", store, journal)
    assert snapshot.source_name == "tradier_sandbox"
    assert snapshot.latency_ms >= 0.0
    assert store.read_latest("SPY") is not None
    assert journal.entries()[-1].kind == "snapshot_ingested"


def test_registry_covers_every_planned_feed():
    assert sorted(FEED_REGISTRY) == ["alpaca", "finnhub", "marketdata_eod", "tradier_sandbox", "yfinance"]
