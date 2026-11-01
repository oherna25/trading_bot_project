from __future__ import annotations

import re
from datetime import datetime

from bot.commands import open_book
from bot.diff import compact_dollars, signed_dollars
from bot.exposure import compute_gex
from bot.ivrank import bs_price, expiry_years, implied_volatility
from bot.journal import Journal
from bot.shapes import ChainSnapshot
from bot.store import SnapshotStore, need_snapshot_text
from bot.tradecards import PROFILE_REQUIRED, TradeCard, query_trade_cards

SHOCK_USAGE = "shock needs a percent, e.g. shock SPY -2%"
OPTION_MULTIPLIER = 100.0
MIN_SHOCK_IV = 0.005
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


def shocked_iv(iv: float, shock_pct: float, iv_shock_pct: float | None = None) -> float:
    if iv_shock_pct is not None:
        return max(MIN_SHOCK_IV, iv * (1.0 + iv_shock_pct / 100.0))
    return max(MIN_SHOCK_IV, iv * (1.0 - shock_pct / 100.0))


def _entry_iv(entry: dict, snapshot: ChainSnapshot, spot: float, now: datetime) -> float | None:
    for contract in snapshot.contracts_list:
        if (
            contract.strike != entry["strike"]
            or contract.expiry != entry["expiry"]
            or contract.call_or_put != entry["kind"]
        ):
            continue
        if contract.iv is not None and contract.iv > 0:
            return contract.iv
        mid: float | None
        if contract.bid is not None and contract.ask is not None:
            mid = (contract.bid + contract.ask) / 2.0
        else:
            mid = contract.price
        years = expiry_years(contract.expiry, now.date())
        if mid is None or years is None or years <= 0:
            return None
        return implied_volatility(mid, spot, contract.strike, years, contract.call_or_put)
    return None


def shock_position_line(
    entry: dict,
    snapshot: ChainSnapshot,
    spot: float,
    shocked: float,
    shock_pct: float,
    now: datetime,
    iv_shock_pct: float | None = None,
) -> tuple[str, float] | None:
    net = float(entry["net"])
    if entry["kind"] == "stock":
        total = (shocked - spot) * net
        return f"{entry['symbol']} stock x{net:g}: {spot:g} -> {shocked:g} ({signed_dollars(total)})", total
    years = expiry_years(str(entry["expiry"]), now.date())
    if years is None or years <= 0:
        return None
    iv = _entry_iv(entry, snapshot, spot, now)
    if iv is None:
        return None
    current = bs_price(spot, float(entry["strike"]), years, iv, str(entry["kind"]))
    stressed = bs_price(shocked, float(entry["strike"]), years, shocked_iv(iv, shock_pct, iv_shock_pct),
                         str(entry["kind"]))
    if current is None or stressed is None:
        return None
    total = (stressed - current) * net * OPTION_MULTIPLIER
    label = f"{entry['symbol']} {entry['kind']} {entry['strike']} {entry['expiry']} x{net:g}"
    return f"{label}: ${current:.2f} -> ${stressed:.2f} ({signed_dollars(total)})", total


def shock_text(
    underlying: str, store: SnapshotStore, journal: Journal, now: datetime, shock_pct: float,
    iv_shock_pct: float | None = None,
) -> str:
    found = store.read_latest(underlying)
    if found is None:
        return f"shock {underlying}: {need_snapshot_text(underlying, store)}"
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(underlying)
    if bar is None:
        return f"shock {underlying}: needs a stored bar for spot -- say load {underlying} to fetch bars"
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
    lines.append("flip and walls assume static gamma (approximation)")
    book = open_book(journal)
    mine = [entry for entry in book if str(entry["symbol"]).upper() == underlying.upper()]
    others = len(book) - len(mine)
    if not mine:
        lines.append("positions under shock: no open positions in journal")
    else:
        lines.append("positions under shock (Black-Scholes, qty from fills):")
        skipped = 0
        total = 0.0
        for entry in mine:
            priced = shock_position_line(
                entry, snapshot, spot, shocked, shock_pct, now, iv_shock_pct
            )
            if priced is None:
                skipped += 1
                continue
            line, pnl = priced
            total += pnl
            lines.append(line)
        lines.append(f"positions total: {signed_dollars(total)}")
        if skipped:
            lines.append(f"positions skipped (no IV or expiry): {skipped}")
    if others:
        lines.append(f"positions in other symbols skipped: {others}")
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
        card_pnl = shock_card_pnl(card, shocked)
        if card_pnl is None:
            lines.append(f"{card.spread} {card.expiry}: n/a (no debit)")
        else:
            lines.append(f"{card.spread} {card.expiry}: {signed_dollars(card_pnl)}")
    return "\n".join(lines)
