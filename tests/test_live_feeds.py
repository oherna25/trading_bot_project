from datetime import datetime, timezone
from typing import Any

import pytest

from bot.exposure import compute_gex
from bot.feeds import (
    FeedConfigError,
    FeedContext,
    FeedUnavailableError,
    ingest_bars,
    ingest_chain,
    live_context,
)
from bot.journal import Journal
from bot.research import live_brief
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

TRADIER_EXPIRATIONS = {"expirations": {"date": ["2026-11-20"]}}
TRADIER_CHAIN = {
    "options": {
        "option": [
            {
                "strike": 100.0,
                "expiration_date": "2026-11-20",
                "option_type": "call",
                "open_interest": 1000.0,
                "last": 2.50,
                "bid": 2.40,
                "ask": 2.60,
            },
            {
                "strike": 100.0,
                "expiration_date": "2026-11-20",
                "option_type": "put",
                "open_interest": 2000.0,
                "last": 3.00,
                "bid": 2.90,
                "ask": 3.10,
            },
        ]
    }
}
ALPACA_BARS = {
    "bars": {
        "SPY": [
            {"t": "2026-10-26T00:00:00Z", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
            {"t": "2026-10-27T00:00:00Z", "o": 100.0, "h": 103.0, "l": 99.0, "c": 102.0, "v": 1200.0},
        ]
    }
}
FINNHUB_QUOTE = {
    "c": 102.0,
    "d": 2.0,
    "dp": 2.0,
    "h": 103.0,
    "l": 100.0,
    "o": 100.5,
    "pc": 100.0,
    "t": 1761609600,
}
MARKETDATA_CHAIN = {
    "s": "ok",
    "side": ["call", "put"],
    "strike": [100.0, 105.0],
    "expiration": [1763596800, 1763596800],
    "openInterest": [500.0, 700.0],
    "gamma": [0.05, 0.04],
    "vega": [0.11, 0.09],
    "bid": [2.40, 2.90],
    "ask": [2.60, 3.10],
    "last": [2.50, 3.00],
    "iv": [0.30, 0.32],
}
YAHOO_CHART = {
    "chart": {
        "result": [
            {
                "timestamp": [1761523200, 1761609600],
                "indicators": {
                    "quote": [
                        {
                            "open": [100.0, 100.5],
                            "high": [101.0, 103.0],
                            "low": [99.0, 100.0],
                            "close": [100.0, 102.0],
                            "volume": [1000.0, 1200.0],
                        }
                    ]
                },
            }
        ],
        "error": None,
    }
}

CREDS = {
    "TRADIER_ACCESS_TOKEN": "test-tradier",
    "APCA_API_KEY_ID": "test-id",
    "APCA_API_SECRET_KEY": "test-secret",
    "FINNHUB_API_KEY": "test-finnhub",
    "MARKETDATA_API_KEY": "test-marketdata",
    "ALPHA_VANTAGE_API_KEY": "test-alpha",
}


def _transport(routes: dict[str, Any]):
    def fake_get(url: str, headers: Any = None, params: Any = None, timeout: Any = None) -> Any:
        for marker, payload in routes.items():
            if marker in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"unexpected url {url}")

    return fake_get


def _ctx(underlying: str, routes: dict[str, Any]) -> FeedContext:
    return FeedContext(underlying=underlying, credentials=dict(CREDS), http_get=_transport(routes))


def test_tradier_adapter_normalizes_chain():
    from bot.feeds import FEED_REGISTRY

    raw = FEED_REGISTRY["tradier_sandbox"](
        _ctx("SPY", {"expirations": TRADIER_EXPIRATIONS, "chains": TRADIER_CHAIN})
    )
    assert raw["underlying"] == "SPY"
    assert [(c["strike"], c["call_or_put"], c["open_interest"]) for c in raw["contracts"]] == [
        (100.0, "call", 1000.0),
        (100.0, "put", 2000.0),
    ]
    assert raw["contracts"][0]["price"] == pytest.approx(2.50)


def test_tradier_adapter_reads_greeks_when_present():
    from bot.feeds import FEED_REGISTRY

    chain = {
        "options": {
            "option": [
                {
                    "strike": 100.0,
                    "expiration_date": "2026-11-20",
                    "option_type": "call",
                    "open_interest": 1000.0,
                    "greeks": {"gamma": 0.05, "vega": 0.11},
                }
            ]
        }
    }
    raw = FEED_REGISTRY["tradier_sandbox"](_ctx("SPY", {"expirations": TRADIER_EXPIRATIONS, "chains": chain}))
    assert raw["contracts"][0]["gamma"] == pytest.approx(0.05)
    assert raw["contracts"][0]["vega"] == pytest.approx(0.11)


def test_alpaca_adapter_normalizes_bars():
    from bot.feeds import FEED_REGISTRY

    raw = FEED_REGISTRY["alpaca"](_ctx("SPY", {"/bars": ALPACA_BARS}))
    assert [b["close"] for b in raw["bars"]] == [100.0, 102.0]
    assert raw["bars"][0]["timestamp"].tzinfo is not None


def test_finnhub_adapter_builds_single_bar_from_quote():
    from bot.feeds import FEED_REGISTRY

    raw = FEED_REGISTRY["finnhub"](_ctx("SPY", {"quote": FINNHUB_QUOTE}))
    assert len(raw["bars"]) == 1
    bar = raw["bars"][0]
    assert bar["close"] == pytest.approx(102.0)
    assert bar["high"] == pytest.approx(103.0)
    assert bar["timestamp"].tzinfo is not None


def test_finnhub_adapter_returns_empty_without_quote_price():
    from bot.feeds import FEED_REGISTRY

    raw = FEED_REGISTRY["finnhub"](_ctx("SPY", {"quote": {}}))
    assert raw["bars"] == []


def test_marketdata_adapter_normalizes_chain():
    from bot.feeds import FEED_REGISTRY

    raw = FEED_REGISTRY["marketdata_eod"](_ctx("SPY", {"options/chain": MARKETDATA_CHAIN}))
    assert [(c["strike"], c["call_or_put"], c["gamma"]) for c in raw["contracts"]] == [
        (100.0, "call", 0.05),
        (105.0, "put", 0.04),
    ]
    assert raw["contracts"][0]["expiry"] == datetime.fromtimestamp(1763596800, tz=timezone.utc).date().isoformat()


def test_yfinance_adapter_normalizes_bars():
    from bot.feeds import FEED_REGISTRY

    raw = FEED_REGISTRY["yfinance"](_ctx("SPY", {"chart": YAHOO_CHART}))
    assert [b["close"] for b in raw["bars"]] == [100.0, 102.0]


ALPHA_DAILY = {
    "Time Series (Daily)": {
        "2026-10-27": {
            "1. open": "100.5", "2. high": "103.0", "3. low": "100.0",
            "4. close": "102.0", "5. volume": "1200",
        },
        "2026-10-26": {
            "1. open": "100.0", "2. high": "101.0", "3. low": "99.0",
            "4. close": "100.5", "5. volume": "1000",
        },
        "bogus-day": {"1. open": "1.0"},
    }
}


def test_alpha_vantage_adapter_normalizes_daily_bars():
    from bot.feeds import FEED_REGISTRY

    raw = FEED_REGISTRY["alpha_vantage"](_ctx("SPY", {"alphavantage": ALPHA_DAILY}))
    assert [b["close"] for b in raw["bars"]] == [100.5, 102.0]
    assert raw["bars"][0]["timestamp"] == datetime(2026, 10, 26, tzinfo=timezone.utc)
    assert raw["bars"][1]["volume"] == pytest.approx(1200.0)


def test_alpha_vantage_adapter_sends_key_and_symbol():
    from bot.feeds import FEED_REGISTRY

    seen = {}

    def spy_get(url, headers=None, params=None, timeout=None):
        seen.update(params or {})
        return ALPHA_DAILY

    ctx = FeedContext(underlying="QQQ", credentials=dict(CREDS), http_get=spy_get)
    raw = FEED_REGISTRY["alpha_vantage"](ctx)
    assert raw["underlying"] == "QQQ"
    assert seen["function"] == "TIME_SERIES_DAILY"
    assert seen["symbol"] == "QQQ"
    assert seen["apikey"] == "test-alpha"


def test_alpha_vantage_rate_limit_is_unavailable():
    from bot.feeds import FEED_REGISTRY

    with pytest.raises(FeedUnavailableError, match="alpha vantage"):
        FEED_REGISTRY["alpha_vantage"](
            _ctx("SPY", {"alphavantage": {"Note": "rate limit hit"}})
        )


def test_alpha_vantage_missing_key_is_config_error():
    from bot.feeds import FEED_REGISTRY

    ctx = FeedContext(underlying="SPY", credentials={}, http_get=_transport({"alphavantage": ALPHA_DAILY}))
    with pytest.raises(FeedConfigError, match="ALPHA_VANTAGE_API_KEY"):
        FEED_REGISTRY["alpha_vantage"](ctx)


def test_alpha_vantage_fetches_once_per_symbol_per_day():
    calls = []

    def counting_get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        return ALPHA_DAILY

    store, journal = SnapshotStore(), Journal()
    ctx = FeedContext(underlying="SPY", credentials=dict(CREDS), http_get=counting_get)
    first = ingest_bars("alpha_vantage", store, journal, ctx)
    assert len(first) == 2
    with pytest.raises(FeedUnavailableError, match="already fetched today"):
        ingest_bars("alpha_vantage", store, journal, ctx)
    assert len(calls) == 1
    other = FeedContext(underlying="QQQ", credentials=dict(CREDS), http_get=counting_get)
    assert len(ingest_bars("alpha_vantage", store, journal, other)) == 2
    assert len(calls) == 2


def test_alpha_vantage_is_last_bar_fallback():
    from bot.feeds import BAR_FEEDS, FEED_KIND, FEED_REGISTRY

    assert BAR_FEEDS[-1] == "alpha_vantage"
    assert FEED_KIND["alpha_vantage"] == "bars"
    assert "alpha_vantage" in FEED_REGISTRY


def test_tradier_base_defaults_to_sandbox(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADIER_BASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    from bot.feeds import TRADIER_SANDBOX_URL, _tradier_base

    assert _tradier_base() == TRADIER_SANDBOX_URL


def test_tradier_base_override_from_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRADIER_BASE_URL", "https://api.tradier.com/v1")
    from bot.feeds import _tradier_base

    assert _tradier_base() == "https://api.tradier.com/v1"


def test_dotenv_supplies_missing_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADIER_ACCESS_TOKEN", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text("TRADIER_ACCESS_TOKEN=file-token\n# comment\nBLANK=\n")
    from bot.feeds import _read_env

    assert _read_env(str(dotenv))["TRADIER_ACCESS_TOKEN"] == "file-token"


def test_process_env_wins_over_dotenv(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("TRADIER_ACCESS_TOKEN=file-token\n")
    monkeypatch.setenv("TRADIER_ACCESS_TOKEN", "env-token")
    from bot.feeds import _read_env

    assert _read_env(str(dotenv))["TRADIER_ACCESS_TOKEN"] == "env-token"


def test_live_adapter_without_credential_names_the_var(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in CREDS:
        monkeypatch.delenv(name, raising=False)
    from bot.feeds import FEED_REGISTRY

    with pytest.raises(FeedConfigError, match="TRADIER_ACCESS_TOKEN"):
        FEED_REGISTRY["tradier_sandbox"](live_context("SPY"))


def test_ingest_chain_persists_and_journals():
    store, journal = SnapshotStore(), Journal()
    snapshot = ingest_chain(
        "tradier_sandbox", store, journal, _ctx("SPY", {"expirations": TRADIER_EXPIRATIONS, "chains": TRADIER_CHAIN})
    )
    assert snapshot.source_name == "tradier_sandbox"
    assert len(snapshot.contracts_list) == 2
    assert store.read_latest("SPY") is not None
    assert journal.entries()[-1].kind == "snapshot_ingested"


def test_ingest_chain_records_average_iv():
    store, journal = SnapshotStore(), Journal()
    ingest_chain("marketdata_eod", store, journal, _ctx("SPY", {"options/chain": MARKETDATA_CHAIN}))
    history = store.read_iv_history("SPY")
    assert len(history) == 1
    assert list(history.values())[0] == pytest.approx(0.31)


def test_ingest_chain_skips_iv_without_greeks():
    store, journal = SnapshotStore(), Journal()
    ingest_chain(
        "tradier_sandbox", store, journal, _ctx("SPY", {"expirations": TRADIER_EXPIRATIONS, "chains": TRADIER_CHAIN})
    )
    assert store.read_iv_history("SPY") == {}


YAHOO_OPTIONS = {
    "optionChain": {
        "result": [
            {
                "underlyingSymbol": "SPY",
                "expirationDates": [1763596800],
                "options": [
                    {
                        "expirationDate": 1763596800,
                        "calls": [
                            {"strike": 100.0, "impliedVolatility": 0.30},
                            {"strike": 105.0, "impliedVolatility": 0.34},
                            {"strike": 110.0, "impliedVolatility": None},
                        ],
                        "puts": [
                            {"strike": 100.0, "impliedVolatility": 0.32},
                            {"strike": 95.0, "impliedVolatility": 0.0},
                        ],
                    }
                ],
            }
        ],
        "error": None,
    }
}


def test_fetch_yahoo_iv_averages_positive_legs():
    from bot.feeds import fetch_yahoo_iv

    result = fetch_yahoo_iv(_ctx("SPY", {"v7/finance/options": YAHOO_OPTIONS}))
    assert result["underlying"] == "SPY"
    assert result["iv_average"] == pytest.approx(0.32)
    assert result["count"] == 3


ALPACA_SNAPSHOTS = {
    "snapshots": {
        "SPY261120C00769000": {"impliedVolatility": 0.30, "greeks": {"gamma": 0.05}},
        "SPY261120P00769000": {"impliedVolatility": 0.34},
        "SPY261120C01000000": {"impliedVolatility": None},
    }
}


def test_fetch_alpaca_iv_averages_snapshots():
    from bot.feeds import fetch_alpaca_iv

    result = fetch_alpaca_iv(_ctx("SPY", {"v1beta1/options/snapshots": ALPACA_SNAPSHOTS}))
    assert result["underlying"] == "SPY"
    assert result["iv_average"] == pytest.approx(0.32)
    assert result["count"] == 2


def test_fetch_yahoo_iv_empty_chain_yields_none():
    from bot.feeds import fetch_yahoo_iv

    result = fetch_yahoo_iv(_ctx("SPY", {"v7/finance/options": {"optionChain": {"result": []}}}))
    assert result["iv_average"] is None
    assert result["count"] == 0


def test_live_brief_prefers_alpaca_iv_when_chain_lacks_greeks():
    store, journal = SnapshotStore(), Journal()

    def factory(underlying: str) -> FeedContext:
        return _ctx(
            underlying,
            {
                "expirations": TRADIER_EXPIRATIONS,
                "chains": TRADIER_CHAIN,
                "/bars": ALPACA_BARS,
                "v1beta1/options/snapshots": ALPACA_SNAPSHOTS,
                "v7/finance/options": YAHOO_OPTIONS,
            },
        )

    live_brief("SPY", store, journal, NOW, context_factory=factory)
    history = store.read_iv_history("SPY")
    assert len(history) == 1
    assert list(history.values())[0] == pytest.approx(0.32)
    recorded = [e for e in journal.entries() if e.kind == "iv_recorded"]
    assert len(recorded) == 1
    assert recorded[0].payload["source"] == "alpaca_options"
    assert recorded[0].payload["points"] == 2


def test_live_brief_falls_back_to_yahoo_iv():
    store, journal = SnapshotStore(), Journal()

    def factory(underlying: str) -> FeedContext:
        return _ctx(
            underlying,
            {
                "expirations": TRADIER_EXPIRATIONS,
                "chains": TRADIER_CHAIN,
                "/bars": ALPACA_BARS,
                "v1beta1/options/snapshots": RuntimeError("boom"),
                "v7/finance/options": YAHOO_OPTIONS,
            },
        )

    live_brief("SPY", store, journal, NOW, context_factory=factory)
    history = store.read_iv_history("SPY")
    assert len(history) == 1
    assert list(history.values())[0] == pytest.approx(0.32)
    recorded = [e for e in journal.entries() if e.kind == "iv_recorded"]
    assert len(recorded) == 1
    assert recorded[0].payload["source"] == "yahoo_options"


def test_live_brief_skips_yahoo_iv_when_vendor_ivs_present():
    store, journal = SnapshotStore(), Journal()

    def factory(underlying: str) -> FeedContext:
        return _ctx(
            underlying,
            {
                "expirations": RuntimeError("boom"),
                "options/chain": MARKETDATA_CHAIN,
                "/bars": ALPACA_BARS,
                "v7/finance/options": AssertionError("must not fetch yahoo with vendor ivs"),
            },
        )

    live_brief("SPY", store, journal, NOW, context_factory=factory)
    history = store.read_iv_history("SPY")
    assert list(history.values())[0] == pytest.approx(0.31)
    assert not [e for e in journal.entries() if e.kind == "iv_recorded"]


def test_ingest_bars_persists_and_journals():
    store, journal = SnapshotStore(), Journal()
    bars = ingest_bars("alpaca", store, journal, _ctx("SPY", {"/bars": ALPACA_BARS}))
    assert len(bars) == 2
    assert store.read_latest_bar("SPY").close == pytest.approx(102.0)
    assert journal.entries()[-1].kind == "bars_ingested"


def test_ingest_bars_empty_raises_unavailable():
    store, journal = SnapshotStore(), Journal()
    with pytest.raises(FeedUnavailableError, match="no bars"):
        ingest_bars("alpaca", store, journal, _ctx("SPY", {"/bars": {"bars": None}}))
    assert store.read_latest_bar("SPY") is None
    assert not [e for e in journal.entries() if e.kind == "bars_ingested"]


def test_ingest_rejects_wrong_kind():
    store, journal = SnapshotStore(), Journal()
    with pytest.raises(ValueError, match="bars, not chains"):
        ingest_chain("alpaca", store, journal, _ctx("SPY", {}))
    with pytest.raises(ValueError, match="chains, not bars"):
        ingest_bars("tradier_sandbox", store, journal, _ctx("SPY", {}))


def test_live_brief_falls_back_and_matches_profile():
    store, journal = SnapshotStore(), Journal()

    def factory(underlying: str) -> FeedContext:
        return _ctx(
            underlying,
            {
                "expirations": RuntimeError("boom"),
                "options/chain": MARKETDATA_CHAIN,
                "/bars": ALPACA_BARS,
            },
        )

    reply = live_brief("SPY", store, journal, NOW, context_factory=factory)
    snapshot_id, snapshot = store.read_latest("SPY")
    bar = store.read_latest_bar("SPY")
    profile = compute_gex(snapshot, snapshot_id, spot=bar.close if bar else None)
    from bot.diff import compact_dollars

    assert f"total exposure: {compact_dollars(profile.total_exposure)}" in reply
    kinds = [e.kind for e in journal.entries()]
    assert "feed_failed" in kinds
    signal_entry = next(e for e in journal.entries() if e.kind == "signal")
    assert signal_entry.payload["snapshot_id"] == snapshot_id


def test_live_brief_falls_through_empty_bars_to_finnhub():
    store, journal = SnapshotStore(), Journal()

    def factory(underlying: str) -> FeedContext:
        return _ctx(
            underlying,
            {
                "expirations": TRADIER_EXPIRATIONS,
                "chains": TRADIER_CHAIN,
                "/bars": {"bars": None},
                "quote": FINNHUB_QUOTE,
            },
        )

    live_brief("SPY", store, journal, NOW, context_factory=factory)
    assert store.read_latest_bar("SPY").close == pytest.approx(102.0)
    failed = [e for e in journal.entries() if e.kind == "feed_failed"]
    assert [e.payload["feed_name"] for e in failed] == ["alpaca"]
    ingested = [e for e in journal.entries() if e.kind == "bars_ingested"]
    assert [e.payload["source_name"] for e in ingested] == ["finnhub"]


def test_live_brief_raises_when_all_chain_feeds_fail():
    store, journal = SnapshotStore(), Journal()

    def factory(underlying: str) -> FeedContext:
        return _ctx(underlying, {"expirations": RuntimeError("down"), "options/chain": RuntimeError("down")})

    with pytest.raises(FeedUnavailableError, match="all chain feeds failed"):
        live_brief("SPY", store, journal, NOW, context_factory=factory)


def test_live_brief_tolerates_bar_failure():
    store, journal = SnapshotStore(), Journal()

    def factory(underlying: str) -> FeedContext:
        return _ctx(
            underlying,
            {
                "expirations": TRADIER_EXPIRATIONS,
                "chains": TRADIER_CHAIN,
                "/bars": RuntimeError("down"),
                "quote": {},
                "chart": {"chart": {"result": [], "error": None}},
            },
        )

    reply = live_brief("SPY", store, journal, NOW, context_factory=factory)
    assert "total exposure:" in reply
    assert "direction: flat" in reply


def test_live_brief_journals_signal_after_snapshot():
    store, journal = SnapshotStore(), Journal()

    def factory(underlying: str) -> FeedContext:
        return _ctx(
            underlying,
            {
                "expirations": TRADIER_EXPIRATIONS,
                "chains": TRADIER_CHAIN,
                "/bars": ALPACA_BARS,
            },
        )

    live_brief("SPY", store, journal, NOW, context_factory=factory)
    ingested = next(e for e in journal.entries() if e.kind == "snapshot_ingested")
    for entry in journal.entries():
        if entry.kind in ("signal", "model_fallback"):
            assert entry.timestamp_utc >= ingested.timestamp_utc


def test_live_brief_paper_trade_fills_and_second_run_skips():
    store, journal = SnapshotStore(), Journal()

    def factory(underlying: str) -> FeedContext:
        return _ctx(
            underlying,
            {
                "expirations": RuntimeError("boom"),
                "options/chain": MARKETDATA_CHAIN,
                "/bars": ALPACA_BARS,
            },
        )

    reply = live_brief("SPY", store, journal, NOW, context_factory=factory, paper_trade=True)
    assert "\npaper trade: " in reply
    assert sum(1 for e in journal.entries() if e.kind == "fill") == 1
    reply2 = live_brief("SPY", store, journal, NOW, context_factory=factory, paper_trade=True)
    assert "paper trade: " not in reply2
    assert sum(1 for e in journal.entries() if e.kind == "fill") == 1
    assert journal.entries()[-1].kind == "trade_skipped"


def test_live_brief_without_paper_trade_journals_no_fill():
    store, journal = SnapshotStore(), Journal()

    def factory(underlying: str) -> FeedContext:
        return _ctx(
            underlying,
            {
                "expirations": RuntimeError("boom"),
                "options/chain": MARKETDATA_CHAIN,
                "/bars": ALPACA_BARS,
            },
        )

    live_brief("SPY", store, journal, NOW, context_factory=factory)
    assert not [e for e in journal.entries() if e.kind == "fill"]


def test_urllib_get_retries_transient_then_succeeds(monkeypatch):
    import io, json as _json, urllib.error
    from bot import feeds
    attempts = {"n": 0}

    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return _json.dumps({"ok": True}).encode()

    def flaky(request, timeout=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)
        return _Resp()

    monkeypatch.setattr(feeds.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(feeds.time, "sleep", lambda *_: None)
    assert feeds._urllib_get("https://example.test/x", None, None, 1.0) == {"ok": True}
    assert attempts["n"] == 3
