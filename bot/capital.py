from __future__ import annotations

from bot.shapes import CapitalLedger, OpenOptionPosition, StrategyDeployment


def deployed_collateral(positions: list[OpenOptionPosition]) -> float:
    return sum(item.contracts * item.collateral_per_contract for item in positions)


def ledger_for(deployment: StrategyDeployment, positions: list[OpenOptionPosition]) -> CapitalLedger:
    deployed = deployed_collateral(positions)
    cap = deployment.capital_cap
    target_amount = cap * deployment.target_utilization
    return CapitalLedger(
        capital_cap=cap,
        deployed_collateral=deployed,
        idle_capital=max(0.0, cap - deployed),
        utilization=(deployed / cap) if cap > 0 else 0.0,
        target_utilization=deployment.target_utilization,
        available_for_new=max(0.0, target_amount - deployed),
    )
