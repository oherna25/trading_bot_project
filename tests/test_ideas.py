import re
from datetime import date, datetime, timezone

import pytest

from bot.exposure import compute_gex
from bot.ideas import fetch_earnings, format_ideas, parse_timeframe, rank_ideas, spans_earnings, symbols_for
from bot.journal import Journal
from bot.research import route_chat
from bot.shapes import Bar, ChainSnapshot, OperatorConfig, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

CONFIG = OperatorConfig(
    max_day_loss=1000.0,
    day_profit_target=500.0,
    max_trade_size=5000.0,
)


def _contract(strike, expiry, call_or_put, open_interest, gamma):
    return OptionContract(
        strike=strike, expiry=expiry, call_or_put=call_or_put,
        open_interest=open_interest, gamma=gamma,
    )


def _seeded_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                _contract(100.0, "2026-11-20", "call", 1000.0, 0.05),
                _contract(100.0, "2026-11-20", "put", 2000.0, 0.03),
                _contract(105.0, "2026-10-30", "call", 500.0, 0.10),
                _contract(95.0, "2027-06-01", "put", 3000.0, 0.02),
                _contract(110.0, "2026-11-20", "call", 9999.0, None),
                _contract(115.0, "2026-11-20", "call", 0.0, 0.09),
            ),
        )
    )
    store.save(
        ChainSnapshot(
            underlying="DEMO",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(_contract(50.0, "2026-11-13", "call", 400.0, 0.08),),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW, open=100.0,
                               high=103.0, low=99.0, close=102.0, volume=1200.0))
    store.save_bar("DEMO", Bar(interval="1d", timestamp_utc=NOW, open=50.0,
                               high=52.0, low=49.0, close=51.0, volume=800.0))
    return store


def _entry(store: SnapshotStore, symbol: str):
    snapshot_id, snapshot = store.read_latest(symbol)
    return (snapshot_id, snapshot, compute_gex(snapshot, snapshot_id), store.read_latest_bar(symbol))


def test_parse_timeframe_names():
    assert parse_timeframe("weekly ideas") == 7
    assert parse_timeframe("week ahead ideas") == 7
    assert parse_timeframe("monthly ideas") == 30
    assert parse_timeframe("month of ideas") == 30
    assert parse_timeframe("quarter ideas") == 90
    assert parse_timeframe("leaps ideas") == 365
    assert parse_timeframe("year long ideas") == 365


def test_parse_timeframe_days_and_max():
    assert parse_timeframe("ideas for the next 45 days") == 45
    assert parse_timeframe("45-day trade ideas") == 45
    assert parse_timeframe("weekly or monthly ideas") == 30
    assert parse_timeframe("10 days or quarter ideas") == 90


def test_parse_timeframe_none():
    assert parse_timeframe("trade ideas please") is None


def test_symbols_for_mentions_and_default():
    store = _seeded_store()
    assert symbols_for("ideas for DEMO", store, "TEST") == ["DEMO"]
    assert symbols_for("demo ideas", store, "TEST") == ["DEMO"]
    assert symbols_for("trade ideas", store, "TEST") == ["TEST"]
    assert symbols_for("ideas for NOPE", store, "TEST") == ["NOPE"]
    assert symbols_for("TEST and DEMO ideas", store, "TEST") == ["TEST", "DEMO"]


def test_rank_ideas_orders_by_gamma_open_interest():
    store = _seeded_store()
    ideas = rank_ideas([_entry(store, "TEST")])
    assert len(ideas) == 4
    scores = []
    for idea in ideas:
        snapshot = store.read_snapshot(idea.snapshot_id)
        contract = next(
            c for c in snapshot.contracts_list
            if (c.strike, c.expiry, c.call_or_put) == (idea.strike, idea.expiry, idea.call_or_put)
        )
        scores.append(abs(contract.gamma * contract.open_interest))
    assert scores == sorted(scores, reverse=True)
    assert (ideas[0].strike, ideas[0].call_or_put) in {(100.0, "put"), (95.0, "put")}


def test_rank_ideas_caps_total_and_per_direction():
    snapshot = ChainSnapshot(
        underlying="WIDE",
        timestamp_utc=NOW,
        source_name="tradier_sandbox",
        latency_ms=1.0,
        contracts_list=tuple(
            _contract(80.0 + n, "2026-11-20", "call", 100.0 + n * 10.0, 0.01) for n in range(12)
        ),
    )
    snapshot_id = f"WIDE:{NOW.isoformat()}"
    ideas = rank_ideas([(snapshot_id, snapshot, compute_gex(snapshot, snapshot_id), None)])
    assert len(ideas) == 7
    assert {idea.strike for idea in ideas} == {85.0 + n for n in range(7)}


def test_rank_ideas_timeframe_filters_by_expiry():
    store = _seeded_store()
    entry = _entry(store, "TEST")
    weekly = rank_ideas([entry], 7, now=NOW)
    assert [(i.strike, i.expiry) for i in weekly] == [(105.0, "2026-10-30")]
    monthly = rank_ideas([entry], 30, now=NOW)
    assert len(monthly) == 3
    assert rank_ideas([entry], 2, now=NOW) == []
    assert len(rank_ideas([entry])) == 4


def test_rank_ideas_direction_follows_flip():
    snapshot = ChainSnapshot(
        underlying="DIR",
        timestamp_utc=NOW,
        source_name="tradier_sandbox",
        latency_ms=1.0,
        contracts_list=(
            _contract(90.0, "2026-11-20", "call", 1000.0, 0.05),
            _contract(110.0, "2026-11-20", "put", 2000.0, 0.03),
        ),
    )
    snapshot_id = f"DIR:{NOW.isoformat()}"
    profile = compute_gex(snapshot, snapshot_id)
    assert profile.flip_point == pytest.approx(106.6667, abs=0.01)

    def bar_at(close):
        return Bar(interval="1d", timestamp_utc=NOW, open=close, high=close,
                   low=close, close=close, volume=1.0)

    assert {i.direction for i in rank_ideas([(snapshot_id, snapshot, profile, bar_at(115.0))])} == {"long"}
    assert {i.direction for i in rank_ideas([(snapshot_id, snapshot, profile, bar_at(102.0))])} == {"short"}
    assert {i.direction for i in rank_ideas([(snapshot_id, snapshot, profile, None)])} == {"short"}


def test_rank_ideas_direction_falls_back_to_regime_without_flip():
    store = _seeded_store()
    ideas = rank_ideas([_entry(store, "DEMO")])
    assert len(ideas) == 1
    assert ideas[0].direction == "long"


def test_rank_ideas_skips_flat_entries():
    snapshot = ChainSnapshot(
        underlying="FLAT",
        timestamp_utc=NOW,
        source_name="tradier_sandbox",
        latency_ms=1.0,
        contracts_list=(
            _contract(100.0, "2026-11-20", "call", 1000.0, 0.05),
            _contract(100.0, "2026-11-20", "put", 1000.0, 0.05),
        ),
    )
    snapshot_id = f"FLAT:{NOW.isoformat()}"
    profile = compute_gex(snapshot, snapshot_id)
    assert profile.total_exposure == 0.0
    assert rank_ideas([(snapshot_id, snapshot, profile, None)]) == []


def test_rank_ideas_confidence_rewards_top_score_and_tight_spread():
    store = _seeded_store()
    ideas = rank_ideas([_entry(store, "TEST")])
    assert ideas[0].confidence == pytest.approx(0.75)
    assert all(i.confidence <= 0.9 for i in ideas)
    assert [i.confidence for i in ideas] == sorted([i.confidence for i in ideas], reverse=True)
    single = rank_ideas([_entry(store, "DEMO")])
    assert single[0].confidence == pytest.approx(0.75)


def test_rank_ideas_thesis_and_trigger_name_fields():
    store = _seeded_store()
    ideas = rank_ideas([_entry(store, "TEST")])
    assert ideas
    for idea in ideas:
        snapshot = store.read_snapshot(idea.snapshot_id)
        profile = compute_gex(snapshot, idea.snapshot_id)
        wall = profile.per_strike_exposure[idea.strike]
        assert str(idea.strike) in idea.thesis
        assert idea.expiry in idea.thesis
        assert f"{wall:.1f}" in idea.thesis
        assert "flip" in idea.trigger
        assert idea.thesis != idea.trigger


def test_rank_ideas_uneconomic_vertical_falls_back_to_single():
    snapshot = ChainSnapshot(
        underlying="TEST",
        timestamp_utc=NOW,
        source_name="tradier_sandbox",
        latency_ms=1.0,
        contracts_list=(
            OptionContract(strike=775.0, expiry="2026-11-20", call_or_put="put",
                           open_interest=5000.0, gamma=0.05, price=2.50, bid=2.40, ask=2.60),
            OptionContract(strike=774.0, expiry="2026-11-20", call_or_put="put",
                           open_interest=4000.0, gamma=0.04, price=0.15, bid=0.10, ask=0.20),
        ),
    )
    snapshot_id = f"TEST:{NOW.isoformat()}"
    bar = Bar(interval="1d", timestamp_utc=NOW, open=770.0, high=776.0,
              low=769.0, close=774.5, volume=1000.0)
    ideas = rank_ideas([(snapshot_id, snapshot, compute_gex(snapshot, snapshot_id), bar)])
    top = ideas[0]
    assert top.spread == "single put 775.0"
    assert top.hedge_strike is None
    assert top.net_debit == pytest.approx(260.0)
    assert top.max_loss == pytest.approx(260.0)
    assert top.breakeven == pytest.approx(772.4)


def test_rank_ideas_builds_defined_risk_vertical():
    snapshot = ChainSnapshot(
        underlying="TEST",
        timestamp_utc=NOW,
        source_name="tradier_sandbox",
        latency_ms=1.0,
        contracts_list=(
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                           open_interest=2000.0, gamma=0.09, price=2.50, bid=2.40, ask=2.60),
            OptionContract(strike=105.0, expiry="2026-11-20", call_or_put="call",
                           open_interest=1000.0, gamma=0.04, price=1.50, bid=1.40, ask=1.50),
        ),
    )
    snapshot_id = f"TEST:{NOW.isoformat()}"
    bar = Bar(interval="1d", timestamp_utc=NOW, open=100.0, high=103.0,
              low=99.0, close=102.0, volume=1200.0)
    ideas = rank_ideas([(snapshot_id, snapshot, compute_gex(snapshot, snapshot_id), bar)])
    assert len(ideas) == 2
    top = ideas[0]
    assert top.spread == "100/105 call vertical"
    assert top.hedge_strike == 105.0
    assert top.max_loss == pytest.approx(120.0)
    assert top.breakeven == pytest.approx(101.2)
    assert "max loss $120" in top.thesis
    assert top.direction in ("long", "short")


def test_rank_ideas_filters_wide_spread_and_thin_oi():
    snapshot = ChainSnapshot(
        underlying="TEST",
        timestamp_utc=NOW,
        source_name="tradier_sandbox",
        latency_ms=1.0,
        contracts_list=(
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                           open_interest=1000.0, gamma=0.05, price=2.50, bid=2.40, ask=2.60),
            OptionContract(strike=105.0, expiry="2026-11-20", call_or_put="call",
                           open_interest=1000.0, gamma=0.09, price=2.50, bid=1.00, ask=4.00),
            OptionContract(strike=110.0, expiry="2026-11-20", call_or_put="call",
                           open_interest=10.0, gamma=0.09, price=2.50, bid=2.40, ask=2.60),
        ),
    )
    snapshot_id = f"TEST:{NOW.isoformat()}"
    ideas = rank_ideas([(snapshot_id, snapshot, compute_gex(snapshot, snapshot_id), None)])
    assert [(i.strike, i.call_or_put) for i in ideas] == [(100.0, "call")]


def test_rank_ideas_empty_without_qualifying_contracts():
    snapshot = ChainSnapshot(
        underlying="EMPTY",
        timestamp_utc=NOW,
        source_name="tradier_sandbox",
        latency_ms=1.0,
        contracts_list=(_contract(100.0, "2026-11-20", "call", 500.0, None),),
    )
    snapshot_id = f"EMPTY:{NOW.isoformat()}"
    assert rank_ideas([(snapshot_id, snapshot, compute_gex(snapshot, snapshot_id), None)]) == []


def test_rank_ideas_single_contract_snapshot():
    snapshot = ChainSnapshot(
        underlying="TEST",
        timestamp_utc=NOW,
        source_name="tradier_sandbox",
        latency_ms=1.0,
        contracts_list=(_contract(100.0, "2026-11-20", "call", 1000.0, 0.05),),
    )
    snapshot_id = "TEST:x"
    ideas = rank_ideas([(snapshot_id, snapshot, compute_gex(snapshot, snapshot_id), None)])
    assert len(ideas) == 1
    assert ideas[0].symbol == "TEST"
    assert ideas[0].snapshot_id == "TEST:x"


def test_format_ideas_numbered_lines():
    store = _seeded_store()
    ideas = rank_ideas([_entry(store, "TEST")])
    lines = format_ideas(ideas).splitlines()
    assert lines[0].startswith("trade ideas")
    assert len(lines) == 1 + len(ideas)
    for number, line in enumerate(lines[1:], 1):
        assert re.match(rf"{number}\. ", line)


def test_format_ideas_empty_states_none_found():
    assert "none found" in format_ideas([])


def test_route_chat_ideas_path_journals_one_event():
    store = _seeded_store()
    journal = Journal()
    reply = route_chat("show me trade ideas", "TEST", store, journal, CONFIG, NOW)
    assert "trade ideas" in reply.lower()
    assert "1." in reply
    assert [e.kind for e in journal.entries()] == ["ideas"]
    payload = journal.entries()[0].payload
    assert payload["query"] == "show me trade ideas"
    assert payload["symbols"] == ["TEST"]
    assert payload["timeframe"] is None
    snapshot_id, _ = store.read_latest("TEST")
    assert payload["snapshot_ids"] == [snapshot_id]
    numbered = [line for line in reply.splitlines()[1:] if re.match(r"\d+\. ", line)]
    assert payload["count"] == len(numbered) == 4


def test_route_chat_ideas_timeframe_and_none_found():
    store = _seeded_store()
    journal = Journal()
    reply = route_chat("2 day trade ideas", "TEST", store, journal, CONFIG, NOW)
    assert "none found" in reply
    payload = journal.entries()[0].payload
    assert payload["timeframe"] == 2
    assert payload["count"] == 0


def test_route_chat_ideas_multiple_symbols():
    store = _seeded_store()
    journal = Journal()
    reply = route_chat("ideas for TEST and DEMO", "TEST", store, journal, CONFIG, NOW)
    payload = journal.entries()[0].payload
    assert payload["symbols"] == ["TEST", "DEMO"]
    assert len(payload["snapshot_ids"]) == 2
    assert payload["count"] == 5
    assert "DEMO" in reply


def test_route_chat_ideas_unknown_symbol_errors_loudly():
    store = _seeded_store()
    with pytest.raises(ValueError, match="NOPE"):
        route_chat("ideas for NOPE", "TEST", store, Journal(), CONFIG, NOW)


def test_rank_ideas_flags_earnings_and_iv_rank():
    store = _seeded_store()
    history = [0.0] * 12 + [100.0] * 12 + [50.0]
    ideas = rank_ideas(
        [_entry(store, "TEST")],
        now=NOW,
        iv_history={"TEST": history},
        earnings={"TEST": date(2026, 11, 5)},
    )
    assert ideas != []
    assert {idea.iv_rank for idea in ideas} == {50.0}
    flagged = [idea for idea in ideas if idea.expiry == "2026-11-20"]
    assert flagged != []
    assert {idea.earnings_date for idea in flagged} == {"2026-11-05"}
    unflagged = [idea for idea in ideas if idea.expiry == "2026-10-30"]
    assert {idea.earnings_date for idea in unflagged} == {None}
    text = format_ideas(ideas)
    assert "| earnings 2026-11-05" in text
    assert "| iv rank 50.0" in text


def test_spans_earnings_bounds_and_bad_expiry():
    assert spans_earnings(NOW, "2026-11-20", date(2026, 11, 5)) is True
    assert spans_earnings(NOW, "2026-10-30", date(2026, 11, 5)) is False
    assert spans_earnings(NOW, "2026-11-20", None) is False
    assert spans_earnings(NOW, "not-a-date", date(2026, 11, 5)) is False


def test_fetch_earnings_parses_earliest_per_symbol():
    seen = []

    def stub(url, params, timeout):
        seen.append(params["symbol"])
        assert params["from"] == "2026-10-27"
        assert params["to"] == "2026-12-26"
        assert params["token"] == "tok"
        if params["symbol"] == "SPY":
            return {
                "earningsCalendar": [
                    {"symbol": "SPY", "date": "2026-11-05T21:00:00.000Z"},
                    {"symbol": "SPY", "date": "2026-10-20"},
                    {"symbol": "OTHER", "date": "2026-11-01"},
                ]
            }
        return {"earningsCalendar": [{"symbol": "QQQ", "date": "bogus"}]}

    assert fetch_earnings(
        ["spy", "QQQ"], "tok", date(2026, 10, 27), date(2026, 12, 26), stub
    ) == {"SPY": date(2026, 10, 20)}
    assert seen == ["QQQ", "SPY"]


def test_route_chat_cards_stay_before_ideas(monkeypatch):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.setattr("bot.metareason._read_dotenv", lambda *args, **kwargs: {})
    store = _seeded_store()
    reply = route_chat("card idea", "TEST", store, Journal(), CONFIG, NOW)
    assert reply.startswith("trade cards")


def test_rank_ideas_excludes_same_day_and_expired_contracts():
    snapshot = ChainSnapshot(
        underlying="TEST",
        timestamp_utc=NOW,
        source_name="tradier_sandbox",
        latency_ms=1.0,
        contracts_list=(
            _contract(100.0, "2026-10-27", "call", 5000.0, 0.20),
            _contract(101.0, "2026-10-26", "call", 5000.0, 0.20),
            _contract(102.0, "2026-10-28", "call", 5000.0, 0.20),
            _contract(103.0, "2026-11-20", "call", 5000.0, 0.20),
        ),
    )
    snapshot_id = f"TEST:{NOW.isoformat()}"
    ideas = rank_ideas([(snapshot_id, snapshot, compute_gex(snapshot, snapshot_id), None)], now=NOW)
    assert {i.strike for i in ideas} == {102.0, 103.0}


def test_rank_ideas_caps_each_direction_in_mixed_universe():
    def _snapshot(symbol, side, sign):
        contracts = tuple(
            OptionContract(
                strike=100.0 + n, expiry="2026-11-20", call_or_put=side,
                open_interest=1000.0, gamma=0.05,
            )
            for n in range(8)
        )
        snapshot = ChainSnapshot(
            underlying=symbol, timestamp_utc=NOW, source_name="tradier_sandbox",
            latency_ms=1.0, contracts_list=contracts,
        )
        snapshot_id = f"{symbol}:{NOW.isoformat()}"
        assert (compute_gex(snapshot, snapshot_id).total_exposure > 0) == (sign > 0)
        return (snapshot_id, snapshot, compute_gex(snapshot, snapshot_id), None)

    ideas = rank_ideas([_snapshot("UP", "call", 1), _snapshot("DOWN", "put", -1)])
    assert len(ideas) == 10
    longs = [i for i in ideas if i.direction == "long"]
    shorts = [i for i in ideas if i.direction == "short"]
    assert len(longs) == 3
    assert len(shorts) == 7


def _spy_only_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="SPY",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                _contract(100.0, "2026-11-20", "call", 1000.0, 0.05),
                _contract(100.0, "2026-11-20", "put", 2000.0, 0.03),
            ),
        )
    )
    store.save_bar("SPY", Bar(interval="1d", timestamp_utc=NOW, open=100.0,
                              high=103.0, low=99.0, close=102.0, volume=1200.0))
    return store


def test_symbols_for_names_requested_ticker_missing_from_store():
    store = _spy_only_store()
    assert symbols_for("trade ideas for QQQ", store, "SPY") == ["QQQ"]


def test_route_chat_ideas_for_missing_ticker_errors_naming_it():
    store = _spy_only_store()
    with pytest.raises(ValueError, match="QQQ"):
        route_chat("trade ideas for QQQ", "SPY", store, Journal(), CONFIG, NOW)


def test_route_chat_ideas_mixed_known_and_missing_errors_naming_missing():
    store = _seeded_store()
    with pytest.raises(ValueError, match="NOPE"):
        route_chat("ideas for TEST and NOPE", "TEST", store, Journal(), CONFIG, NOW)


def test_route_chat_ideas_lowercase_name_after_for_errors():
    store = _seeded_store()
    with pytest.raises(ValueError, match="NOPE"):
        route_chat("ideas for nope", "TEST", store, Journal(), CONFIG, NOW)


def test_route_chat_ideas_timeframe_words_after_for_still_fall_back():
    store = _seeded_store()
    journal = Journal()
    reply = route_chat("ideas for the week ahead", "TEST", store, journal, CONFIG, NOW)
    assert "trade ideas" in reply.lower()
    assert journal.entries()[0].payload["symbols"] == ["TEST"]


def test_rank_ideas_orders_by_printed_confidence_not_raw_score():
    snapshot = ChainSnapshot(
        underlying="TEST",
        timestamp_utc=NOW,
        source_name="tradier_sandbox",
        latency_ms=1.0,
        contracts_list=(
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                           open_interest=1000.0, gamma=0.10,
                           price=2.50, bid=2.40, ask=2.60),
            OptionContract(strike=100.0, expiry="2026-12-18", call_or_put="call",
                           open_interest=900.0, gamma=0.10,
                           price=5.00, bid=4.95, ask=5.05),
        ),
    )
    snapshot_id = f"TEST:{NOW.isoformat()}"
    ideas = rank_ideas([(snapshot_id, snapshot, compute_gex(snapshot, snapshot_id), None)])
    assert [i.expiry for i in ideas] == ["2026-12-18", "2026-11-20"]
    assert [i.confidence for i in ideas] == sorted([i.confidence for i in ideas], reverse=True)


def test_rank_ideas_single_leg_carries_premium_risk_numbers():
    snapshot = ChainSnapshot(
        underlying="TEST",
        timestamp_utc=NOW,
        source_name="tradier_sandbox",
        latency_ms=1.0,
        contracts_list=(
            OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                           open_interest=1000.0, gamma=0.05,
                           price=2.50, bid=2.40, ask=2.60),
        ),
    )
    snapshot_id = f"TEST:{NOW.isoformat()}"
    (idea,) = rank_ideas([(snapshot_id, snapshot, compute_gex(snapshot, snapshot_id), None)])
    assert idea.spread == "single call 100.0"
    assert idea.net_debit == pytest.approx(260.0)
    assert idea.max_loss == pytest.approx(260.0)
    assert idea.breakeven == pytest.approx(102.6)
    assert "max loss $260" in idea.thesis
