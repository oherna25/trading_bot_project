"""Muse Spark audit block: model proposes, engine disposes.

Spark is the hero for the Meta hackathon. The analyst model proposes a
direction and confidence. This module checks that claim against the
computed GEX profile and journals both sides. Deterministic math always
has the last word.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional

import html

from bot.diff import pct
from bot.exposure import compute_gex
from bot.ideas import resolve_direction
from bot.journal import Journal
from bot.models import HttpPost
from bot.store import SnapshotStore, need_snapshot_text


MATCH = "MATCH"
CONTRADICTION = "CONTRADICTION"
UNVERIFIED = "UNVERIFIED"
OFF = "OFF"


@dataclass(frozen=True)
class SparkClaim:
    direction: str
    confidence: float
    thesis: str
    model_id: str


@dataclass(frozen=True)
class SparkVerdict:
    status: str
    detail: str


def verify_spark_claim(claimed: str, expected: str) -> SparkVerdict:
    claimed = (claimed or "").strip().lower()
    expected = (expected or "").strip().lower()
    if not claimed or not expected:
        return SparkVerdict(UNVERIFIED, "missing direction on one side")
    if claimed == expected:
        return SparkVerdict(MATCH, f"model {claimed} matches engine {expected}")
    return SparkVerdict(CONTRADICTION, f"model {claimed} vs engine {expected}")


def expected_direction(
    bar_close: Optional[float],
    flip_point: Optional[float],
    total_exposure: float,
) -> str:
    if flip_point is not None and bar_close is not None:
        return "long" if bar_close > flip_point else "short"
    if total_exposure > 0:
        return "long"
    if total_exposure < 0:
        return "short"
    return "flat"


def format_spark_block(
    claim: Optional[SparkClaim], verdict: SparkVerdict, expected: str
) -> str:
    if claim is None:
        return f"spark audit: {verdict.status} -- {verdict.detail}"
    lines = [
        f"spark audit: {verdict.status} (model {claim.direction} vs engine {expected})",
        f"model: {claim.model_id} confidence {pct(claim.confidence)}",
        f"thesis: {claim.thesis}",
        f"check: {verdict.detail}",
        "note: free prose is unverified, only direction vs engine counts",
    ]
    return "\n".join(lines)


def latest_spark_audit(journal: Journal) -> tuple[str, str, str, float | None]:
    status, expected, model_dir, confidence = OFF, "", "", None
    for entry in journal.entries():
        if entry.kind == "spark_claim":
            model_dir = str(entry.payload.get("direction", "") or "")
            raw = entry.payload.get("confidence")
            confidence = float(raw) if isinstance(raw, (int, float)) else None
        elif entry.kind == "spark_verdict":
            status = str(entry.payload.get("status", "") or OFF)
            expected = str(entry.payload.get("expected", "") or expected)
    return status, expected, model_dir, confidence


def spark_panel_html(store: SnapshotStore, journal: Journal, underlying: str) -> str:
    found = store.read_latest(underlying)
    if found is None:
        return ""
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(underlying)
    spot = bar.close if bar is not None else None
    profile = compute_gex(snapshot, snapshot_id, spot=spot)
    engine = resolve_direction(bar, profile.flip_point, profile.total_exposure)
    status, _expected, model_dir, confidence = latest_spark_audit(journal)
    cls = {"MATCH": "match", "CONTRADICTION": "contradiction"}.get(status, "off")
    if status == OFF or not model_dir:
        verdict = '<span class="pill verdict off">SPARK AUDIT · OFF</span>'
        model_side = "<span>Spark (live models)</span><b>not run yet</b><p class=\"sub\">Turn live models on and ask for a spark audit. The deterministic engine still answers offline.</p>"
        note = "Offline by design: no key, no problem. The engine grades every claim when Spark is on."
    else:
        verdict = f'<span class="pill verdict {cls}">SPARK AUDIT · {html.escape(status)}</span>'
        conf = f" · confidence {pct(confidence)}" if confidence is not None else ""
        model_side = (
            f"<span>Muse Spark proposes</span><b>{html.escape(model_dir)}</b>"
            f"<p class=\"sub\">muse-spark-1.3{html.escape(conf)}</p>"
        )
        note = (
            "Spark proposed. The engine graded it. Both landed in the journal — free prose never counts, only direction vs engine."
            if status == "MATCH"
            else "Disagreement is the product: the model and the tape differ, so sizing waits. Both sides are journaled."
        )
    engine_side = (
        f"<span>Engine disposes</span><b>{html.escape(engine)}</b>"
        f"<p class=\"sub\">spot {spot:.2f} vs flip {profile.flip_point:.1f}</p>"
        if spot is not None and profile.flip_point is not None
        else f"<span>Engine disposes</span><b>{html.escape(engine)}</b><p class=\"sub\">from total GEX {profile.total_exposure:,.0f}</p>"
    )
    return (
        "<section class=\"panel spark-panel\" aria-label=\"Muse Spark audit\">"
        "<span class=\"eyebrow\">Meta · Muse Spark audit</span>"
        f"<h2 style=\"margin-top:.4rem\">Spark proposes. The engine grades it. {verdict}</h2>"
        f"<div class=\"duel\"><div class=\"side\">{model_side}</div>"
        "<div class=\"vs\">VS</div>"
        f"<div class=\"side\">{engine_side}</div></div>"
        f"<p class=\"sub\">{html.escape(note)}</p>"
        "<p><a class=\"btn\" href=\"/action?tool=get_spark_audit&amp;underlying="
        f"{html.escape(underlying, quote=True)}\">Run spark audit</a> "
        "<span class=\"sub\">journals spark_claim + spark_verdict</span></p>"
        "</section>"
    )


def spark_audit_response(
    text: str,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    grounding: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    local_runner=None,
) -> str:
    from bot.research_analyst import analyst_backend

    found = store.read_latest(underlying)
    if found is None:
        raise ValueError(need_snapshot_text(underlying, store))
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(underlying)
    spot = bar.close if bar is not None else None
    profile = compute_gex(snapshot, snapshot_id, spot=spot)
    expected = resolve_direction(bar, profile.flip_point, profile.total_exposure)

    signal = analyst_backend(
        text or f"spark audit on {underlying}",
        snapshot_id,
        snapshot,
        profile,
        bar,
        journal,
        now,
        live_models,
        grounding,
        env,
        http_post,
        local_runner,
    )
    if not live_models:
        verdict = SparkVerdict(OFF, "live models off, deterministic engine only")
        journal.append(
            now, "spark_verdict", {"status": verdict.status, "expected": expected}
        )
        return format_spark_block(None, verdict, expected)

    claim = SparkClaim(
        direction=signal.direction,
        confidence=signal.confidence,
        thesis=signal.thesis,
        model_id="muse-spark-1.3",
    )
    journal.append(
        now,
        "spark_claim",
        {
            "direction": claim.direction,
            "confidence": claim.confidence,
            "model_id": claim.model_id,
            "snapshot_id": snapshot_id,
        },
    )
    verdict = verify_spark_claim(claim.direction, expected)
    journal.append(
        now,
        "spark_verdict",
        {"status": verdict.status, "expected": expected, "detail": verdict.detail},
    )
    return format_spark_block(claim, verdict, expected)
