from datetime import datetime, timedelta, timezone

import pytest

from bot.feeds import FEED_REGISTRY, ingest_chain
from bot.journal import Journal
from bot.replay import paper_evidence_from_journal, replay_export, replay_report, replay_response, replay_week
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
    assert evidence.clean_days == 2
    assert evidence.max_drawdown == 1000.0
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
    assert sorted(FEED_REGISTRY) == ["alpaca", "alpha_vantage", "finnhub", "marketdata_eod", "tradier_sandbox", "yfinance"]


def _stored_week() -> SnapshotStore:
    store = SnapshotStore()
    for offset in range(3):
        store.save(_flippable_snapshot(offset))
        store.save_bar("TEST", _bar(offset, 100.0 + offset * 4.0))
    return store


def test_replay_response_feeds_pnl_without_touching_live_positions():
    from bot.research import open_trade_size, paper_pnl_text

    store = _stored_week()
    journal = Journal()
    reply = replay_response("TEST", store, journal, DAY + timedelta(days=2))
    assert reply.startswith("replay TEST: 3 snapshots, ")
    assert "fills" in reply
    assert "clean days" in reply
    kinds = [entry.kind for entry in journal.entries()]
    assert set(kinds) == {"fill", "replay_summary"}
    assert all(e.payload["venue"] == "replay" for e in journal.entries() if e.kind == "fill")
    assert "signal" not in kinds and "intent_proposed" not in kinds
    assert open_trade_size(journal, "TEST") == 0.0
    assert "total replay pnl:" in paper_pnl_text(journal, store)


def test_replay_report_lists_trade_log_and_day_span():
    text, fills, _ = replay_report("TEST", _stored_week())
    assert fills
    for stamp, fill in fills:
        assert f"{fill['side']} {fill['size']:g} TEST @ {fill['price']:.2f}" in text
    assert "distinct days" in text
    assert "only direction flips trade" in text


def test_replay_holds_same_side_then_halts_before_force_close():
    journal = replay_week(
        [_flippable_snapshot(0), _flippable_snapshot(1), _flippable_snapshot(2)],
        Journal(),
        bars=[_bar(0, 100.0), _bar(1, 104.0), _bar(2, 108.0)],
    )
    actions = [e.payload["action"] for e in journal.entries() if e.kind == "backtest_step"]
    assert actions == ["pending", "open", "pending"]
    assert sum(1 for e in journal.entries() if e.kind == "fill") == 1
    assert sum(1 for e in journal.entries() if e.kind == "replay_hold") == 1
    kinds = [e.kind for e in journal.entries()]
    assert "halt" in kinds
    assert "replay_unclosed" in kinds


def test_replay_force_closes_open_position_at_last_close():
    from bot.shapes import RiskLimits

    relaxed = RiskLimits(max_trade_size=5000.0, day_profit_target=500000.0, max_day_loss=1000000.0)
    journal = replay_week(
        [_flippable_snapshot(0), _flippable_snapshot(1), _flippable_snapshot(2)],
        Journal(),
        bars=[_bar(0, 100.0), _bar(1, 104.0), _bar(2, 108.0)],
        limits=relaxed,
    )
    actions = [e.payload["action"] for e in journal.entries() if e.kind == "backtest_step"]
    assert actions == ["pending", "open", "pending", "force-close"]
    fills = [(e.payload["side"], e.payload["size"], e.payload["price"])
             for e in journal.entries() if e.kind == "fill"]
    assert fills == [("sell", 500.0, 104.0), ("buy", 500.0, 108.0)]


def test_replay_fills_pending_at_next_bar_open():
    journal = replay_week(
        [_flippable_snapshot(0), _flippable_snapshot(1), _flippable_snapshot(2)],
        Journal(),
        bars=[
            Bar(interval="1d", timestamp_utc=DAY, open=100.0,
                high=100.0, low=100.0, close=100.0, volume=1000.0),
            Bar(interval="1d", timestamp_utc=DAY + timedelta(days=1), open=105.0,
                high=108.0, low=105.0, close=108.0, volume=1000.0),
            Bar(interval="1d", timestamp_utc=DAY + timedelta(days=2), open=108.0,
                high=110.0, low=108.0, close=110.0, volume=1000.0),
        ],
    )
    fills = [e.payload for e in journal.entries() if e.kind == "fill"]
    assert fills
    assert fills[0]["price"] == 105.0


def test_replay_response_rerun_does_not_double_journal_fills():
    store = _stored_week()
    journal = Journal()
    replay_response("TEST", store, journal, DAY + timedelta(days=2))
    fills_first = sum(1 for e in journal.entries() if e.kind == "fill")
    assert fills_first > 0
    rerun = replay_response("TEST", store, journal, DAY + timedelta(days=2))
    assert "already journaled" in rerun
    assert sum(1 for e in journal.entries() if e.kind == "fill") == fills_first
    assert sum(1 for e in journal.entries() if e.kind == "replay_summary") == 1


def test_replay_export_steps_csv():
    import csv
    import io

    rows = list(csv.DictReader(io.StringIO(replay_export("TEST", _stored_week()))))
    assert [row["action"] for row in rows] == ["pending", "open", "pending"]
    assert rows[0]["run_id"].startswith("replay-TEST-")
    assert float(rows[-1]["day_pnl"]) == -2000.0
    assert float(rows[-1]["equity"]) == 98000.0


def test_replay_export_fills_csv():
    import csv
    import io

    rows = list(csv.DictReader(io.StringIO(replay_export("TEST", _stored_week(), "fills"))))
    assert len(rows) == 1
    assert rows[0]["side"] == "sell"
    assert float(rows[0]["size"]) == 500.0
    assert float(rows[0]["price"]) == 104.0
    assert rows[0]["venue"] == "replay"
    assert rows[0]["run_id"].startswith("replay-TEST-")


def test_replay_export_benchmark_csv():
    import csv
    import io

    store = _stored_week()
    for day, close in ((0, 500.0), (1, 510.0), (2, 520.0)):
        store.save_bar(
            "SPY",
            Bar(interval="1d", timestamp_utc=DAY + timedelta(days=day), open=close,
                high=close, low=close, close=close, volume=1000.0),
        )
    rows = list(csv.DictReader(io.StringIO(replay_export("TEST", store, "benchmark"))))
    assert [row["date"] for row in rows] == ["2026-10-19", "2026-10-20", "2026-10-21"]
    assert float(rows[0]["spy_close"]) == 500.0
    assert float(rows[-1]["spy_equity"]) == pytest.approx(104.0 * 520.0)
    assert float(rows[-1]["strategy_equity"]) == 98000.0
    bare = list(csv.DictReader(io.StringIO(replay_export("TEST", _stored_week(), "benchmark"))))
    assert bare[0]["spy_close"] == ""
    assert float(bare[-1]["strategy_equity"]) == 98000.0


def test_replay_export_rejects_unknown_section_and_missing_data():
    with pytest.raises(ValueError, match="unknown replay section"):
        replay_export("TEST", _stored_week(), "nope")
    with pytest.raises(ValueError, match="needs at least 2"):
        replay_export("TEST", SnapshotStore(), "steps")


def test_replay_report_reports_daily_deltas_and_run_id():
    text, fills, summary = replay_report("TEST", _stored_week())
    assert fills
    assert summary["run_id"].startswith("replay-TEST-")
    assert all(fill["run_id"] == summary["run_id"] for _, fill in fills)
    assert "equity by day:" in text
    assert set(summary["daily_deltas"]) == {"2026-10-19", "2026-10-20", "2026-10-21"}
    assert summary["day_pnl"] == sum(summary["daily_deltas"].values())


def test_replay_response_needs_two_snapshots_and_bars():
    assert replay_response("NOPE", SnapshotStore(), Journal(), DAY).startswith(
        "replay NOPE: needs at least 2 stored snapshots"
    )
    store = SnapshotStore()
    store.save(_flippable_snapshot(0))
    store.save(_flippable_snapshot(1))
    assert replay_response("TEST", store, Journal(), DAY) == (
        "replay TEST: needs stored bars -- say load TEST to fetch bars"
    )


def test_replay_tool_and_keyword_reach_replay():
    from bot.muse import dispatch_tool
    from bot.research import route_chat
    from bot.shapes import OperatorConfig

    config = OperatorConfig(max_day_loss=1000.0, day_profit_target=500.0, max_trade_size=5000.0)
    tool_reply = dispatch_tool(
        "run_replay", {}, _stored_week(), Journal(), config, DAY, underlying="TEST"
    )
    assert tool_reply.ok is True
    assert tool_reply.reply.startswith("replay TEST: 3 snapshots, ")
    chat = route_chat("replay the last squeeze day", "TEST", _stored_week(), Journal(), config, DAY)
    assert chat.startswith("replay TEST: 3 snapshots, ")


def test_spy_benchmark_ignores_bars_outside_replay_window():
    from bot.replay import _spy_benchmark
    store = SnapshotStore()
    store.save_bar("SPY", Bar(interval="1d", timestamp_utc=DAY - timedelta(days=30),
                              open=100.0, high=101.0, low=99.0, close=100.0, volume=100.0))
    store.save_bar("SPY", Bar(interval="1d", timestamp_utc=DAY + timedelta(days=30),
                              open=200.0, high=201.0, low=199.0, close=200.0, volume=100.0))
    line, payload = _spy_benchmark(store, DAY - timedelta(days=1), DAY + timedelta(days=1), [])
    assert "n/a" in line
    assert payload["spy_benchmark_pct"] is None
