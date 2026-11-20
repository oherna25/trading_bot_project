from __future__ import annotations

import html
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, NamedTuple, Optional
from urllib.parse import quote as _urlencode

from bot.diff import compact_dollars
from bot.exposure import compute_exposures, compute_gex, compute_gex_grid
from bot.shapes import (
    Bar,
    ChainSnapshot,
    CharmProfile,
    DashboardSettings,
    DexProfile,
    ExposureGrid,
    GexProfile,
    VannaProfile,
    VexProfile,
)
from bot.store import SnapshotStore, empty_store_text

CHART_REFRESH_SECONDS = 60
CHART_MAX_STRIKES = 24
HEATMAP_MAX_EXPIRIES = 8
QUOTE_FRESH_SECONDS = 300
SNAPSHOT_FRESH_SECONDS = 6 * 3600
LIVE_POLL_SECONDS = 5

_LADDER_WIDTH = 920
_ROW_HEIGHT = 24
_BAR_HEIGHT = 14
_HEADER_HEIGHT = 22
_FOOTER_HEIGHT = 8
_LABEL_GUTTER = 58
_TAG_GUTTER = 96
_VALUE_ROOM = 56


def _money(value: float) -> str:
    return compact_dollars(value)


def _amount(value: float, unit: str) -> str:
    if unit == "$":
        return compact_dollars(value)
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude >= 1_000_000_000:
        return f"{sign}{magnitude / 1_000_000_000:.1f}B"
    if magnitude >= 1_000_000:
        return f"{sign}{magnitude / 1_000_000:.0f}M"
    if magnitude >= 1_000:
        return f"{sign}{magnitude / 1_000:.0f}K"
    return f"{sign}{magnitude:.0f}" if magnitude >= 10 or magnitude == 0 else f"{sign}{magnitude:.1f}"


def ladder_strikes(
    magnitudes: Mapping[float, float], spot: Optional[float], max_strikes: int = CHART_MAX_STRIKES
) -> list[float]:
    """Ascending strikes: the contiguous window nearest spot, or the largest by magnitude without a spot."""
    count = max(1, max_strikes)
    if spot is None:
        ranked = sorted(magnitudes, key=lambda strike: -abs(magnitudes[strike]))
    else:
        ranked = sorted(magnitudes, key=lambda strike: (abs(strike - spot), strike))
    return sorted(ranked[:count])


def chart_strikes(profile: GexProfile, max_strikes: int = CHART_MAX_STRIKES) -> list[float]:
    return ladder_strikes(profile.per_strike_exposure, profile.spot, max_strikes)


class _Row(NamedTuple):
    strike: float
    right: float
    left: float


def _wall(values: Mapping[float, float], positive: bool) -> Optional[tuple[float, float]]:
    if not values:
        return None
    if positive:
        strike = max(values, key=lambda key: values[key])
        return (strike, values[strike]) if values[strike] > 0 else None
    strike = min(values, key=lambda key: values[key])
    return (strike, values[strike]) if values[strike] < 0 else None


def _wall_strike(values: Mapping[float, float], positive: bool) -> Optional[float]:
    found = _wall(values, positive)
    return found[0] if found is not None else None


def _marker_y(price: float, strikes_desc: list[float]) -> tuple[float, str]:
    """Row-center y of a price level, interpolated between the rows it falls between."""

    def center(index: int) -> float:
        return _HEADER_HEIGHT + (index + 0.5) * _ROW_HEIGHT

    if price >= strikes_desc[0]:
        return center(0), "above" if price > strikes_desc[0] else ""
    if price <= strikes_desc[-1]:
        return center(len(strikes_desc) - 1), "below" if price < strikes_desc[-1] else ""
    for index in range(len(strikes_desc) - 1):
        high, low = strikes_desc[index], strikes_desc[index + 1]
        if low <= price <= high:
            fraction = (high - price) / (high - low)
            return center(index) + fraction * (center(index + 1) - center(index)), ""
    return center(0), ""


def _ladder_svg(
    rows: list[_Row],
    spot: Optional[float],
    flip: Optional[float],
    walls: tuple[Optional[float], Optional[float]],
    unit: str,
    heads: tuple[str, str],
    tips: tuple[str, str],
    label: str,
) -> str:
    width = _LADDER_WIDTH
    ordered = sorted(rows, key=lambda row: -row.strike)
    count = len(ordered)
    if count == 0:
        return (
            f'<svg class="cx ladder" width="100%" viewBox="0 0 {width} 48" role="img" '
            f'aria-label="{html.escape(label, quote=True)}">'
            f'<text class="dim" x="{width / 2:.0f}" y="28" text-anchor="middle">no strikes in this snapshot</text></svg>'
        )
    height = _HEADER_HEIGHT + count * _ROW_HEIGHT + _FOOTER_HEIGHT
    plot_left = _LABEL_GUTTER
    plot_right = width - _TAG_GUTTER
    center_x = (plot_left + plot_right) / 2
    reach = (plot_right - plot_left) / 2 - _VALUE_ROOM
    peak = max([abs(value) for row in ordered for value in (row.right, row.left)] + [0.0])
    if peak <= 0.0:
        peak = 1.0
    near = None
    if spot is not None:
        near = min(ordered, key=lambda row: abs(row.strike - spot)).strike
    call_wall, put_wall = walls
    parts = [
        f'<svg class="cx ladder" width="100%" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{html.escape(label, quote=True)}">',
        f'<text class="dim" x="{center_x - 6:.1f}" y="14" text-anchor="end">{html.escape(heads[0])}</text>',
        f'<text class="dim" x="{center_x + 6:.1f}" y="14">{html.escape(heads[1])}</text>',
    ]
    bars: list[str] = []
    for index, row in enumerate(ordered):
        top = _HEADER_HEIGHT + index * _ROW_HEIGHT
        mid = top + _ROW_HEIGHT / 2
        if row.strike == near:
            parts.append(f'<rect class="band" x="0" y="{top}" width="{width}" height="{_ROW_HEIGHT}"/>')
        parts.append(
            f'<line class="grid" x1="{plot_left}" y1="{top + _ROW_HEIGHT}" '
            f'x2="{plot_right}" y2="{top + _ROW_HEIGHT}"/>'
        )
        emphasized = row.strike in (call_wall, put_wall)
        strike_class = "strong" if row.strike == near or emphasized else "dim"
        parts.append(
            f'<text class="{strike_class}" x="{plot_left - 8}" y="{mid + 4:.1f}" '
            f'text-anchor="end">{row.strike:g}</text>'
        )
        for value, side, tip in ((row.right, "pos", tips[1]), (row.left, "neg", tips[0])):
            if value == 0:
                continue
            length = max(1.0, abs(value) / peak * reach)
            is_wall = (side == "pos" and row.strike == call_wall) or (side == "neg" and row.strike == put_wall)
            x = center_x if side == "pos" else center_x - length
            text_x = center_x + length + 4 if side == "pos" else center_x - length - 4
            anchor = "start" if side == "pos" else "end"
            wall_class = " wall" if is_wall else ""
            text_class = "strong" if is_wall else "dim"
            bars.append(
                f'<rect class="{side}{wall_class}" x="{x:.1f}" y="{mid - _BAR_HEIGHT / 2:.1f}" '
                f'width="{length:.1f}" height="{_BAR_HEIGHT}"><title>{row.strike:g} {tip} '
                f'{_amount(value, unit)}</title></rect>'
                f'<text class="{text_class}" x="{text_x:.1f}" y="{mid + 4:.1f}" '
                f'text-anchor="{anchor}">{_amount(value, unit)}</text>'
            )
    strikes_desc = [row.strike for row in ordered]
    markers: list[tuple[float, str, str, str]] = []
    if spot is not None:
        y, off = _marker_y(spot, strikes_desc)
        markers.append((y, "spot", f"spot {spot:.2f}", off))
    if flip is not None:
        y, off = _marker_y(flip, strikes_desc)
        markers.append((y, "flip", f"flip {flip:.1f}", off))
    tags = []
    # The marker higher on screen labels above its line and the other below, so tags never collide.
    for rank, (y, kind, text, off) in enumerate(sorted(markers, key=lambda marker: marker[0])):
        arrow = {"above": "\u2191 ", "below": "\u2193 "}.get(off, "")
        tag_y = y - 3 if rank == 0 else y + 11
        parts.append(
            f'<line class="{kind}" x1="{plot_left}" y1="{y:.1f}" x2="{plot_right + 8}" y2="{y:.1f}">'
            f'<title>{html.escape(text)}</title></line>'
        )
        tags.append(
            f'<text class="{kind}-tag" x="{width - 4}" y="{tag_y:.1f}" text-anchor="end">'
            f'{arrow}{html.escape(text)}</text>'
        )
    parts.append(
        f'<line class="zero" x1="{center_x:.1f}" y1="{_HEADER_HEIGHT}" '
        f'x2="{center_x:.1f}" y2="{height - _FOOTER_HEIGHT}"/>'
    )
    parts.extend(bars)
    parts.extend(tags)
    parts.append("</svg>")
    return "".join(parts)


def gamma_svg(profile: GexProfile) -> str:
    strikes = chart_strikes(profile)
    rows = [
        _Row(
            strike,
            max(0.0, profile.per_strike_call_exposure.get(strike, 0.0)),
            min(0.0, profile.per_strike_put_exposure.get(strike, 0.0)),
        )
        for strike in strikes
    ]
    walls = (
        _wall_strike(profile.per_strike_call_exposure, True),
        _wall_strike(profile.per_strike_put_exposure, False),
    )
    return _ladder_svg(
        rows, profile.spot, profile.flip_point, walls, "$",
        ("put exposure", "call exposure"), ("puts", "calls"), "gamma exposure by strike",
    )


def net_ladder_svg(
    per_strike: Mapping[float, float], spot: Optional[float], unit: str, label: str
) -> str:
    rows = [
        _Row(strike, max(0.0, per_strike[strike]), min(0.0, per_strike[strike]))
        for strike in ladder_strikes(per_strike, spot)
    ]
    walls = (_wall_strike(per_strike, True), _wall_strike(per_strike, False))
    return _ladder_svg(rows, spot, None, walls, unit, ("negative", "positive"), ("net", "net"), label)


def _legend(*items: tuple[str, str]) -> str:
    keys = "".join(f'<span><i class="{cls}"></i>{html.escape(text)}</span>' for cls, text in items)
    return f'<div class="legend">{keys}</div>'


_GEX_LEGEND = _legend(
    ("pos", "call exposure"), ("neg", "put exposure"), ("wall", "largest wall"),
    ("spot", "spot"), ("flip", "zero gamma"),
)
_NET_LEGEND = _legend(
    ("pos", "positive net"), ("neg", "negative net"), ("wall", "largest in each direction"),
    ("spot", "spot"),
)


def _no_data(what: str) -> str:
    return (
        f'<p class="sub">No {html.escape(what)} in this snapshot. Sandbox chains carry no Greeks, '
        "so zero means unknown, not flat.</p>"
    )


def _gex_panel(profile: GexProfile) -> str:
    if not any(profile.per_strike_exposure.values()):
        return _no_data("gamma exposure")
    return f'<div class="chart-panel">{gamma_svg(profile)}</div>{_GEX_LEGEND}'


@dataclass(frozen=True)
class ChartData:
    snapshot: ChainSnapshot
    spot: Optional[float]
    gex: GexProfile
    vex: VexProfile
    dex: DexProfile
    charm: CharmProfile
    vanna: VannaProfile


@dataclass(frozen=True)
class ChartView:
    key: str
    label: str
    title: str
    render: Callable[[ChartData], str]


def _net_view(
    key: str,
    label: str,
    title: str,
    greek: str,
    unit: str,
    note: str,
    pick: Callable[[ChartData], Mapping[float, float]],
) -> ChartView:
    def render(data: ChartData) -> str:
        values = pick(data)
        if not any(values.values()):
            return _no_data(f"{greek} exposure")
        svg = net_ladder_svg(values, data.spot, unit, title)
        return (
            f'<div class="chart-panel">{svg}</div>{_NET_LEGEND}'
            f'<p class="sub">{html.escape(note)}</p>'
        )

    return ChartView(key, label, title, render)


def _heat_cell(value: float, peak: float, tip: str, unit: str, extra: str = "") -> str:
    if value == 0:
        return f'<td class="cell zero{extra}">&middot;</td>'
    side = "pos" if value > 0 else "neg"
    return (
        f'<td class="cell {side}{extra}" style="--i:{abs(value) / peak:.2f}" '
        f'title="{html.escape(tip, quote=True)}">{_amount(value, unit)}</td>'
    )


def heatmap_html(grid: ExposureGrid, spot: Optional[float]) -> str:
    if not grid.cells:
        return _no_data("gamma exposure")
    totals = {
        strike: sum(grid.cells.get((strike, expiry), 0.0) for expiry in grid.expiries)
        for strike in grid.strikes
    }
    strikes = sorted(ladder_strikes(totals, spot), reverse=True)
    shown = grid.expiries[:HEATMAP_MAX_EXPIRIES]
    later = grid.expiries[HEATMAP_MAX_EXPIRIES:]
    near = min(strikes, key=lambda strike: abs(strike - spot)) if spot is not None else None
    matrix = {
        strike: [grid.cells.get((strike, expiry), 0.0) for expiry in shown]
        + ([sum(grid.cells.get((strike, expiry), 0.0) for expiry in later)] if later else [])
        for strike in strikes
    }
    peak = max([abs(value) for row in matrix.values() for value in row] + [0.0]) or 1.0
    total_peak = max([abs(totals[strike]) for strike in strikes] + [0.0]) or 1.0
    columns = list(shown) + ([f"+{len(later)} later"] if later else [])
    head = "".join(f"<th>{html.escape(column)}</th>" for column in columns)
    body = []
    for strike in strikes:
        tag = '<span class="tag">spot</span>' if strike == near else ""
        cells = "".join(
            _heat_cell(value, peak, f"{strike:g} {column} {_money(value)}", "$")
            for column, value in zip(columns, matrix[strike])
        )
        total = _heat_cell(totals[strike], total_peak, f"{strike:g} all expiries {_money(totals[strike])}", "$", " total")
        row_class = ' class="near"' if strike == near else ""
        body.append(f'<tr{row_class}><td class="k">{strike:g}{tag}</td>{cells}{total}</tr>')
    table = (
        '<div class="table-scroll"><table class="heat"><thead><tr><th>strike</th>'
        f'{head}<th class="total">total</th></tr></thead><tbody>{"".join(body)}</tbody></table></div>'
    )
    note = (
        "Cell tint scales with the cell's share of the largest cell: green is long gamma, red is short. "
        "Total sums every expiry, so it matches the GEX tab."
    )
    return f'{table}<p class="sub">{html.escape(note)}</p>'


def _heatmap_panel(data: ChartData) -> str:
    grid = compute_gex_grid(data.snapshot, data.gex.snapshot_id, spot=data.gex.spot)
    return heatmap_html(grid, data.spot)


CHART_VIEWS: tuple[ChartView, ...] = (
    ChartView("gex", "GEX", "gamma by strike", lambda data: _gex_panel(data.gex)),
    ChartView("heatmap", "Heatmap", "gamma by strike and expiry", _heatmap_panel),
    _net_view(
        "vex", "VEX", "vega exposure by strike", "vega", "$", "Dollars per vol point.",
        lambda data: data.vex.per_strike_exposure,
    ),
    _net_view(
        "dex", "DEX", "delta exposure by strike", "delta", "",
        "Share-equivalents: delta x open interest x contract multiplier.",
        lambda data: data.dex.per_strike_exposure,
    ),
    _net_view(
        "charm", "Charm", "charm exposure by strike", "charm", "",
        "Delta bleed per day, in share-equivalents.",
        lambda data: data.charm.per_strike_exposure,
    ),
    _net_view(
        "vanna", "Vanna", "vanna exposure by strike", "vanna", "",
        "Delta change per vol point, in share-equivalents.",
        lambda data: data.vanna.per_strike_exposure,
    ),
)
DEFAULT_CHART_VIEW = CHART_VIEWS[0]


def chart_view(key: str) -> ChartView:
    return next((view for view in CHART_VIEWS if view.key == key), DEFAULT_CHART_VIEW)


def _tile(label: str, value: str, cls: str = "") -> str:
    attr = f' class="{cls}"' if cls else ""
    return f'<div class="stat"><b{attr}>{html.escape(value)}</b><span>{html.escape(label)}</span></div>'


def chart_stats_html(data: ChartData) -> str:
    profile, spot = data.gex, data.spot
    has_data = bool(profile.per_strike_exposure)
    regime = "long-gamma" if profile.total_exposure >= 0.0 else "short-gamma"
    flip = "n/a"
    if profile.flip_point is not None:
        flip = f"{profile.flip_point:.1f}"
        if spot:
            flip += f" ({(profile.flip_point - spot) / spot * 100.0:+.1f}%)"
    call_wall = _wall(profile.per_strike_call_exposure, True)
    put_wall = _wall(profile.per_strike_put_exposure, False)
    tiles = [
        _tile("spot", f"{spot:.2f}" if spot is not None else "n/a"),
        _tile("net GEX", _money(profile.total_exposure) if has_data else "n/a",
              ("long" if profile.total_exposure >= 0.0 else "short") if has_data else ""),
        _tile("zero gamma", flip),
        _tile("call wall", f"{call_wall[0]:g} {_money(call_wall[1])}" if call_wall else "n/a", "call" if call_wall else ""),
        _tile("put wall", f"{put_wall[0]:g} {_money(put_wall[1])}" if put_wall else "n/a", "put" if put_wall else ""),
        _tile("regime", regime if has_data else "n/a", ("long" if regime == "long-gamma" else "short") if has_data else ""),
    ]
    return f'<div class="stats">{"".join(tiles)}</div>'


def _tabs_html(underlying: str, active: str) -> str:
    current = ' aria-current="page"'
    links = "".join(
        f'<a href="/chart?underlying={_urlencode(underlying)}&amp;view={view.key}"'
        f'{current if view.key == active else ""}>{html.escape(view.label)}</a>'
        for view in CHART_VIEWS
    )
    return f'<nav class="tabs" aria-label="chart views">{links}</nav>'


def ticker_search_html(current: str, symbols: list[str], view: str = "") -> str:
    options = "".join(
        f"<option value=\"{html.escape(symbol, quote=True)}\"></option>"
        for symbol in symbols
    )
    keep_view = f'<input type="hidden" name="view" value="{html.escape(view, quote=True)}">' if view else ""
    return (
        "<form class=\"block\" method=\"get\" action=\"/chart\" role=\"search\">"
        "<label class=\"field\">ticker"
        f"<input name=\"underlying\" value=\"{html.escape(current, quote=True)}\" "
        "list=\"chart-tickers\" placeholder=\"SPY\" autocomplete=\"off\">"
        f"</label>{keep_view}<datalist id=\"chart-tickers\">{options}</datalist>"
        "<button type=\"submit\">view chart</button></form>"
    )


def gamma_chart_html(
    underlying: str,
    snapshot: ChainSnapshot,
    profile: GexProfile,
    bar: Optional[Bar],
    now: Optional[datetime] = None,
    settings: Optional[DashboardSettings] = None,
    symbols: Optional[list[str]] = None,
    view: str = DEFAULT_CHART_VIEW.key,
) -> str:
    from bot.theme import page

    resolved = settings or DashboardSettings()
    selected = chart_view(view)
    spot = profile.spot if profile.spot is not None else (bar.close if bar is not None else None)
    _, vex, dex, charm, vanna = compute_exposures(snapshot, profile.snapshot_id, spot=profile.spot)
    data = ChartData(snapshot, spot, profile, vex, dex, charm, vanna)
    age = ""
    if now is not None:
        seconds = max(0, int((now - snapshot.timestamp_utc).total_seconds()))
        age = f" age {seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f" age {seconds}s"
    refresh = resolved.refresh_seconds if resolved.refresh_seconds > 0 else CHART_REFRESH_SECONDS
    title = f"{underlying} {selected.title}"
    sub = (
        f"snapshot {profile.snapshot_id}{age} | source {snapshot.source_name} "
        f"| refreshes every {refresh}s"
    )
    body = (
        f"{ticker_search_html(underlying, symbols or [underlying], selected.key)}"
        f"{chart_stats_html(data)}"
        f"{_tabs_html(underlying, selected.key)}"
        f"{selected.render(data)}"
    )
    return page(
        title, title, sub, body, underlying, "chart", resolved,
        f"/chart?underlying={_urlencode(underlying)}&view={selected.key}", refresh=True,
    )


def gex_profile_panel_html(store: SnapshotStore, underlying: str) -> str:
    found = store.read_latest(underlying)
    if found is None:
        return ""
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(underlying)
    profile = compute_gex(snapshot, snapshot_id, spot=bar.close if bar is not None else None)
    others = ", ".join(view.label for view in CHART_VIEWS[1:])
    return (
        f"<h2>GEX profile</h2>{_gex_panel(profile)}"
        f'<p class="sub"><a href="/chart?underlying={_urlencode(underlying)}">All chart views</a>: {html.escape(others)}</p>'
    )


def board_rows(store: SnapshotStore) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
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


def _age_seconds(now: datetime, then: datetime | None) -> int | None:
    if then is None:
        return None
    if (now.tzinfo is None) != (then.tzinfo is None):
        utc = timezone.utc
        now = now if now.tzinfo is not None else now.replace(tzinfo=utc)
        then = then if then.tzinfo is not None else then.replace(tzinfo=utc)
    return max(0, int((now - then).total_seconds()))


def _age_text(now: datetime, then: datetime | None) -> str:
    seconds = _age_seconds(now, then)
    if seconds is None:
        return "none"
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def _freshness(now: datetime, then: datetime | None, limit_seconds: int) -> str:
    age = _age_seconds(now, then)
    if age is None:
        return "stale"
    return "fresh" if age <= limit_seconds else "stale"


def _badge(label: str, state: str) -> str:
    return f"<span class=\"{state}\">{html.escape(label)}</span>"


def live_board_fragment(store: SnapshotStore, now: datetime) -> str:
    rows = board_rows(store)
    if not rows:
        return f"<p>{html.escape(empty_store_text())}</p>"
    cells = [
        "<div class=\"table-scroll\"><table><tr><th>symbol</th><th>regime</th><th>total gex</th><th>spot</th>"
        "<th>flip</th><th>flip dist %</th><th>biggest wall</th>"
        "<th>feed</th><th>quote</th></tr>"
    ]
    for row in rows:
        symbol = str(row["symbol"])
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
        found = store.read_latest(symbol)
        bar = store.read_latest_bar(symbol)
        feed_state = _freshness(
            now, found[1].timestamp_utc if found else None, SNAPSHOT_FRESH_SECONDS
        )
        quote_state = _freshness(
            now, bar.timestamp_utc if bar else None, QUOTE_FRESH_SECONDS
        )
        feed_label = found[1].source_name if found else "none"
        quote_label = f"quote {_age_text(now, bar.timestamp_utc if bar else None)}"
        cells.append(
            f"<tr><td>{html.escape(symbol)}</td>"
            f"<td class=\"{cls}\">{regime}</td>"
            f"<td>{_money(float(row['total']))}</td><td>{spot_text}</td>"
            f"<td>{flip_text}</td><td>{dist_text}</td><td>{wall_text}</td>"
            f"<td>{_badge(feed_label, feed_state)}</td>"
            f"<td>{_badge(quote_label, quote_state)}</td></tr>"
        )
    cells.append("</table></div>")
    cells.append(
        "<p class=\"sub\">quote green at 5m or fresher, amber older; "
        "feed green at 6h or fresher, amber older</p>"
    )
    return "".join(cells)


def live_board_text(store: SnapshotStore, now: datetime) -> str:
    rows = board_rows(store)
    if not rows:
        return f"live board: {empty_store_text()}"
    lines = [
        f"live board {now.isoformat()} -- quote green at 5m or fresher, amber older; "
        f"feed green at 6h or fresher, amber older"
    ]
    for row in rows:
        symbol = str(row["symbol"])
        spot = row["spot"]
        spot_text = f"{spot:.2f}" if spot is not None else "n/a"
        strike, wall = row["wall_strike"], row["wall_value"]
        wall_text = f"{strike:g} {_money(float(wall))}" if strike is not None else "none"
        found = store.read_latest(symbol)
        bar = store.read_latest_bar(symbol)
        feed_state = _freshness(
            now, found[1].timestamp_utc if found else None, SNAPSHOT_FRESH_SECONDS
        )
        quote_state = _freshness(
            now, bar.timestamp_utc if bar else None, QUOTE_FRESH_SECONDS
        )
        feed_label = found[1].source_name if found else "none"
        quote_label = f"quote {_age_text(now, bar.timestamp_utc if bar else None)}"
        lines.append(
            f"{symbol}: {row['regime']} spot {spot_text} wall {wall_text} | "
            f"feed {feed_label} {feed_state} | {quote_label} {quote_state}"
        )
    return "\n".join(lines)


def regime_board_html(store: SnapshotStore, now: datetime | None = None) -> str:
    from bot.theme import page

    settings = store.read_settings()
    title = "Dealer positioning board"
    rows = board_rows(store)
    stamp = now.isoformat() if now is not None else ""
    sub = f"{len(rows)} underlyings, from stored snapshots {stamp}".strip()
    moment = now or datetime.now(timezone.utc)
    poll_ms = LIVE_POLL_SECONDS * 1000
    script = (
        f"<script>setInterval(async()=>{{try{{const r=await fetch(\"/board/live\");"
        f"if(r.ok)document.getElementById(\"live-board\").innerHTML=await r.text();}}"
        f"catch(e){{}}}},{poll_ms});</script>"
    )
    symbols = [str(row["symbol"]) for row in rows]
    search = ticker_search_html(settings.default_underlying, symbols) if symbols else ""
    body = f"{search}<div id=\"live-board\">{live_board_fragment(store, moment)}</div>{script}"
    return page(
        title, title, sub, body, settings.default_underlying, "board", settings, "/board",
        refresh=True,
    )
