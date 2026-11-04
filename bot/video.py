from __future__ import annotations

import html
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from bot.charts import CALL_COLOR, PUT_COLOR, board_rows, gamma_svg
from bot.diff import compact_dollars, pct
from bot.exposure import compute_gex
from bot.ideas import cached_earnings, rank_ideas
from bot.ivrank import histories_for
from bot.journal import Journal
from bot.research import paper_pnl_text
from bot.store import SnapshotStore, empty_store_text, need_snapshot_text

VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
TITLE_SECONDS = 4
GAMMA_SECONDS = 6
BOARD_SECONDS = 5
IDEAS_SECONDS = 6
OUTRO_SECONDS = 4
MAX_SYMBOLS = 4
MAX_IDEAS = 5
DEFAULT_PROJECT_DIR = "data/video-brief"
RENDER_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class VideoScene:
    scene_id: str
    title: str
    start_seconds: int
    duration_seconds: int
    body_html: str
    narration: str = ""


@dataclass(frozen=True)
class VideoBrief:
    composition_id: str
    underlyings: tuple[str, ...]
    scenes: tuple[VideoScene, ...]
    total_seconds: int
    generated_at: datetime


@dataclass(frozen=True)
class RenderResult:
    ok: bool
    output: str
    error: str


def _money(value: float) -> str:
    return compact_dollars(value)


def _regime(symbol: str, store: SnapshotStore) -> str:
    found = store.read_latest(symbol)
    if found is None:
        return "no snapshot"
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(symbol)
    profile = compute_gex(snapshot, snapshot_id, spot=bar.close if bar is not None else None)
    return "long-gamma" if profile.total_exposure >= 0.0 else "short-gamma"


def _title_body(symbols: Sequence[str], store: SnapshotStore, now: datetime) -> str:
    rows = "".join(
        f"<li>{html.escape(symbol)}: {html.escape(_regime(symbol, store))}</li>" for symbol in symbols
    )
    names = ", ".join(symbols) or "none"
    return (
        f"<p class=\"stats\">session {html.escape(now.date().isoformat())} "
        f"| symbols {html.escape(names)}</p>"
        f"<ul>{rows}</ul>"
    )


def _gamma_body(symbol: str, store: SnapshotStore) -> str:
    found = store.read_latest(symbol)
    if found is None:
        return f"<p>{html.escape(need_snapshot_text(symbol, store))}</p>"
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(symbol)
    spot = bar.close if bar is not None else None
    profile = compute_gex(snapshot, snapshot_id, spot=spot)
    spot_text = f"{spot:.2f}" if spot is not None else "unknown"
    flip_text = f"{profile.flip_point:.1f}" if profile.flip_point is not None else "none"
    walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))[:5]
    items = "".join(
        f"<li class=\"{'call' if value >= 0 else 'put'}\">{strike:g} {_money(value)}</li>"
        for strike, value in walls
    )
    return (
        f"<p class=\"stats\">spot {html.escape(spot_text)} | flip {html.escape(flip_text)} | "
        f"total {_money(profile.total_exposure)}</p>"
        f"<div class=\"chart-panel\">{gamma_svg(profile)}</div>"
        f"<ul>{items}</ul>"
    )


def _board_body(store: SnapshotStore) -> str:
    rows = board_rows(store)
    if not rows:
        return f"<p>{html.escape(empty_store_text())}</p>"
    cells = [
        "<table><tr><th>symbol</th><th>regime</th><th>total gex</th><th>spot</th><th>flip</th></tr>"
    ]
    for row in rows:
        spot = row["spot"]
        spot_text = f"{spot:.2f}" if spot is not None else "n/a"
        flip = row["flip"]
        flip_text = f"{flip:.1f}" if flip is not None else "n/a"
        regime = str(row["regime"])
        cls = "call" if regime == "long-gamma" else "put"
        cells.append(
            f"<tr><td>{html.escape(str(row['symbol']))}</td>"
            f"<td class=\"{cls}\">{html.escape(regime)}</td>"
            f"<td>{_money(float(row['total']))}</td><td>{spot_text}</td><td>{flip_text}</td></tr>"
        )
    cells.append("</table>")
    return "".join(cells)


def _ideas_body(symbols: Sequence[str], store: SnapshotStore, now: datetime) -> str:
    entries = []
    used = []
    for symbol in symbols:
        found = store.read_latest(symbol)
        if found is None:
            continue
        snapshot_id, snapshot = found
        bar = store.read_latest_bar(symbol)
        profile = compute_gex(snapshot, snapshot_id, spot=bar.close if bar is not None else None)
        entries.append((snapshot_id, snapshot, profile, bar))
        used.append(symbol)
    if not entries:
        return "<p>trade ideas: none found</p>"
    ideas = rank_ideas(
        entries,
        None,
        now=now,
        iv_history=histories_for(store, used),
        earnings=cached_earnings(used, now),
    )[:MAX_IDEAS]
    if not ideas:
        return "<p>trade ideas: none found</p>"
    items = []
    for idea in ideas:
        line = (
            f"{idea.symbol} {idea.direction} {idea.spread or idea.call_or_put} "
            f"expiry {idea.expiry} confidence {pct(idea.confidence)}: {idea.thesis} | {idea.trigger}"
        )
        items.append(f"<li>{html.escape(line)}</li>")
    return f"<ol>{''.join(items)}</ol>"


def _outro_body(store: SnapshotStore, journal: Journal, now: datetime) -> str:
    text = paper_pnl_text(journal, store)
    first = text.splitlines()[0] if text.splitlines() else "paper pnl: no fills journaled"
    return f"<p>{html.escape(first)}</p><p>session {html.escape(now.isoformat())}</p>"


def build_video_brief(
    symbols: Sequence[str], store: SnapshotStore, journal: Journal, now: datetime
) -> VideoBrief:
    capped = list(symbols)[:MAX_SYMBOLS]
    scenes: list[VideoScene] = []
    start = 0

    def _push(scene_id: str, title: str, duration: int, body: str) -> None:
        nonlocal start
        scenes.append(VideoScene(scene_id, title, start, duration, body))
        start += duration

    _push("title", "Title", TITLE_SECONDS, _title_body(capped, store, now))
    for symbol in capped:
        _push(f"gamma-{symbol}", f"{symbol} gamma", GAMMA_SECONDS, _gamma_body(symbol, store))
    _push("board", "Regime board", BOARD_SECONDS, _board_body(store))
    _push("ideas", "Trade ideas", IDEAS_SECONDS, _ideas_body(capped, store, now))
    _push("outro", "Outro", OUTRO_SECONDS, _outro_body(store, journal, now))
    return VideoBrief(
        f"video-brief-{now.strftime('%Y%m%dT%H%M%S')}",
        tuple(capped),
        tuple(scenes),
        start,
        now,
    )


def composition_html(brief: VideoBrief, *, storyboard: bool = False) -> str:
    style = (
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "margin:0;background:#0d1117;color:#e6edf3}"
        f"div[data-composition-id]{{position:relative;width:{VIDEO_WIDTH}px;"
        f"height:{VIDEO_HEIGHT}px;overflow:hidden}}"
        ".clip{position:absolute;inset:0;box-sizing:border-box;background:#161b22;"
        "border:1px solid #30363d;border-radius:12px;padding:32px 40px}"
        "h1{font-size:2rem;margin:0 0 1rem}h2{font-size:1.5rem;margin:0 0 1rem}"
        ".time{color:#8b949e;font-size:1rem;font-weight:normal}"
        ".stats{color:#8b949e}ul,ol{line-height:1.7}"
        f".call{{color:{CALL_COLOR}}}.put{{color:{PUT_COLOR}}}"
        "table{border-collapse:collapse;min-width:60rem}"
        "th,td{border:1px solid #30363d;padding:.4rem .7rem;text-align:right}"
        "td:first-child,th:first-child{text-align:left}"
        ".chart-panel{background:#fff;border-radius:8px;padding:16px;margin:1rem 0}"
        ".chart-panel svg{max-width:100%;height:auto}"
    )
    clips = "".join(
        f"<section id=\"{html.escape(scene.scene_id, quote=True)}\" class=\"clip\" "
        f"data-track-index=\"{lane}\" data-start=\"{scene.start_seconds}\" "
        f"data-duration=\"{scene.duration_seconds}\">"
        f"<h2>{html.escape(scene.title)} "
        f"<span class=\"time\">{scene.start_seconds}s to "
        f"{scene.start_seconds + scene.duration_seconds}s</span></h2>"
        f"{scene.body_html}</section>"
        for lane, scene in enumerate(brief.scenes, start=1)
    )
    override = ""
    if storyboard:
        override = (
            "<style id=\"storyboard-override\">"
            "div[data-composition-id]{position:static;width:auto;height:auto;"
            "max-width:1920px;margin:0 auto;padding:48px}"
            ".clip{position:static;margin:0 0 32px}</style>"
        )
    from bot.theme import BRAND

    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width={VIDEO_WIDTH}, height={VIDEO_HEIGHT}\">"
        f"<title>{html.escape(brief.composition_id)} | {BRAND}</title><style>{style}</style>"
        f"{override}</head><body>"
        f"<div data-composition-id=\"{html.escape(brief.composition_id, quote=True)}\" "
        f"data-start=\"0\" data-width=\"{VIDEO_WIDTH}\" data-height=\"{VIDEO_HEIGHT}\" "
        f"data-duration=\"{brief.total_seconds}\" data-no-timeline>"
        f"{clips}</div></body></html>"
    )


SUBMISSION_SCENES = (
    ("hook", 6),
    ("engine", 12),
    ("spark-audit", 16),
    ("top-card", 14),
    ("safety", 11),
    ("replay", 13),
    ("shock", 11),
    ("mcp", 12),
    ("wheel", 13),
    ("outro", 7),
)


def _caption(text: str) -> str:
    return f"<p class=\"caption\">{html.escape(text)}</p>"


def _spark_scene_body(symbol: str, store: SnapshotStore, journal: Journal) -> str:
    from bot.spark import latest_spark_audit
    from bot.ideas import resolve_direction

    found = store.read_latest(symbol)
    if found is None:
        return f"<p>{html.escape(need_snapshot_text(symbol, store))}</p>"
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(symbol)
    spot = bar.close if bar is not None else None
    profile = compute_gex(snapshot, snapshot_id, spot=spot)
    engine = resolve_direction(bar, profile.flip_point, profile.total_exposure)
    status, _expected, model_dir, confidence = latest_spark_audit(journal)
    live = status != "OFF" and bool(model_dir)
    model_text = model_dir if live else "not run yet — live models off"
    if live and confidence is not None:
        model_text = f"{model_dir} · confidence {pct(confidence)}"
    verdict_cls = {"MATCH": "match", "CONTRADICTION": "contradiction"}.get(status, "off")
    return (
        "<div class=\"duel\">"
        f"<div class=\"side\"><span>Muse Spark proposes</span><b>{html.escape(model_text)}</b>"
        "<p>muse-spark-1.3 · DIRECTION + CONFIDENCE, forced tool call</p></div>"
        "<div class=\"vs\">VS</div>"
        f"<div class=\"side\"><span>Engine disposes</span><b>{html.escape(engine)}</b>"
        f"<p>spot {spot:.2f} vs flip {profile.flip_point:.1f}</p></div></div>"
        if spot is not None and profile.flip_point is not None
        else "<div class=\"duel\">"
        f"<div class=\"side\"><span>Muse Spark proposes</span><b>{html.escape(model_text)}</b></div>"
        "<div class=\"vs\">VS</div>"
        f"<div class=\"side\"><span>Engine disposes</span><b>{html.escape(engine)}</b></div></div>"
    ) + (
        "<div class=\"verdicts\">"
        "<span class=\"pill match\">MATCH — model agrees with the tape</span>"
        "<span class=\"pill contradiction\">CONTRADICTION — sizing waits</span>"
        f"<span class=\"pill {verdict_cls}\">THIS RUN: {html.escape(status)}</span></div>"
        "<p>Free prose never counts. Only direction vs engine counts — and both sides land in the journal as spark_claim + spark_verdict.</p>"
        + _caption("Spark proposes. The engine grades it. Deterministic math has the last word.")
    )


def _card_scene_body(symbol: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    from bot.demo import demo_cards
    from bot.tradecards import PROFILE_REQUIRED

    if store.read_profile() is None:
        return f"<p>{html.escape(PROFILE_REQUIRED)}</p>" + _caption("One 4-question profile unlocks sized, defined-risk cards.")
    _snapshot_id, cards = demo_cards(symbol, store, journal, now)
    if not cards:
        return "<p>trade cards: none found</p>"
    card = cards[0]
    loss = _money(card.max_loss) if card.max_loss is not None else "n/a"
    be = f"{card.breakeven:g}" if card.breakeven is not None else "n/a"
    return (
        f"<p class=\"big\">{html.escape(card.symbol)} {html.escape(card.direction)} {html.escape(card.spread)} · {html.escape(card.expiry)}</p>"
        f"<p>confidence {pct(card.confidence)} · max loss {loss} · breakeven {be} · regime {html.escape(card.regime)}</p>"
        f"<p>entry: {html.escape(card.entry)}</p><p>why: {html.escape(card.reason)}</p>"
        + _caption("Same screener in chat, /cards, and /demo — surfaces never disagree.")
    )


def _mcp_scene_body(symbol: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    from bot.muse_chat import dispatch_tool
    from bot.muse_tools import TOOL_REGISTRY
    from bot.shapes import default_operator_config

    reply = dispatch_tool(
        "get_gex_brief", {}, store, journal, default_operator_config(), now, underlying=symbol
    )
    reply_lines = (reply.reply if reply.ok else reply.error).splitlines()[:3]
    wire = "\n".join(
        [
            'POST /mcp {"method": "tools/list"}',
            f'POST /mcp {{"method": "tools/call", "params": {{"name": "get_gex_brief", "underlying": "{symbol}"}}}}',
            *reply_lines,
        ]
    )
    return (
        f"<p class=\"big\">{len(TOOL_REGISTRY)} tools · one registry</p>"
        f"<pre>{html.escape(wire)}</pre>"
        "<div class=\"verdicts\">"
        "<span class=\"pill match\">tools/list</span>"
        "<span class=\"pill match\">tools/call</span>"
        "<span class=\"pill off\">same tools in chat · slash · HTTP</span></div>"
        + _caption("Every surface calls the same registry. No second implementation to drift.")
    )


def _wheel_scene_body(symbol: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    from bot.wheel import wheel_response

    if store.read_latest(symbol) is None:
        return f"<p>{html.escape(need_snapshot_text(symbol, store))}</p>"
    before = len(journal.entries())
    lines = wheel_response("wheel scan", symbol, store, journal, now).splitlines()[:6]
    screened = next(
        (e.payload for e in reversed(journal.entries()[before:]) if e.kind == "wheel_screened"),
        {},
    )
    dropped = screened.get("dropped") or {}
    funnel = ""
    if dropped:
        top = sorted(dropped.items(), key=lambda item: -item[1])[:3]
        total = sum(dropped.values())
        stages = " · ".join(f"{stage} {count}" for stage, count in top if count)
        funnel = f"<p>{total} contracts screened — dropped: {html.escape(stages)}. The funnel says no before the trader has to.</p>"
    return (
        f"<pre>{html.escape(chr(10).join(lines))}</pre>"
        f"{funnel}"
        "<div class=\"verdicts\">"
        "<span class=\"pill match\">7–30 DTE</span>"
        "<span class=\"pill match\">|Δ| 0.15–0.30</span>"
        "<span class=\"pill match\">OI ≥ 100</span>"
        "<span class=\"pill off\">≥1% premium · IV rank ≥ 30 · earnings skipped</span></div>"
        + _caption("Cash-secured puts and covered calls, ranked in plain English. Stored chains only — no keys, no network.")
    )


def build_submission_brief(
    symbol: str, store: SnapshotStore, journal: Journal, now: datetime
) -> VideoBrief:
    from bot.replay import replay_report
    from bot.shock import shock_text

    replay_text, _, _ = replay_report(symbol, store)
    shock_lines = shock_text(symbol, store, journal, now, -2.0).splitlines()[:5]
    bodies = {
        "hook": (
            "<p class=\"big\">Your AI says long.<br>Your walls say otherwise.</p>"
            "<p>Most tools show you one of those. This desk shows you both — and journals who was right.</p>"
            + _caption("MarketWingMan — a paper-first options desk with a Muse Spark audit block.")
        ),
        "engine": _gamma_body(symbol, store)
        + _caption("Home-grown GEX/VEX/DEX/CHARM/VANNA from stored chains. No vendor GEX feed."),
        "spark-audit": _spark_scene_body(symbol, store, journal),
        "top-card": _card_scene_body(symbol, store, journal, now),
        "safety": (
            "<ul><li>review_intent runs inside every submit — paper and live. No caller can skip the gate.</li>"
            "<li>Live ordering ships disabled. Arming needs a signed action, paper evidence, and a clean halt/kill state.</li>"
            "<li>Kill switch flattens auto-ordering and needs an operator reset.</li>"
            "<li>Every signal, intent, fill, and model call lands in the journal. Missing write = bug.</li></ul>"
            + _caption("The model cannot move the gate. Local code checks every claim.")
        ),
        "replay": f"<pre>{html.escape(chr(10).join(replay_text.splitlines()[:5]))}</pre>"
        + _caption("Honest replay: next-bar fills, daily halt reset, scored against buy-and-hold on the same bars."),
        "shock": f"<pre>{html.escape(chr(10).join(shock_lines))}</pre>"
        + _caption("Shock first, size second: walls broken and positions repriced at a moved spot."),
        "mcp": _mcp_scene_body(symbol, store, journal, now),
        "wheel": _wheel_scene_body(symbol, store, journal, now),
        "outro": (
            "<p class=\"big\">Spark proposes.<br>The engine grades it.</p>"
            "<p>Try it: run the app, open /demo, ask for a spark audit. Live it grades every claim; offline it answers OFF — and still works.</p>"
            f"<p>{html.escape(paper_pnl_text(journal, store).splitlines()[0] if paper_pnl_text(journal, store).splitlines() else '')}</p>"
            + _caption("Meta Global AI Developer Hackathon · Indie Developer · Build for Your World · Solo build, Oct 27–Nov 4.")
        ),
    }
    titles = {
        "hook": "The disagreement problem",
        "engine": f"{symbol} gamma — the tape",
        "spark-audit": "Muse Spark audit",
        "top-card": "Top trade card",
        "safety": "Safety gates",
        "replay": "Replay vs buy-and-hold",
        "shock": "Shock test −2%",
        "mcp": "MCP tools over HTTP",
        "wheel": "Wheel premium screen",
        "outro": "MarketWingMan",
    }
    narrations = {
        "hook": "Your AI says long. Your walls say otherwise. This desk shows you both, and journals who was right.",
        "engine": "The engine computes gamma, vega, delta, charm, and vanna exposure from stored option chains. No vendor exposure feed.",
        "spark-audit": "Muse Spark proposes a direction. The deterministic engine grades it: match, contradiction, unverified, or off. Free prose never counts.",
        "top-card": "The top trade card is sized and defined-risk, with max loss and breakeven before you ever see a buy button.",
        "safety": "Every submit passes the same risk gate. Live ordering ships disabled, and the kill switch always wins.",
        "replay": "Replay scores the strategy against buy-and-hold on the same bars, with next-bar fills and no lookahead.",
        "shock": "A minus two percent shock reprices every wall and position before any sizing decision.",
        "mcp": "One tool registry, exposed over MCP-style HTTP. Chat, slash commands, and external clients all call the same handlers.",
        "wheel": "The wheel screen ranks cash-secured puts and covered calls by premium return, filtered by delta, open interest, IV rank, and earnings.",
        "outro": "Spark proposes. The engine grades it. Try the demo, ask for a spark audit, and check the journal.",
    }
    scenes: list[VideoScene] = []
    start = 0
    for scene_id, duration in SUBMISSION_SCENES:
        scenes.append(
            VideoScene(scene_id, titles[scene_id], start, duration, bodies[scene_id], narrations[scene_id])
        )
        start += duration
    return VideoBrief(
        f"submission-{now.strftime('%Y%m%dT%H%M%S')}",
        (symbol,),
        tuple(scenes),
        start,
        now,
    )


def narration_script(brief: VideoBrief) -> str:
    lines = [
        f"narration script · {brief.composition_id} · {brief.total_seconds}s total",
        "Voice: record these lines, or draft polished variants with Muse Spark / Muse Wonder chat.",
        "No Muse text-to-speech endpoint exists in MODEL_REGISTRY (muse-voice-transcribe-1.0 is speech-to-text only), so Muse cannot voice this track in-repo.",
        "",
    ]
    for scene in brief.scenes:
        end = scene.start_seconds + scene.duration_seconds
        lines.append(f"[{scene.start_seconds:>3}s-{end:>3}s] {scene.scene_id}: {scene.narration}")
    return "\n".join(lines) + "\n"


def submission_composition_html(brief: VideoBrief, *, storyboard: bool = False) -> str:
    style = (
        "body{font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "margin:0;background:#070b14;color:#eef2f8}"
        f"div[data-composition-id]{{position:relative;width:{VIDEO_WIDTH}px;height:{VIDEO_HEIGHT}px;overflow:hidden;"
        "background:radial-gradient(1100px 500px at 15% -10%,rgba(91,140,255,.22),transparent 60%),#070b14}"
        ".clip{position:absolute;inset:0;box-sizing:border-box;padding:72px 88px;display:flex;flex-direction:column}"
        ".kicker{font-size:22px;font-weight:800;letter-spacing:.16em;text-transform:uppercase;color:#5b8cff;margin:0 0 18px}"
        "h2{font-size:64px;line-height:1.08;margin:0 0 28px;letter-spacing:-.02em}"
        "p,li{font-size:30px;line-height:1.5;color:#c7d2e4}ul{line-height:1.6}"
        ".big{font-size:58px;font-weight:800;line-height:1.15;color:#eef2f8;letter-spacing:-.02em}"
        ".caption{margin-top:auto;font-size:28px;font-weight:700;color:#eef2f8;border-top:2px solid #22304a;padding-top:22px}"
        "pre{font-size:26px;line-height:1.55;background:#0d1524;border:1px solid #22304a;border-radius:14px;padding:28px;white-space:pre-wrap}"
        ".chart-panel{background:#101827;border:1px solid #22304a;border-radius:14px;padding:16px;margin:1rem 0}"
        ".chart-panel svg{max-width:100%;height:auto}"
        f".call{{color:{CALL_COLOR}}}.put{{color:{PUT_COLOR}}}.stats{{color:#93a1b5}}"
        "table{border-collapse:collapse;min-width:60rem}th,td{border:1px solid #22304a;padding:.4rem .7rem;text-align:right;font-size:26px}"
        ".duel{display:grid;grid-template-columns:1fr auto 1fr;gap:24px;margin:12px 0 24px}"
        ".side{border:2px solid #2c3f5f;border-radius:16px;padding:28px;background:#0d1524}"
        ".side span{display:block;font-size:20px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;color:#64748f}"
        ".side b{font-size:52px}.side p{font-size:24px}.vs{align-self:center;font-weight:800;color:#64748f;font-size:24px}"
        ".verdicts{display:flex;flex-wrap:wrap;gap:14px;margin:8px 0 20px}"
        ".pill{font-size:24px;font-weight:800;border:2px solid #2c3f5f;border-radius:999px;padding:10px 20px}"
        ".pill.match{color:#34d399;border-color:#34d399}.pill.contradiction{color:#fb7185;border-color:#fb7185}"
        ".pill.off{color:#f5b83d;border-color:#f5b83d}"
    )
    clips = "".join(
        f"<section id=\"{html.escape(scene.scene_id, quote=True)}\" class=\"clip\" "
        f"data-track-index=\"{lane}\" data-start=\"{scene.start_seconds}\" "
        f"data-duration=\"{scene.duration_seconds}\">"
        f"<p class=\"kicker\">MarketWingMan · {scene.start_seconds}s</p>"
        f"<h2>{html.escape(scene.title)}</h2>{scene.body_html}</section>"
        for lane, scene in enumerate(brief.scenes, start=1)
    )
    override = ""
    if storyboard:
        override = (
            "<style id=\"storyboard-override\">"
            "div[data-composition-id]{position:static;width:auto;height:auto;max-width:1920px;margin:0 auto;padding:48px}"
            ".clip{position:static;margin:0 0 32px;min-height:600px}</style>"
        )
    from bot.theme import BRAND

    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width={VIDEO_WIDTH}, height={VIDEO_HEIGHT}\">"
        f"<title>{html.escape(brief.composition_id)} | {BRAND}</title><style>{style}</style>"
        f"{override}</head><body>"
        f"<div data-composition-id=\"{html.escape(brief.composition_id, quote=True)}\" "
        f"data-start=\"0\" data-width=\"{VIDEO_WIDTH}\" data-height=\"{VIDEO_HEIGHT}\" "
        f"data-duration=\"{brief.total_seconds}\" data-no-timeline>"
        f"{clips}</div></body></html>"
    )


def write_video_project(project_dir: str, brief: VideoBrief) -> str:
    os.makedirs(project_dir, exist_ok=True)
    path = os.path.join(project_dir, "index.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(composition_html(brief))
    return path


def write_submission_project(project_dir: str, brief: VideoBrief) -> str:
    os.makedirs(project_dir, exist_ok=True)
    path = os.path.join(project_dir, "index.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(submission_composition_html(brief))
    with open(os.path.join(project_dir, "narration.txt"), "w", encoding="utf-8") as handle:
        handle.write(narration_script(brief))
    return path


def render_video(project_dir: str, output_mp4: str) -> RenderResult:
    npx = shutil.which("npx")
    if npx is None:
        return RenderResult(
            False,
            output_mp4,
            "cannot render video because npx is missing. "
            "Install Node 22 or later so npx is on PATH, then retry.",
        )
    try:
        completed = subprocess.run(
            [npx, "hyperframes", "render", "--output", output_mp4],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=RENDER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return RenderResult(False, output_mp4, f"render timed out after {RENDER_TIMEOUT_SECONDS} seconds")
    except Exception as exc:
        return RenderResult(False, output_mp4, f"render failed to start: {exc}"[-2000:])
    if completed.returncode == 0:
        return RenderResult(True, output_mp4, "")
    tail = (completed.stderr or completed.stdout or "").strip()[-2000:]
    if not tail:
        tail = f"hyperframes render exited with code {completed.returncode}"
    return RenderResult(False, output_mp4, tail)


def video_response(
    query: str,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    project_dir: str | None = None,
) -> str:
    del query
    from bot.schedule import normalize_underlyings, tick_symbols

    symbols = tick_symbols(normalize_underlyings(underlying), store)[:MAX_SYMBOLS]
    brief = build_video_brief(symbols, store, journal, now)
    target = project_dir or DEFAULT_PROJECT_DIR
    path = write_video_project(target, brief)
    journal.append(
        now,
        "video_brief",
        {
            "underlyings": list(symbols),
            "scenes": len(brief.scenes),
            "total_seconds": brief.total_seconds,
            "project_dir": target,
        },
    )
    return (
        f"video brief: {len(brief.scenes)} scenes, {brief.total_seconds}s total\n"
        f"project: {path}\n"
        f"render: cd {target} && npx hyperframes render --output brief.mp4"
    )
