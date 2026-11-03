from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from bot import baseten
from bot.journal import Journal
from bot.models import HttpPost
from bot.shapes import AgentProposal, SellCandidate, StrategyDeployment


def candidate_payload(candidate: SellCandidate, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "symbol": candidate.symbol,
        "action": candidate.action,
        "legs": [
            {"side": leg.side, "call_or_put": leg.call_or_put, "strike": leg.strike}
            for leg in candidate.legs
        ],
        "expiry": candidate.expiry,
        "dte": candidate.dte,
        "credit": round(candidate.credit, 4),
        "collateral_per_contract": round(candidate.collateral_per_contract, 2),
        "annualized_return": round(candidate.annualized_return, 4),
        "delta": candidate.delta,
        "spot": candidate.spot,
    }


def parse_proposal(raw: Any, candidate_count: int, backend: str, model_id: str) -> AgentProposal:
    if isinstance(raw, dict):
        body = raw
    elif isinstance(raw, str):
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("agent returned no JSON")
        parsed = json.loads(raw[start : end + 1])
        if not isinstance(parsed, dict):
            raise ValueError("agent returned bad JSON")
        body = parsed
    else:
        raise ValueError("agent returned bad JSON")
    try:
        index = int(body.get("index"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("agent index must be an integer") from None
    rationale = str(body.get("rationale") or body.get("reason") or "").strip()
    if not rationale:
        raise ValueError("agent rationale required")
    if index == -1:
        return AgentProposal("skip", -1, rationale, backend, model_id)
    if not 0 <= index < candidate_count:
        raise ValueError("agent index out of range")
    return AgentProposal("open", index, rationale, backend, model_id)


def propose_order(
    candidates: Sequence[SellCandidate],
    deployment: StrategyDeployment,
    journal: Journal,
    now: datetime,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> tuple[list[SellCandidate], Optional[AgentProposal]]:
    ordered = sorted(candidates, key=lambda item: -item.annualized_return)
    if len(ordered) <= 1:
        return ordered, None
    backend, reason = baseten.select_reason_backend(journal, now, env)
    if backend != "baseten":
        journal.append(
            now, "agent_fallback",
            {"deployment_id": deployment.id, "symbol": ordered[0].symbol, "reason": reason, "candidates": len(ordered)},
        )
        return ordered, None
    payload = [candidate_payload(item, index) for index, item in enumerate(ordered)]
    try:
        index, rationale, latency_ms, model_id = baseten.generate_pick(payload, env, http_post)
        proposal = parse_proposal({"index": index, "rationale": rationale}, len(ordered), "baseten", model_id)
    except Exception as exc:
        journal.append(
            now, "agent_proposal_rejected",
            {"deployment_id": deployment.id, "symbol": ordered[0].symbol, "reason": type(exc).__name__, "backend": "baseten"},
        )
        return ordered, None
    journal.append(
        now, "agent_proposal",
        {
            "deployment_id": deployment.id,
            "strategy_id": deployment.strategy_id,
            "symbol": ordered[0].symbol,
            "action": proposal.action,
            "candidate_index": proposal.candidate_index,
            "candidates": len(ordered),
            "rationale": proposal.rationale[:280],
            "backend": proposal.backend,
            "model_id": proposal.model_id,
            "latency_ms": round(latency_ms, 2),
        },
    )
    if proposal.action == "skip":
        return [], proposal
    chosen = ordered[proposal.candidate_index]
    rest = [item for item in ordered if item is not chosen]
    return [chosen, *rest], proposal


def run_agent_cycle(store: Any, journal: Journal, now: datetime, underlying: str = "SPY") -> str:
    from bot.deployments import deployments_text, run_deployments, strategy_trackrecord_text

    steps: list[str] = []
    before = len(journal.entries())
    journal.append(now, "agent_step", {"step": "observe", "underlying": underlying})
    deployments = store.list_deployments()
    steps.append(f"observe: {len(deployments)} deployment(s), symbols {underlying}")
    journal.append(now, "agent_step", {"step": "propose_and_act", "deployments": len(deployments)})
    acted = run_deployments(store, journal, now)
    if acted:
        steps.append(f"propose+act: {acted}")
    else:
        steps.append("propose+act: nothing opened or closed")
    journal.append(now, "agent_step", {"step": "verify", "underlying": underlying})
    steps.append(f"verify: {deployments_text(store, journal).splitlines()[0]}")
    steps.append(strategy_trackrecord_text(journal))
    new_kinds = [entry.kind for entry in journal.entries()[before:]]
    journal.append(
        now, "agent_run",
        {
            "underlying": underlying,
            "deployments": len(deployments),
            "proposals": new_kinds.count("agent_proposal"),
            "rejected": new_kinds.count("agent_proposal_rejected"),
            "fallbacks": new_kinds.count("agent_fallback"),
            "opened": new_kinds.count("strategy_opened"),
            "closed": new_kinds.count("strategy_closed"),
        },
    )
    return "agent cycle:\n" + "\n".join(f"  {line}" for line in steps)
