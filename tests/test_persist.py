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
    assert kinds[-1] == "session_saved"


def test_scheduled_session_accumulates_across_runs(tmp_path):
    run_scheduled_session("TEST", str(tmp_path), NOW, context_factory=default_context)
    _, journal = load_bundle(str(tmp_path))
    first_count = len(journal.entries())
    run_scheduled_session("TEST", str(tmp_path), NOW, context_factory=default_context)
    _, journal = load_bundle(str(tmp_path))
    assert len(journal.entries()) > first_count


def test_scheduled_session_reports_failed_chains_and_still_persists(tmp_path):
    def exploding_factory(underlying):
        ctx = default_context(underlying)
        raise RuntimeError("feed down")

    reply = run_scheduled_session("TEST", str(tmp_path), NOW, context_factory=exploding_factory)
    assert reply.startswith("scheduled session failed")
    _, journal = load_bundle(str(tmp_path))
    assert journal.entries()[-1].kind == "session_failed"


def test_modal_app_imports_without_modal_package():
    import modal_app

    assert modal_app.STORE_DIR == "/data/trading-bot"
    assert modal_app.SCHEDULE == "30 13 * * 1-5"
