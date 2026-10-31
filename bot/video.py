from __future__ import annotations

import html
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from bot.charts import CALL_COLOR, PUT_COLOR, board_rows, gamma_svg
from bot.diff import compact_dollars
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
            f"expiry {idea.expiry} confidence {idea.confidence:.2f}: {idea.thesis} | {idea.trigger}"
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


def write_video_project(project_dir: str, brief: VideoBrief) -> str:
    os.makedirs(project_dir, exist_ok=True)
    path = os.path.join(project_dir, "index.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(composition_html(brief))
    return path


def render_video(project_dir: str, output_mp4: str) -> RenderResult:
    if shutil.which("npx") is None:
        return RenderResult(
            False,
            output_mp4,
            "cannot render video because npx is missing. "
            "Install Node 22 or later so npx is on PATH, then retry.",
        )
    try:
        completed = subprocess.run(
            ["npx", "hyperframes", "render", "--output", output_mp4],
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
