from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import bot.feeds as feeds
from bot.feeds import FeedContext, ingest_chain
from bot.greeks import bs_greeks, fill_greeks, implied_spot
from bot.ivrank import bs_price
from bot.journal import Journal
from bot.persist import _freeze_contract, _thaw_contract, load_bundle, save_bundle
from bot.shapes import ChainSnapshot, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
SPOT = 100.0
VOL = 0.30
RATE = 0.04
YEAR = timedelta(days=365)


def _close_utc(day: str) -> datetime:
    # November expiries close at 16:00 EST, which is 21:00 UTC.
    return datetime.fromisoformat(f"{day}T21:00:00+00:00")


def _years(day: str, now: datetime = NOW) -> float:
    return (_close_utc(day) - now) / YEAR


def _quoted(strike, expiry, side, now=NOW, spot=SPOT, vol=VOL, spread=0.0005, **extra):
    price = bs_price(spot, strike, _years(expiry, now), vol, side, RATE)
    return OptionContract(
        strike=strike, expiry=expiry, call_or_put=side, open_interest=500.0,
        bid=price - spread, ask=price + spread, **extra,
    )


def _chain(now=NOW):
    return tuple(
        _quoted(strike, expiry, side, now)
        for expiry in ("2026-11-20", "2026-12-18")
        for strike in (95.0, 100.0, 105.0)
        for side in ("call", "put")
    )


def test_fill_recovers_true_greeks_and_spot():
    contracts = _chain()
    assert implied_spot(contracts, NOW, RATE) == pytest.approx(SPOT, abs=1e-6)
    filled, count = fill_greeks(contracts, NOW)
    assert count == len(contracts)
    for original, contract in zip(contracts, filled):
        truth = bs_greeks(SPOT, original.strike, _years(original.expiry), VOL, original.call_or_put, RATE)
        assert contract.greeks_modelled is True
        assert contract.iv is None
        assert contract.delta == pytest.approx(truth.delta, abs=2e-3)
        assert contract.gamma == pytest.approx(truth.gamma, rel=2e-2)
        assert contract.vega == pytest.approx(truth.vega, rel=2e-2)
        assert contract.charm == pytest.approx(truth.charm, abs=2e-4)
        assert contract.vanna == pytest.approx(truth.vanna, abs=2e-3)


def test_fallback_spot_used_only_without_a_parity_pair():
    calls_only = tuple(c for c in _chain() if c.call_or_put == "call")
    assert implied_spot(calls_only, NOW) is None
    unchanged, none_count = fill_greeks(calls_only, NOW)
    assert (unchanged, none_count) == (calls_only, 0)
    filled, count = fill_greeks(calls_only, NOW, fallback_spot=SPOT)
    assert count == len(calls_only)
    assert all(c.delta is not None and 0.0 < c.delta < 1.0 for c in filled)


def _price(spot=SPOT, strike=105.0, years=0.25, vol=VOL, side="call"):
    return bs_price(spot, strike, years, vol, side, RATE)


def _delta_fd(side, spot=SPOT, years=0.25, vol=VOL, strike=105.0, h=0.01):
    up = _price(spot + h, strike, years, vol, side)
    down = _price(spot - h, strike, years, vol, side)
    return (up - down) / (2 * h)


@pytest.mark.parametrize("side", ["call", "put"])
def test_greeks_match_finite_differences(side):
    strike, years = 105.0, 0.25
    greeks = bs_greeks(SPOT, strike, years, VOL, side, RATE)
    assert greeks.delta == pytest.approx(_delta_fd(side), rel=1e-6)
    h = 0.1
    gamma_fd = (_price(SPOT + h, side=side) - 2 * _price(side=side) + _price(SPOT - h, side=side)) / h**2
    assert greeks.gamma == pytest.approx(gamma_fd, rel=1e-4)
    vega_fd = (_price(vol=VOL + 0.01, side=side) - _price(vol=VOL - 0.01, side=side)) / 2
    assert greeks.vega == pytest.approx(vega_fd, rel=1e-4)
    vanna_fd = (_delta_fd(side, vol=VOL + 0.01) - _delta_fd(side, vol=VOL - 0.01)) / 2
    assert greeks.vanna == pytest.approx(vanna_fd, rel=1e-3)
    day = 1 / 365
    charm_fd = (_delta_fd(side, years=years - day) - _delta_fd(side, years=years + day)) / 2
    assert greeks.charm == pytest.approx(charm_fd, rel=1e-3)
    assert greeks.charm != 0.0


def test_put_delta_is_negative_and_call_delta_positive():
    assert bs_greeks(SPOT, 100.0, 0.1, VOL, "put").delta < 0.0
    assert bs_greeks(SPOT, 100.0, 0.1, VOL, "call").delta > 0.0


@pytest.mark.parametrize(
    "args",
    [(0.0, 100.0, 0.1, 0.3, "call"), (100.0, 0.0, 0.1, 0.3, "call"), (100.0, 100.0, 0.0, 0.3, "call"),
     (100.0, 100.0, 0.1, 0.0, "call"), (100.0, 100.0, 0.1, 0.3, "straddle")],
)
def test_bs_greeks_rejects_bad_inputs(args):
    assert bs_greeks(*args) is None


def test_vendor_greeks_are_never_overwritten():
    full = _quoted(100.0, "2026-11-20", "call", gamma=0.9, vega=0.8, delta=0.7, charm=0.6, vanna=0.5)
    partial = _quoted(100.0, "2026-11-20", "put", gamma=0.9, delta=-0.7)
    parity_partner = _quoted(100.0, "2026-12-18", "call")
    filled, count = fill_greeks((full, partial, parity_partner), NOW, fallback_spot=SPOT)
    assert filled[0] == full and filled[0].greeks_modelled is False
    assert count == 2
    assert (filled[1].gamma, filled[1].delta) == (0.9, -0.7)
    truth = bs_greeks(SPOT, 100.0, _years("2026-11-20"), VOL, "put", RATE)
    assert filled[1].vega == pytest.approx(truth.vega, rel=2e-2)
    assert filled[1].charm == pytest.approx(truth.charm, abs=2e-4)
    assert filled[1].vanna == pytest.approx(truth.vanna, abs=2e-3)
    assert filled[1].greeks_modelled is True


def test_a_last_price_alone_is_not_a_quote():
    bare = OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=1.0, price=2.5)
    no_ask = OptionContract(
        strike=100.0, expiry="2026-11-20", call_or_put="put", open_interest=1.0, bid=2.0, price=2.5
    )
    crossed = OptionContract(
        strike=105.0, expiry="2026-11-20", call_or_put="call", open_interest=1.0, bid=3.0, ask=2.0
    )
    contracts = (bare, no_ask, crossed)
    assert implied_spot(contracts, NOW) is None
    assert fill_greeks(contracts, NOW, fallback_spot=SPOT) == (contracts, 0)


def test_expired_contract_gets_nothing_and_same_day_before_close_does():
    expired = _quoted(100.0, "2026-11-20", "call")
    after_close = _close_utc("2026-11-20") + timedelta(minutes=1)
    assert fill_greeks((expired,), after_close, fallback_spot=SPOT) == ((expired,), 0)

    morning = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
    same_day = _quoted(100.0, "2026-11-20", "call", now=morning)
    filled, count = fill_greeks((same_day,), morning, fallback_spot=SPOT)
    assert count == 1
    assert 0.0 < filled[0].gamma


def test_naive_now_is_read_as_utc():
    naive = NOW.replace(tzinfo=None)
    _, count = fill_greeks(_chain(), naive)
    assert count == len(_chain())


def test_greeks_modelled_survives_persist(tmp_path):
    modelled, _ = fill_greeks(_chain(), NOW)
    snapshot = ChainSnapshot("SPY", NOW, "tradier_sandbox", 1.0, modelled)
    store = SnapshotStore()
    store.save(snapshot)
    save_bundle(str(tmp_path), store, Journal())
    loaded, _ = load_bundle(str(tmp_path))
    contracts = loaded.read_latest("SPY")[1].contracts_list
    assert all(c.greeks_modelled for c in contracts)
    assert contracts == modelled


def test_old_bundle_contract_without_the_key_thaws_unmodelled():
    frozen = _freeze_contract(replace(_chain()[0], greeks_modelled=True))
    assert _thaw_contract(frozen).greeks_modelled is True
    del frozen["greeks_modelled"]
    assert _thaw_contract(frozen).greeks_modelled is False


def _tradier_payload(now):
    rows = []
    for strike in (98.0, 100.0, 102.0):
        for side in ("call", "put"):
            contract = _quoted(strike, "2026-11-20", side, now)
            rows.append({
                "strike": strike, "expiration_date": "2026-11-20", "option_type": side,
                "open_interest": 500.0, "last": contract.bid, "bid": contract.bid, "ask": contract.ask,
            })
    return {"options": {"option": rows}}


def _ctx(chain):
    def fake_get(url, headers=None, params=None, timeout=None):
        if "expirations" in url:
            return {"expirations": {"date": ["2026-11-20"]}}
        return chain

    return FeedContext(underlying="SPY", credentials={"TRADIER_ACCESS_TOKEN": "t"}, http_get=fake_get)


def test_ingest_chain_models_greeks_and_journals_the_count(monkeypatch):
    now = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(feeds, "_now_utc", lambda: now)
    store, journal = SnapshotStore(), Journal()
    ingest_chain("tradier_sandbox", store, journal, _ctx(_tradier_payload(now)))
    contracts = store.read_latest("SPY")[1].contracts_list
    assert len(contracts) == 6
    assert all(c.greeks_modelled and c.gamma > 0 and c.vega > 0 for c in contracts)
    assert all(c.delta > 0 for c in contracts if c.call_or_put == "call")
    assert all(c.delta < 0 for c in contracts if c.call_or_put == "put")
    entry = journal.entries()[-1]
    assert entry.kind == "snapshot_ingested"
    assert entry.payload["modelled_greeks"] == 6
    assert store.read_iv_history("SPY") == {}


def test_chart_and_brief_say_greeks_are_modelled():
    from bot.charts import gamma_chart_html
    from bot.exposure import compute_dex, compute_gex
    from bot.research_formatters import format_brief, format_exposure_brief
    from bot.shapes import Signal

    modelled, _ = fill_greeks(_chain(), NOW)
    plain = _chain()
    for contracts, expected in ((modelled, True), (plain, False)):
        snapshot = ChainSnapshot("SPY", NOW, "tradier_sandbox", 1.0, contracts)
        profile = compute_gex(snapshot, "SPY:1", spot=SPOT)
        page = gamma_chart_html("SPY", snapshot, profile, None, now=NOW)
        signal = Signal(direction="long", thesis="t", confidence=0.5, snapshot_id="SPY:1")
        gex_brief = format_brief(profile, signal, snapshot, now=NOW)
        dex_brief = format_exposure_brief("DEX", compute_dex(snapshot, "SPY:1"), snapshot, None, NOW)
        for text in (page, gex_brief, dex_brief):
            assert ("Black-Scholes from quote mids" in text) is expected


def test_canned_fixture_without_quotes_still_has_no_greeks():
    store, journal = SnapshotStore(), Journal()
    ingest_chain("tradier_sandbox", store, journal)
    contracts = store.read_latest("SPY")[1].contracts_list
    assert contracts
    assert not any(c.greeks_modelled or c.gamma is not None for c in contracts)
    assert journal.entries()[-1].payload["modelled_greeks"] == 0
