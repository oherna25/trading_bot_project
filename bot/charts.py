from __future__ import annotations

import html
from datetime import datetime
from typing import Optional

from bot.exposure import compute_gex
from bot.shapes import Bar, ChainSnapshot, GexProfile
from bot.store import SnapshotStore

CHART_WIDTH = 920
CHART_HEIGHT = 420
CHART_REFRESH_SECONDS = 60
CHART_MAX_STRIKES = 24
CALL_COLOR = "#1a7f37"
PUT_COLOR = "#cf222e"
AXIS_COLOR = "#57606a"
FLIP_COLOR = "#24292f"


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude >= 1e9:
        return f"{sign}${magnitude / 1e9:.1f}B"
    if magnitude >= 1e6:
        return f"{sign}${magnitude / 1e6:.0f}M"
    if magnitude >= 1e3:
        return f"{sign}${magnitude / 1e3:.0f}K"
    return f"{sign}${magnitude:.0f}"


def chart_strikes(profile: GexProfile, max_strikes: int = CHART_MAX_STRIKES) -> list[float]:
    ranked = sorted(
        profile.per_strike_exposure, key=lambda strike: -abs(profile.per_strike_exposure[strike])
    )
    return sorted(ranked[:max(1, max_strikes)])


def gamma_svg(profile: GexProfile, width: int = CHART_WIDTH, height: int = CHART_HEIGHT) -> str:
    strikes = chart_strikes(profile)
    calls = [profile.per_strike_call_exposure.get(strike, 0.0) for strike in strikes]
    puts = [profile.per_strike_put_exposure.get(strike, 0.0) for strike in strikes]
    pos_max = max([value for value in calls + puts if value > 0] + [0.0])
    neg_max = max([-value for value in calls + puts if value < 0] + [0.0])
    if pos_max <= 0 and neg_max <= 0:
        pos_max = neg_max = 1.0
    elif pos_max <= 0:
        pos_max = neg_max
    elif neg_max <= 0:
        neg_max = pos_max
    pos_peak = pos_max * 1.1
    neg_peak = neg_max * 1.1
    span = pos_peak + neg_peak
    left, right, top, bottom = 76, 16, 18, 44
    plot_width = width - left - right
    plot_height = height - top - bottom

    def y_of(value: float) -> float:
        return top + (pos_peak - value) / span * plot_height

    def x_of(strike: float) -> float:
        if len(strikes) == 1:
            return left + plot_width / 2
        span = strikes[-1] - strikes[0]
        if span == 0:
            return left + plot_width / 2
        return left + (strike - strikes[0]) / span * plot_width

    zero_y = y_of(0.0)
    slot = plot_width / len(strikes)
    bar_width = max(2.0, min(slot * 0.6, 48.0))
    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif">'
    ]
    for value, label in (
        (pos_max, _money(pos_max)),
        (pos_max / 2, _money(pos_max / 2)),
        (0.0, "$0"),
        (-neg_max / 2, _money(-neg_max / 2)),
        (-neg_max, _money(-neg_max)),
    ):
        y = y_of(value)
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            f'stroke="{AXIS_COLOR}" stroke-width="{1.5 if value == 0 else 0.5}" opacity="0.5"/>'
        )
        parts.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="12" '
            f'fill="{AXIS_COLOR}">{label}</text>'
        )
    for strike, call, put in zip(strikes, calls, puts):
        center = x_of(strike)
        if call > 0:
            y = y_of(call)
            parts.append(
                f'<rect x="{center - bar_width / 2:.1f}" y="{y:.1f}" width="{bar_width:.1f}" '
                f'height="{zero_y - y:.1f}" fill="{CALL_COLOR}"><title>{strike:g} calls '
                f'{_money(call)}</title></rect>'
            )
        if put < 0:
            y = y_of(put)
            parts.append(
                f'<rect x="{center - bar_width / 2:.1f}" y="{zero_y:.1f}" width="{bar_width:.1f}" '
                f'height="{y - zero_y:.1f}" fill="{PUT_COLOR}"><title>{strike:g} puts '
                f'{_money(put)}</title></rect>'
            )
    step = max(1, len(strikes) // 12)
    for index, strike in enumerate(strikes):
        if index % step:
            continue
        parts.append(
            f'<text x="{x_of(strike):.1f}" y="{height - 24}" text-anchor="middle" font-size="12" '
            f'fill="{AXIS_COLOR}">{strike:g}</text>'
        )
    if profile.flip_point is not None:
        x = min(max(x_of(profile.flip_point), left), width - right)
        parts.append(
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_height}" '
            f'stroke="{FLIP_COLOR}" stroke-width="2" stroke-dasharray="6 4">'
            f'<title>flip {profile.flip_point:.1f}</title></line>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{top + 14}" text-anchor="middle" font-size="12" '
            f'fill="{FLIP_COLOR}">flip {profile.flip_point:.1f}</text>'
        )
    if profile.spot is not None:
        x = min(max(x_of(profile.spot), left), width - right)
        parts.append(
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_height}" '
            f'stroke="{AXIS_COLOR}" stroke-width="1.5" stroke-dasharray="2 3">'
            f'<title>spot {profile.spot:.2f}</title></line>'
        )
    parts.append(
        f'<text x="{left}" y="{height - 6}" font-size="12" fill="{CALL_COLOR}">'
        f"call walls</text>"
        f'<text x="{left + 90}" y="{height - 6}" font-size="12" fill="{PUT_COLOR}">'
        f"put walls</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


def gamma_chart_html(
    underlying: str,
    snapshot: ChainSnapshot,
    profile: GexProfile,
    bar: Optional[Bar],
    now: Optional[datetime] = None,
) -> str:
    regime = "long-gamma" if profile.total_exposure >= 0.0 else "short-gamma"
    spot = f"{bar.close:.2f}" if bar is not None else "unknown"
    flip = f"{profile.flip_point:.1f}" if profile.flip_point is not None else "none"
    age = ""
    if now is not None:
        seconds = max(0, int((now - snapshot.timestamp_utc).total_seconds()))
        age = f" age {seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f" age {seconds}s"
    title = f"{underlying} gamma by strike"
    style = (
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "margin:2rem;color:#24292f;background:#fff}"
        "h1{font-size:1.4rem;margin:0 0 .25rem}"
        "p.stats{margin:.25rem 0 1rem;color:#57606a}"
        "p.meta{margin:1rem 0 0;color:#57606a;font-size:.85rem}"
    )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<meta http-equiv=\"refresh\" content=\"{CHART_REFRESH_SECONDS}\">"
        f"<title>{html.escape(title)}</title><style>{style}</style></head><body>"
        f"<h1>{html.escape(title)}</h1>"
        f"<p class=\"stats\">spot {html.escape(spot)} | total {_money(profile.total_exposure)} "
        f"| flip {html.escape(flip)} | {html.escape(regime)}</p>"
        f"{gamma_svg(profile)}"
        f"<p class=\"meta\">snapshot {html.escape(profile.snapshot_id)}{html.escape(age)} "
        f"source {html.escape(snapshot.source_name)} | refreshes every "
        f"{CHART_REFRESH_SECONDS}s</p>"
        "</body></html>"
    )


def board_rows(store: SnapshotStore) -> list[dict[str, object]]:
    rows = []
    for symbol in sorted(store.list_underlyings()):
        found = store.read_latest(symbol)
        if found is None:
            continue
        snapshot_id, snapshot = found
        bar = store.read_latest_bar(symbol)
        spot = bar.close if bar is not None else None
        profile = compute_gex(snapshot, snapshot_id, spot=spot)
        walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))
        strike, wall = walls[0] if walls else (None, None)
        flip_distance_pct = None
        if profile.flip_point is not None and spot:
            flip_distance_pct = (profile.flip_point - spot) / spot * 100.0
        rows.append(
            {
                "symbol": symbol,
                "regime": "long-gamma" if profile.total_exposure >= 0.0 else "short-gamma",
                "total": profile.total_exposure,
                "spot": spot,
                "flip": profile.flip_point,
                "flip_distance_pct": flip_distance_pct,
                "wall_strike": strike,
                "wall_value": wall,
                "snapshot_id": snapshot_id,
            }
        )
    return rows


def regime_board_html(store: SnapshotStore, now: datetime | None = None) -> str:
    del now
    title = "Dealer positioning board"
    style = (
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "margin:2rem;color:#24292f;background:#fff}"
        "h1{font-size:1.4rem;margin:0 0 1rem}"
        "table{border-collapse:collapse;min-width:52rem}"
        "th,td{border:1px solid #d0d7de;padding:.4rem .7rem;text-align:right;font-variant-numeric:tabular-nums}"
        "th{background:#f6f8fa;text-align:right}"
        "td:first-child,th:first-child{text-align:left}"
        ".long{color:#1a7f37}.short{color:#cf222e}"
    )
    head = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<meta http-equiv=\"refresh\" content=\"{CHART_REFRESH_SECONDS}\">"
        f"<title>{html.escape(title)}</title><style>{style}</style></head><body>"
        f"<h1>{html.escape(title)}</h1>"
    )
    rows = board_rows(store)
    if not rows:
        return head + "<p>no snapshots stored</p></body></html>"
    cells = [
        "<table><tr><th>symbol</th><th>regime</th><th>total gex</th><th>spot</th>"
        "<th>flip</th><th>flip dist %</th><th>biggest wall</th></tr>"
    ]
    for row in rows:
        regime = str(row["regime"])
        cls = "long" if regime == "long-gamma" else "short"
        dist = row["flip_distance_pct"]
        dist_text = f"{dist:+.1f}%" if dist is not None else "n/a"
        spot = row["spot"]
        spot_text = f"{spot:.2f}" if spot is not None else "n/a"
        flip = row["flip"]
        flip_text = f"{flip:.1f}" if flip is not None else "n/a"
        strike, wall = row["wall_strike"], row["wall_value"]
        wall_text = f"{strike:g} {_money(float(wall))}" if strike is not None else "n/a"
        cells.append(
            f"<tr><td>{html.escape(str(row['symbol']))}</td>"
            f"<td class=\"{cls}\">{regime}</td>"
            f"<td>{_money(float(row['total']))}</td><td>{spot_text}</td>"
            f"<td>{flip_text}</td><td>{dist_text}</td><td>{wall_text}</td></tr>"
        )
    cells.append("</table>")
    return head + "".join(cells) + "</body></html>"
