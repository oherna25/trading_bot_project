from __future__ import annotations

import html
from datetime import datetime
from typing import Mapping
from urllib.parse import quote as _urlencode

from bot.charts import gamma_svg
from bot.diff import compact_dollars
from bot.exposure import compute_gex
from bot.ivrank import histories_for
from bot.journal import Journal
from bot.models import HttpPost
from bot.replay import replay_report
from bot.research import handle_chat_query
from bot.shock import shock_text
from bot.store import SnapshotStore, need_snapshot_text
from bot.tradecards import PROFILE_REQUIRED, bar_at_or_before, build_trade_cards, card_article
from bot.wheel import wheel_response

SHOCK_BUTTON_PCTS = (-2.0, -1.0, 1.0, 2.0)


def demo_cards(
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    snapshot_id: str | None = None,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
):
    if snapshot_id is None:
        found = store.read_latest(underlying)
        if found is None:
            return None, []
        snapshot_id, snapshot = found
    else:
        match = [
            s for sid, s in store.snapshots_in_order()
            if sid == snapshot_id and s.underlying == underlying
        ]
        if not match:
            return None, []
        snapshot = match[0]
    bar = bar_at_or_before(store, underlying, snapshot.timestamp_utc)
    entries = [
        (
            snapshot_id,
            snapshot,
            compute_gex(snapshot, snapshot_id, spot=bar.close if bar is not None else None),
            bar,
        )
    ]
    cards = build_trade_cards(
        entries, None, now, journal, env, http_post,
        iv_history=histories_for(store, [underlying]), earnings=store.all_earnings(),
    )
    return snapshot_id, cards


def shock_buttons(underlying: str) -> str:
    links = []
    for pct in SHOCK_BUTTON_PCTS:
        label = f"{pct:+.0f}%"
        href = f"/shock?underlying={_urlencode(underlying)}&pct={pct:g}"
        links.append(f"<a class=\"shock-btn\" href=\"{html.escape(href, quote=True)}\">{label} shock</a>")
    return f"<p>{''.join(links)}</p>"


def shock_html(
    underlying: str, store: SnapshotStore, journal: Journal, now: datetime, pct: float
) -> str:
    from bot.theme import page

    body = shock_text(underlying, store, journal, now, pct)
    demo_href = f"/demo?underlying={_urlencode(underlying)}"
    title = f"{underlying} shock {pct:+g}%"
    content = (
        f"<p><a class=\"btn\" href=\"{html.escape(demo_href, quote=True)}\">← back to demo</a></p>"
        f"{shock_buttons(underlying)}"
        f"<pre>{html.escape(body)}</pre>"
    )
    return page(
        title, f"{title}, from stored snapshots", body.splitlines()[0] if body else "",
        content, underlying, "demo", store.read_settings(),
        f"/shock?underlying={_urlencode(underlying)}&pct={pct:g}",
    )


def _chat_box(underlying: str) -> str:
    sym = html.escape(underlying, quote=True)
    chips = "".join(
        f"<button type=\"button\" class=\"chipbtn\" data-q=\"{html.escape(q, quote=True)}\">{html.escape(q)}</button>"
        for q in ("gex brief", "trade cards", "spark audit", "wheel ideas", f"shock {underlying} -2%")
    )
    return (
        "<section class=\"chatbox\" aria-label=\"Ask the desk\">"
        "<span class=\"eyebrow\">Try it live</span><h2 style=\"margin-top:.35rem\">Ask the desk</h2>"
        f"<p class=\"sub\">Same chat loop as the API. Try: <code>gex brief</code>, <code>trade cards</code>, <code>spark audit</code>.</p>"
        f"<div class=\"chips\">{chips}</div>"
        "<form id=\"chat-form\"><label class=\"field\" style=\"flex:1 1 16rem\">question"
        f"<input type=\"text\" id=\"chat-q\" name=\"q\" value=\"gex brief\" autocomplete=\"off\" aria-label=\"chat question\"></label>"
        "<button class=\"solid\" type=\"submit\">Ask</button></form>"
        "<pre id=\"chat-reply\" aria-live=\"polite\"></pre>"
        "<script>"
        "const f=document.getElementById('chat-form'),q=document.getElementById('chat-q'),r=document.getElementById('chat-reply');"
        "document.querySelectorAll('.chipbtn').forEach(b=>b.addEventListener('click',()=>{q.value=b.dataset.q;f.requestSubmit()}));"
        "f.addEventListener('submit',async e=>{e.preventDefault();r.className='show';r.textContent='thinking…';"
        "try{const res=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},"
        f"body:JSON.stringify({{text:q.value,underlying:'{sym}'}})}});const j=await res.json();"
        "r.textContent=j.reply||j.error||('HTTP '+res.status)}catch(err){r.textContent='chat failed: '+err}});"
        "</script></section>"
    )


def demo_html(underlying: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    from bot.spark import spark_panel_html
    from bot.theme import BRAND, hero_html, page

    found = store.read_latest(underlying)
    if found is None:
        raise ValueError(need_snapshot_text(underlying, store))
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(underlying)
    profile = compute_gex(snapshot, snapshot_id, spot=bar.close if bar is not None else None)
    brief, _ = handle_chat_query("judge demo brief", underlying, store, journal, now)
    _, cards = demo_cards(underlying, store, journal, now)
    profile_saved = store.read_profile()
    wheel = wheel_response("wheel ideas", underlying, store, journal, now)
    replay_text, _, _ = replay_report(underlying, store)
    link = f"/card?underlying={_urlencode(underlying)}&snapshot={_urlencode(snapshot_id)}&index=0"
    regime = "long-gamma" if profile.total_exposure >= 0 else "short-gamma"
    regime_cls = "good" if regime == "long-gamma" else "bad"
    spot = bar.close if bar is not None else None
    if spot is not None and profile.flip_point is not None:
        side = "above flip — dealers dampen moves" if spot >= profile.flip_point else "below flip — moves can accelerate"
        flip_line = f"spot {spot:.2f} vs flip {profile.flip_point:.1f} · {side}"
    else:
        flip_line = f"total GEX {compact_dollars(profile.total_exposure)}"
    pills = (
        f"<span class=\"pill {regime_cls}\">● {html.escape(regime)}</span>"
        f"<span class=\"pill\">{html.escape(flip_line)}</span>"
        f"<span class=\"pill spark\">◆ Muse Spark audit inside</span>"
        f"<span class=\"pill\">snapshot {html.escape(snapshot_id)}</span>"
    )
    hero = hero_html(
        "Meta hackathon · Indie Developer · Build for Your World",
        f"{underlying}: Spark proposes. The engine grades it.",
        "A paper-first options desk. Muse Spark writes the thesis, the home-grown GEX engine checks it against walls and the flip, and every claim, verdict, intent, and fill lands in the journal.",
        pills,
    )
    if profile_saved is None:
        top_html = f"<div class=\"panel\"><p>{html.escape(PROFILE_REQUIRED)}</p><p class=\"sub\">One 4-question profile unlocks sized trade cards everywhere.</p></div>"
    elif cards:
        top_html = card_article(cards[0], 0, profile=profile_saved)
    else:
        top_html = "<div class=\"panel\"><p>trade cards: none found</p></div>"
    steps = (
        "<div class=\"steps\">"
        "<div class=\"step\"><span class=\"n\">1</span><b>Read the regime</b><p>Walls, flip, and $GEX per 1% from the stored chain — no vendor GEX feed.</p></div>"
        "<div class=\"step\"><span class=\"n\">2</span><b>Grade the model</b><p>Spark direction vs engine direction: MATCH, CONTRADICTION, or OFF. Journaled either way.</p></div>"
        "<div class=\"step\"><span class=\"n\">3</span><b>Trade on paper</b><p>Defined-risk cards, sized to your profile, gated by the same risk review as live.</p></div>"
        "<div class=\"step\"><span class=\"n\">4</span><b>Prove it</b><p>Honest next-bar replay vs buy-and-hold, plus shock repricing before you size.</p></div>"
        "</div>"
    )
    content = (
        f"{hero}{steps}"
        f"{spark_panel_html(store, journal, underlying)}"
        f"<h2>1. Regime read</h2><div class=\"grid2\"><div class=\"panel\"><pre style=\"margin:0\">{html.escape(brief)}</pre></div>"
        f"<div class=\"panel\"><h3 style=\"margin-top:0\">Gamma by strike</h3><div class=\"chart-panel\" style=\"margin:0\">{gamma_svg(profile)}</div>"
        f"<p class=\"sub\">Green call walls, red put walls, dashed flip. <a href=\"/chart?underlying={_urlencode(underlying)}\">Open full chart</a></p></div></div>"
        f"<h2>2. Top card</h2>{top_html}"
        f"<p><a href=\"{html.escape(link, quote=True)}\">top card permalink</a> · <a href=\"/cards?underlying={_urlencode(underlying)}\">all cards</a></p>"
        f"<div class=\"grid2\"><div><h2>3. Wheel screen</h2><details open><summary>Premium candidates</summary><pre>{html.escape(wheel)}</pre></details></div>"
        f"<div><h2>4. Replay</h2><details open><summary>Backtest vs buy-and-hold</summary><pre>{html.escape(replay_text)}</pre></details></div></div>"
        f"<h2>5. Shock</h2>{shock_buttons(underlying)}"
        "<p class=\"sub\">Pick a shock to reprice walls, flip, and open positions at a moved spot (Black-Scholes).</p>"
        f"{_chat_box(underlying)}"
    )
    title = f"{underlying} demo"
    return page(
        title, f"{title}, from stored snapshots, on {BRAND}",
        f"snapshot {snapshot_id}",
        content, underlying, "demo", store.read_settings(),
        f"/demo?underlying={_urlencode(underlying)}",
    )
