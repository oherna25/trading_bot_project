from datetime import datetime, timezone

import pytest

import bot.autonomy as autonomy
from bot.autonomy import LiveDisabledError, enable_live, submit_live
from bot.journal import Journal
from bot.risk import even_legs
from bot.shapes import OrderIntent, OrderLeg, PaperEvidence, RiskLimits, RiskState
from bot.venue import LiveAck, LiveConfigError, LiveOrderError, TradierVenue, occ_symbol

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

LIMITS = RiskLimits(max_trade_size=5000.0, day_profit_target=500.0, max_day_loss=1000.0)


@pytest.fixture(autouse=True)
def _live_off():
    autonomy.live_enabled = False
    yield
    autonomy.live_enabled = False


def _state(**overrides) -> RiskState:
    base = {"account_size": 100000.0, "risk_per_trade": 0.01, "day_pnl": 0.0,
            "halt_active": False, "kill_active": False}
    base.update(overrides)
    return RiskState(**base)


def _intent() -> OrderIntent:
    return OrderIntent(symbol="TEST", side="buy", size=1000.0,
                       risk_numbers={"size": 1000.0}, signal_id="sig-1")


class _StubVenue:
    def __init__(self, ack):
        self.ack = ack
        self.calls = []

    def place_order(self, intent, reference_price):
        self.calls.append((intent, reference_price))
        if isinstance(self.ack, Exception):
            raise self.ack
        return self.ack


def test_occ_symbol_matches_known_contract():
    assert occ_symbol("AAPL", "2026-01-16", "call", 150.0) == "AAPL  260116C00150000"
    assert occ_symbol("SPY", "2026-11-20", "put", 600.5) == "SPY   261120P00600500"


@pytest.mark.parametrize(
    "underlying,expiry,side,strike",
    [("AAPL", "next friday", "call", 150.0), ("AAPL", "2026-01-16", "maybe", 150.0),
     ("AAPL", "2026-01-16", "call", 0.0)],
)
def test_occ_symbol_rejects_bad_inputs(underlying, expiry, side, strike):
    with pytest.raises(LiveOrderError):
        occ_symbol(underlying, expiry, side, strike)


def test_from_env_needs_token_and_account():
    with pytest.raises(LiveConfigError, match="TRADIER_ACCESS_TOKEN"):
        TradierVenue.from_env({})
    with pytest.raises(LiveConfigError, match="TRADIER_ACCOUNT_ID"):
        TradierVenue.from_env({"TRADIER_ACCESS_TOKEN": "tok"})
    venue = TradierVenue.from_env({"TRADIER_ACCESS_TOKEN": "tok", "TRADIER_ACCOUNT_ID": "acc"})
    assert venue.base_url == "https://sandbox.tradier.com/v1"


def _canned_venue(post_body, get_body, log):
    def http_post(url, headers, payload, timeout):
        log.append(("POST", url, payload))
        return post_body

    def http_get(url, headers, timeout):
        log.append(("GET", url, None))
        return get_body

    return TradierVenue(account_id="acc", token="tok", http_post=http_post, http_get=http_get)


def test_place_equity_order_sizes_from_reference_price():
    log = []
    venue = _canned_venue({"order": {"id": 123, "status": "ok"}},
                          {"order": {"id": 123, "status": "filled", "avg_fill_price": 101.5}}, log)
    ack = venue.place_order(_intent(), 100.0)
    assert ack == LiveAck(order_id="123", status="filled", fill_price=101.5, quantity=10)
    method, url, payload = log[0]
    assert method == "POST"
    assert url.endswith("/accounts/acc/orders")
    assert payload["class"] == "equity"
    assert payload["symbol"] == "TEST"
    assert payload["side"] == "buy"
    assert payload["quantity"] == 10
    assert payload["type"] == "market"


def test_place_single_option_leg_uses_occ_symbol():
    log = []
    venue = _canned_venue({"order": {"id": 7, "status": "ok"}},
                          {"order": {"id": 7, "status": "filled", "avg_fill_price": 2.5}}, log)
    leg = OrderLeg(symbol="TEST", side="buy", size=500.0, strike=100.0, expiry="2026-11-20", call_or_put="call")
    intent = OrderIntent(symbol="TEST", side="buy", size=500.0,
                         risk_numbers={"size": 500.0}, signal_id="sig-1", legs=(leg,))
    ack = venue.place_order(intent, 2.5)
    assert ack.fill_price == 2.5
    assert ack.quantity == 2
    _, _, payload = log[0]
    assert payload["class"] == "option"
    assert payload["symbol"] == "TEST  261120C00100000"


def _combo_venue(fills, log):
    state = {"next": 0}

    def http_post(url, headers, payload, timeout):
        log.append(("POST", url, payload))
        order_id = fills[state["next"]][0]
        state["next"] += 1
        return {"order": {"id": order_id, "status": "ok"}}

    def http_get(url, headers, timeout):
        log.append(("GET", url, None))
        wanted = url.rsplit("/", 1)[-1]
        for order_id, price, status in fills:
            if str(order_id) == wanted:
                node = {"id": order_id, "status": status}
                if price is not None:
                    node["avg_fill_price"] = price
                return {"order": node}
        raise AssertionError(f"unexpected url {url}")

    return TradierVenue(account_id="acc", token="tok", http_post=http_post, http_get=http_get)


def _combo_intent():
    legs = even_legs(1000.0, [("TEST", "buy", 100.0, "2026-11-20", "call"),
                              ("TEST", "sell", 105.0, "2026-11-20", "call")])
    return OrderIntent(symbol="TEST", side="buy", size=1000.0,
                       risk_numbers={"size": 1000.0}, signal_id="sig-1", legs=legs)


def test_place_combo_fills_each_leg_and_nets_premium():
    log = []
    venue = _combo_venue([(11, 2.5, "filled"), (12, 1.0, "filled")], log)
    ack = venue.place_order(_combo_intent(), 2.5)
    assert ack == LiveAck(order_id="11+12", status="filled", fill_price=pytest.approx(1.5), quantity=4)
    posts = [entry for entry in log if entry[0] == "POST"]
    assert len(posts) == 2
    assert posts[0][2]["class"] == "option"
    assert posts[0][2]["symbol"] == "TEST  261120C00100000"
    assert posts[0][2]["side"] == "buy"
    assert posts[0][2]["quantity"] == 2
    assert posts[0][2]["duration"] == "day"
    assert posts[1][2]["class"] == "option"
    assert posts[1][2]["symbol"] == "TEST  261120C00105000"
    assert posts[1][2]["side"] == "sell"
    assert posts[1][2]["quantity"] == 2
    assert posts[1][2]["duration"] == "day"


def test_place_combo_partial_fill_names_filled_and_failed_leg():
    log = []
    venue = _combo_venue([(21, 2.5, "filled"), (22, None, "open")], log)
    with pytest.raises(LiveOrderError) as excinfo:
        venue.place_order(_combo_intent(), 2.5)
    message = str(excinfo.value)
    assert "21" in message
    assert "combo leg 2/2" in message
    assert "TEST  261120C00105000" in message
    assert "manual" in message


def test_place_combo_rejects_non_option_leg():
    venue = _canned_venue({}, {}, [])
    legs = (
        OrderLeg(symbol="TEST", side="buy", size=500.0, strike=100.0,
                 expiry="2026-11-20", call_or_put="call"),
        OrderLeg(symbol="TEST", side="sell", size=500.0),
    )
    intent = OrderIntent(symbol="TEST", side="buy", size=1000.0,
                         risk_numbers={"size": 1000.0}, signal_id="sig-1", legs=legs)
    with pytest.raises(LiveOrderError, match="not an option leg"):
        venue.place_order(intent, 2.5)


def test_place_order_rejects_missing_reference_price():
    venue = _canned_venue({}, {}, [])
    with pytest.raises(LiveOrderError, match="reference price"):
        venue.place_order(_intent(), 0.0)


def test_place_order_reports_unfilled_status():
    venue = _canned_venue({"order": {"id": 9, "status": "ok"}},
                          {"order": {"id": 9, "status": "open"}}, [])
    with pytest.raises(LiveOrderError, match="order 9 not filled"):
        venue.place_order(_intent(), 100.0)


def _evidence() -> PaperEvidence:
    return PaperEvidence(clean_days=12, clean_days_target=10, max_drawdown=100.0,
                         drawdown_bound=500.0, halt_drills_passed=True)


def _checklist() -> dict[str, bool]:
    return {"paper_evidence_reviewed": True, "risk_limits_set": True, "kill_switch_tested": True}


def test_submit_live_returns_fill_and_updates_state():
    journal = Journal()
    enable_live("op-1", _state(), journal, NOW, _evidence(), _checklist())
    venue = _StubVenue(LiveAck(order_id="1", status="filled", fill_price=100.0, quantity=10))
    fill, position, updated = submit_live(_intent(), _state(), None, LIMITS, journal, NOW, venue, 100.0)
    assert fill.venue == "live"
    assert fill.price == 100.0
    assert fill.size == 1000.0
    assert position is not None and position.net_size == 1000.0
    assert updated is not None and updated.day_pnl == 0.0
    assert journal.entries()[-1].kind == "live_fill"
    assert venue.calls[0][1] == 100.0


def test_submit_live_blocks_while_halted():
    journal = Journal()
    enable_live("op-1", _state(), journal, NOW, _evidence(), _checklist())
    venue = _StubVenue(LiveAck(order_id="1", status="filled", fill_price=100.0, quantity=10))
    with pytest.raises(LiveDisabledError):
        submit_live(_intent(), _state(halt_active=True), None, LIMITS, journal, NOW, venue, 100.0)
    assert journal.entries()[-1].kind == "live_blocked"
    assert venue.calls == []
