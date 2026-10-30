import threading
import time
from datetime import datetime, timedelta, timezone

from bot.feeds import FeedContext, default_context, ingest_chain
from bot.journal import Journal
from bot.muse import open_server_warning, start_refresh_loop
from bot.schedule import refresh_core, refresh_store
from bot.shapes import ChainSnapshot
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


def test_refresh_loop_picks_up_tickers_added_after_startup(tmp_path):
    store, journal = SnapshotStore(), Journal()
    ingest_chain("tradier_sandbox", store, journal, default_context("SPY"))
    stop = threading.Event()
    try:
        start_refresh_loop(
            store, journal, ["SPY"], str(tmp_path), 0.05, threading.Lock(),
            context_factory=_offline_factory, stop=stop,
        )
        # Operator adds QQQ via run_session.py; the bundle reload lands it in the store.
        ingest_chain("tradier_sandbox", store, journal, default_context("QQQ"))
        refreshed = []
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            refreshed = [
                e for e in journal.entries()
                if e.kind == "refreshed" and "QQQ" in (e.payload.get("underlyings") or [])
            ]
            if refreshed:
                break
            time.sleep(0.05)
        assert refreshed, "refresh loop never picked up QQQ added after startup"
    finally:
        stop.set()


def _seed_snapshots(store, count, newest):
    for n in range(count):
        store.save(
            ChainSnapshot(
                underlying="SPY",
                timestamp_utc=newest - timedelta(seconds=count - n),
                source_name="seed",
                latency_ms=1.0,
            )
        )


def test_refresh_core_ingests_without_pruning_or_saving(tmp_path):
    store, journal = SnapshotStore(), Journal()
    base = datetime(2026, 10, 27, tzinfo=timezone.utc)
    _seed_snapshots(store, 505, base)
    for n in range(20005):
        journal.append(base, "note", {"n": n})
    used = refresh_core("SPY", store, journal, now=base, context_factory=_offline_factory)
    assert used == {"SPY": "tradier_sandbox"}
    kinds = [entry.kind for entry in journal.entries()]
    assert "snapshot_ingested" in kinds
    assert "bars_ingested" in kinds
    assert "refreshed" in kinds
    assert "signal" not in kinds
    entries = journal.entries()
    assert len(entries) > 20005
    assert entries[0].kind == "note" and entries[0].payload == {"n": 0}
    snapshots = [snap for _, snap in store.snapshots_in_order() if snap.underlying == "SPY"]
    assert len(snapshots) == 506
    assert sum(1 for snap in snapshots if snap.source_name == "seed") == 505
    assert list(tmp_path.iterdir()) == []


def test_refresh_loop_tick_does_not_block_locked_reads(tmp_path):
    store, journal = SnapshotStore(), Journal()
    ingest_chain("tradier_sandbox", store, journal, default_context("SPY"))
    lock = threading.Lock()

    def slow_factory(symbol):
        time.sleep(1.0)
        return default_context(symbol)

    stop = threading.Event()
    try:
        start_refresh_loop(
            store, journal, ["SPY"], str(tmp_path), 0.05, lock,
            context_factory=slow_factory, stop=stop,
        )
        time.sleep(0.2)
        started = time.monotonic()
        with lock:
            latest = store.read_latest("SPY")
        read_elapsed = time.monotonic() - started
        assert latest is not None
        assert read_elapsed < 1.0
        assert not any(entry.kind == "refreshed" for entry in journal.entries())
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if any(entry.kind == "refreshed" for entry in journal.entries()):
                break
            time.sleep(0.05)
        assert any(entry.kind == "refreshed" for entry in journal.entries())
    finally:
        stop.set()


def test_refresh_loop_tick_merges_snapshot_and_prunes_live(tmp_path):
    store, journal = SnapshotStore(), Journal()
    tick_start = datetime.now(timezone.utc)
    _seed_snapshots(store, 505, tick_start)
    journal.append(tick_start, "note", {"n": 0})
    stop = threading.Event()
    try:
        start_refresh_loop(
            store, journal, ["SPY"], str(tmp_path), 0.05, threading.Lock(),
            context_factory=_offline_factory, stop=stop,
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if any(entry.kind == "refreshed" for entry in journal.entries()):
                break
            time.sleep(0.05)
        assert any(entry.kind == "refreshed" for entry in journal.entries())
    finally:
        stop.set()
    time.sleep(0.5)
    snapshots = [snap for _, snap in store.snapshots_in_order() if snap.underlying == "SPY"]
    assert len(snapshots) == 500
    latest = store.read_latest("SPY")
    assert latest is not None and latest[1].source_name == "tradier_sandbox"
    assert any(entry.kind == "note" for entry in journal.entries())
    assert (tmp_path / "snapshots.json").exists()
    assert (tmp_path / "journal.json").exists()


def test_open_server_warning_only_for_remote_unauthenticated():
    assert open_server_warning("0.0.0.0", "") is not None
    assert open_server_warning("0.0.0.0", None) is not None
    assert open_server_warning("0.0.0.0", "secret") is None
    assert open_server_warning("127.0.0.1", "") is None
    assert open_server_warning("localhost", None) is None
