from __future__ import annotations

from datetime import datetime
from typing import Any

from bot.diff import compact_dollars
from bot.exposure import compute_gex
from bot.shapes import GexProfile, Holding
from bot.store import SnapshotStore

KINDS = ("stock", "call", "put")
SIDES = ("long", "short")


def parse_holdings(raw: Any) -> list[Holding]:
    if not isinstance(raw, list):
        raise ValueError("holdings must be a JSON list")
    holdings = []
    for index, item in enumerate(raw):
        label = f"holding {index}"
        if not isinstance(item, dict):
            raise ValueError(f"{label}: must be an object")
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            raise ValueError(f"{label}: missing symbol")
        kind = str(item.get("kind", "")).strip().lower()
        if kind not in KINDS:
            raise ValueError(f"{label}: kind must be one of {', '.join(KINDS)}")
        side = str(item.get("side", "")).strip().lower()
        if side not in SIDES:
            raise ValueError(f"{label}: side must be one of {', '.join(SIDES)}")
        try:
            qty = float(item.get("qty", 1.0))
        except (TypeError, ValueError):
            raise ValueError(f"{label}: qty must be a number") from None
        if qty <= 0:
            raise ValueError(f"{label}: qty must be positive")
        strike = item.get("strike")
        if kind == "stock":
            strike_value = None
        else:
            if strike is None:
                raise ValueError(f"{label}: {kind} needs a strike")
            try:
                strike_value = float(strike)
            except (TypeError, ValueError):
                raise ValueError(f"{label}: strike must be a number") from None
            if strike_value <= 0:
                raise ValueError(f"{label}: strike must be positive")
        expiry = item.get("expiry")
        holdings.append(
            Holding(
                symbol=symbol, kind=kind, side=side, strike=strike_value,
                expiry=None if expiry is None else str(expiry), qty=qty,
            )
        )
    return holdings


def describe_holding(holding: Holding) -> str:
    if holding.kind == "stock":
        return f"{holding.side.upper()} {holding.qty:g} {holding.symbol} stock"
    bits = f"{holding.side.upper()} {holding.qty:g} {holding.symbol} {holding.kind} {holding.strike:g}"
    if holding.expiry is not None:
        bits += f" {holding.expiry}"
    return bits


def _moneyness(kind: str, strike: float, spot: float) -> str:
    edge = spot - strike if kind == "call" else strike - spot
    if edge >= 0:
        return f"${edge:.0f} ITM"
    return f"${-edge:.0f} OTM"


def _flip_bit(ref: float, ref_name: str, flip: float | None) -> str:
    if flip is None:
        return "no flip in chain"
    gap = ref - flip
    where = "above" if gap >= 0 else "below"
    return f"{ref_name} ${abs(gap):.1f} {where} the {flip:g} flip"


def _stock_line(holding: Holding, profile: GexProfile, spot: float) -> str:
    walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))
    wall_bit = "no walls"
    if walls:
        strike, value = walls[0]
        wall_bit = f"top wall {strike:g} at {compact_dollars(value)}"
    return f"{describe_holding(holding)} @ spot {spot:g}: {_flip_bit(spot, 'spot', profile.flip_point)}; {wall_bit}"


def _option_line(holding: Holding, profile: GexProfile, spot: float) -> str:
    assert holding.strike is not None
    wall = profile.per_strike_exposure.get(holding.strike, 0.0)
    return (
        f"{describe_holding(holding)}: "
        f"{_flip_bit(holding.strike, 'strike', profile.flip_point)}; "
        f"{_moneyness(holding.kind, holding.strike, spot)} vs spot {spot:g}; "
        f"wall at {holding.strike:g}: {compact_dollars(wall)}"
    )


def save_holdings_text(raw: Any, store: SnapshotStore, journal: Any, now: datetime) -> str:
    holdings = parse_holdings(raw)
    store.save_holdings(holdings)
    journal.append(now, "holdings_saved", {"count": len(holdings)})
    if not holdings:
        return "holdings cleared"
    lines = [f"saved {len(holdings)} holding{'s' if len(holdings) != 1 else ''}:"]
    lines.extend(describe_holding(holding) for holding in holdings)
    return "\n".join(lines)


def holdings_text(store: SnapshotStore) -> str:
    holdings = store.read_holdings()
    if not holdings:
        return "holdings: none saved (set them with the set_holdings tool)"
    lines = ["your book vs current positioning:"]
    for holding in holdings:
        found = store.read_latest(holding.symbol)
        bar = store.read_latest_bar(holding.symbol)
        if found is None or bar is None:
            lines.append(f"{describe_holding(holding)}: no snapshot stored")
            continue
        snapshot_id, snapshot = found
        profile = compute_gex(snapshot, snapshot_id, spot=bar.close)
        if holding.kind == "stock":
            lines.append(_stock_line(holding, profile, bar.close))
        else:
            lines.append(_option_line(holding, profile, bar.close))
    return "\n".join(lines)
