from datetime import date, datetime, timezone

import pytest

from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OptionContract
from bot.store import SnapshotStore
from bot.wheel import (
    format_wheel,
    pick_expiries,
    premium_return,
    screen_premium,
    spans_earnings,
    wheel_response,
)

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
TODAY = date(2026, 10, 27)
FRIDAY = "2026-11-13"
OTHER_FRIDAY = "2026-11-06"


def _contract(strike, expiry, side, oi=500.0, delta=-0.20, bid=1.20, ask=1.30, iv=0.30):
    return OptionContract(
        strike=strike, expiry=expiry, call_or_put=side,
        open_interest=oi, delta=delta, bid=bid, ask=ask, iv=iv,
    )


def _snapshot(symbol, *contracts):
    return ChainSnapshot(
        underlying=symbol, timestamp_utc=NOW, source_name="tradier_sandbox",
        latency_ms=9.0, contracts_list=contracts,
    )


def _bar(close=100.0):
    return Bar(interval="1d", timestamp_utc=NOW, open=99.0, high=101.0,
               low=98.0, close=close, volume=1000.0)


def test_premium_return_put_uses_strike_as_capital():
    assert premium_return(1.20, 95.0, None, "put") == pytest.approx(120.0 / 9500.0)


def test_premium_return_call_uses_spot_as_capital():
    assert premium_return(0.80, 105.0, 100.0, "call") == pytest.approx(80.0 / 10000.0)


def test_premium_return_call_without_spot_is_none():
    assert premium_return(0.80, 105.0, None, "call") is None


def test_spans_earnings_boundaries():
    expiry = date(2026, 11, 13)
    assert spans_earnings(TODAY, expiry, None) is False
    assert spans_earnings(TODAY, expiry, date(2026, 10, 20)) is False
    assert spans_earnings(TODAY, expiry, TODAY) is True
    assert spans_earnings(TODAY, expiry, expiry) is True
    assert spans_earnings(TODAY, expiry, date(2026, 11, 15)) is True
    assert spans_earnings(TODAY, expiry, date(2026, 11, 16)) is False


def test_pick_expiries_prefers_fridays_then_date():
    picked = pick_expiries(
        ["2026-11-10", "2026-11-06", "2026-11-03", "2026-11-13", "2026-11-20"], TODAY
    )
    assert picked == ["2026-11-06", "2026-11-13", "2026-11-20"]


def test_pick_expiries_falls_back_to_non_fridays_and_caps_count():
    picked = pick_expiries(["2026-11-10", "2026-11-03", "2026-11-04", "2026-11-05"], TODAY, count=2)
    assert picked == ["2026-11-03", "2026-11-04"]


def test_pick_expiries_skips_bad_strings_and_out_of_window():
    picked = pick_expiries(["nope", "2026-10-28", "2026-11-06", "2027-06-01", ""], TODAY)
    assert picked == ["2026-11-06"]


def test_screen_drops_one_contract_per_stage():
    snapshot = _snapshot(
        "TEST",
        _contract(95.0, FRIDAY, "put"),
        _contract(95.0, FRIDAY, "stock"),
        _contract(95.0, "2027-06-01", "put"),
        _contract(95.0, FRIDAY, "put", oi=50.0),
        _contract(95.0, FRIDAY, "put", bid=2.00, ask=3.00),
        _contract(95.0, FRIDAY, "put", delta=0.50),
        _contract(95.0, FRIDAY, "put", bid=0.50, ask=0.55),
    )
    candidates, dropped = screen_premium([("TEST:snap", snapshot, _bar())], NOW)
    assert [c.strike for c in candidates] == [95.0]
    assert candidates[0].action == "SELL_PUT"
    assert candidates[0].dte == 17
    assert candidates[0].capital_at_risk == pytest.approx(9500.0)
    assert candidates[0].needs_shares is False
    assert dropped == {
        "side": 1, "expiry": 1, "earnings": 0, "open_interest": 1, "spread": 1,
        "delta": 1, "no_bar": 0, "premium_return": 1, "iv_rank": 0,
    }


def test_screen_drops_delta_sign_mismatch():
    snapshot = _snapshot(
        "TEST",
        _contract(95.0, FRIDAY, "put", delta=0.20),
        _contract(105.0, FRIDAY, "call", delta=-0.20, bid=1.10, ask=1.20),
    )
    candidates, dropped = screen_premium([("TEST:snap", snapshot, _bar())], NOW)
    assert candidates == []
    assert dropped["delta"] == 2


def test_screen_applies_absolute_spread_cap_to_cheap_contracts():
    snapshot = _snapshot(
        "TEST",
        _contract(95.0, FRIDAY, "put", bid=0.50, ask=0.70),
        _contract(40.0, FRIDAY, "put", bid=0.50, ask=0.60),
    )
    candidates, dropped = screen_premium([("TEST:snap", snapshot, _bar())], NOW)
    assert [c.strike for c in candidates] == [40.0]
    assert dropped["spread"] == 1


def test_screen_skips_expiry_spanning_earnings():
    snapshot = _snapshot(
        "TEST",
        _contract(95.0, FRIDAY, "put"),
        _contract(96.0, OTHER_FRIDAY, "put"),
    )
    entries = [("TEST:snap", snapshot, _bar())]
    candidates, dropped = screen_premium(entries, NOW, earnings={"TEST": date(2026, 11, 10)})
    assert [c.expiry for c in candidates] == [OTHER_FRIDAY]
    assert dropped["earnings"] == 1
    candidates, dropped = screen_premium(entries, NOW, earnings={"TEST": date(2026, 11, 20)})
    assert len(candidates) == 2
    assert dropped["earnings"] == 0


def test_screen_iv_rank_gate_blocks_low_rank_only():
    snapshot = _snapshot("TEST", _contract(95.0, FRIDAY, "put"))
    entries = [("TEST:snap", snapshot, _bar())]
    low = {"TEST": [0.5] * 19 + [0.1]}
    candidates, dropped = screen_premium(entries, NOW, iv_history=low)
    assert candidates == []
    assert dropped["iv_rank"] == 1
    high = {"TEST": [0.1] * 19 + [0.5]}
    candidates, dropped = screen_premium(entries, NOW, iv_history=high)
    assert len(candidates) == 1
    assert candidates[0].iv_rank == pytest.approx(100.0)
    assert dropped["iv_rank"] == 0
    thin = {"TEST": [0.1, 0.2, 0.3]}
    candidates, _ = screen_premium(entries, NOW, iv_history=thin)
    assert len(candidates) == 1
    assert candidates[0].iv_rank is None


def test_screen_ranks_by_premium_return_desc():
    snapshot = _snapshot(
        "TEST",
        _contract(95.0, FRIDAY, "put", bid=1.00, ask=1.10),
        _contract(90.0, FRIDAY, "put", bid=1.50, ask=1.60),
    )
    candidates, _ = screen_premium([("TEST:snap", snapshot, _bar())], NOW)
    assert [c.strike for c in candidates] == [90.0, 95.0]


def test_screen_skips_calls_without_a_bar():
    snapshot = _snapshot(
        "TEST",
        _contract(105.0, FRIDAY, "call", delta=0.22, bid=1.10, ask=1.20),
        _contract(95.0, FRIDAY, "put"),
    )
    candidates, dropped = screen_premium([("TEST:snap", snapshot, None)], NOW)
    assert [c.action for c in candidates] == ["SELL_PUT"]
    assert dropped["no_bar"] == 1


def _wheel_store():
    store = SnapshotStore()
    store.save(_snapshot(
        "TEST",
        _contract(95.0, FRIDAY, "put"),
        _contract(105.0, FRIDAY, "call", delta=0.22, bid=1.10, ask=1.20, iv=0.28),
    ))
    store.save_bar("TEST", _bar())
    return store


def test_wheel_response_formats_header_and_call_shares_note():
    journal = Journal()
    reply = wheel_response("wheel ideas", "TEST", _wheel_store(), journal, NOW)
    assert reply.startswith("wheel ideas (2):")
    assert "SELL_PUT strike 95" in reply
    assert "SELL_CALL strike 105" in reply
    assert "return 1.26%" in reply
    assert "capital $9,500" in reply
    assert "dte 17" in reply
    assert "delta -0.20" in reply
    assert "needs 100 shares" in reply
    assert "iv rank" not in reply
    kinds = [e.kind for e in journal.entries()]
    assert "wheel_screened" in kinds
    assert "iv_recorded" in kinds
    recorded = [e for e in journal.entries() if e.kind == "iv_recorded"][0]
    assert recorded.payload == {"symbol": "TEST", "date": "2026-10-27", "iv": pytest.approx(0.29), "points": 2}


def test_wheel_response_records_iv_into_store_and_shows_rank_when_known():
    store = _wheel_store()
    for day in range(1, 20):
        store.save_iv("TEST", f"2026-09-{day:02d}", 0.10)
    store.save_iv("TEST", "2026-09-20", 0.30)
    journal = Journal()
    reply = wheel_response("wheel ideas", "TEST", store, journal, NOW)
    assert "iv rank 100.0" in reply
    assert store.read_iv_history("TEST")["2026-10-27"] == pytest.approx(0.29)


def test_wheel_response_empty_reports_none_found():
    store = SnapshotStore()
    store.save(_snapshot("TEST", _contract(95.0, FRIDAY, "put", delta=0.90)))
    store.save_bar("TEST", _bar())
    assert wheel_response("wheel ideas", "TEST", store, Journal(), NOW) == "wheel ideas: none found"


def test_wheel_response_unknown_symbol_matches_ideas_error_shape():
    store = SnapshotStore()
    store.save(_snapshot("TEST", _contract(95.0, FRIDAY, "put")))
    with pytest.raises(ValueError, match=r"no snapshot stored for QQQ \(store has: TEST\); ingest it first"):
        wheel_response("wheel ideas for QQQ", "TEST", store, Journal(), NOW)


def test_format_wheel_empty_reports_none_found():
    assert format_wheel([]) == "wheel ideas: none found"
