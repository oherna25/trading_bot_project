import os
from datetime import datetime, timezone

from bot.feeds import default_context, ingest_bars, ingest_chain
from bot.journal import Journal
from bot.persist import load_bundle, save_bundle
from bot.schedule import run_scheduled_session
from bot.shapes import OperatorProfile
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)


def _seeded() -> tuple[SnapshotStore, Journal]:
    store, journal = SnapshotStore(), Journal()
    ctx = default_context("TEST")
    ingest_chain("tradier_sandbox", store, journal, ctx)
    ingest_bars("alpaca", store, journal, ctx)
    store.save_profile(
        OperatorProfile(
            risk_tolerance="moderate", portfolio_size=50000.0, knowledge="intermediate", options_ok=True
        )
    )
    return store, journal


def test_bundle_round_trip_preserves_snapshots_bars_journal_profile(tmp_path):
    store, journal = _seeded()
    save_bundle(str(tmp_path), store, journal)
    for name in ("snapshots.json", "bars.json", "journal.json", "profile.json"):
        assert os.path.exists(tmp_path / name)
    loaded_store, loaded_journal = load_bundle(str(tmp_path))
    assert loaded_store.snapshots_in_order() == store.snapshots_in_order()
    assert loaded_store.all_bars() == store.all_bars()
    assert loaded_store.list_underlyings() == ["TEST"]
    assert [e.kind for e in loaded_journal.entries()] == [e.kind for e in journal.entries()]
    assert loaded_journal.entries()[0].payload == journal.entries()[0].payload
    assert loaded_store.read_profile() == store.read_profile()


def test_bundle_round_trip_preserves_delta(tmp_path):
    from bot.shapes import ChainSnapshot, OptionContract

    store, journal = SnapshotStore(), Journal()
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW,
            source_name="marketdata_eod",
            latency_ms=1.0,
            contracts_list=(
                OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                               open_interest=100.0, gamma=0.05, delta=0.55),
            ),
        )
    )
    save_bundle(str(tmp_path), store, journal)
    loaded, _ = load_bundle(str(tmp_path))
    _, snapshot = loaded.read_latest("TEST")
    assert snapshot.contracts_list[0].delta == 0.55


def test_save_bundle_applies_retention(tmp_path):
    from datetime import timedelta

    from bot.shapes import ChainSnapshot, OptionContract

    store, journal = SnapshotStore(), Journal()
    for day in range(5):
        store.save(
            ChainSnapshot(
                underlying="TEST",
                timestamp_utc=NOW + timedelta(days=day),
                source_name="tradier_sandbox",
                latency_ms=1.0,
                contracts_list=(
                    OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                                   open_interest=100.0, gamma=0.05),
                ),
            )
        )
    for n in range(5):
        journal.append(NOW, "note", {"n": n})
    save_bundle(str(tmp_path), store, journal, keep_snapshots=2, keep_journal=3)
    loaded, loaded_journal = load_bundle(str(tmp_path))
    assert len(loaded.snapshots_in_order()) == 2
    assert loaded.read_latest("TEST")[1].timestamp_utc == NOW + timedelta(days=4)
    assert [e.payload["n"] for e in loaded_journal.entries()] == [2, 3, 4]


def test_load_missing_dir_returns_empty_bundle(tmp_path):
    store, journal = load_bundle(str(tmp_path / "fresh"))
    assert store.snapshots_in_order() == []
    assert journal.entries() == []
    assert store.read_profile() is None


def test_save_bundle_overwrites_previous_run(tmp_path):
    store, journal = _seeded()
    save_bundle(str(tmp_path), store, journal)
    journal.append(NOW, "marker", {"n": 1})
    save_bundle(str(tmp_path), store, journal)
    _, loaded_journal = load_bundle(str(tmp_path))
    assert loaded_journal.entries()[-1].kind == "marker"


def test_scheduled_session_ingests_briefs_and_persists(tmp_path):
    reply = run_scheduled_session("TEST", str(tmp_path), NOW, context_factory=default_context)
    assert "total exposure" in reply
    store, journal = load_bundle(str(tmp_path))
    assert store.read_latest("TEST") is not None
    kinds = [e.kind for e in journal.entries()]
    assert "signal" in kinds
    assert "session_saved" in kinds
    assert kinds[-1] == "video_brief"


def test_scheduled_session_accumulates_across_runs(tmp_path):
    run_scheduled_session("TEST", str(tmp_path), NOW, context_factory=default_context)
    _, journal = load_bundle(str(tmp_path))
    first_count = len(journal.entries())
    run_scheduled_session("TEST", str(tmp_path), NOW, context_factory=default_context)
    _, journal = load_bundle(str(tmp_path))
    assert len(journal.entries()) > first_count


def test_scheduled_session_briefs_multiple_underlyings(tmp_path):
    reply = run_scheduled_session(
        "SPY,QQQ", str(tmp_path), NOW, context_factory=lambda symbol: default_context(symbol)
    )
    assert "SPY GEX brief" in reply
    assert "QQQ GEX brief" in reply
    store, _ = load_bundle(str(tmp_path))
    assert store.list_underlyings() == ["QQQ", "SPY"]


def test_scheduled_session_expands_to_stored_underlyings(tmp_path):
    first = run_scheduled_session("SPY", str(tmp_path), NOW, context_factory=default_context)
    assert "SPY GEX brief" in first
    second = run_scheduled_session("QQQ", str(tmp_path), NOW, context_factory=default_context)
    assert "QQQ GEX brief" in second
    assert "SPY GEX brief" in second


def test_scheduled_session_reports_failed_chains_and_still_persists(tmp_path):
    def exploding_factory(underlying):
        ctx = default_context(underlying)
        raise RuntimeError("feed down")

    reply = run_scheduled_session("TEST", str(tmp_path), NOW, context_factory=exploding_factory)
    assert reply.startswith("scheduled session failed")
    _, journal = load_bundle(str(tmp_path))
    kinds = [entry.kind for entry in journal.entries()]
    assert "session_failed" in kinds
    assert kinds[-1] == "video_brief"


def test_save_bundle_returns_current_mtimes_and_leaves_no_tmp(tmp_path):
    from bot.persist import bundle_mtimes
    store, journal = _seeded()
    written = save_bundle(str(tmp_path), store, journal)
    assert written == bundle_mtimes(str(tmp_path))
    assert list(tmp_path.glob("*.tmp")) == []
