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


def test_refresh_core_stores_quote_bar_alongside_daily():
    store, journal = SnapshotStore(), Journal()
    refresh_core("SPY", store, journal, context_factory=_offline_factory)
    stamps = [bar.timestamp_utc for bar in store.read_bars("SPY")]
    quote_stamp = datetime.fromtimestamp(1761609600, tz=timezone.utc)
    assert quote_stamp in stamps
    quote = next(bar for bar in store.read_bars("SPY") if bar.timestamp_utc == quote_stamp)
    assert quote.close == 102.0
    assert len(stamps) >= 3


def test_refresh_core_skips_quote_topup_when_finnhub_won():
    from bot.feeds import FeedContext, FeedUnavailableError

    quote = {"c": 102.0, "h": 103.0, "l": 100.0, "o": 100.5, "t": 1761609600}

    def factory(underlying: str) -> FeedContext:
        def fake_get(url, headers=None, params=None, timeout=None):
            if "/bars" in url or "chart" in url or "alphavantage" in url:
                raise FeedUnavailableError("down")
            if "quote" in url:
                return quote
            if "expirations" in url:
                return {"expirations": {"date": ["2026-11-20"]}}
            if "options/chains" in url:
                return {"options": {"option": []}}
            raise AssertionError(f"unexpected url {url}")

        return FeedContext(
            underlying=underlying, credentials={"FINNHUB_API_KEY": "test"}, http_get=fake_get
        )

    store, journal = SnapshotStore(), Journal()
    refresh_core("SPY", store, journal, context_factory=factory)
    finnhub_bars = [
        e for e in journal.entries()
        if e.kind == "bars_ingested" and e.payload.get("source_name") == "finnhub"
    ]
    assert len(finnhub_bars) == 1


def test_refresh_core_backfills_iv_from_priced_snapshots():
    from bot.shapes import Bar, ChainSnapshot, OptionContract

    store, journal = SnapshotStore(), Journal()
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc),
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                OptionContract(
                    strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=1000.0,
                    gamma=0.05, price=2.50, bid=2.40, ask=2.60, iv=None,
                ),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=datetime(2026, 10, 27, tzinfo=timezone.utc),
                               open=100.0, high=103.0, low=99.0, close=102.0, volume=1200.0))
    refresh_core("SPY", store, journal, context_factory=_offline_factory)
    assert "TEST" in store.list_underlyings()
    assert len(store.read_iv_history("TEST")) == 1
    assert any(e.kind == "iv_backfilled" for e in journal.entries())


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
        time.sleep(0.3)
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
