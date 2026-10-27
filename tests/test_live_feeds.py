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


def test_ingest_bars_persists_and_journals():
    store, journal = SnapshotStore(), Journal()
    bars = ingest_bars("alpaca", store, journal, _ctx("SPY", {"/bars": ALPACA_BARS}))
    assert len(bars) == 2
    assert store.read_latest_bar("SPY").close == pytest.approx(102.0)
    assert journal.entries()[-1].kind == "bars_ingested"


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
    profile = compute_gex(snapshot, snapshot_id)
    assert f"total exposure: {profile.total_exposure:.1f}" in reply
    kinds = [e.kind for e in journal.entries()]
    assert "feed_failed" in kinds
    signal_entry = next(e for e in journal.entries() if e.kind == "signal")
    assert signal_entry.payload["snapshot_id"] == snapshot_id


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
