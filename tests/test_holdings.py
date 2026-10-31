from datetime import datetime, timezone

import pytest

from bot.holdings import holdings_text, parse_holdings, save_holdings_shapes, save_holdings_text
from bot.journal import Journal
from bot.research import route_chat
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


BOOK = [
    {"symbol": "test", "kind": "put", "side": "short", "strike": 105, "expiry": "2026-11-20", "qty": 1},
    {"symbol": "TEST", "kind": "stock", "side": "long", "qty": 50},
]


def test_parse_holdings_validates_book():
    holdings = parse_holdings(BOOK)
    assert [h.symbol for h in holdings] == ["TEST", "TEST"]
    assert holdings[0].strike == 105.0
    with pytest.raises(ValueError, match="kind must be"):
        parse_holdings([{"symbol": "TEST", "kind": "future", "side": "long"}])
    with pytest.raises(ValueError, match="needs a strike"):
        parse_holdings([{"symbol": "TEST", "kind": "call", "side": "long"}])
    with pytest.raises(ValueError, match="qty must be positive"):
        parse_holdings([{"symbol": "TEST", "kind": "stock", "side": "long", "qty": 0}])
    with pytest.raises(ValueError, match="must be a JSON list"):
        parse_holdings({"symbol": "TEST"})


def test_holdings_report_reads_positional_risk():
    store = _seeded_store()
    save_holdings_text(BOOK, store, Journal(), NOW)
    text = holdings_text(store)
    assert text.startswith("your book vs current positioning:")
    assert "SHORT 1 TEST put 105 2026-11-20" in text
    assert "strike $10.0 below the 115 flip" in text
    assert "$5 ITM vs spot 100" in text
    assert "wall at 105: $2M" in text
    assert "LONG 50 TEST stock @ spot 100" in text
    assert "spot $15.0 below the 115 flip" in text
    assert "top wall 95 at -$3M" in text


def test_holdings_empty_and_missing_snapshot():
    assert "none saved" in holdings_text(SnapshotStore())
    store = _seeded_store()
    save_holdings_text(
        [{"symbol": "NOPE", "kind": "call", "side": "long", "strike": 10}], store, Journal(), NOW
    )
    assert "LONG 1 NOPE call 10: no snapshot stored" in holdings_text(store)


def test_holdings_tool_and_chat_wiring():
    from bot.muse import TOOL_REGISTRY, dispatch_tool
    from bot.research import route_chat

    assert "set_holdings" in TOOL_REGISTRY
    store = _seeded_store()
    saved = dispatch_tool("set_holdings", {"holdings": BOOK}, store, Journal(), CONFIG, NOW)
    assert saved.ok and "saved 2 holdings" in saved.reply
    bad = dispatch_tool("set_holdings", {"holdings": [{"symbol": "x"}]}, store, Journal(), CONFIG, NOW)
    assert not bad.ok and "kind must be" in bad.error
    assert "strike $10.0 below" in route_chat("show my holdings", "TEST", store, Journal(), CONFIG, NOW)
    chat_saved = route_chat(
        'holdings [{"symbol": "TEST", "kind": "stock", "side": "long", "qty": 10}]',
        "TEST", SnapshotStore(), Journal(), CONFIG, NOW,
    )
    assert "saved 1 holding" in chat_saved
    assert "could not parse holdings JSON" in route_chat(
        "holdings [oops", "TEST", SnapshotStore(), Journal(), CONFIG, NOW
    )
    cleared = dispatch_tool("set_holdings", {"holdings": []}, store, Journal(), CONFIG, NOW)
    assert cleared.ok and cleared.reply == "holdings cleared"


def test_holdings_persist_round_trip(tmp_path):
    from bot.persist import HOLDINGS_FILE, load_bundle, save_bundle

    store, journal = _seeded_store(), Journal()
    save_holdings_text(BOOK, store, journal, NOW)
    save_bundle(str(tmp_path), store, journal)
    loaded, _ = load_bundle(str(tmp_path))
    assert loaded.read_holdings() == store.read_holdings()
    assert (tmp_path / HOLDINGS_FILE).exists()
    assert "SHORT 1 TEST put 105" in holdings_text(loaded)


def test_save_holdings_shapes_accept_string_dict_list_positions():
    cases = [
        '[{"symbol": "SPY", "kind": "stock", "side": "long", "qty": 100}]',
        {"symbol": "SPY", "kind": "stock", "side": "long", "qty": 100},
        [{"symbol": "SPY", "kind": "stock", "side": "long", "qty": 100}],
        {"positions": [{"symbol": "SPY", "kind": "stock", "side": "long", "qty": 100}]},
        {"holdings": [{"symbol": "SPY", "kind": "stock", "side": "long", "qty": 100}]},
        {"SPY": {"kind": "stock", "side": "long", "qty": 100}},
    ]
    for raw in cases:
        store, journal = SnapshotStore(), Journal()
        reply = save_holdings_shapes(raw, store, journal, NOW)
        assert "saved 1 holding" in reply
        assert store.read_holdings()[0].symbol == "SPY"


def test_save_holdings_shapes_rejects_loudly_with_format():
    for raw in (42, {"foo": 1}, "not json", [{"kind": "stock"}], None):
        with pytest.raises(ValueError, match="holdings format"):
            save_holdings_shapes(raw, SnapshotStore(), Journal(), NOW)


def test_chat_save_holding_plain_english():
    store, journal = SnapshotStore(), Journal()
    reply = route_chat("save holding long 100 SPY stock", "TEST", store, journal, CONFIG, NOW)
    assert "saved 1 holding" in reply
    assert store.read_holdings()[0].qty == 100.0


def test_chat_save_holding_garbage_shows_format():
    reply = route_chat("save holding xyz", "TEST", SnapshotStore(), Journal(), CONFIG, NOW)
    assert "holdings format" in reply


def test_chat_read_intent_still_reads():
    for text in ("show my holdings", "holdings", "my book"):
        reply = route_chat(text, "TEST", SnapshotStore(), Journal(), CONFIG, NOW)
        assert reply == "holdings: none saved -- chat 'save holding long 100 SPY stock' or call set_holdings"
