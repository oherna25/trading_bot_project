from datetime import datetime, timedelta, timezone

from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OptionContract, default_operator_config
from bot.shock import SHOCK_USAGE, parse_shock_pct, shock_text
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = default_operator_config()


def _contract(strike, side, oi, gamma):
    return OptionContract(
        strike=strike, expiry="2026-11-20", call_or_put=side, open_interest=oi,
        gamma=gamma, price=2.50, bid=2.40, ask=2.60, iv=0.30,
    )


def _single_wall_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST", timestamp_utc=NOW,
            source_name="tradier_sandbox", latency_ms=9.0,
            contracts_list=(
                _contract(100.0, "call", 1000.0, 0.05),
                _contract(100.0, "put", 2000.0, 0.03),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=100.0, high=103.0, low=99.0, close=102.0, volume=1200.0))
    return store


def _flip_store() -> SnapshotStore:
    store = SnapshotStore()
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


def test_parse_shock_pct():
    assert parse_shock_pct("shock SPY -2%") == -2.0
    assert parse_shock_pct("shock +1.5% tomorrow") == 1.5
    assert parse_shock_pct("shock it") is None


def test_shock_reports_broken_wall_and_intrinsic_pnl():
    text = shock_text("TEST", _single_wall_store(), Journal(), NOW, -2.0)
    assert "TEST shock -2.0%: spot 102 -> 99.96" in text
    assert "walls broken: 100 " in text
    assert "flip: none in chain" in text
    assert "regime short-gamma (positions unchanged)" in text
    assert "card P&L at 99.96 (intrinsic):" in text
    assert "single call 100.0 2026-11-20: -$200" in text
    assert "single put 100.0 2026-11-20: +$4" in text


def test_shock_without_break_reports_nearest_walls():
    text = shock_text("TEST", _single_wall_store(), Journal(), NOW, 2.0)
    assert "walls broken: none" in text
    assert "nearest walls: 100 below / none above" in text


def test_shock_crosses_flip_and_multiple_walls():
    text = shock_text("TEST", _flip_store(), Journal(), NOW, 20.0)
    assert "TEST shock +20.0%: spot 100 -> 120" in text
    assert "spot crosses the flip 115" in text
    broken = next(line for line in text.splitlines() if line.startswith("walls broken:"))
    assert "105 " in broken and "115 " in broken


def test_shock_missing_snapshot_or_bar():
    assert "no snapshot stored" in shock_text("NOPE", SnapshotStore(), Journal(), NOW, -2.0)
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST", timestamp_utc=NOW - timedelta(days=1),
            source_name="tradier_sandbox", latency_ms=9.0,
            contracts_list=(_contract(100.0, "call", 1000.0, 0.05),),
        )
    )
    assert "needs a stored bar" in shock_text("TEST", store, Journal(), NOW, -2.0)


def test_shock_chat_keyword_and_tool():
    from bot.muse import TOOL_REGISTRY, dispatch_tool
    from bot.research import route_chat

    assert route_chat("shock it", "TEST", _single_wall_store(), Journal(), CONFIG, NOW) == SHOCK_USAGE
    reply = route_chat("shock TEST -2%", "TEST", _single_wall_store(), Journal(), CONFIG, NOW)
    assert "TEST shock -2.0%" in reply
    assert "shock_test" in TOOL_REGISTRY
    tool_reply = dispatch_tool(
        "shock_test", {"shock_pct": -2.0}, _single_wall_store(), Journal(), CONFIG, NOW, underlying="TEST"
    )
    assert tool_reply.ok and "walls broken" in tool_reply.reply
    usage = dispatch_tool("shock_test", {}, _single_wall_store(), Journal(), CONFIG, NOW, underlying="TEST")
    assert usage.ok and usage.reply == SHOCK_USAGE
