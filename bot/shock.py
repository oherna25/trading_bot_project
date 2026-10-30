from __future__ import annotations

import re
from datetime import datetime

from bot.diff import compact_dollars, signed_dollars
from bot.exposure import compute_gex
from bot.journal import Journal
from bot.store import SnapshotStore
from bot.tradecards import PROFILE_REQUIRED, TradeCard, query_trade_cards

SHOCK_USAGE = "shock needs a percent, e.g. shock SPY -2%"
SHOCK_PCT_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")
SHOCK_PERCENT_WORD_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*percent", re.IGNORECASE)
SHOCK_BARE_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")


def parse_shock_pct(text: str) -> float | None:
    text = text.replace("\u2212", "-")
    match = SHOCK_PCT_RE.search(text)
    if match is not None:
        return float(match.group(1))
    match = SHOCK_PERCENT_WORD_RE.search(text)
    if match is not None:
        return float(match.group(1))
    if SHOCK_BARE_RE.fullmatch(text.strip()) is not None:
        return float(text.strip())
    return None


def _intrinsic(call_or_put: str, strike: float, spot: float) -> float:
    if call_or_put == "call":
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def shock_card_pnl(card: TradeCard, shocked: float) -> float | None:
    if card.net_debit is None:
        return None
    value = _intrinsic(card.call_or_put, card.strike, shocked)
    if card.hedge_strike is not None:
        value -= _intrinsic(card.call_or_put, card.hedge_strike, shocked)
    return value * 100.0 - card.net_debit


def shock_text(
    underlying: str, store: SnapshotStore, journal: Journal, now: datetime, shock_pct: float
) -> str:
    found = store.read_latest(underlying)
    if found is None:
        return f"shock {underlying}: no snapshot stored"
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(underlying)
    if bar is None:
        return f"shock {underlying}: needs a stored bar for spot"
    spot = bar.close
    shocked = spot * (1.0 + shock_pct / 100.0)
    profile = compute_gex(snapshot, snapshot_id, spot=spot)
    lines = [f"{underlying} shock {shock_pct:+.1f}%: spot {spot:g} -> {shocked:g}"]
    flip = profile.flip_point
    if flip is None:
        lines.append("flip: none in chain")
    elif min(spot, shocked) <= flip <= max(spot, shocked):
        lines.append(f"spot crosses the flip {flip:g}")
    else:
        side = "above" if flip > shocked else "below"
        lines.append(f"flip {flip:g} holds ({side} shocked spot)")
    walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))
    low, high = (shocked, spot) if shocked < spot else (spot, shocked)
    broken = [
        (strike, value) for strike, value in walls
        if low < strike < high or (strike == shocked and shocked != spot)
    ][:5]
    if broken:
        hips = ", ".join(f"{strike:g} ({compact_dollars(value)})" for strike, value in broken)
        lines.append(f"walls broken: {hips}")
    else:
        lines.append("walls broken: none")
    strikes = sorted(profile.per_strike_exposure)
    below = [s for s in strikes if s < shocked]
    above = [s for s in strikes if s > shocked]
    near_low = f"{below[-1]:g}" if below else "none"
    near_high = f"{above[0]:g}" if above else "none"
    lines.append(f"nearest walls: {near_low} below / {near_high} above")
    lines.append(f"regime {('long-gamma' if profile.total_exposure >= 0.0 else 'short-gamma')} (positions unchanged)")
    cards = query_trade_cards(underlying, underlying, store, journal, now, env={})
    if cards is None:
        if store.read_profile() is None:
            lines.append(f"card P&L: {PROFILE_REQUIRED}")
        else:
            lines.append("card P&L: options blocked by profile")
        return "\n".join(lines)
    if not cards:
        lines.append("card P&L: no open cards")
        return "\n".join(lines)
    lines.append(f"card P&L at {shocked:g} (intrinsic, vs debit paid):")
    for card in cards:
        pnl = shock_card_pnl(card, shocked)
        if pnl is None:
            lines.append(f"{card.spread} {card.expiry}: n/a (no debit)")
        else:
            lines.append(f"{card.spread} {card.expiry}: {signed_dollars(pnl)}")
    return "\n".join(lines)
