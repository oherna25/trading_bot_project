from datetime import datetime, timezone

import pytest

from bot.feeds import FeedContext
from bot.journal import Journal
from bot.research import route_chat
from bot.shapes import Bar, ChainSnapshot, OperatorConfig, OptionContract
from bot.store import SnapshotStore, need_snapshot_text

NOW = datetime(2026, 10, 30, 14, 30, tzinfo=timezone.utc)
CONFIG = OperatorConfig(max_day_loss=1000.0, day_profit_target=500.0, max_trade_size=5000.0)


def _seeded_store():
    store = SnapshotStore()
    store.save(ChainSnapshot(
        underlying="TEST", timestamp_utc=NOW, source_name="tradier_sandbox", latency_ms=9.0,
        contracts_list=(OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                                       open_interest=1000.0, gamma=0.05),)))
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW, open=100.0, high=103.0,
                               low=99.0, close=102.0, volume=9.0))
    return store


def test_need_snapshot_text_names_next_command():
    text = need_snapshot_text("TSLA", SnapshotStore())
    assert text.startswith("no snapshot stored for TSLA")
    assert "say load TSLA" in text


def test_brief_unknown_ticker_points_at_load():
    with pytest.raises(ValueError, match="say load TSLA"):
        route_chat("brief", "TSLA", SnapshotStore(), Journal(), CONFIG, NOW)


def test_ideas_unknown_ticker_points_at_load():
    with pytest.raises(ValueError, match=r"say load QQQ"):
        route_chat("trade ideas for QQQ", "TEST", _seeded_store(), Journal(), CONFIG, NOW)


def test_wheel_unknown_ticker_points_at_load():
    with pytest.raises(ValueError, match=r"say load QQQ"):
        route_chat("wheel ideas for QQQ", "TEST", _seeded_store(), Journal(), CONFIG, NOW)


def test_shock_unknown_ticker_points_at_load():
    reply = route_chat("shock TSLA -2%", "TSLA", SnapshotStore(), Journal(), CONFIG, NOW)
    assert "say load TSLA" in reply


def test_replay_short_store_points_at_load():
    reply = route_chat("replay", "TEST", _seeded_store(), Journal(), CONFIG, NOW)
    assert "say load TEST" in reply


def test_load_without_keys_names_the_key(monkeypatch):
    def canned_ctx(underlying):
        def explode(*args, **kwargs):
            raise AssertionError("no network without keys")

        return FeedContext(underlying=underlying, credentials={}, http_get=explode)

    monkeypatch.setattr("bot.research.live_context", canned_ctx)
    reply = route_chat("load TSLA", "TEST", SnapshotStore(), Journal(), CONFIG, NOW)
    assert "TRADIER_ACCESS_TOKEN" in reply
    assert "MARKETDATA_API_KEY" in reply


def _keyed_ctx(underlying):
    def explode(*args, **kwargs):
        raise AssertionError("stubbed ingest must not touch the network")

    return FeedContext(underlying=underlying, credentials={"TRADIER_ACCESS_TOKEN": "x"},
                       http_get=explode)


def test_load_ingests_and_confirms(monkeypatch):
    monkeypatch.setattr("bot.research.live_context", _keyed_ctx)

    def fake_chain(feed_name, store, journal, ctx=None):
        store.save(ChainSnapshot(
            underlying="TSLA", timestamp_utc=NOW, source_name=feed_name, latency_ms=9.0,
            contracts_list=(OptionContract(strike=100.0, expiry="2026-11-20",
                                           call_or_put="call", open_interest=5.0),)))
        return "ok"

    def fake_bars(feed_name, store, journal, ctx=None):
        store.save_bar("TSLA", Bar(interval="1d", timestamp_utc=NOW, open=1.0, high=1.0,
                                   low=1.0, close=1.0, volume=1.0))
        return "ok"

    monkeypatch.setattr("bot.research.ingest_chain", fake_chain)
    monkeypatch.setattr("bot.research.ingest_bars", fake_bars)
    store = SnapshotStore()
    reply = route_chat("load TSLA", "TEST", store, Journal(), CONFIG, NOW)
    assert "loaded TSLA" in reply
    assert store.read_latest("TSLA") is not None


def test_load_defaults_to_requested_underlying(monkeypatch):
    monkeypatch.setattr("bot.research.live_context", _keyed_ctx)
    seen = []

    def fake_chain(feed_name, store, journal, ctx=None):
        seen.append(feed_name)
        return "ok"

    def fake_bars(feed_name, store, journal, ctx=None):
        return "ok"

    monkeypatch.setattr("bot.research.ingest_chain", fake_chain)
    monkeypatch.setattr("bot.research.ingest_bars", fake_bars)
    reply = route_chat("load", "TEST", SnapshotStore(), Journal(), CONFIG, NOW)
    assert "loaded TEST" in reply
