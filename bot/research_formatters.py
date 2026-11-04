"""Brief and exposure formatting over engine profiles."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Optional

from bot.diff import compact_dollars, signed_dollars
from bot.exposure import compute_gex
from bot.shapes import (
    Bar,
    ChainSnapshot,
    CharmProfile,
    DexProfile,
    GexProfile,
    Signal,
    VannaProfile,
    VexProfile,
)
from bot.store import SnapshotStore

def derive_display(signal: Signal, snapshot: ChainSnapshot, profile: GexProfile, bar: Optional[Bar]) -> Signal:
    contracts = snapshot.contracts_list
    checks = []
    if contracts:
        checks.append("has_contracts")
    if any(c.gamma is not None for c in contracts):
        checks.append("has_greeks")
    if profile.flip_point is not None:
        checks.append("flip_found")
    coverage = sum(1 for c in contracts if c.gamma is not None) / len(contracts) if contracts else 0.0
    aligned = (signal.direction == "long" and profile.total_exposure >= 0.0) or (
        signal.direction == "short" and profile.total_exposure < 0.0
    )
    if profile.flip_point is not None and bar is not None:
        entry_band = f"{abs(bar.close - profile.flip_point):.1f} points from flip"
    else:
        entry_band = "unknown"
    return replace(
        signal,
        status="aligned" if aligned else "watch",
        trigger=f"flip {profile.flip_point}" if profile.flip_point is not None else "none",
        entry_band=entry_band,
        checklist=tuple(checks),
        coverage=coverage,
    )


def _money(value: float) -> str:
    return compact_dollars(value)


def _gex_so_what(profile: GexProfile, spot: Optional[float]) -> str:
    regime = "long-gamma" if profile.total_exposure >= 0.0 else "short-gamma"
    dealers = "dampen" if regime == "long-gamma" else "amplify"
    bits = [f"{regime}: dealers {dealers} moves"]
    walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))
    if walls:
        strike = walls[0][0]
        if spot:
            dist = abs(strike - spot) / spot * 100.0
            pin = "pinned near " if dist <= 0.5 else ""
            bits.append(f"nearest magnet: {pin}{strike:g} ({dist:.1f}% away)")
        else:
            bits.append(f"nearest magnet: {strike:g}")
    if profile.flip_point is not None and spot:
        side = "above" if spot >= profile.flip_point else "below"
        bits.append(f"spot sits {side} flip {profile.flip_point:.1f}")
    return "so what: " + "; ".join(bits) + "."


def _exposure_so_what(kind: str, profile: VexProfile | DexProfile | CharmProfile | VannaProfile) -> str:
    walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))
    heavy = f"; heaviest strike {walls[0][0]:g}" if walls else ""
    if kind == "VEX":
        return f"so what: a 1pt IV rise is worth {compact_dollars(profile.total_exposure)} across the chain{heavy}."
    if kind == "CHARM":
        return f"so what: charm bleeds {compact_dollars(profile.total_exposure)} delta per day{heavy}."
    if kind == "VANNA":
        return f"so what: a 1pt IV rise shifts delta by {compact_dollars(profile.total_exposure)} share-equivalents{heavy}."
    return f"so what: net {compact_dollars(profile.total_exposure)} share-equivalents{heavy}."


def _format_age(age: timedelta) -> str:
    seconds = max(0, int(age.total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def format_brief(
    profile: GexProfile,
    signal: Signal,
    snapshot: ChainSnapshot,
    bar: Optional[Bar] = None,
    now: Optional[datetime] = None,
    previous: Optional[GexProfile] = None,
) -> str:
    spot = bar.close if bar is not None else None
    age = _format_age(now - snapshot.timestamp_utc) if now is not None else "unknown"
    regime = "long-gamma" if profile.total_exposure >= 0.0 else "short-gamma"
    units = "$GEX per 1% move" if profile.spot else "raw gamma x OI (no spot)"
    spot_text = f"{spot:.2f}" if spot is not None else "unknown"
    stale_quote = False
    if spot is not None and bar is not None and now is not None:
        quote_age = now - bar.timestamp_utc
        snap_age = now - snapshot.timestamp_utc
        spot_text += f" (quote {_format_age(quote_age)})"
        stale_quote = quote_age > timedelta(minutes=15) and quote_age > 3 * snap_age
    lines = [
        f"{snapshot.underlying} GEX brief -- snapshot {profile.snapshot_id} (age {age}, source {snapshot.source_name})",
        f"spot {spot_text} | coverage {signal.coverage * 100.0:.0f}% | regime {regime}",
    ]
    if stale_quote:
        lines.append("STALE -- refresh the quote before trading")
    front = f"; front {profile.front_expiry}: {_money(profile.front_exposure)}" if profile.front_expiry else ""
    lines.append(f"total exposure: {_money(profile.total_exposure)} ({units}{front})")
    if profile.flip_point is not None and spot:
        pct = (profile.flip_point - spot) / spot * 100.0
        side = "above" if profile.flip_point >= spot else "below"
        lines.append(f"flip {profile.flip_point:.1f} ({abs(pct):.1f}% {side} spot)")
    elif profile.flip_point is not None:
        lines.append(f"flip {profile.flip_point:.1f} (no spot)")
    else:
        lines.append("flip none found")
    lines.append(f"calls {_money(profile.call_exposure)} / puts {_money(profile.put_exposure)}")
    walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))[:10]
    lines.append("walls: " + "; ".join(f"{strike:g} {_money(value)}" for strike, value in walls))
    lines.append(_gex_so_what(profile, spot))
    if previous is None:
        lines.append("change vs previous: n/a (first snapshot)")
    else:
        delta = profile.total_exposure - previous.total_exposure
        if previous.total_exposure != 0:
            if (previous.total_exposure >= 0.0) != (profile.total_exposure >= 0.0):
                lines.append(f"change vs previous: {_money(delta)} (sign flip)")
            else:
                pct = delta / abs(previous.total_exposure) * 100.0
                note = ""
                if previous.total_exposure < 0:
                    if delta > 0:
                        note = ", less negative"
                    elif delta < 0:
                        note = ", more negative"
                lines.append(f"change vs previous: {_money(delta)} ({pct:+.1f}%{note})")
        else:
            lines.append(f"change vs previous: {_money(delta)}")
    basis = ""
    if (profile.flip_point is None or bar is None) and signal.direction != "flat":
        basis = " (regime fallback)"
    lines.append(f"direction: {signal.direction} confidence: {signal.confidence:.2f}{basis}")
    lines.append(f"status: {signal.status} trigger: {signal.trigger}")
    return "\n".join(lines)


SPARK_LEVELS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    low, high = min(values), max(values)
    if high == low:
        return SPARK_LEVELS[0] * len(values)
    return "".join(
        SPARK_LEVELS[min(7, int((value - low) / (high - low) * 7))] for value in values
    )


def wall_migration_text(underlying: str, store: SnapshotStore, now: datetime) -> str:
    snaps = [
        (snapshot_id, snapshot)
        for snapshot_id, snapshot in store.snapshots_in_order()
        if snapshot.underlying == underlying and snapshot.timestamp_utc.date() == now.date()
    ]
    if len(snaps) < 2:
        return ""
    bar = store.read_latest_bar(underlying)
    spot = bar.close if bar is not None else None
    profiles = [compute_gex(snapshot, snapshot_id, spot=spot) for snapshot_id, snapshot in snaps]
    first, last = profiles[0], profiles[-1]
    deltas = sorted(
        (
            (
                strike,
                last.per_strike_exposure.get(strike, 0.0) - first.per_strike_exposure.get(strike, 0.0),
            )
            for strike in set(first.per_strike_exposure) | set(last.per_strike_exposure)
        ),
        key=lambda item: -abs(item[1]),
    )[:3]
    lines = []
    movers = [(strike, delta) for strike, delta in deltas if delta != 0.0]
    if movers:
        lines.append("walls since the open:")
        for strike, delta in movers:
            before = first.per_strike_exposure.get(strike, 0.0)
            after = last.per_strike_exposure.get(strike, 0.0)
            lines.append(f"  {strike:g}: {_money(before)} -> {_money(after)} ({signed_dollars(delta)})")
    flips = [profile.flip_point for profile in profiles if profile.flip_point is not None]
    if flips:
        lines.append("flip path: " + " -> ".join(f"{flip:.1f}" for flip in flips))
    totals = [profile.total_exposure for profile in profiles]
    lines.append(f"day exposure: {_sparkline(totals)} {_money(totals[0])} -> {_money(totals[-1])}")
    return "\n".join(lines)


def previous_profile(
    store: SnapshotStore, underlying: str, snapshot_id: str, spot: Optional[float] = None
) -> Optional[GexProfile]:
    found = store.read_previous(underlying, snapshot_id)
    if found is None:
        return None
    prev_id, prev_snapshot = found
    return compute_gex(prev_snapshot, prev_id, spot=spot)


def format_exposure_brief(
    kind: str,
    profile: VexProfile | DexProfile | CharmProfile | VannaProfile,
    snapshot: ChainSnapshot,
    bar: Optional[Bar],
    now: datetime,
) -> str:
    spot = bar.close if bar is not None else None
    age = _format_age(now - snapshot.timestamp_utc)
    units = {
        "VEX": "$ per vol point",
        "DEX": "delta x OI x 100",
        "CHARM": "delta bleed per day",
        "VANNA": "delta per vol point",
    }.get(kind, "delta x OI x 100")
    greek_attr = {"VEX": "vega", "DEX": "delta", "CHARM": "charm", "VANNA": "vanna"}.get(kind)
    contracts = snapshot.contracts_list
    if greek_attr is not None and contracts:
        coverage = sum(1 for c in contracts if getattr(c, greek_attr) is not None) / len(contracts)
    else:
        coverage = 0.0 if not contracts else 1.0
    spot_text = f"{spot:.2f}" if spot is not None else "unknown"
    walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))[:10]
    lines = [
        f"{snapshot.underlying} {kind} brief -- snapshot {profile.snapshot_id} (age {age}, source {snapshot.source_name})",
        f"spot {spot_text} | coverage {coverage * 100.0:.0f}%",
        f"total exposure: {compact_dollars(profile.total_exposure)} ({units})",
    ]
    if greek_attr is not None and contracts and coverage == 0.0:
        lines.append(
            f"note: no {greek_attr} Greeks in snapshot {profile.snapshot_id} "
            f"(sandbox carries no Greeks) -- walls $0 mean unknown, not flat"
        )
    lines.extend([
        "walls: " + "; ".join(f"{strike:g} {compact_dollars(value)}" for strike, value in walls),
        _exposure_so_what(kind, profile),
    ])
    return "\n".join(lines)


def format_bars_brief(symbol: str, bars: list[Bar], now: datetime) -> str:
    """Price-action brief over stored daily bars; no options snapshot needed."""
    ordered = sorted(bars, key=lambda bar: bar.timestamp_utc)
    first, last = ordered[0], ordered[-1]
    closes = [bar.close for bar in ordered]
    period_pct = (last.close - first.close) / first.close * 100.0 if first.close else 0.0
    day_pct = [
        (bar.close - bar.open) / bar.open * 100.0 if bar.open else 0.0 for bar in ordered
    ]
    up_days = sum(1 for pct in day_pct if pct > 0)
    best = max(day_pct)
    worst = min(day_pct)
    avg_volume = sum(bar.volume for bar in ordered) / len(ordered)
    age = _format_age(now - last.timestamp_utc) if now >= last.timestamp_utc else "0m"
    direction = "up" if period_pct >= 0 else "down"
    lines = [
        f"{symbol} price brief -- {len(ordered)} daily bars "
        f"{first.timestamp_utc.date()} -> {last.timestamp_utc.date()} (quote age {age})",
        f"last {last.close:.2f} ({day_pct[-1]:+.2f}% on the day) | "
        f"period {first.close:.2f} -> {last.close:.2f} ({period_pct:+.2f}%)",
        f"range {min(closes):.2f} - {max(closes):.2f} | "
        f"best day {best:+.2f}% / worst day {worst:+.2f}% | up days {up_days}/{len(ordered)}",
        f"volume avg {avg_volume:,.0f} vs last {last.volume:,.0f}",
        f"closes: {_sparkline(closes)}",
        f"so what: {direction} {abs(period_pct):.1f}% over {len(ordered)} sessions "
        f"with {up_days} up days.",
        f"no options snapshot stored -- ideas and GEX need one: load {symbol} "
        f"(needs TRADIER_ACCESS_TOKEN or MARKETDATA_API_KEY)",
    ]
    return "\n".join(lines)


def format_intraday_brief(symbol: str, bars: list[Bar], now: datetime, source: str) -> str:
    """Session-by-session brief over one week of 15m bars."""
    ordered = sorted(bars, key=lambda bar: bar.timestamp_utc)
    sessions: dict[str, list[Bar]] = {}
    for bar in ordered:
        sessions.setdefault(bar.timestamp_utc.date().isoformat(), []).append(bar)
    days = sorted(sessions)
    first_open = sessions[days[0]][0].open
    last = ordered[-1]
    week_pct = (last.close - first_open) / first_open * 100.0 if first_open else 0.0
    lines = [
        f"{symbol} intraday brief -- {ordered[0].interval} x {len(ordered)} "
        f"over {len(days)} sessions {days[0]} -> {days[-1]} ({source})",
        f"last {last.close:.2f} at {last.timestamp_utc:%m-%d %H:%M} UTC | "
        f"week {first_open:.2f} -> {last.close:.2f} ({week_pct:+.2f}%)",
    ]
    prev_close: float | None = None
    for day in days[-5:]:
        day_bars = sessions[day]
        day_open = day_bars[0].open
        day_close = day_bars[-1].close
        base = prev_close if prev_close is not None else day_open
        day_pct = (day_close - base) / base * 100.0 if base else 0.0
        lines.append(
            f"{day[5:]}: {day_open:.2f} -> {day_close:.2f} ({day_pct:+.2f}%) | "
            f"range {min(b.low for b in day_bars):.2f} - {max(b.high for b in day_bars):.2f} | "
            f"vol {sum(b.volume for b in day_bars):,.0f}"
        )
        prev_close = day_close
    lines.append(f"closes: {_sparkline([bar.close for bar in ordered])}")
    return "\n".join(lines)
