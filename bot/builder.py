"""Visual strategy builder: server-rendered form plus data-backed preview."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import urlencode

from bot.diff import pct
from bot.ideas import ideas_limit, parse_timeframe, rank_ideas
from bot.ivrank import histories_for
from bot.ideas import cached_earnings
from bot.journal import Journal
from bot.store import SnapshotStore


def builder_defaults(params: Mapping[str, Any]) -> dict[str, str]:
    def _get(name: str, fallback: str) -> str:
        raw = params.get(name, fallback)
        if isinstance(raw, list):
            raw = raw[0] if raw else fallback
        return str(raw if raw is not None else fallback)

    return {
        "underlying": _get("underlying", "SPY").upper() or "SPY",
        "direction": _get("direction", "both"),
        "dte_min": _get("dte_min", "1"),
        "dte_max": _get("dte_max", "30"),
        "min_oi": _get("min_oi", "100"),
        "max_spread": _get("max_spread", "10"),
        "use_flip": _get("use_flip", "on"),
    }


def builder_html(
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    params: Mapping[str, Any],
    preview: str = "",
) -> str:
    from bot.theme import page

    settings = store.read_settings()
    defaults = builder_defaults(params)
    underlyings = store.list_underlyings() or ["SPY"]
    options = "".join(
        f"<option value=\"{html.escape(sym)}\"{' selected' if sym == defaults['underlying'] else ''}>"
        f"{html.escape(sym)}</option>"
        for sym in sorted(set(underlyings + [defaults["underlying"]]))
    )
    direction_options = "".join(
        f"<option value=\"{choice}\"{' selected' if choice == defaults['direction'] else ''}>"
        f"{choice}</option>"
        for choice in ("long", "short", "both")
    )
    checked = " checked" if str(defaults["use_flip"]).lower() in ("1", "true", "yes", "on") else ""
    query = urlencode({k: v for k, v in defaults.items()})
    body = (
        "<h2>Visual builder</h2>"
        "<p class=\"sub\">Pick guardrails, preview matching ideas and a backtest. "
        "No JavaScript build step: one form, one preview.</p>"
        "<form class=\"block\" method=\"get\" action=\"/builder\">"
        f"<label class=\"field\">symbol<select name=\"underlying\">{options}</select></label>"
        f"<label class=\"field\">direction<select name=\"direction\">"
        f"{direction_options}</select></label>"
        f"<label class=\"field\">dte min<input name=\"dte_min\" value=\""
        f"{html.escape(defaults['dte_min'], quote=True)}\" size=\"6\"></label>"
        f"<label class=\"field\">dte max<input name=\"dte_max\" value=\""
        f"{html.escape(defaults['dte_max'], quote=True)}\" size=\"6\"></label>"
        f"<label class=\"field\">min OI<input name=\"min_oi\" value=\""
        f"{html.escape(defaults['min_oi'], quote=True)}\" size=\"8\"></label>"
        f"<label class=\"field\">max spread %<input name=\"max_spread\" value=\""
        f"{html.escape(defaults['max_spread'], quote=True)}\" size=\"6\"></label>"
        f"<label class=\"field\">use flip<input type=\"checkbox\" name=\"use_flip\" "
        f"value=\"on\"{checked}></label>"
        "<button class=\"solid\" type=\"submit\">preview</button></form>"
        f"<p class=\"sub\">share this view: <a href=\"/builder?{html.escape(query)}\">"
        f"/builder?{html.escape(query)}</a></p>"
        f"<h2>Preview</h2><pre>{html.escape(preview or 'pick guardrails and press preview')}</pre>"
    )
    return page(
        "strategy builder", "strategy builder, from stored snapshots",
        f"{len(underlyings)} underlyings, limit {ideas_limit()} ideas",
        body, defaults["underlying"], "dashboard", settings, "/builder",
    )


def builder_preview_text(
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    params: Mapping[str, Any],
) -> str:
    from bot.exposure import compute_gex
    from bot.strategy import StrategySpec, evaluate_strategy

    defaults = builder_defaults(params)
    symbol = (defaults["underlying"] or "SPY").upper()
    try:
        dte_min = max(1, int(float(defaults["dte_min"])))
    except ValueError:
        dte_min = 1
    try:
        dte_max = max(dte_min, int(float(defaults["dte_max"])))
    except ValueError:
        dte_max = max(dte_min, 30)
    try:
        min_oi = max(1.0, float(defaults["min_oi"]))
    except ValueError:
        min_oi = 100.0
    try:
        max_spread = max(1.0, min(50.0, float(defaults["max_spread"])))
    except ValueError:
        max_spread = 10.0
    direction = defaults["direction"] if defaults["direction"] in ("long", "short", "both") else "both"
    use_flip = str(defaults["use_flip"]).lower() in ("1", "true", "yes", "on")
    found = store.read_latest(symbol)
    if found is None:
        from bot.store import need_snapshot_text

        return need_snapshot_text(symbol, store)
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(symbol)
    profile = compute_gex(snapshot, snapshot_id, spot=bar.close if bar is not None else None)
    ideas = rank_ideas(
        [(snapshot_id, snapshot, profile, bar)],
        max_days=dte_max,
        now=now,
        min_open_interest=min_oi,
        max_spread_pct=max_spread,
        iv_history=histories_for(store, [symbol]),
        earnings=cached_earnings([symbol], now),
    )
    ideas = [idea for idea in ideas if idea.expiry >= snapshot.timestamp_utc.date().isoformat()]
    filtered = []
    for idea in ideas:
        try:
            from datetime import datetime as _dt

            days = (_dt.fromisoformat(idea.expiry).date() - now.date()).days
        except ValueError:
            continue
        if days < dte_min or days > dte_max:
            continue
        if direction != "both" and idea.direction != direction:
            continue
        filtered.append(idea)
    lines = [
        f"builder {symbol} {direction} DTE {dte_min}-{dte_max} "
        f"OI>={min_oi:g} spread<={max_spread:g}%: {len(filtered)} of {len(ideas)} ideas pass"
    ]
    for number, idea in enumerate(filtered[:5], 1):
        lines.append(
            f"{number}. {idea.direction} {idea.spread} {idea.expiry} "
            f"conf {pct(idea.confidence)}: {idea.thesis}"
        )
    spec = StrategySpec(
        underlying=symbol, direction=direction, dte_min=dte_min, dte_max=dte_max,
        min_oi=min_oi, max_spread_pct=max_spread, use_flip=use_flip,
        label=f"builder {symbol} {direction}",
    )
    lines.append("")
    lines.append(evaluate_strategy(spec, store, journal, now))
    _ = parse_timeframe
    return "\n".join(lines)
