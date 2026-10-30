from datetime import datetime, timedelta, timezone

from bot.journal import Journal
from bot.research import open_trade_size, paper_pnl_text, paper_trade_step
from bot.shapes import Bar, ChainSnapshot, OptionContract, Signal
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)


def _seeded_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                OptionContract(
                    strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=1000.0,
                    gamma=0.05, price=2.50, bid=2.40, ask=2.60, iv=0.30,
                ),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=99.0, high=101.0, low=98.0, close=100.0, volume=1000.0))
    return store


def _signal(direction="long"):
    return Signal(direction=direction, thesis="thesis", confidence=0.9, snapshot_id="TEST:snap")


def test_paper_trade_fills_once_and_pnl_marks_it():
    store = _seeded_store()
    journal = Journal()
    bar = store.read_latest_bar("TEST")
    note = paper_trade_step("TEST", _signal("long"), bar, journal, NOW)
    assert note == "paper trade: buy 1000 TEST @ 100.00"
    kinds = [entry.kind for entry in journal.entries()]
    assert kinds == ["intent_proposed", "intent_approved", "fill"]
    fill = journal.entries()[-1].payload
    assert fill["symbol"] == "TEST"
    assert fill["side"] == "buy"
    assert fill["price"] == 100.0
    assert fill["size"] == 1000.0
    assert open_trade_size(journal, "TEST") == 1000.0
    assert open_trade_size(journal, "OTHER") == 0.0
    pnl = paper_pnl_text(journal, store)
    assert "TEST: size 1000 avg 100.00 mark 100.00 pnl +0.00" in pnl
    assert "total paper pnl: +0.00" in pnl


def test_paper_trade_marks_profit_when_bar_moves():
    store = _seeded_store()
    journal = Journal()
    paper_trade_step("TEST", _signal("short"), store.read_latest_bar("TEST"), journal, NOW)
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW + timedelta(minutes=15),
                               open=100.0, high=100.0, low=99.0, close=99.0, volume=500.0))
    pnl = paper_pnl_text(journal, store)
    assert "TEST: size -1000 avg 100.00 mark 99.00 pnl +1000.00" in pnl


def test_paper_trade_short_at_same_mark_prints_positive_zero():
    store = _seeded_store()
    journal = Journal()
    paper_trade_step("TEST", _signal("short"), store.read_latest_bar("TEST"), journal, NOW)
    assert "pnl +0.00" in paper_pnl_text(journal, store)


def test_paper_trade_skips_when_position_open():
    store = _seeded_store()
    journal = Journal()
    bar = store.read_latest_bar("TEST")
    assert paper_trade_step("TEST", _signal("long"), bar, journal, NOW) != ""
    assert paper_trade_step("TEST", _signal("long"), bar, journal, NOW + timedelta(minutes=15)) == ""
    kinds = [entry.kind for entry in journal.entries()]
    assert kinds.count("fill") == 1
    assert journal.entries()[-1].kind == "trade_skipped"
    assert journal.entries()[-1].payload["reason"] == "position open"


def test_paper_trade_skips_flat_direction():
    store = _seeded_store()
    journal = Journal()
    note = paper_trade_step("TEST", _signal("flat"), store.read_latest_bar("TEST"), journal, NOW)
    assert note == ""
    assert [entry.kind for entry in journal.entries()] == ["intent_skipped"]


def test_paper_trade_skips_without_bar():
    journal = Journal()
    note = paper_trade_step("TEST", _signal("long"), None, journal, NOW)
    assert note == ""
    assert journal.entries()[-1].kind == "trade_skipped"
    assert journal.entries()[-1].payload["reason"] == "no bar"
