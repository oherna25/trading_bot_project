from datetime import datetime, timedelta, timezone

from bot.feeds import FEED_REGISTRY, ingest_chain
from bot.journal import Journal
from bot.replay import replay_week
from bot.shapes import ChainSnapshot, OptionContract
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
