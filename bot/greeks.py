from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime, time, timezone
from typing import NamedTuple, Optional, Sequence
from zoneinfo import ZoneInfo

from bot.ivrank import implied_volatility
from bot.shapes import OptionContract

DEFAULT_RATE = 0.04
# Same zone as bot.morning.ET_ZONE; importing that module here would be circular via feeds.
ET_ZONE = ZoneInfo("America/New_York")
SECONDS_PER_YEAR = 365.0 * 86400.0
EXPIRY_CLOSE = time(16, 0)
MODELLED_NOTE = "Greeks modelled: Black-Scholes from quote mids, 4% rate, no dividends"
_GREEK_FIELDS = ("gamma", "vega", "delta", "charm", "vanna")


class Greeks(NamedTuple):
    delta: float
    gamma: float
    vega: float
    charm: float
    vanna: float


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_greeks(
    spot: float, strike: float, years: float, vol: float, call_or_put: str, rate: float = DEFAULT_RATE
) -> Optional[Greeks]:
    """Black-Scholes Greeks in vendor units, no dividends.

    delta and gamma per $1 of spot (put delta negative), vega and vanna per
    one vol point, charm as the change in delta over one calendar day.
    """
    if call_or_put not in ("call", "put"):
        return None
    if spot <= 0 or strike <= 0 or years <= 0 or vol <= 0:
        return None
    root = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2.0) * years) / (vol * root)
    d2 = d1 - vol * root
    pdf = _norm_pdf(d1)
    delta = _norm_cdf(d1) if call_or_put == "call" else _norm_cdf(d1) - 1.0
    gamma = pdf / (spot * vol * root)
    vega = spot * pdf * root / 100.0
    vanna = -pdf * d2 / vol / 100.0
    d1_by_years = (rate + vol * vol / 2.0) / (vol * root) - d1 / (2.0 * years)
    charm = -pdf * d1_by_years / 365.0
    return Greeks(delta, gamma, vega, charm, vanna)


def _mid(contract: OptionContract) -> Optional[float]:
    bid, ask = contract.bid, contract.ask
    if bid is None or ask is None or ask <= 0 or bid < 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


def _years_to_close(expiry: str, now: datetime) -> Optional[float]:
    try:
        day = date.fromisoformat(expiry[:10])
    except ValueError:
        return None
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    close = datetime.combine(day, EXPIRY_CLOSE, tzinfo=ET_ZONE)
    return (close - moment).total_seconds() / SECONDS_PER_YEAR


def implied_spot(
    contracts: Sequence[OptionContract], now: datetime, rate: float = DEFAULT_RATE
) -> Optional[float]:
    """Spot from put-call parity at the nearest expiry with a call and put mid on one strike."""
    by_expiry: dict[str, dict[float, dict[str, float]]] = {}
    for contract in contracts:
        mid = _mid(contract)
        if mid is None or contract.call_or_put not in ("call", "put"):
            continue
        sides = by_expiry.setdefault(contract.expiry, {}).setdefault(contract.strike, {})
        sides.setdefault(contract.call_or_put, mid)
    timed = []
    for expiry in by_expiry:
        years = _years_to_close(expiry, now)
        if years is not None and years > 0:
            timed.append((years, expiry))
    for years, expiry in sorted(timed):
        pairs = [(strike, s["call"] - s["put"]) for strike, s in by_expiry[expiry].items() if len(s) == 2]
        if not pairs:
            continue
        strike, diff = min(pairs, key=lambda pair: abs(pair[1]))
        return diff + strike * math.exp(-rate * years)
    return None


def fill_greeks(
    contracts: Sequence[OptionContract],
    now: datetime,
    fallback_spot: Optional[float] = None,
    rate: float = DEFAULT_RATE,
) -> tuple[tuple[OptionContract, ...], int]:
    """Fill missing Greeks from quote mids; vendor values are never overwritten."""
    original = tuple(contracts)
    spot = implied_spot(original, now, rate)
    if spot is None:
        spot = fallback_spot
    if spot is None or spot <= 0:
        return original, 0
    filled: list[OptionContract] = []
    count = 0
    for contract in original:
        updated = _filled(contract, spot, now, rate)
        if updated is not contract:
            count += 1
        filled.append(updated)
    return tuple(filled), count


def _filled(contract: OptionContract, spot: float, now: datetime, rate: float) -> OptionContract:
    missing = [name for name in _GREEK_FIELDS if getattr(contract, name) is None]
    if not missing or contract.call_or_put not in ("call", "put"):
        return contract
    mid = _mid(contract)
    years = _years_to_close(contract.expiry, now)
    if mid is None or years is None or years <= 0:
        return contract
    vol = implied_volatility(mid, spot, contract.strike, years, contract.call_or_put, rate)
    if vol is None:
        return contract
    greeks = bs_greeks(spot, contract.strike, years, vol, contract.call_or_put, rate)
    if greeks is None:
        return contract
    values = greeks._asdict()
    return replace(contract, greeks_modelled=True, **{name: values[name] for name in missing})
