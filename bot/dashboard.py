from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from urllib.parse import quote as _urlencode

from bot.charts import board_rows
from bot.diff import compact_dollars
from bot.journal import Journal
from bot.store import SnapshotStore


def _board_line(row: dict[str, Any]) -> str:
    spot = row["spot"]
    spot_text = f"{spot:.2f}" if spot is not None else "n/a"
    flip = row["flip"]
    flip_text = f"{flip:.1f}" if flip is not None else "none"
    strike, wall = row["wall_strike"], row["wall_value"]
    wall_text = f"{strike:g} ${float(wall):,.0f}" if strike is not None else "none"
    return (
        f"{row['symbol']}: {row['regime']} spot {spot_text} "
        f"flip {flip_text} wall {wall_text}"
    )


def dashboard_text(store: SnapshotStore, journal: Journal, now: datetime) -> str:
    from bot.alerts import collect_alerts
    from bot.holdings import holdings_text

    rows = board_rows(store)
    if not rows:
        return "dashboard: no snapshots stored"
    count = len(store.snapshots_in_order())
    lines = [
        f"dashboard {now.isoformat()} -- "
        f"{len(store.list_underlyings())} underlyings, {count} snapshots"
    ]
    for row in rows:
        lines.append(_board_line(row))
        lines.extend(collect_alerts(str(row["symbol"]), store, journal, now))
    if store.read_holdings():
        lines.append(holdings_text(store))
    return "\n".join(lines)


def _regime_span(regime: str) -> str:
    cls = "long" if regime == "long-gamma" else "short"
    return f"<span class=\"{cls}\">{html.escape(regime)}</span>"


def _board_table(rows: list[dict[str, Any]]) -> str:
    cells = [
        "<table><thead><tr><th>Symbol</th><th>Regime</th><th>Spot</th>"
        "<th>Flip</th><th>Wall</th></tr></thead><tbody>"
    ]
    for row in rows:
        spot = row["spot"]
        spot_text = f"{spot:.2f}" if spot is not None else "n/a"
        flip = row["flip"]
        flip_text = f"{flip:.1f}" if flip is not None else "none"
        strike, wall = row["wall_strike"], row["wall_value"]
        wall_text = f"{strike:g} ${float(wall):,.0f}" if strike is not None else "none"
        cells.append(
            f"<tr><td>{html.escape(str(row['symbol']))}</td><td>{_regime_span(str(row['regime']))}</td>"
            f"<td>{spot_text}</td><td>{flip_text}</td><td>{html.escape(wall_text)}</td></tr>"
        )
    cells.append("</tbody></table>")
    return "".join(cells)


def _alerts_list(alerts: list[str]) -> str:
    if not alerts:
        return "<p>no alerts</p>"
    items = "".join(f"<li>{html.escape(alert)}</li>" for alert in alerts)
    return f"<ul>{items}</ul>"


def _money(value: Any) -> str:
    return f"${value:,.0f}" if value is not None else "n/a"


def _top_card_html(symbol: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    from bot.demo import demo_cards
    from bot.tradecards import PROFILE_REQUIRED

    if store.read_profile() is None:
        return f"<p>{html.escape(PROFILE_REQUIRED)}</p>"
    snapshot_id, cards = demo_cards(symbol, store, journal, now)
    if snapshot_id is None or not cards:
        return "<p>trade cards: none found</p>"
    card = cards[0]
    link = (
        f"/card?underlying={_urlencode(symbol)}"
        f"&snapshot={_urlencode(snapshot_id)}&index=0"
    )
    direction_cls = "long" if card.direction == "long" else "short"
    wall = compact_dollars(card.wall_exposure) if card.wall_exposure is not None else "n/a"
    spot = f"{card.spot:g}" if card.spot is not None else "n/a"
    flip = f"{card.flip_point:g}" if card.flip_point is not None else "n/a"
    rows = [
        ("Direction", f"<span class=\"{direction_cls}\">{html.escape(card.direction)}</span>"),
        ("Spread", html.escape(f"{card.spread} {card.expiry}")),
        ("Strike", f"{card.strike:g}"),
        ("Confidence", f"{card.confidence:.2f}"),
        ("Wall", html.escape(wall)),
        ("Regime", _regime_span(card.regime)),
        ("Spot vs flip", f"{spot} vs {flip}"),
        ("Max loss", _money(card.max_loss)),
        ("Breakeven", f"{card.breakeven}" if card.breakeven is not None else "n/a"),
        ("Reason", html.escape(f"{card.reason} [{card.reason_source}]")),
    ]
    if card.earnings_date is not None:
        rows.append(("Earnings", html.escape(card.earnings_date)))
    if card.iv_rank is not None:
        rows.append(("IV rank", f"{card.iv_rank:.0f}"))
    terms = "".join(f"<dt>{name}</dt><dd>{value}</dd>" for name, value in rows)
    return (
        f"<p>{html.escape(card.symbol)} {html.escape(card.spread)} "
        f"<a href=\"{html.escape(link, quote=True)}\">trade cards permalink</a></p>"
        f"<dl>{terms}</dl>"
    )


def _holdings_html(store: SnapshotStore) -> str:
    from bot.holdings import holdings_text

    if not store.read_holdings():
        return f"<p>{html.escape(holdings_text(store))}</p>"
    lines = holdings_text(store).splitlines()
    items = "".join(f"<li>{html.escape(line)}</li>" for line in lines[1:] if line.strip())
    return f"<p>{html.escape(lines[0])}</p><ul>{items}</ul>"


def dashboard_html(
    store: SnapshotStore, journal: Journal, now: datetime, underlying: str
) -> str:
    from bot.alerts import collect_alerts

    rows = board_rows(store)
    alerts: list[str] = []
    for row in rows:
        alerts.extend(collect_alerts(str(row["symbol"]), store, journal, now))
    count = len(store.snapshots_in_order())
    header = (
        f"dashboard {now.isoformat()} -- "
        f"{len(store.list_underlyings())} underlyings, {count} snapshots"
    )
    style = (
        ":root{color-scheme:dark}"
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "margin:0;background:#0d1117;color:#e6edf3;font-variant-numeric:tabular-nums}"
        ".wrap{max-width:72rem;margin:0 auto;padding:2rem}"
        "h1{font-size:1.5rem;margin:0 0 .25rem}"
        ".sub{color:#8b949e;margin:0 0 1rem}"
        "h2{font-size:1.15rem;margin:2rem 0 .75rem;border-bottom:1px solid #30363d;padding-bottom:.25rem}"
        "nav{margin:0 0 1.5rem}nav a{margin-right:1rem;color:#58a6ff;text-decoration:none}"
        ".stats{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin:1.5rem 0}"
        ".stat{border:1px solid #30363d;border-radius:6px;padding:.75rem 1rem;background:#161b22}"
        ".stat b{display:block;font-size:1.5rem;font-weight:600}"
        ".stat span{color:#8b949e;font-size:.85rem}"
        "table{width:100%;border-collapse:collapse;margin:0 0 1rem}"
        "th,td{text-align:left;padding:.5rem .75rem;border-bottom:1px solid #30363d}"
        "th{color:#8b949e;font-size:.85rem;font-weight:600}"
        ".long{color:#3fb950}.short{color:#f85149}"
        "ul{margin:.5rem 0;padding-left:1.25rem}li{margin:.25rem 0}"
        "dl{display:grid;grid-template-columns:9rem 1fr;gap:.25rem 1rem;margin:.5rem 0}"
        "dt{color:#8b949e}dd{margin:0}"
        "a{color:#58a6ff}"
    )
    escaped = html.escape(underlying, quote=True)
    nav = (
        f"<nav><a href=\"/chart?underlying={escaped}\">chart</a>"
        "<a href=\"/board\">board</a>"
        f"<a href=\"/cards?underlying={escaped}\">cards</a></nav>"
    )
    stats = (
        "<div class=\"stats\">"
        f"<div class=\"stat\"><b>{len(store.list_underlyings())}</b><span>underlyings</span></div>"
        f"<div class=\"stat\"><b>{count}</b><span>snapshots</span></div>"
        f"<div class=\"stat\"><b>{len(alerts)}</b><span>active alerts</span></div>"
        f"<div class=\"stat\"><b>{len(store.read_holdings())}</b><span>holdings</span></div>"
        "</div>"
    )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta http-equiv=\"refresh\" content=\"60\">"
        f"<title>{escaped} dashboard</title><style>{style}</style></head><body>"
        "<div class=\"wrap\">"
        f"<h1>{escaped} dashboard, from stored snapshots</h1>"
        f"<p class=\"sub\">{html.escape(header)}</p>"
        f"{nav}{stats}"
        f"<h2>Digest</h2>{_board_table(rows)}{_alerts_list(alerts)}"
        f"<h2>Top card</h2>{_top_card_html(underlying, store, journal, now)}"
        f"<h2>Holdings</h2>{_holdings_html(store)}"
        "</div></body></html>"
    )
