from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from bot.journal import Journal
from bot.localmodels import LocalRunner, run_local
from bot.models import (
    MODEL_REGISTRY,
    HttpPost,
    model_api_key,
    prompt_hash,
    receipt_payload,
    run_analyst,
)
from bot.store import SnapshotStore
from bot.tiers import select_model_tier

COACH_SYSTEM = (
    "You are a trading coach reading a journaled paper-trading record. "
    "Reply with two short sections, Keep: and Fix:, three bullets each, "
    "every bullet citing one journaled fact from the prompt. "
    "You advise only and approve nothing."
)


def journal_stats(journal: Journal) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "fills": 0,
        "symbols": [],
        "buys": 0,
        "sells": 0,
        "halts": 0,
        "kills": 0,
        "vetoed": 0,
        "approved": 0,
        "veto_reasons": {},
        "signals": {"long": 0, "short": 0, "flat": 0},
        "alerts": 0,
        "days": 0,
    }
    symbols: set[str] = set()
    stamps = []
    for entry in journal.entries():
        stamps.append(entry.timestamp_utc)
        payload = entry.payload
        if entry.kind == "fill":
            stats["fills"] += 1
            if payload.get("side") == "sell":
                stats["sells"] += 1
            else:
                stats["buys"] += 1
            if payload.get("symbol"):
                symbols.add(str(payload["symbol"]))
        elif entry.kind == "halt":
            stats["halts"] += 1
        elif entry.kind in ("kill_fired", "kill"):
            stats["kills"] += 1
        elif entry.kind == "intent_vetoed":
            stats["vetoed"] += 1
            reason = str(payload.get("reason", "unknown"))
            stats["veto_reasons"][reason] = stats["veto_reasons"].get(reason, 0) + 1
        elif entry.kind == "intent_approved":
            stats["approved"] += 1
        elif entry.kind == "signal":
            direction = str(payload.get("direction", "flat"))
            if direction in stats["signals"]:
                stats["signals"][direction] += 1
        elif entry.kind == "alert":
            stats["alerts"] += 1
    stats["symbols"] = sorted(symbols)
    if stamps:
        stats["days"] = (max(stamps).date() - min(stamps).date()).days + 1
    return stats


def fallback_coach(stats: Mapping[str, Any]) -> str:
    keep = []
    fix = []
    fills = int(stats.get("fills", 0))
    if fills:
        keep.append(f"{fills} fills journaled across {len(stats.get('symbols', []))} symbols")
    else:
        fix.append("no fills yet: run one paper session to generate reviewable trades")
    kills = int(stats.get("kills", 0))
    if kills:
        fix.append(f"kill switch fired {kills} times: size down before re-arming")
    else:
        keep.append("no kill fired: risk stayed inside the gate")
    halts = int(stats.get("halts", 0))
    if halts:
        fix.append(f"halt tripped {halts} times: respect the daily loss line earlier")
    vetoed = int(stats.get("vetoed", 0))
    reasons = stats.get("veto_reasons", {})
    if vetoed and reasons:
        top = max(reasons, key=lambda reason: reasons[reason])
        fix.append(f"{vetoed} vetoed intents (top: {top}): pre-check size before submitting")
    elif vetoed:
        fix.append(f"{vetoed} vetoed intents: pre-check size before submitting")
    signals = stats.get("signals", {})
    if signals.get("long") and signals.get("short"):
        keep.append(
            f"signals split long {signals['long']} / short {signals['short']}: reading both sides"
        )
    if len(keep) < 2:
        keep.append("keep the journal running: every signal lands with its snapshot id")
    if len(fix) < 2:
        fix.append("review one stopped trade per week against its original thesis")
    lines = ["coach [rules]:", "Keep:"]
    lines.extend(f"- {line}" for line in keep[:3])
    lines.append("Fix:")
    lines.extend(f"- {line}" for line in fix[:3])
    return "\n".join(lines)


def coach_review(
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    local_runner: LocalRunner | None = None,
) -> str:
    del store
    stats = journal_stats(journal)
    if not live_models:
        journal.append(now, "coach_skipped", {"reason": "live models off"})
        return fallback_coach(stats)
    decision = select_model_tier("coach", journal, now, env)
    prompt = json.dumps(stats, sort_keys=True, default=str)
    try:
        if decision.tier == "local":
            text, receipt = run_local("coach", prompt, env, runner=local_runner)
            receipts = [receipt]
            model_id = receipt.model_id
        elif decision.tier == "model":
            spec = MODEL_REGISTRY[decision.role]
            text, receipts = run_analyst(
                spec,
                model_api_key(env),
                [
                    {"role": "system", "content": COACH_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                [],
                {},
                prompt,
                http_post,
            )
            model_id = spec.model_id
        else:
            journal.append(now, "coach_skipped", {"reason": decision.reason})
            return fallback_coach(stats)
    except Exception as exc:
        journal.append(now, "coach_skipped", {"reason": type(exc).__name__})
        return fallback_coach(stats)
    note = " ".join(text.split())
    if not note:
        journal.append(now, "coach_skipped", {"reason": "empty coach note"})
        return fallback_coach(stats)
    for receipt in receipts:
        journal.append(now, "model_receipt", receipt_payload(receipt, ""))
    journal.append(now, "coach_note", {"note": note, "prompt_hash": prompt_hash(prompt)})
    return f"coach [{model_id}]:\n{note}"
