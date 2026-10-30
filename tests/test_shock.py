import json
from datetime import datetime, timedelta, timezone

from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OperatorProfile, OptionContract, default_operator_config
from bot.shock import SHOCK_USAGE, parse_shock_pct, shock_text
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = default_operator_config()


def _contract(strike, side, oi, gamma, price=2.50, bid=2.40, ask=2.60):
    return OptionContract(
        strike=strike, expiry="2026-11-20", call_or_put=side, open_interest=oi,
        gamma=gamma, price=price, bid=bid, ask=ask, iv=0.30,
    )


def _single_wall_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST", timestamp_utc=NOW,
            source_name="tradier_sandbox", latency_ms=9.0,
            contracts_list=(
                _contract(100.0, "call", 1000.0, 0.05, price=2.50, bid=2.40, ask=2.50),
                _contract(100.0, "put", 2000.0, 0.03, price=3.00, bid=2.90, ask=3.00),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=100.0, high=103.0, low=99.0, close=102.0, volume=1200.0))
    store.save_profile(OperatorProfile(
        risk_tolerance="moderate", portfolio_size=50000.0, knowledge="intermediate", options_ok=True
    ))
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
    store.save_profile(OperatorProfile(
        risk_tolerance="moderate", portfolio_size=50000.0, knowledge="intermediate", options_ok=True
    ))
    return store


def test_parse_shock_pct():
    assert parse_shock_pct("shock SPY -2%") == -2.0
    assert parse_shock_pct("shock +1.5% tomorrow") == 1.5
    assert parse_shock_pct("shock it") is None
    assert parse_shock_pct("-2") == -2.0
    assert parse_shock_pct("shock SPY -2 percent") == -2.0
    assert parse_shock_pct("shock SPY \u22122%") == -2.0


def test_shock_reports_broken_wall_and_intrinsic_pnl():
    text = shock_text("TEST", _single_wall_store(), Journal(), NOW, -2.0)
    assert "TEST shock -2.0%: spot 102 -> 99.96" in text
    assert "walls broken: 100 " in text
    assert "flip: none in chain" in text
    assert "regime short-gamma (positions unchanged)" in text
    assert "card P&L at 99.96 (intrinsic, vs debit paid):" in text
    assert "single call 100.0 2026-11-20: -$250" in text
    assert "single put 100.0 2026-11-20: -$296" in text


def _vertical_card(**overrides):
    from bot.tradecards import TradeCard

    base = dict(
        symbol="TEST", direction="long", spread="100/110 call vertical", strike=100.0,
        expiry="2026-11-20", call_or_put="call", hedge_strike=110.0, confidence=0.5,
        snapshot_id="TEST:x", max_loss=300.0, breakeven=103.0, net_debit=300.0,
        spread_pct=4.0, wall_exposure=1000.0, regime="long-gamma", spot=102.0,
        flip_point=None, reason="r", reason_source="fallback",
    )
    base.update(overrides)
    return TradeCard(**base)


def test_shock_card_pnl_is_intrinsic_value_less_debit():
    import pytest

    from bot.shock import shock_card_pnl

    assert shock_card_pnl(_vertical_card(), 130.0) == pytest.approx(700.0)
    assert shock_card_pnl(_vertical_card(net_debit=-150.0), 130.0) == pytest.approx(1150.0)
    assert shock_card_pnl(_vertical_card(net_debit=None), 130.0) is None
    assert shock_card_pnl(_vertical_card(hedge_strike=None, net_debit=250.0), 99.96) == pytest.approx(-250.0)


def test_shock_text_marks_cards_without_debit():
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST", timestamp_utc=NOW,
            source_name="tradier_sandbox", latency_ms=9.0,
            contracts_list=(
                OptionContract(
                    strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=1000.0,
                    gamma=0.05, price=None, bid=None, ask=None, iv=None,
                ),
                OptionContract(
                    strike=100.0, expiry="2026-11-20", call_or_put="put", open_interest=2000.0,
                    gamma=0.03, price=None, bid=None, ask=None, iv=None,
                ),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=100.0, high=103.0, low=99.0, close=102.0, volume=1200.0))
    store.save_profile(OperatorProfile(
        risk_tolerance="moderate", portfolio_size=50000.0, knowledge="intermediate", options_ok=True
    ))
    text = shock_text("TEST", store, Journal(), NOW, -2.0)
    assert "n/a (no debit)" in text


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


def test_shock_tool_accepts_numbers_strings_and_percent_alias():
    from bot.muse import dispatch_tool, route_http

    cases = [
        ({"shock_pct": -2.0}, "TEST shock -2.0%"),
        ({"shock_pct": "-2"}, "TEST shock -2.0%"),
        ({"shock_pct": "-2%"}, "TEST shock -2.0%"),
        ({"shock_pct": "-2 percent"}, "TEST shock -2.0%"),
        ({"percent": -2}, "TEST shock -2.0%"),
        ({}, SHOCK_USAGE),
    ]
    for args, expected in cases:
        reply = dispatch_tool("shock_test", args, _single_wall_store(), Journal(), CONFIG, NOW, underlying="TEST")
        assert reply.ok
        assert expected in reply.reply
        if args != {}:
            assert "walls broken" in reply.reply
    for args, expected in cases:
        payload = {"method": "tools/call", "params": {"name": "shock_test", "arguments": args, "underlying": "TEST"}}
        response = route_http("POST", "/mcp", payload, _single_wall_store(), Journal(), CONFIG, NOW)
        assert response.status == 200
        body = json.loads(response.body.decode("utf-8"))
        assert body["ok"] is True
        assert expected in body["reply"]
