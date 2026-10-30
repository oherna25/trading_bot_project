from datetime import datetime, timedelta, timezone

from bot.diff import compact_dollars, snapshot_changes, snapshot_diff_text
from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OptionContract, default_operator_config
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = default_operator_config()


def _contract(strike, side, oi, gamma):
    return OptionContract(
        strike=strike, expiry="2026-11-20", call_or_put=side, open_interest=oi,
        gamma=gamma, price=2.50, bid=2.40, ask=2.60, iv=0.30,
    )


def _seeded_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST", timestamp_utc=NOW - timedelta(hours=1),
            source_name="tradier_sandbox", latency_ms=9.0,
            contracts_list=(
                _contract(95.0, "put", 3000.0, 0.05),
                _contract(105.0, "call", 3000.0, 0.05),
            ),
        )
    )
    store.save(
        ChainSnapshot(
            underlying="TEST", timestamp_utc=NOW,
            source_name="tradier_sandbox", latency_ms=9.0,
            contracts_list=(
                _contract(95.0, "put", 6000.0, 0.05),
                _contract(105.0, "call", 3000.0, 0.05),
                _contract(115.0, "call", 3000.0, 0.05),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=99.0, high=101.0, low=98.0, close=100.0, volume=1000.0))
    return store


def test_compact_dollars_scales():
    assert compact_dollars(12_400_000_000.0) == "$12.4B"
    assert compact_dollars(400_000_000.0) == "$400M"
    assert compact_dollars(-17_000_000.0) == "-$17M"
    assert compact_dollars(500.0) == "$500"


def test_snapshot_changes_structures_regime_flip_and_wall():
    changes = snapshot_changes("TEST", _seeded_store())
    assert changes is not None
    assert (changes.previous_flip, changes.flip) == (105.0, 115.0)
    assert changes.regime == changes.previous_regime == "long-gamma"
    assert changes.wall_strike == changes.previous_wall_strike == 95.0
    assert changes.wall_value is not None and changes.previous_wall_value is not None
    assert changes.wall_value == 2 * changes.previous_wall_value
    assert snapshot_changes("TEST", SnapshotStore()) is None


def test_snapshot_diff_text_reports_flip_wall_and_movers():
    text = snapshot_diff_text("TEST", _seeded_store())
    assert text.startswith("TEST 13:30 -> 14:30:")
    assert "flip 105.0 -> 115.0" in text
    assert "top wall 95.0" in text and "+100.0%" in text
    assert "movers: " in text and "95.0 " in text and "115.0 " in text
    assert "total " in text
    assert "needs at least 2 stored snapshots" in snapshot_diff_text("TEST", SnapshotStore())


def test_diff_chat_keyword_and_tool():
    from bot.muse import TOOL_REGISTRY, dispatch_tool
    from bot.research import route_chat

    store = _seeded_store()
    reply = route_chat("diff the last snapshots", "TEST", store, Journal(), CONFIG, NOW)
    assert "flip 105.0 -> 115.0" in reply
    assert "snapshot_diff" in TOOL_REGISTRY
    tool_reply = dispatch_tool("snapshot_diff", {}, store, Journal(), CONFIG, NOW, underlying="TEST")
    assert tool_reply.ok
    assert "top wall 95.0" in tool_reply.reply
