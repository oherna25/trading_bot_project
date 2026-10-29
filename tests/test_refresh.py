import threading
import time
from datetime import datetime, timezone

from bot.feeds import FeedContext, default_context
from bot.journal import Journal
from bot.muse import open_server_warning, start_refresh_loop
from bot.schedule import refresh_store
from bot.store import SnapshotStore


def _offline_factory(underlying: str) -> FeedContext:
    return default_context(underlying)


def test_refresh_store_ingests_prunes_and_persists(tmp_path):
    store, journal = SnapshotStore(), Journal()
    used = refresh_store("SPY", store, journal, str(tmp_path), context_factory=_offline_factory)
    assert used == {"SPY": "tradier_sandbox"}
    assert store.read_latest("SPY") is not None
    kinds = [e.kind for e in journal.entries()]
    assert "snapshot_ingested" in kinds
    assert "bars_ingested" in kinds
    assert "refreshed" in kinds
    assert "signal" not in kinds
    assert (tmp_path / "snapshots.json").exists()
    assert (tmp_path / "journal.json").exists()


def test_refresh_store_journals_failure_and_still_saves(tmp_path):
    def _failing(underlying: str) -> FeedContext:
        raise RuntimeError("down")

    store, journal = SnapshotStore(), Journal()
    used = refresh_store("SPY", store, journal, str(tmp_path), context_factory=_failing)
    assert used == {"SPY": None}
    kinds = [e.kind for e in journal.entries()]
    assert "refresh_failed" in kinds
    assert "refreshed" in kinds
    assert (tmp_path / "journal.json").exists()


def test_journal_prune_drops_oldest_first():
    journal = Journal()
    base = datetime(2026, 10, 27, tzinfo=timezone.utc)
    for n in range(5):
        journal.append(base, "note", {"n": n})
    assert journal.prune(keep=2) == 3
    assert [e.payload["n"] for e in journal.entries()] == [3, 4]


def test_refresh_loop_ingests_on_a_timer(tmp_path):
    store, journal = SnapshotStore(), Journal()
    stop = threading.Event()
    try:
        start_refresh_loop(
            store, journal, ["SPY"], str(tmp_path), 0.05, threading.Lock(),
            context_factory=_offline_factory, stop=stop,
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if any(e.kind == "refreshed" for e in journal.entries()):
                break
            time.sleep(0.05)
        assert any(e.kind == "refreshed" for e in journal.entries())
        assert store.read_latest("SPY") is not None
    finally:
        stop.set()


def test_open_server_warning_only_for_remote_unauthenticated():
    assert open_server_warning("0.0.0.0", "") is not None
    assert open_server_warning("0.0.0.0", None) is not None
    assert open_server_warning("0.0.0.0", "secret") is None
    assert open_server_warning("127.0.0.1", "") is None
    assert open_server_warning("localhost", None) is None
