from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from bot.config import env_value, read_dotenv as _read_dotenv
from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OptionContract
from bot.store import SnapshotStore

TRADIER_SANDBOX_URL = "https://sandbox.tradier.com/v1"
TRADIER_PRODUCTION_URL = "https://api.tradier.com/v1"
ALPACA_BASE_URL = "https://data.alpaca.markets"
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
MARKETDATA_BASE_URL = "https://api.marketdata.app/v1"
YAHOO_BASE_URL = "https://query1.finance.yahoo.com"

HTTP_TIMEOUT_SECONDS = 30.0
BAR_DAYS = 5
TRADIER_MAX_EXPIRIES = 2

HttpGet = Callable[..., Any]


class FeedConfigError(ValueError):
    pass


class FeedUnavailableError(RuntimeError):
    pass


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _urllib_get(url: str, headers: dict[str, str] | None, params: dict[str, Any] | None, timeout: float) -> Any:
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    full_url = url + ("?" + query if query else "")
    request = urllib.request.Request(full_url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _canned_get(url: str, headers: dict[str, str] | None, params: dict[str, Any] | None, timeout: float) -> Any:
    if "options/expirations" in url:
        return {"expirations": {"date": ["2026-11-20"]}}
    if "options/chains" in url:
        return {
            "options": {
                "option": [
                    {
                        "strike": 100.0,
                        "expiration_date": "2026-11-20",
                        "option_type": "call",
                        "open_interest": 1000.0,
                    }
                ]
            }
        }
    if "options/chain" in url:
        return {
            "s": "ok",
            "side": ["call"],
            "strike": [100.0],
            "expiration": [1763596800],
            "openInterest": [500.0],
            "gamma": [0.05],
            "vega": [0.11],
            "delta": [0.55],
            "bid": [2.40],
            "ask": [2.60],
            "last": [2.50],
            "iv": [0.30],
        }
    if "/bars" in url:
        return {
            "bars": {
                "SPY": [
                    {"t": "2026-10-26T00:00:00Z", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1000.0},
                    {"t": "2026-10-27T00:00:00Z", "o": 100.5, "h": 103.0, "l": 100.0, "c": 102.0, "v": 1200.0},
                ]
            }
        }
    if "quote" in url:
        return {
            "c": 102.0,
            "h": 103.0,
            "l": 100.0,
            "o": 100.5,
            "pc": 100.0,
            "t": 1761609600,
        }
    if "chart" in url:
        return {
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
                                    "close": [100.5, 102.0],
                                    "volume": [1000.0, 1200.0],
                                }
                            ]
                        },
                    }
                ],
                "error": None,
            }
        }
    raise ValueError(f"no canned payload for {url}")


def _credential_or_offline(ctx: FeedContext, name: str) -> str:
    if ctx.http_get is _canned_get:
        return "offline"
    return ctx.require(name)


@dataclass(frozen=True)
class FeedContext:
    underlying: str
    credentials: Mapping[str, str] = field(default_factory=dict)
    http_get: HttpGet = _canned_get

    def require(self, name: str) -> str:
        try:
            return self.credentials[name]
        except KeyError:
            raise FeedConfigError(f"missing credential {name} for live feed") from None


CREDENTIAL_NAMES = (
    "TRADIER_ACCESS_TOKEN",
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
    "FINNHUB_API_KEY",
    "MARKETDATA_API_KEY",
)


def _read_env(dotenv_path: str = ".env") -> dict[str, str]:
    file_values = _read_dotenv(dotenv_path)
    return {
        name: os.environ.get(name, file_values.get(name, ""))
        for name in CREDENTIAL_NAMES
        if os.environ.get(name) or file_values.get(name)
    }


def _tradier_base() -> str:
    return env_value("TRADIER_BASE_URL", TRADIER_SANDBOX_URL)


def default_context(underlying: str = "SPY") -> FeedContext:
    return FeedContext(underlying=underlying, credentials=_read_env(), http_get=_canned_get)


def live_context(underlying: str) -> FeedContext:
    return FeedContext(underlying=underlying, credentials=_read_env(), http_get=_urllib_get)


def _tradier_expiry(entry: Any) -> str | None:
    if isinstance(entry, dict):
        value = entry.get("date") or entry.get("expiration")
        return str(value) if value is not None else None
    return str(entry) if entry is not None else None


def _tradier_sandbox(ctx: FeedContext) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {_credential_or_offline(ctx, 'TRADIER_ACCESS_TOKEN')}", "Accept": "application/json"}
    base = _tradier_base()
    greeks = "false" if base == TRADIER_SANDBOX_URL else "true"
    expirations_payload = ctx.http_get(
        f"{base}/markets/options/expirations",
        headers=headers,
        params={"symbol": ctx.underlying, "includeAllRoots": "true"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    node = expirations_payload.get("expirations") or {}
    raw_expiries = _as_list(node.get("date") or node.get("expiration"))
    expiries = [e for e in (_tradier_expiry(entry) for entry in raw_expiries) if e]
    contracts: list[dict[str, Any]] = []
    for expiry in expiries[:TRADIER_MAX_EXPIRIES]:
        chain_payload = ctx.http_get(
            f"{base}/markets/options/chains",
            headers=headers,
            params={"symbol": ctx.underlying, "expiration": expiry, "greeks": greeks},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        for item in _as_list((chain_payload.get("options") or {}).get("option")):
            side = str(item.get("option_type") or "").lower()
            if item.get("strike") is None or side not in ("call", "put"):
                continue
            greek_values = item.get("greeks") or {}
            contracts.append(
                {
                    "strike": float(item["strike"]),
                    "expiry": str(item.get("expiration_date") or expiry),
                    "call_or_put": side,
                    "open_interest": float(item.get("open_interest") or 0.0),
                    "gamma": greek_values.get("gamma"),
                    "vega": greek_values.get("vega"),
                    "delta": greek_values.get("delta"),
                    "price": item.get("last"),
                    "bid": item.get("bid"),
                    "ask": item.get("ask"),
                }
            )
    return {"underlying": ctx.underlying, "contracts": contracts}


def _alpaca(ctx: FeedContext) -> dict[str, Any]:
    headers = {
        "APCA-API-KEY-ID": _credential_or_offline(ctx, "APCA_API_KEY_ID"),
        "APCA-API-SECRET-KEY": _credential_or_offline(ctx, "APCA_API_SECRET_KEY"),
    }
    payload = ctx.http_get(
        f"{ALPACA_BASE_URL}/v2/stocks/{ctx.underlying}/bars",
        headers=headers,
        params={"timeframe": "1Day", "limit": str(BAR_DAYS), "adjustment": "raw", "feed": "iex"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    node = payload.get("bars")
    rows = node.get(ctx.underlying, []) if isinstance(node, dict) else _as_list(node)
    bars = [
        {
            "timestamp": datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00")),
            "open": float(row["o"]),
            "high": float(row["h"]),
            "low": float(row["l"]),
            "close": float(row["c"]),
            "volume": float(row.get("v") or 0.0),
        }
        for row in rows
        if row.get("t") is not None
    ]
    bars.sort(key=lambda b: b["timestamp"])
    return {"underlying": ctx.underlying, "bars": bars}


def _finnhub(ctx: FeedContext) -> dict[str, Any]:
    payload = ctx.http_get(
        f"{FINNHUB_BASE_URL}/quote",
        headers={},
        params={
            "symbol": ctx.underlying,
            "token": _credential_or_offline(ctx, "FINNHUB_API_KEY"),
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if payload.get("c") is None:
        return {"underlying": ctx.underlying, "bars": []}
    stamp = payload.get("t")
    timestamp = datetime.fromtimestamp(int(stamp), tz=timezone.utc) if stamp else _now_utc()
    close = float(payload["c"])
    bars = [
        {
            "timestamp": timestamp,
            "open": float(payload.get("o") or close),
            "high": float(payload.get("h") or close),
            "low": float(payload.get("l") or close),
            "close": close,
            "volume": 0.0,
        }
    ]
    return {"underlying": ctx.underlying, "bars": bars}


def _marketdata_eod(ctx: FeedContext) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {_credential_or_offline(ctx, 'MARKETDATA_API_KEY')}", "Accept": "application/json"}
    payload = ctx.http_get(
        f"{MARKETDATA_BASE_URL}/options/chain/{ctx.underlying}/",
        headers=headers,
        params={},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    columns = payload
    count = len(columns.get("strike") or [])
    contracts: list[dict[str, Any]] = []
    for index in range(count):
        side = str((columns.get("side") or [None] * count)[index] or "").lower()
        if side not in ("call", "put"):
            continue
        strike = (columns.get("strike") or [None] * count)[index]
        raw_expiry = (columns.get("expiration") or [None] * count)[index]
        if strike is None or raw_expiry is None:
            continue
        expiry = (
            datetime.fromtimestamp(int(raw_expiry), tz=timezone.utc).date().isoformat()
            if isinstance(raw_expiry, (int, float))
            else str(raw_expiry)
        )

        def at(name: str) -> Any:
            values = columns.get(name) or [None] * count
            return values[index] if index < len(values) else None

        contracts.append(
            {
                "strike": float(strike),
                "expiry": expiry,
                "call_or_put": side,
                "open_interest": float(at("openInterest") or 0.0),
                "gamma": at("gamma"),
                "vega": at("vega"),
                "delta": at("delta"),
                "price": at("last"),
                "bid": at("bid"),
                "ask": at("ask"),
                "iv": at("iv"),
            }
        )
    return {"underlying": ctx.underlying, "contracts": contracts}


def _yfinance(ctx: FeedContext) -> dict[str, Any]:
    payload = ctx.http_get(
        f"{YAHOO_BASE_URL}/v8/finance/chart/{ctx.underlying}",
        headers={"User-Agent": "trading-bot/0.1"},
        params={"interval": "1d", "range": f"{BAR_DAYS}d"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    results = _as_list((payload.get("chart") or {}).get("result"))
    node = results[0] if results else {}
    stamps = node.get("timestamp") or []
    quotes = _as_list((node.get("indicators") or {}).get("quote"))
    quote = quotes[0] if quotes else {}
    bars = []
    for stamp, o, h, low, c, v in zip(
        stamps,
        quote.get("open") or [],
        quote.get("high") or [],
        quote.get("low") or [],
        quote.get("close") or [],
        quote.get("volume") or [],
    ):
        if stamp is None or c is None:
            continue
        bars.append(
            {
                "timestamp": datetime.fromtimestamp(int(stamp), tz=timezone.utc),
                "open": float(o or c),
                "high": float(h or c),
                "low": float(low or c),
                "close": float(c),
                "volume": float(v or 0.0),
            }
        )
    bars.sort(key=lambda b: b["timestamp"])
    return {"underlying": ctx.underlying, "bars": bars}


FEED_REGISTRY: dict[str, Callable[[FeedContext], dict[str, Any]]] = {
    "tradier_sandbox": _tradier_sandbox,
    "alpaca": _alpaca,
    "finnhub": _finnhub,
    "marketdata_eod": _marketdata_eod,
    "yfinance": _yfinance,
}

FEED_KIND: dict[str, str] = {
    "tradier_sandbox": "chain",
    "alpaca": "bars",
    "finnhub": "bars",
    "marketdata_eod": "chain",
    "yfinance": "bars",
}

CHAIN_FEEDS = ("tradier_sandbox", "marketdata_eod")
BAR_FEEDS = ("alpaca", "finnhub", "yfinance")


def ingest_chain(
    feed_name: str, store: SnapshotStore, journal: Journal, ctx: FeedContext | None = None
) -> ChainSnapshot:
    if FEED_KIND[feed_name] != "chain":
        raise ValueError(f"feed {feed_name} carries bars, not chains")
    adapter = FEED_REGISTRY[feed_name]
    started = time.perf_counter()
    raw = adapter(ctx or default_context())
    latency_ms = (time.perf_counter() - started) * 1000.0
    snapshot = ChainSnapshot(
        underlying=raw["underlying"],
        timestamp_utc=_now_utc(),
        source_name=feed_name,
        latency_ms=latency_ms,
        contracts_list=tuple(
            OptionContract(
                strike=c["strike"],
                expiry=c["expiry"],
                call_or_put=c["call_or_put"],
                open_interest=c["open_interest"],
                gamma=c.get("gamma"),
                vega=c.get("vega"),
                delta=c.get("delta"),
                price=c.get("price"),
                bid=c.get("bid"),
                ask=c.get("ask"),
                iv=c.get("iv"),
            )
            for c in raw["contracts"]
        ),
    )
    snapshot_id = store.save(snapshot)
    journal.append(snapshot.timestamp_utc, "snapshot_ingested", {"snapshot_id": snapshot_id, "source_name": feed_name})
    return snapshot


def ingest_bars(feed_name: str, store: SnapshotStore, journal: Journal, ctx: FeedContext | None = None) -> list[Bar]:
    if FEED_KIND[feed_name] != "bars":
        raise ValueError(f"feed {feed_name} carries chains, not bars")
    adapter = FEED_REGISTRY[feed_name]
    context = ctx or default_context()
    started = time.perf_counter()
    raw = adapter(context)
    latency_ms = (time.perf_counter() - started) * 1000.0
    bars = [
        Bar(
            interval="1d",
            timestamp_utc=b["timestamp"],
            open=b["open"],
            high=b["high"],
            low=b["low"],
            close=b["close"],
            volume=b["volume"],
        )
        for b in raw["bars"]
    ]
    for bar in bars:
        store.save_bar(raw["underlying"], bar)
    journal.append(
        _now_utc(),
        "bars_ingested",
        {"source_name": feed_name, "underlying": raw["underlying"], "count": len(bars), "latency_ms": latency_ms},
    )
    return bars


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)
