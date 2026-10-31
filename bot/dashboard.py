from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from urllib.parse import quote as _urlencode

from bot.charts import board_rows
from bot.diff import compact_dollars
from bot.journal import Journal
from bot.store import SnapshotStore, empty_store_text


def _board_line(row: dict[str, Any]) -> str:
    spot = row["spot"]
    spot_text = f"{spot:.2f}" if spot is not None else "n/a"
    flip = row["flip"]
    flip_text = f"{flip:.1f}" if flip is not None else "none"
    strike, wall = row["wall_strike"], row["wall_value"]
    wall_text = f"{strike:g} {compact_dollars(float(wall))}" if strike is not None else "none"
    return (
        f"{row['symbol']}: {row['regime']} spot {spot_text} "
        f"flip {flip_text} wall {wall_text}"
    )


def dashboard_text(store: SnapshotStore, journal: Journal, now: datetime) -> str:
    from bot.alerts import alerts_header, collect_alerts
    from bot.holdings import holdings_text

    rows = board_rows(store)
    if not rows:
        return f"dashboard: {empty_store_text()}"
    count = len(store.snapshots_in_order())
    lines = [
        f"dashboard {now.isoformat()} -- "
        f"{len(store.list_underlyings())} underlyings, {count} snapshots"
    ]
    total = 0
    for row in rows:
        lines.append(_board_line(row))
        row_alerts = collect_alerts(str(row["symbol"]), store, journal, now)
        total += len(row_alerts)
        lines.extend(row_alerts)
    lines.insert(1, alerts_header(total, now, "/alerts"))
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
        wall_text = f"{strike:g} {compact_dollars(float(wall))}" if strike is not None else "none"
        cells.append(
            f"<tr><td>{html.escape(str(row['symbol']))}</td><td>{_regime_span(str(row['regime']))}</td>"
            f"<td>{spot_text}</td><td>{flip_text}</td><td>{html.escape(wall_text)}</td></tr>"
        )
    cells.append("</tbody></table>")
    return "".join(cells)


def _alerts_list(alerts: list[str], now: datetime) -> str:
    from bot.alerts import alerts_header

    head = f"<p class=\"sub\">{html.escape(alerts_header(len(alerts), now, '/alerts'))}</p>"
    if not alerts:
        return head + "<p>no alerts</p>"
    items = "".join(f"<li>{html.escape(alert)}</li>" for alert in alerts)
    return f"<ul>{items}</ul>"


def _money(value: Any) -> str:
    return compact_dollars(value) if value is not None else "n/a"


def _top_card_html(symbol: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    from bot.demo import demo_cards
    from bot.tradecards import PROFILE_REQUIRED, risk_cap_for, size_for_card

    profile = store.read_profile()
    if profile is None:
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
    _, _, sizing = size_for_card(card.max_loss, risk_cap_for(profile))
    rows = [
        ("Direction", f"<span class=\"{direction_cls}\">{html.escape(card.direction)}</span>"),
        ("Spread", html.escape(f"{card.spread} {card.expiry}")),
        ("Strike", f"{card.strike:g}"),
        ("Confidence", f"{card.confidence:.2f}"),
        ("Wall", html.escape(wall)),
        ("Regime", _regime_span(card.regime)),
        ("Spot vs flip", f"{spot} vs {flip}"),
        ("Max loss", _money(card.max_loss)),
        ("Size", html.escape(sizing)),
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


ACTION_LINKS = (
    ("brief", "get_gex_brief", "Computed GEX brief for the symbol."),
    ("ideas", "get_trade_ideas", "Ranked trade ideas with triggers."),
    ("wheel", "get_wheel_ideas", "Cash-secured-put and covered-call screen."),
    ("vex", "get_vex_brief", "Vega exposure brief."),
    ("dex", "get_dex_brief", "Delta exposure brief."),
    ("diff", "snapshot_diff", "Latest two snapshots compared."),
    ("replay", "run_replay", "Paper backtest over stored snapshots."),
    ("pnl", "paper_pnl", "Paper P and L from journaled fills."),
    ("track record", "get_trackrecord", "Closed paper trades with win rate."),
    ("coach", "get_coach_review", "Trading-coach review of the record."),
    ("tier", "get_tier_status", "Model spend, budget, backend status."),
    ("alerts", "get_alerts", "Current alerts across underlyings."),
    ("narrated alerts", "get_narrated_alerts", "Alerts with one-line reasons."),
    ("journal", "journal_tail", "Tail of the journal."),
    ("iv rank", "get_iv_rank", "IV rank per underlying."),
    ("profile", "get_profile", "Saved operator profile."),
    ("book", "get_holdings", "Saved holdings vs positioning."),
    ("settings", "get_settings", "Global dashboard settings."),
    ("earnings", "get_earnings_radar", "Prints in 14 days with positioning."),
    ("explain position", "explain_position", "One-paragraph read on your shares."),
    ("alert rules", "list_alert_rules", "Custom break and flip-cross rules."),
    ("morning brief", "get_open_brief", "Regime, book risk, earnings, narrated alerts."),
    ("live board", "get_live_board", "Tickers with feed and quote badges."),
)


def _actions_html(underlying: str) -> str:
    escaped = html.escape(underlying, quote=True)
    encoded = _urlencode(underlying)
    links = "".join(
        f"<div class=\"action\"><h3><a href=\"/action?tool={tool}&amp;underlying={encoded}\">"
        f"{html.escape(label)}</a></h3><p>{html.escape(hint)}</p></div>"
        for label, tool, hint in ACTION_LINKS
    )
    back = f"/dashboard?underlying={encoded}"
    back_field = f"<input type=\"hidden\" name=\"next\" value=\"{html.escape(back, quote=True)}\">"
    hidden_symbol = f"<input type=\"hidden\" name=\"underlying\" value=\"{escaped}\">"
    runs = (
        "<div class=\"action\"><h3>shock</h3><p>Reprice walls against a moved spot.</p>"
        f"<form class=\"block\" method=\"get\" action=\"/shock\">{hidden_symbol}"
        "<label class=\"field\">pct<input name=\"pct\" value=\"-2\" size=\"6\"></label>"
        "<button type=\"submit\">shock</button></form></div>"
        "<div class=\"action\"><h3>shock with news</h3><p>Explain the move with cited news.</p>"
        f"<form class=\"block\" method=\"get\" action=\"/action\">"
        "<input type=\"hidden\" name=\"tool\" value=\"shock_news\">"
        f"{hidden_symbol}<label class=\"field\">pct<input name=\"pct\" value=\"-2\" size=\"6\"></label>"
        "<button type=\"submit\">shock</button></form></div>"
        "<div class=\"action\"><h3>close</h3><p>Natural-language paper portfolio command.</p>"
        f"<form class=\"block\" method=\"get\" action=\"/action\">"
        "<input type=\"hidden\" name=\"tool\" value=\"portfolio_command\">"
        f"{hidden_symbol}<label class=\"field\">command<input name=\"command\" value=\"close losers\" size=\"18\">"
        "</label><button type=\"submit\">run</button></form></div>"
        "<div class=\"action\"><h3>debate</h3><p>Bull-bear debate for one trade card.</p>"
        f"<form class=\"block\" method=\"get\" action=\"/card\">{hidden_symbol}"
        "<input type=\"hidden\" name=\"debate\" value=\"1\">"
        "<label class=\"field\">index<input name=\"index\" value=\"0\" size=\"4\"></label>"
        "<button type=\"submit\">debate</button></form></div>"
        "<div class=\"action\"><h3>share</h3><p>Shareable trade card caption with art.</p>"
        f"<form class=\"block\" method=\"get\" action=\"/share\">{hidden_symbol}"
        "<label class=\"field\">index<input name=\"index\" value=\"0\" size=\"4\"></label>"
        "<button type=\"submit\">share</button></form></div>"
        "<div class=\"action\"><h3>run session</h3><p>Run a scheduled session now.</p>"
        f"<form class=\"block\" method=\"post\" action=\"/session\">{hidden_symbol}"
        "<label class=\"field\">paper trade<input type=\"checkbox\" name=\"paper_trade\" value=\"1\" checked>"
        "</label><button class=\"solid\" type=\"submit\">run session</button></form></div>"
        "<div class=\"action\"><h3>take trade</h3><p>Take a trade card as a paper trade.</p>"
        f"<form class=\"block\" method=\"post\" action=\"/action\">"
        "<input type=\"hidden\" name=\"tool\" value=\"take_paper_trade\">"
        f"{hidden_symbol}<label class=\"field\">index<input name=\"index\" value=\"0\" size=\"4\">"
        "</label><button class=\"solid\" type=\"submit\">take this trade (paper)</button></form></div>"
        "<div class=\"action\"><h3>load</h3><p>Fetch a fresh snapshot for a symbol.</p>"
        "<form class=\"block\" method=\"post\" action=\"/action\">"
        "<input type=\"hidden\" name=\"tool\" value=\"load_snapshot\">"
        f"<label class=\"field\">symbol<input name=\"symbol\" value=\"{escaped}\" size=\"8\"></label>"
        "<button type=\"submit\">load</button></form></div>"
        "<div class=\"action\"><h3>backfill IV</h3><p>Rebuild IV history from snapshots.</p>"
        "<form class=\"block\" method=\"post\" action=\"/action\">"
        "<input type=\"hidden\" name=\"tool\" value=\"backfill_iv\">"
        f"{hidden_symbol}<button type=\"submit\">backfill</button></form></div>"
        "<div class=\"action\"><h3>video brief</h3><p>Render-ready HyperFrames project.</p>"
        f"<p><a href=\"/video?underlying={encoded}\">open composition</a> or "
        f"<a href=\"/action?tool=get_video_brief&amp;underlying={encoded}\">rebuild it</a></p></div>"
        "<div class=\"action\"><h3>replay with costs</h3><p>Backtest net of commission and slippage.</p>"
        f"<form class=\"block\" method=\"get\" action=\"/action\">"
        "<input type=\"hidden\" name=\"tool\" value=\"run_replay\">"
        f"{hidden_symbol}"
        "<label class=\"field\">commission<input name=\"commission\" value=\"0\" size=\"6\"></label>"
        "<label class=\"field\">slippage bps<input name=\"slippage_bps\" value=\"0\" size=\"6\"></label>"
        "<button type=\"submit\">replay</button></form></div>"
        "<div class=\"action\"><h3>explain position</h3><p>One paragraph on your shares.</p>"
        f"<form class=\"block\" method=\"get\" action=\"/action\">"
        "<input type=\"hidden\" name=\"tool\" value=\"explain_position\">"
        f"<label class=\"field\">symbol<input name=\"symbol\" value=\"{escaped}\" size=\"8\"></label>"
        "<button type=\"submit\">explain</button></form></div>"
        "<div class=\"action\"><h3>onboarding</h3><p>4-question profile flow, then your first sized card.</p>"
        f"<form class=\"block\" method=\"post\" action=\"/action\">"
        "<input type=\"hidden\" name=\"tool\" value=\"onboarding_step\">"
        f"{hidden_symbol}"
        "<label class=\"field\">answer<input name=\"answer\" value=\"\" size=\"18\"></label>"
        "<button class=\"solid\" type=\"submit\">start or answer</button></form></div>"
        "<div class=\"action\"><h3>alert rule</h3><p>Fire when a level breaks or spot crosses its flip.</p>"
        f"<form class=\"block\" method=\"post\" action=\"/action\">"
        "<input type=\"hidden\" name=\"tool\" value=\"set_alert_rule\">"
        f"{hidden_symbol}"
        "<label class=\"field\">rule<input name=\"text\" value=\"alert me if SPY breaks 750\" size=\"26\">"
        "</label><button class=\"solid\" type=\"submit\">save rule</button></form></div>"
        "<div class=\"action\"><h3>cancel alert</h3><p>Drop one custom rule by id.</p>"
        f"<form class=\"block\" method=\"post\" action=\"/action\">"
        "<input type=\"hidden\" name=\"tool\" value=\"cancel_alert_rule\">"
        f"{hidden_symbol}"
        "<label class=\"field\">rule id<input name=\"rule_id\" value=\"1\" size=\"6\"></label>"
        "<button type=\"submit\">cancel rule</button></form></div>"
    )
    edits = (
        "<div class=\"action\"><h3>holdings</h3><p>Save the operator book as JSON.</p>"
        f"<form method=\"post\" action=\"/holdings\">{back_field}"
        "<textarea name=\"holdings\" rows=\"3\">"
        "[{\"symbol\": \"SPY\", \"kind\": \"stock\", \"side\": \"long\", \"qty\": 10}]</textarea>"
        "<p><button type=\"submit\">save holdings</button></p></form></div>"
        "<div class=\"action\"><h3>profile</h3><p>Save the operator questionnaire.</p>"
        f"<form class=\"block\" method=\"post\" action=\"/profile\">{back_field}"
        "<label class=\"field\">risk_tolerance<input name=\"risk_tolerance\" value=\"moderate\" size=\"10\">"
        "</label><label class=\"field\">portfolio_size<input name=\"portfolio_size\" value=\"50000\" size=\"10\">"
        "</label><label class=\"field\">knowledge<input name=\"knowledge\" value=\"intermediate\" size=\"10\">"
        "</label><label class=\"field\">options_ok<input name=\"options_ok\" value=\"yes\" size=\"6\">"
        "</label><button type=\"submit\">save profile</button></form></div>"
        "<div class=\"action\"><h3>read chart</h3><p>Upload a chart screenshot.</p>"
        f"<form class=\"block\" method=\"post\" action=\"/chartread?underlying={encoded}\" "
        "enctype=\"multipart/form-data\">"
        "<label class=\"field\">image<input type=\"file\" name=\"image\"></label>"
        "<button type=\"submit\">read chart</button></form></div>"
        "<div class=\"action\"><h3>read document</h3><p>Upload a document scan.</p>"
        f"<form class=\"block\" method=\"post\" action=\"/docread?underlying={encoded}\" "
        "enctype=\"multipart/form-data\">"
        "<label class=\"field\">image<input type=\"file\" name=\"image\"></label>"
        "<button type=\"submit\">read document</button></form></div>"
    )
    return (
        f"<h2>Actions</h2><div class=\"actions\">{links}</div>"
        f"<h2>Runs</h2><div class=\"actions\">{runs}</div>"
        f"<h2>Edits and uploads</h2><div class=\"actions\">{edits}</div>"
    )


def dashboard_html(
    store: SnapshotStore, journal: Journal, now: datetime, underlying: str
) -> str:
    from bot.alerts import collect_alerts
    from bot.theme import page

    settings = store.read_settings()
    rows = board_rows(store)
    alerts: list[str] = []
    for row in rows:
        alerts.extend(collect_alerts(str(row["symbol"]), store, journal, now))
    count = len(store.snapshots_in_order())
    header = (
        f"dashboard {now.isoformat()} -- "
        f"{len(store.list_underlyings())} underlyings, {count} snapshots"
    )
    stats = (
        "<div class=\"stats\">"
        f"<div class=\"stat\"><b>{len(store.list_underlyings())}</b><span>underlyings</span></div>"
        f"<div class=\"stat\"><b>{count}</b><span>snapshots</span></div>"
        f"<div class=\"stat\"><b>{len(alerts)}</b><span>active alerts</span></div>"
        f"<div class=\"stat\"><b>{len(store.read_holdings())}</b><span>holdings</span></div>"
        "</div>"
    )
    body = (
        f"{stats}"
        f"<h2>Digest</h2>{_board_table(rows)}{_alerts_list(alerts, now)}"
        f"<h2>Top card</h2>{_top_card_html(underlying, store, journal, now)}"
        f"<h2>Holdings</h2>{_holdings_html(store)}"
        f"{_actions_html(underlying)}"
    )
    next_path = f"/dashboard?underlying={_urlencode(underlying)}"
    return page(
        f"{underlying} dashboard",
        f"{underlying} dashboard, from stored snapshots",
        header,
        body,
        underlying,
        "dashboard",
        settings,
        next_path,
    )
