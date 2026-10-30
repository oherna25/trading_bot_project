from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from bot.config import read_dotenv as _read_dotenv
from bot.journal import Journal
from bot.localmodels import LOCAL_REGISTRY, local_available, local_weights
from bot.models import MODEL_REGISTRY, select_backend, spend_today

TASK_CLASS = {
    "reason": "draft",
    "narrate": "draft",
    "triage": "draft",
    "route": "draft",
    "classify": "draft",
    "analyst": "deep",
    "debate": "deep",
    "chart": "deep",
    "shock_news": "deep",
    "portfolio": "deep",
    "share": "deep",
    "review": "premium",
    "coach": "premium",
}

CLASS_ORDER = {
    "draft": ("local", "analyst"),
    "deep": ("analyst", "local"),
    "premium": ("reviewer", "analyst", "local"),
}

DEFAULT_TASK_CLASS = "deep"


@dataclass(frozen=True)
class TierDecision:
    task: str
    tier: str
    role: str
    model_id: str
    reason: str


def task_class(task: str) -> str:
    return TASK_CLASS.get(task, DEFAULT_TASK_CLASS)


def select_model_tier(
    task: str, journal: Journal, now: datetime, env: Mapping[str, str] | None = None
) -> TierDecision:
    klass = task_class(task)
    last_reason = ""
    for step in CLASS_ORDER[klass]:
        if step == "local":
            if local_available(env):
                name, _ = local_weights(env) or ("", "")
                model_id = LOCAL_REGISTRY[name].model_id if name else ""
                decision = TierDecision(task, "local", "local", model_id, "")
                journal.append(
                    now, "tier_selected",
                    {"task": task, "class": klass, "tier": "local", "model": model_id},
                )
                return decision
            if not last_reason:
                last_reason = "no local weights"
            continue
        backend, reason = select_backend(step, journal, now, env)
        if backend == "model":
            model_id = MODEL_REGISTRY[step].model_id
            decision = TierDecision(task, "model", step, model_id, "")
            journal.append(
                now, "tier_selected",
                {"task": task, "class": klass, "tier": "model", "role": step, "model": model_id},
            )
            return decision
        last_reason = reason
    decision = TierDecision(task, "fallback", "fallback", "", last_reason)
    journal.append(
        now, "tier_selected",
        {"task": task, "class": klass, "tier": "fallback", "reason": last_reason},
    )
    return decision


def _day_budget(env: Mapping[str, str] | None) -> float | None:
    if env is not None:
        values = dict(env)
    else:
        file_values = _read_dotenv()
        raw = os.environ.get("MODEL_DAY_BUDGET_USD") or file_values.get("MODEL_DAY_BUDGET_USD")
        values = {"MODEL_DAY_BUDGET_USD": raw} if raw else {}
    try:
        return float(values.get("MODEL_DAY_BUDGET_USD", ""))
    except (TypeError, ValueError):
        return None


def format_tier_status(status: Mapping[str, object]) -> str:
    spend = status.get("spend_today_usd", 0.0)
    budget = status.get("day_budget_usd")
    try:
        spend_text = f"${float(spend):,.2f}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        spend_text = "$0.00"
    budget_text = f"${float(budget):,.2f}" if isinstance(budget, (int, float)) else "unset"
    lines = [
        f"tier status: spend {spend_text} / {budget_text} today",
        f"local: {status.get('local_model') or 'none'}",
    ]
    for klass, order in CLASS_ORDER.items():
        lines.append(f"{klass}: {' -> '.join(order)}")
    return "\n".join(lines)


def tier_status(
    journal: Journal, now: datetime, env: Mapping[str, str] | None = None
) -> dict[str, object]:
    found = local_weights(env)
    name = found[0] if found is not None else ""
    return {
        "spend_today_usd": spend_today(journal, now),
        "day_budget_usd": _day_budget(env),
        "local_available": found is not None,
        "local_model": LOCAL_REGISTRY[name].model_id if name else None,
        "tasks": dict(TASK_CLASS),
    }
