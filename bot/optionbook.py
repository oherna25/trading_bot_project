from __future__ import annotations

from typing import Any

from bot.journal import Journal
from bot.shapes import OpenOptionPosition, SellLeg


def position_key(deployment_id: str, symbol: str, legs: tuple[SellLeg, ...], expiry: str) -> str:
    leg_text = "+".join(f"{leg.side}:{leg.call_or_put}:{leg.strike:g}" for leg in legs)
    return f"{deployment_id}|{symbol}|{expiry}|{leg_text}"


def _legs_from(raw: Any) -> tuple[SellLeg, ...]:
    legs: list[SellLeg] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            strike = item.get("strike")
            if strike is None:
                continue
            try:
                legs.append(
                    SellLeg(
                        side=str(item.get("side", "")),
                        call_or_put=str(item.get("call_or_put", "")),
                        strike=float(strike),  # type: ignore[arg-type]
                    )
                )
            except (TypeError, ValueError):
                continue
    return tuple(legs)


def _traverse(journal: Journal) -> tuple[list[OpenOptionPosition], list[dict[str, Any]]]:
    open_slots: dict[str, dict[str, Any]] = {}
    closed: list[dict[str, Any]] = []
    for entry in journal.entries():
        payload = entry.payload
        if not payload.get("option_position"):
            continue
        if entry.kind not in ("fill", "live_fill", "option_settled"):
            continue
        action = str(payload.get("trade_action", ""))
        key = str(payload.get("position_key", ""))
        contracts = float(payload.get("contracts") or 0.0)
        if not key or contracts <= 0:
            continue
        if action == "open":
            legs = _legs_from(payload.get("legs"))
            slot = open_slots.get(key)
            if slot is None:
                open_slots[key] = {
                    "key": key,
                    "deployment_id": str(payload.get("deployment_id", "")),
                    "strategy_id": str(payload.get("strategy_id", "")),
                    "symbol": str(payload.get("symbol", "")),
                    "action": str(payload.get("position_action", "")),
                    "legs": legs,
                    "expiry": str(payload.get("expiry", "")),
                    "contracts": contracts,
                    "entry_credit": float(payload.get("entry_credit") or 0.0),
                    "collateral_per_contract": float(payload.get("collateral_per_contract") or 0.0),
                    "opened_date": str(payload.get("opened_date") or entry.timestamp_utc.date().isoformat()),
                    "snapshot_id": str(payload.get("snapshot_id", "")),
                    "venue": str(payload.get("venue", "paper")),
                }
            else:
                total = slot["contracts"] + contracts
                slot["entry_credit"] = (
                    slot["entry_credit"] * slot["contracts"] + float(payload.get("entry_credit") or 0.0) * contracts
                ) / total
                slot["contracts"] = total
            continue
        if action in ("close", "settlement"):
            slot = open_slots.get(key)
            if slot is None:
                continue
            take = min(contracts, float(slot["contracts"]))
            exit_debit = float(payload.get("exit_debit", payload.get("price") or 0.0))
            entry_credit = float(slot["entry_credit"])
            pnl = (entry_credit - exit_debit) * take * 100.0
            closed.append(
                {
                    "key": key,
                    "deployment_id": slot["deployment_id"],
                    "strategy_id": slot["strategy_id"],
                    "symbol": slot["symbol"],
                    "contracts": take,
                    "entry_credit": entry_credit,
                    "exit_debit": exit_debit,
                    "pnl": pnl,
                    "opened_date": slot["opened_date"],
                    "closed_date": entry.timestamp_utc.date().isoformat(),
                    "reason": str(payload.get("reason", action)),
                    "expiry": slot["expiry"],
                }
            )
            slot["contracts"] = float(slot["contracts"]) - take
            if slot["contracts"] <= 1e-9:
                del open_slots[key]
    positions = [
        OpenOptionPosition(
            key=str(slot["key"]),
            deployment_id=str(slot["deployment_id"]),
            strategy_id=str(slot["strategy_id"]),
            symbol=str(slot["symbol"]),
            action=str(slot["action"]),
            legs=tuple(slot["legs"]),
            expiry=str(slot["expiry"]),
            contracts=float(slot["contracts"]),
            entry_credit=float(slot["entry_credit"]),
            collateral_per_contract=float(slot["collateral_per_contract"]),
            opened_date=str(slot["opened_date"]),
            snapshot_id=str(slot["snapshot_id"]),
            venue=str(slot["venue"]),
        )
        for slot in open_slots.values()
    ]
    positions.sort(key=lambda item: (item.symbol, item.expiry, item.key))
    return positions, closed


def open_option_positions(journal: Journal, deployment_id: str | None = None) -> list[OpenOptionPosition]:
    positions, _ = _traverse(journal)
    if deployment_id is None:
        return positions
    return [item for item in positions if item.deployment_id == deployment_id]


def closed_option_trades(journal: Journal, deployment_id: str | None = None) -> list[dict[str, Any]]:
    _, closed = _traverse(journal)
    if deployment_id is None:
        return closed
    return [item for item in closed if item["deployment_id"] == deployment_id]
