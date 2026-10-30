from __future__ import annotations

import math
from datetime import datetime
from typing import Mapping, Sequence

MIN_IV_POINTS = 20


def iv_rank(values: Sequence[float]) -> float | None:
    points = list(values)
    if len(points) < MIN_IV_POINTS:
        return None
    current = points[-1]
    low, high = min(points), max(points)
    if high == low:
        return 50.0
    return round((current - low) / (high - low) * 100.0, 1)


def iv_percentile(values: Sequence[float]) -> float | None:
    points = list(values)
    if len(points) < MIN_IV_POINTS:
        return None
    current = points[-1]
    below = sum(1 for point in points if point < current)
    return round(below / len(points) * 100.0, 1)


def history_values(history: Mapping[str, float]) -> list[float]:
    return [history[day] for day in sorted(history)]


def histories_for(store, symbols: Sequence[str]) -> dict[str, list[float]]:
    return {symbol: history_values(store.read_iv_history(symbol)) for symbol in symbols}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(spot: float, strike: float, years: float, vol: float, call_or_put: str, rate: float) -> float:
    discount = math.exp(-rate * years)
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2.0) * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)
    if call_or_put == "call":
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_volatility(
    mid: float, spot: float, strike: float, years: float, call_or_put: str, rate: float = 0.04
) -> float | None:
    """Vol matching mid under Black-Scholes; European-exercise approximation for listed equity options."""
    if years <= 0 or mid <= 0 or spot <= 0 or strike <= 0:
        return None
    if call_or_put == "call":
        intrinsic = max(spot - strike, 0.0)
    else:
        intrinsic = max(strike - spot, 0.0)
    if mid < intrinsic:
        return None
    low, high = 0.005, 5.0
    if _bs_price(spot, strike, years, low, call_or_put, rate) > mid:
        return None
    if _bs_price(spot, strike, years, high, call_or_put, rate) < mid:
        return None
    for _ in range(200):
        guess = (low + high) / 2.0
        if _bs_price(spot, strike, years, guess, call_or_put, rate) < mid:
            low = guess
        else:
            high = guess
    vol = (low + high) / 2.0
    if abs(_bs_price(spot, strike, years, vol, call_or_put, rate) - mid) > 1e-6 * max(1.0, mid):
        return None
    return vol


def _contract_mid(contract) -> float | None:
    if contract.bid is not None and contract.ask is not None:
        return (contract.bid + contract.ask) / 2.0
    return contract.price


def _expiry_years(expiry: str, ref) -> float | None:
    try:
        day = datetime.strptime(expiry, "%Y-%m-%d").date()
    except ValueError:
        try:
            day = datetime.fromisoformat(expiry).date()
        except ValueError:
            return None
    return (day - ref).days / 365.0


MIN_INVERT_MID = 0.10
MIN_INVERT_OI = 100.0
MIN_INVERT_DTE = 1
MAX_INVERT_DTE = 60
INVERT_MONEYNESS_BAND = 0.02


def _invertible_mid(contract, spot: float, ref) -> tuple[float, float] | None:
    mid = _contract_mid(contract)
    if mid is None or mid < MIN_INVERT_MID:
        return None
    if contract.open_interest < MIN_INVERT_OI:
        return None
    years = _expiry_years(contract.expiry, ref)
    if years is None:
        return None
    dte = years * 365.0
    if dte < MIN_INVERT_DTE or dte > MAX_INVERT_DTE:
        return None
    if contract.call_or_put == "call":
        if contract.strike < spot * (1.0 - INVERT_MONEYNESS_BAND):
            return None
    else:
        if contract.strike > spot * (1.0 + INVERT_MONEYNESS_BAND):
            return None
    return mid, years


def backfill_iv_history(store, journal, now) -> str:
    """Seed IV history from vendor IVs, inverting Black-Scholes from mids when a snapshot has none."""
    added = 0
    skipped = 0
    without_iv = 0
    filled_symbols = 0
    for symbol in sorted(store.list_underlyings()):
        history = store.read_iv_history(symbol)
        samples = 0
        inverted = 0
        first = None
        last = None
        for _, snapshot in store.snapshots_in_order():
            if snapshot.underlying != symbol:
                continue
            key = snapshot.timestamp_utc.isoformat()
            if key in history:
                skipped += 1
                continue
            ivs = [c.iv for c in snapshot.contracts_list if c.iv is not None]
            if ivs:
                average = sum(ivs) / len(ivs)
            else:
                bars = store.read_bars(symbol)
                eligible = [b for b in bars if b.timestamp_utc <= snapshot.timestamp_utc]
                bar = eligible[-1] if eligible else (bars[-1] if bars else None)
                if bar is None:
                    without_iv += 1
                    continue
                solved = []
                for contract in snapshot.contracts_list:
                    gated = _invertible_mid(contract, bar.close, snapshot.timestamp_utc.date())
                    if gated is None:
                        continue
                    mid, years = gated
                    vol = implied_volatility(mid, bar.close, contract.strike, years, contract.call_or_put)
                    if vol is not None:
                        solved.append(vol)
                if not solved:
                    without_iv += 1
                    continue
                average = sum(solved) / len(solved)
                inverted += 1
            store.save_iv(symbol, key, average)
            history[key] = average
            samples += 1
            first = key if first is None else min(first, key)
            last = key if last is None else max(last, key)
        if samples:
            filled_symbols += 1
            added += samples
            journal.append(
                now, "iv_backfilled",
                {"symbol": symbol, "samples": samples, "first": first, "last": last, "inverted": inverted},
            )
    rankable = [
        symbol
        for symbol in sorted(store.list_underlyings())
        if len(store.read_iv_history(symbol)) >= MIN_IV_POINTS
    ]
    if added == 0:
        return (
            f"backfilled IV: no new samples "
            f"({without_iv} snapshots without priced IV, {skipped} already recorded)"
        )
    ranked = ", ".join(rankable) if rankable else "none yet"
    return (
        f"backfilled IV: {added} samples across {filled_symbols} symbols "
        f"({skipped} already recorded); rankable: {ranked}"
    )
