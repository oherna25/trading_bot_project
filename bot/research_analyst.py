"""Analyst backends: Spark, local, grounded, advisory."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Mapping, Optional

from bot.ideas import (
    resolve_direction,
)
from bot.journal import Journal
from bot.localmodels import LocalRunner, local_available, run_local
from bot.tiers import select_model_tier
from bot.models import (
    MODEL_REGISTRY,
    HttpPost,
    gex_tool_result,
    model_api_key,
    prompt_hash,
    receipt_payload,
    run_analyst,
    run_responses,
    select_backend,
)
from bot.shapes import (
    Bar,
    ChainSnapshot,
    GexProfile,
    PaperEvidence,
    Signal,
)

def analyst_tool(text: str, snapshot: ChainSnapshot, profile: GexProfile, bar: Optional[Bar]) -> Signal:
    direction = resolve_direction(bar, profile.flip_point, profile.total_exposure)
    return Signal(
        direction=direction,
        thesis=f"analyst read on {snapshot.underlying} for query: {text}",
        confidence=0.5,
        snapshot_id="",
    )


ANALYST_SYSTEM = (
    "You must call get_gex_profile exactly once for the underlying before answering. "
    "Reply with a one-paragraph thesis, then two lines, exactly: "
    "DIRECTION: long|short|flat and CONFIDENCE: 0 to 1."
)

GEX_TOOL = {
    "type": "function",
    "function": {
        "name": "get_gex_profile",
        "description": "Read the computed GEX profile for an underlying",
        "parameters": {
            "type": "object",
            "properties": {"underlying": {"type": "string"}},
            "required": ["underlying"],
        },
    },
}


def _analyst_prompt(text: str, snapshot: ChainSnapshot, profile: GexProfile, bar: Optional[Bar]) -> str:
    summary = {
        "query": text,
        "underlying": snapshot.underlying,
        "total_exposure": profile.total_exposure,
        "flip_point": profile.flip_point,
        "per_strike_exposure": profile.per_strike_exposure,
        "bar_close": bar.close if bar is not None else None,
    }
    return f"Snapshot: {json.dumps(summary)}"


def _parse_direction(text: str, fallback: str) -> str:
    match = re.search(r"DIRECTION:\s*(long|short|flat)", text, re.IGNORECASE)
    return match.group(1).lower() if match else fallback


def _parse_confidence(text: str) -> float:
    match = re.search(r"CONFIDENCE:\s*([0-9]*\.?[0-9]+)", text)
    if not match:
        return 0.5
    try:
        return min(1.0, max(0.0, float(match.group(1))))
    except ValueError:
        return 0.5


def _strip_markers(text: str) -> str:
    lines = [
        line
        for line in text.splitlines()
        if not re.match(r"\s*(DIRECTION|CONFIDENCE)\s*:", line, re.IGNORECASE)
    ]
    return "\n".join(lines).strip()


def spark_signal(
    text: str,
    snapshot_id: str,
    snapshot: ChainSnapshot,
    profile: GexProfile,
    bar: Optional[Bar],
    journal: Journal,
    now: datetime,
    env: Mapping[str, str] | None,
    http_post: HttpPost | None,
    grounding: bool = False,
) -> Signal:
    spec = MODEL_REGISTRY["analyst"]
    prompt = _analyst_prompt(text, snapshot, profile, bar)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": ANALYST_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    reply, receipts = run_analyst(
        spec,
        model_api_key(env),
        messages,
        [GEX_TOOL],
        {
            "get_gex_profile": lambda args: gex_tool_result(
                snapshot, snapshot_id, args, spot=bar.close if bar is not None else None
            )
        },
        prompt,
        http_post,
    )
    for receipt in receipts:
        journal.append(now, "model_receipt", receipt_payload(receipt, snapshot_id))
    thesis = _strip_markers(reply)
    if not thesis:
        raise ValueError("empty model thesis")
    if grounding:
        note = grounded_note(
            f"Is there current market news that contradicts this thesis on {snapshot.underlying}? One sentence.",
            journal,
            now,
            snapshot_id,
            env,
            http_post,
        )
        if note:
            thesis = f"{thesis}\nGrounded note: {note}"
    fallback_direction = analyst_tool(text, snapshot, profile, bar).direction
    return Signal(
        direction=_parse_direction(reply, fallback_direction),
        thesis=thesis,
        confidence=_parse_confidence(reply),
        snapshot_id=snapshot_id,
    )


def grounded_note(
    question: str,
    journal: Journal,
    now: datetime,
    snapshot_id: str,
    env: Mapping[str, str] | None,
    http_post: HttpPost | None,
) -> str:
    backend, reason = select_backend("grounding", journal, now, env)
    if backend != "model":
        journal.append(now, "grounding_skipped", {"reason": reason})
        return ""
    try:
        text, receipt = run_responses(
            MODEL_REGISTRY["grounding"],
            model_api_key(env),
            question,
            [{"type": "web_search"}],
            question,
            "grounding",
            http_post,
        )
    except Exception as exc:
        journal.append(now, "grounding_skipped", {"reason": type(exc).__name__})
        return ""
    journal.append(now, "model_receipt", receipt_payload(receipt, snapshot_id))
    return " ".join(text.split())


REVIEW_SYSTEM = (
    "You review paper-trading evidence for a go-live decision. Reply with a short risk review: "
    "top 3 concerns, then one line VERDICT: ready|not-ready. You advise only and approve nothing."
)


def _review_prompt(evidence: PaperEvidence, journal: Journal) -> str:
    kinds: dict[str, int] = {}
    signals = []
    for entry in journal.entries():
        kinds[entry.kind] = kinds.get(entry.kind, 0) + 1
        if entry.kind == "signal":
            signals.append(entry.payload)
    return json.dumps(
        {
            "evidence": {
                "clean_days": evidence.clean_days,
                "clean_days_target": evidence.clean_days_target,
                "max_drawdown": evidence.max_drawdown,
                "drawdown_bound": evidence.drawdown_bound,
                "halt_drills_passed": evidence.halt_drills_passed,
            },
            "journal_kinds": kinds,
            "recent_signals": signals[-3:],
        }
    )


def advisory_review(
    evidence: PaperEvidence,
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> str:
    if not live_models:
        journal.append(now, "review_skipped", {"reason": "live models off"})
        return ""
    backend, reason = select_backend("reviewer", journal, now, env)
    if backend != "model":
        journal.append(now, "review_skipped", {"reason": reason})
        return ""
    prompt = _review_prompt(evidence, journal)
    try:
        text, receipts = run_analyst(
            MODEL_REGISTRY["reviewer"],
            model_api_key(env),
            [
                {"role": "system", "content": REVIEW_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            [],
            {},
            prompt,
            http_post,
            reasoning_effort="high",
        )
    except Exception as exc:
        journal.append(now, "review_skipped", {"reason": type(exc).__name__})
        return ""
    for receipt in receipts:
        journal.append(now, "model_receipt", receipt_payload(receipt, ""))
    note = " ".join(text.split())
    if not note:
        journal.append(now, "review_skipped", {"reason": "empty review"})
        return ""
    journal.append(now, "review_note", {"note": note, "prompt_hash": prompt_hash(prompt)})
    return note


def local_signal(
    text: str,
    snapshot_id: str,
    snapshot: ChainSnapshot,
    profile: GexProfile,
    bar: Optional[Bar],
    journal: Journal,
    now: datetime,
    grounding: bool = False,
    env: Mapping[str, str] | None = None,
    runner: LocalRunner | None = None,
) -> Signal:
    prompt = _analyst_prompt(text, snapshot, profile, bar)
    reply, receipt = run_local("analyst", prompt, env, runner=runner)
    journal.append(now, "model_receipt", receipt_payload(receipt, snapshot_id))
    thesis = _strip_markers(reply)
    if not thesis:
        raise ValueError("empty local thesis")
    if grounding:
        journal.append(now, "grounding_skipped", {"reason": "local tier"})
    fallback_direction = analyst_tool(text, snapshot, profile, bar).direction
    return Signal(
        direction=_parse_direction(reply, fallback_direction),
        thesis=thesis,
        confidence=_parse_confidence(reply),
        snapshot_id=snapshot_id,
    )


def analyst_backend(
    text: str,
    snapshot_id: str,
    snapshot: ChainSnapshot,
    profile: GexProfile,
    bar: Optional[Bar],
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    grounding: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    local_runner: LocalRunner | None = None,
) -> Signal:
    if not live_models:
        journal.append(now, "model_fallback", {"reason": "live models off"})
        return analyst_tool(text, snapshot, profile, bar)
    decision = select_model_tier("analyst", journal, now, env)
    if decision.tier == "local":
        try:
            return local_signal(
                text, snapshot_id, snapshot, profile, bar, journal, now, grounding, env, local_runner
            )
        except Exception as exc:
            journal.append(now, "model_fallback", {"reason": type(exc).__name__})
            return analyst_tool(text, snapshot, profile, bar)
    if decision.tier == "fallback":
        journal.append(now, "model_fallback", {"reason": decision.reason})
        return analyst_tool(text, snapshot, profile, bar)
    try:
        return spark_signal(text, snapshot_id, snapshot, profile, bar, journal, now, env, http_post, grounding)
    except Exception as exc:
        if local_available(env):
            try:
                return local_signal(
                    text, snapshot_id, snapshot, profile, bar, journal, now, grounding, env, local_runner
                )
            except Exception:
                pass
        journal.append(now, "model_fallback", {"reason": type(exc).__name__})
        return analyst_tool(text, snapshot, profile, bar)
