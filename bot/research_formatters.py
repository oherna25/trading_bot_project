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
    DexProfile,
    GexProfile,
    Signal,
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


def _exposure_so_what(kind: str, profile: VexProfile | DexProfile) -> str:
    walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))
    heavy = f"; heaviest strike {walls[0][0]:g}" if walls else ""
    if kind == "VEX":
        return f"so what: a 1pt IV rise is worth {compact_dollars(profile.total_exposure)} across the chain{heavy}."
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
    profile: VexProfile | DexProfile,
    snapshot: ChainSnapshot,
    bar: Optional[Bar],
    now: datetime,
) -> str:
    spot = bar.close if bar is not None else None
    age = _format_age(now - snapshot.timestamp_utc)
    units = "$ per vol point" if kind == "VEX" else "delta x OI x 100"
    spot_text = f"{spot:.2f}" if spot is not None else "unknown"
    walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))[:10]
    lines = [
        f"{snapshot.underlying} {kind} brief -- snapshot {profile.snapshot_id} (age {age}, source {snapshot.source_name})",
        f"spot {spot_text}",
        f"total exposure: {compact_dollars(profile.total_exposure)} ({units})",
        "walls: " + "; ".join(f"{strike:g} {compact_dollars(value)}" for strike, value in walls),
        _exposure_so_what(kind, profile),
    ]
    return "\n".join(lines)
