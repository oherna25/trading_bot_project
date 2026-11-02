from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

from bot.capital import ledger_for
from bot.journal import Journal
from bot.optionbook import closed_option_trades, open_option_positions, position_key
from bot.profile import suggest_params
from bot.risk import IntentRejectedError, build_intent, new_paper_account, record_paper_state, roll_paper_account, submit_paper
from bot.shapes import (
    OpenOptionPosition,
    OrderIntent,
    OrderLeg,
    PortfolioView,
    RiskLimits,
    RiskState,
    SellCandidate,
    SellLeg,
    Signal,
    StrategyDeployment,
)
from bot.store import SnapshotStore
from bot.wheel import screen_premium, spans_earnings

Generator = Callable[[SnapshotStore, str, StrategyDeployment, datetime], list[SellCandidate]]

MAX_POSITION_FRACTION = 0.25
STOP_CREDIT_MULTIPLE = 2.0


def _annualized(credit: float, collateral: float, dte: int) -> float:
    if collateral <= 0 or credit <= 0:
        return 0.0
    return (credit * 100.0 / collateral) * (365.0 / max(1, dte))


def _wheel_candidates(action: str, strategy_id: str) -> Generator:
    def generate(store: SnapshotStore, symbol: str, deployment: StrategyDeployment, now: datetime) -> list[SellCandidate]:
        del deployment
        found = store.read_latest(symbol)
        if found is None:
            return []
        snapshot_id, snapshot = found
        bar = store.read_latest_bar(symbol)
        from bot.ivrank import histories_for

        candidates, _ = screen_premium(
            [(snapshot_id, snapshot, bar)],
            now,
            iv_history=histories_for(store, [symbol]),
            earnings=store.all_earnings(),
        )
        results: list[SellCandidate] = []
        for item in candidates:
            if item.action != action:
                continue
            kind = "put" if action == "SELL_PUT" else "call"
            results.append(
                SellCandidate(
                    symbol=item.symbol,
                    strategy_id=strategy_id,
                    action=item.action,
                    legs=(SellLeg(side="sell", call_or_put=kind, strike=item.strike),),
                    expiry=item.expiry,
                    dte=item.dte,
                    credit=item.bid,
                    collateral_per_contract=item.capital_at_risk,
                    annualized_return=_annualized(item.bid, item.capital_at_risk, item.dte),
                    delta=item.delta,
                    snapshot_id=item.snapshot_id,
                    spot=bar.close if bar is not None else None,
                )
            )
        return results

    return generate


def _spread_candidates(kind: str, strategy_id: str) -> Generator:
    def generate(store: SnapshotStore, symbol: str, deployment: StrategyDeployment, now: datetime) -> list[SellCandidate]:
        del deployment
        found = store.read_latest(symbol)
        if found is None:
            return []
        snapshot_id, snapshot = found
        bar = store.read_latest_bar(symbol)
        spot = bar.close if bar is not None else None
        earnings_date = store.read_earnings(symbol)
        by_expiry: dict[str, list[Any]] = {}
        for contract in snapshot.contracts_list:
            if contract.call_or_put == kind:
                by_expiry.setdefault(contract.expiry, []).append(contract)
        results: list[SellCandidate] = []
        for expiry, contracts in by_expiry.items():
            try:
                expiry_date = datetime.fromisoformat(expiry).date()
            except ValueError:
                continue
            dte = (expiry_date - now.date()).days
            if dte < 7 or dte > 30:
                continue
            if spans_earnings(now.date(), expiry_date, earnings_date):
                continue
            ordered = sorted(contracts, key=lambda item: item.strike)
            for sell in ordered:
                if sell.delta is None or sell.bid is None or sell.ask is None:
                    continue
                delta_abs = abs(sell.delta)
                if not (0.15 <= delta_abs <= 0.30):
                    continue
                if sell.open_interest < 100:
                    continue
                if kind == "put" and sell.delta >= 0:
                    continue
                if kind == "call" and sell.delta <= 0:
                    continue
                hedges = []
                for hedge in ordered:
                    if kind == "put" and hedge.strike < sell.strike:
                        hedges.append(hedge)
                    if kind == "call" and hedge.strike > sell.strike:
                        hedges.append(hedge)
                hedges.sort(key=lambda item: abs(abs(item.strike - sell.strike) - 5.0))
                for hedge in hedges[:1]:
                    if hedge.ask is None or hedge.open_interest < 100:
                        continue
                    width = abs(sell.strike - hedge.strike)
                    if width <= 0 or width > 10:
                        continue
                    credit = sell.bid - hedge.ask
                    if credit < 0.30:
                        continue
                    collateral = (width - credit) * 100.0
                    if collateral <= 0:
                        continue
                    action = "SELL_PUT_SPREAD" if kind == "put" else "SELL_CALL_SPREAD"
                    results.append(
                        SellCandidate(
                            symbol=symbol,
                            strategy_id=strategy_id,
                            action=action,
                            legs=(
                                SellLeg(side="sell", call_or_put=kind, strike=sell.strike),
                                SellLeg(side="buy", call_or_put=kind, strike=hedge.strike),
                            ),
                            expiry=expiry,
                            dte=dte,
                            credit=credit,
                            collateral_per_contract=collateral,
                            annualized_return=_annualized(credit, collateral, dte),
                            delta=sell.delta,
                            snapshot_id=snapshot_id,
                            spot=spot,
                        )
                    )
        results.sort(key=lambda item: -item.annualized_return)
        return results

    return generate


STRATEGY_REGISTRY: dict[str, dict[str, Any]] = {
    "cash_secured_put": {
        "description": "Sell cash-secured puts, 7-30 DTE, delta 0.15-0.30",
        "generator": _wheel_candidates("SELL_PUT", "cash_secured_put"),
    },
    "covered_call": {
        "description": "Sell covered calls against 100-share lots, 7-30 DTE, delta 0.15-0.30",
        "generator": _wheel_candidates("SELL_CALL", "covered_call"),
    },
    "put_credit_spread": {
        "description": "Sell defined-risk put credit spreads, 7-30 DTE, short delta 0.15-0.30",
        "generator": _spread_candidates("put", "put_credit_spread"),
    },
    "call_credit_spread": {
        "description": "Sell defined-risk call credit spreads, 7-30 DTE, short delta 0.15-0.30",
        "generator": _spread_candidates("call", "call_credit_spread"),
    },
}


def list_strategies_text() -> str:
    lines = ["strategies:"]
    for strategy_id, info in STRATEGY_REGISTRY.items():
        lines.append(f"  {strategy_id}: {info['description']}")
    lines.append("deploy with: deploy cash_secured_put on SPY,QQQ paper capital 50000")
    return "\n".join(lines)


def _next_deployment_id(store: SnapshotStore) -> str:
    numbers = []
    for item in store.list_deployments():
        match = re.fullmatch(r"d(\d+)", item.id)
        if match:
            numbers.append(int(match.group(1)))
    return f"d{max(numbers, default=0) + 1}"


def create_deployment(
    store: SnapshotStore,
    strategy_id: str,
    symbols: Sequence[str],
    mode: str = "paper",
    capital_cap: float = 50000.0,
    target_utilization: float = 0.7,
) -> StrategyDeployment:
    clean_strategy = str(strategy_id).strip().lower()
    if clean_strategy not in STRATEGY_REGISTRY:
        raise ValueError(f"unknown strategy {strategy_id!r}; choose one of {', '.join(sorted(STRATEGY_REGISTRY))}")
    clean_symbols = tuple(dict.fromkeys(str(item).strip().upper() for item in symbols if str(item).strip()))
    if not clean_symbols:
        raise ValueError("deployment needs at least one symbol")
    clean_mode = str(mode).strip().lower()
    if clean_mode not in ("paper", "live"):
        raise ValueError("deployment mode must be paper or live")
    if capital_cap <= 0:
        raise ValueError("deployment capital must be positive")
    if not 0 < target_utilization <= 1:
        raise ValueError("deployment target utilization must be in (0, 1]")
    deployment = StrategyDeployment(
        id=_next_deployment_id(store),
        strategy_id=clean_strategy,
        symbols=clean_symbols,
        mode=clean_mode,
        capital_cap=float(capital_cap),
    )
    return store.save_deployment(deployment)


def _account_for(store: SnapshotStore, deployment: StrategyDeployment, now: datetime) -> tuple[Any, RiskState, RiskLimits]:
    account_id = f"deploy-{deployment.id}"
    profile = store.read_profile()
    risk_per_trade = 0.01
    if profile is not None:
        risk_per_trade = suggest_params(profile)["risk_per_trade"]
    account = store.read_paper_account(account_id)
    today = now.date().isoformat()
    if account is None:
        state = RiskState(
            account_size=deployment.capital_cap,
            risk_per_trade=risk_per_trade,
            day_pnl=0.0,
            halt_active=False,
            kill_active=False,
        )
        account = new_paper_account(account_id, state, today)
        store.save_paper_account(account)
    elif account.last_day and account.last_day != today:
        account = roll_paper_account(account, today)
        store.save_paper_account(account)
    limits = RiskLimits(
        max_trade_size=max(deployment.capital_cap, 1.0),
        day_profit_target=deployment.capital_cap,
        max_day_loss=max(1.0, 0.02 * deployment.capital_cap),
    )
    return account, account.state, limits


def _tag_new_fill(
    journal: Journal,
    before: int,
    kind: str,
    payload: dict[str, Any],
) -> bool:
    entries = journal.entries()
    for entry in entries[before:]:
        if entry.kind == kind:
            entry.payload.update(payload)
            return True
    return False


def _already_opened_today(journal: Journal, key: str, now: datetime) -> bool:
    today = now.date().isoformat()
    for entry in journal.entries():
        if entry.payload.get("position_key") == key and entry.payload.get("trade_action") == "open":
            if entry.timestamp_utc.date().isoformat() == today:
                return True
    return False


def _already_closed_today(journal: Journal, key: str, now: datetime) -> bool:
    today = now.date().isoformat()
    for entry in journal.entries():
        if entry.payload.get("position_key") == key and entry.payload.get("trade_action") in ("close", "settlement"):
            if entry.timestamp_utc.date().isoformat() == today:
                return True
    return False


def _contracts_for(
    deployment: StrategyDeployment,
    candidate: SellCandidate,
    available: float,
    store: SnapshotStore,
) -> int:
    if candidate.collateral_per_contract <= 0:
        return 0
    by_available = int(available // candidate.collateral_per_contract)
    by_position_cap = int((deployment.capital_cap * MAX_POSITION_FRACTION) // candidate.collateral_per_contract)
    contracts = min(by_available, by_position_cap)
    if candidate.action == "SELL_CALL":
        shares = 0.0
        for holding in store.read_holdings():
            if holding.symbol == candidate.symbol and holding.kind == "stock" and holding.side == "long":
                shares += holding.qty
        contracts = min(contracts, int(shares // 100))
    scaled = int(contracts * deployment.sizing_scale)
    if contracts >= 1 and scaled < 1:
        scaled = 1
    return max(0, scaled)


def open_candidate(
    deployment: StrategyDeployment,
    candidate: SellCandidate,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    venue: Any = None,
) -> Optional[str]:
    profile = store.read_profile()
    if profile is None or not profile.options_ok:
        journal.append(now, "strategy_skipped", {"deployment_id": deployment.id, "reason": "profile with options_ok required"})
        return None
    positions = open_option_positions(journal, deployment.id)
    key = position_key(deployment.id, candidate.symbol, candidate.legs, candidate.expiry)
    if any(item.key == key for item in positions):
        journal.append(now, "strategy_skipped", {"deployment_id": deployment.id, "reason": "contract already open", "position_key": key})
        return None
    if _already_opened_today(journal, key, now):
        journal.append(now, "strategy_skipped", {"deployment_id": deployment.id, "reason": "already opened today", "position_key": key})
        return None
    if _already_closed_today(journal, key, now):
        journal.append(now, "strategy_skipped", {"deployment_id": deployment.id, "reason": "already closed today", "position_key": key})
        return None
    ledger = ledger_for(deployment, positions)
    contracts = _contracts_for(deployment, candidate, ledger.available_for_new, store)
    if contracts < 1:
        journal.append(
            now, "strategy_skipped",
            {"deployment_id": deployment.id, "reason": "capital target reached or insufficient collateral", "symbol": candidate.symbol},
        )
        return None
    account, state, limits = _account_for(store, deployment, now)
    view = PortfolioView(
        open_positions=len(positions),
        open_symbols=tuple(sorted({item.symbol for item in positions})),
        peak_day_pnl=account.peak_day_pnl,
    )
    legs = tuple(
        OrderLeg(
            symbol=candidate.symbol,
            side=leg.side,
            size=contracts * candidate.credit * 100.0,
            strike=leg.strike,
            expiry=candidate.expiry,
            call_or_put=leg.call_or_put,
        )
        for leg in candidate.legs
    )
    payload = {
        "option_position": True,
        "trade_action": "open",
        "position_action": candidate.action,
        "deployment_id": deployment.id,
        "strategy_id": deployment.strategy_id,
        "contracts": float(contracts),
        "entry_credit": candidate.credit,
        "collateral_per_contract": candidate.collateral_per_contract,
        "position_key": key,
        "expiry": candidate.expiry,
        "legs": [
            {"side": leg.side, "call_or_put": leg.call_or_put, "strike": leg.strike} for leg in candidate.legs
        ],
        "opened_date": now.date().isoformat(),
        "snapshot_id": candidate.snapshot_id,
        "venue": deployment.mode,
    }
    before = len(journal.entries())
    if deployment.mode == "live":
        if venue is None:
            journal.append(now, "strategy_skipped", {"deployment_id": deployment.id, "reason": "live deployment needs a venue in this runner"})
            return None
        from bot.autonomy import submit_live

        signal = Signal(
            direction="short",
            thesis=f"{deployment.strategy_id} {candidate.action} {candidate.symbol}",
            confidence=0.65,
            snapshot_id=candidate.snapshot_id,
            symbol=candidate.symbol,
        )
        intent = build_intent(signal, f"deploy-{deployment.id}-{key}", state, journal, now, legs=legs)
        if intent is None:
            return None
        try:
            submit_live(intent, state, None, limits, journal, now, venue=venue, reference_price=candidate.credit, profile=profile, view=view)
        except Exception as exc:
            journal.append(now, "strategy_skipped", {"deployment_id": deployment.id, "reason": str(exc)[:160]})
            return None
        _tag_new_fill(journal, before, "live_fill", payload)
    else:
        signal = Signal(
            direction="short",
            thesis=f"{deployment.strategy_id} {candidate.action} {candidate.symbol}",
            confidence=0.65,
            snapshot_id=candidate.snapshot_id,
            symbol=candidate.symbol,
        )
        intent = build_intent(signal, f"deploy-{deployment.id}-{key}", state, journal, now, legs=legs)
        if intent is None:
            return None
        try:
            _, _, updated = submit_paper(
                intent, candidate.credit, state, None, limits, journal, now,
                bid=candidate.credit, ask=candidate.credit, profile=profile, view=view,
            )
        except IntentRejectedError:
            return None
        _tag_new_fill(journal, before, "fill", payload)
        store.save_paper_account(record_paper_state(account, updated, now.date().isoformat()))
    journal.append(
        now, "strategy_opened",
        {
            "deployment_id": deployment.id, "strategy_id": deployment.strategy_id,
            "symbol": candidate.symbol, "contracts": contracts, "credit": candidate.credit,
            "collateral": contracts * candidate.collateral_per_contract, "position_key": key,
        },
    )
    return (
        f"sold {contracts} {candidate.symbol} {candidate.action} "
        f"{'/'.join(str(leg.strike) for leg in candidate.legs)} {candidate.expiry} "
        f"credit {candidate.credit:.2f} collateral {contracts * candidate.collateral_per_contract:.0f}"
    )


def current_debit(position: OpenOptionPosition, store: SnapshotStore) -> Optional[float]:
    found = store.read_latest(position.symbol)
    if found is None:
        return None
    _, snapshot = found
    prices: list[float] = []
    for leg in position.legs:
        match = None
        for contract in snapshot.contracts_list:
            if contract.call_or_put == leg.call_or_put and contract.strike == leg.strike and contract.expiry == position.expiry:
                match = contract
                break
        if match is None:
            return None
        if leg.side == "sell":
            price = match.ask if match.ask is not None else match.price if match.price is not None else match.bid
        else:
            price = match.bid if match.bid is not None else match.price if match.price is not None else match.ask
        if price is None:
            return None
        prices.append(float(price) if leg.side == "sell" else -float(price))
    return max(0.0, sum(prices))


def settlement_debit(position: OpenOptionPosition, spot: float) -> float:
    if len(position.legs) == 1:
        leg = position.legs[0]
        if leg.call_or_put == "put":
            return max(0.0, leg.strike - spot)
        return max(0.0, spot - leg.strike)
    sell_leg = next((leg for leg in position.legs if leg.side == "sell"), position.legs[0])
    buy_leg = next((leg for leg in position.legs if leg.side == "buy"), position.legs[-1])
    width = abs(sell_leg.strike - buy_leg.strike)
    if sell_leg.call_or_put == "put":
        return min(width, max(0.0, sell_leg.strike - spot))
    return min(width, max(0.0, spot - sell_leg.strike))


def _close_position(
    deployment: StrategyDeployment,
    position: OpenOptionPosition,
    debit: float,
    reason: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    venue: Any = None,
) -> Optional[str]:
    profile = store.read_profile()
    payload = {
        "option_position": True,
        "trade_action": "close",
        "deployment_id": deployment.id,
        "strategy_id": deployment.strategy_id,
        "contracts": position.contracts,
        "exit_debit": debit,
        "position_key": position.key,
        "expiry": position.expiry,
        "legs": [
            {"side": leg.side, "call_or_put": leg.call_or_put, "strike": leg.strike} for leg in position.legs
        ],
        "reason": reason,
        "venue": deployment.mode,
    }
    if debit <= 0:
        journal.append(now, "option_settled", {**payload, "symbol": position.symbol, "price": 0.0})
        return f"closed {position.symbol} {position.expiry} at 0.00 ({reason})"
    account, state, limits = _account_for(store, deployment, now)
    close_legs = tuple(
        OrderLeg(
            symbol=position.symbol,
            side="buy" if leg.side == "sell" else "sell",
            size=position.contracts * debit * 100.0,
            strike=leg.strike,
            expiry=position.expiry,
            call_or_put=leg.call_or_put,
        )
        for leg in position.legs
    )
    intent = OrderIntent(
        symbol=position.symbol,
        side="buy",
        size=position.contracts * debit * 100.0,
        risk_numbers={"close": 1.0, "size": position.contracts * debit * 100.0},
        signal_id=f"deploy-close-{position.key}",
        legs=close_legs,
    )
    before = len(journal.entries())
    if deployment.mode == "live":
        if venue is None:
            journal.append(now, "strategy_skipped", {"deployment_id": deployment.id, "reason": "live close needs a venue", "position_key": position.key})
            return None
        from bot.autonomy import submit_live

        try:
            submit_live(intent, state, None, limits, journal, now, venue=venue, reference_price=debit, profile=profile, view=None)
        except Exception as exc:
            journal.append(now, "strategy_skipped", {"deployment_id": deployment.id, "reason": str(exc)[:160]})
            return None
        _tag_new_fill(journal, before, "live_fill", payload)
    else:
        try:
            _, _, updated = submit_paper(
                intent, debit, state, None, limits, journal, now,
                bid=debit, ask=debit, profile=profile,
            )
        except IntentRejectedError:
            return None
        _tag_new_fill(journal, before, "fill", payload)
        store.save_paper_account(record_paper_state(account, updated, now.date().isoformat()))
    pnl = (position.entry_credit - debit) * position.contracts * 100.0
    journal.append(
        now, "strategy_closed",
        {"deployment_id": deployment.id, "position_key": position.key, "reason": reason, "pnl": pnl},
    )
    return f"closed {position.symbol} {position.expiry} at {debit:.2f} pnl {pnl:+.0f} ({reason})"


def manage_positions(
    deployment: StrategyDeployment,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    venue: Any = None,
) -> list[str]:
    lines: list[str] = []
    for position in open_option_positions(journal, deployment.id):
        try:
            expiry_date = datetime.fromisoformat(position.expiry).date()
        except ValueError:
            continue
        dte = (expiry_date - now.date()).days
        if dte < 0:
            bar = store.read_latest_bar(position.symbol)
            if bar is None:
                continue
            debit = settlement_debit(position, bar.close)
            payload = {
                "option_position": True,
                "trade_action": "settlement",
                "deployment_id": deployment.id,
                "strategy_id": deployment.strategy_id,
                "symbol": position.symbol,
                "contracts": position.contracts,
                "exit_debit": debit,
                "position_key": position.key,
                "expiry": position.expiry,
                "legs": [
                    {"side": leg.side, "call_or_put": leg.call_or_put, "strike": leg.strike}
                    for leg in position.legs
                ],
                "reason": "expiry_settlement",
                "venue": deployment.mode,
            }
            journal.append(now, "option_settled", payload)
            if debit > 0 and len(position.legs) == 1:
                journal.append(
                    now, "assignment",
                    {
                        "deployment_id": deployment.id, "symbol": position.symbol,
                        "shares": position.contracts * 100.0, "strike": position.legs[0].strike,
                        "kind": position.legs[0].call_or_put,
                    },
                )
            pnl = (position.entry_credit - debit) * position.contracts * 100.0
            lines.append(f"settled {position.symbol} {position.expiry} debit {debit:.2f} pnl {pnl:+.0f} (expiry)")
            continue
        current = current_debit(position, store)
        if current is None:
            continue
        reason = None
        if position.entry_credit > 0 and current <= position.entry_credit * (1.0 - deployment.profit_target_pct):
            reason = "profit_target"
        elif current >= position.entry_credit * STOP_CREDIT_MULTIPLE and position.entry_credit > 0:
            reason = "stop_2x_credit"
        elif dte <= deployment.close_dte:
            reason = "close_dte"
        if reason is None:
            continue
        note = _close_position(deployment, position, current, reason, store, journal, now, venue=venue)
        if note:
            lines.append(note)
    return lines


def apply_feedback(deployment: StrategyDeployment, store: SnapshotStore, journal: Journal, now: datetime) -> StrategyDeployment:
    closed = closed_option_trades(journal, deployment.id)
    if not closed:
        return deployment
    realized = sum(float(item["pnl"]) for item in closed)
    scale = min(1.25, max(0.5, 1.0 + (realized / deployment.capital_cap) * 2.0)) if deployment.capital_cap > 0 else 1.0
    status = deployment.status
    if len(closed) >= 3 and realized <= -0.05 * deployment.capital_cap and status == "active":
        status = "paused"
    if scale == deployment.sizing_scale and status == deployment.status:
        return deployment
    updated = replace(deployment, sizing_scale=scale, status=status)
    store.save_deployment(updated)
    journal.append(
        now, "strategy_feedback",
        {
            "deployment_id": deployment.id, "closed_trades": len(closed), "realized_pnl": realized,
            "sizing_scale": scale, "status": status,
        },
    )
    return updated


def run_deployments(
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    venue: Any = None,
) -> str:
    deployments = store.list_deployments()
    if not deployments:
        return ""
    lines: list[str] = []
    opened = 0
    closed_notes = 0
    for deployment in deployments:
        deployment = apply_feedback(deployment, store, journal, now)
        managed = manage_positions(deployment, store, journal, now, venue=venue)
        if managed:
            closed_notes += len(managed)
            lines.append(f"deployment {deployment.id} {deployment.strategy_id} management: " + "; ".join(managed))
        if deployment.status != "active":
            lines.append(f"deployment {deployment.id} {deployment.strategy_id}: {deployment.status}, no new positions")
            continue
        if now.weekday() >= 5:
            journal.append(now, "strategy_skipped", {"deployment_id": deployment.id, "reason": "weekend"})
            continue
        generator = STRATEGY_REGISTRY[deployment.strategy_id]["generator"]
        for symbol in deployment.symbols:
            try:
                candidates = generator(store, symbol, deployment, now)
            except Exception as exc:
                journal.append(now, "strategy_skipped", {"deployment_id": deployment.id, "reason": type(exc).__name__, "symbol": symbol})
                continue
            candidates.sort(key=lambda item: -item.annualized_return)
            for candidate in candidates:
                open_for_symbol = [
                    item for item in open_option_positions(journal, deployment.id) if item.symbol == symbol
                ]
                if len(open_for_symbol) >= deployment.max_per_symbol:
                    break
                note = open_candidate(deployment, candidate, store, journal, now, venue=venue)
                if note:
                    opened += 1
                    lines.append(f"deployment {deployment.id}: {note}")
                    break
    journal.append(now, "deployment_run", {"deployments": len(deployments), "opened": opened, "managed": closed_notes})
    return "\n".join(lines)


def deployments_text(store: SnapshotStore, journal: Journal) -> str:
    deployments = store.list_deployments()
    if not deployments:
        return "deployments: none\n" + list_strategies_text()
    lines = ["deployments:"]
    for deployment in deployments:
        positions = open_option_positions(journal, deployment.id)
        ledger = ledger_for(deployment, positions)
        closed = closed_option_trades(journal, deployment.id)
        realized = sum(float(item["pnl"]) for item in closed)
        lines.append(
            f"  {deployment.id} {deployment.strategy_id} {deployment.mode} {deployment.status} "
            f"symbols {','.join(deployment.symbols)} capital {deployment.capital_cap:.0f} "
            f"deployed {ledger.deployed_collateral:.0f} utilization {ledger.utilization:.0%} "
            f"target {ledger.target_utilization:.0%} open {len(positions)} closed {len(closed)} "
            f"realized {realized:+.0f} sizing_scale {deployment.sizing_scale:.2f}"
        )
    return "\n".join(lines)


def deployments_html(store: SnapshotStore, journal: Journal, now: datetime) -> str:
    import html as html_module

    from bot.theme import page

    settings = store.read_settings()
    deployments = store.list_deployments()
    if not deployments:
        body = (
            "<p>no deployments yet. Create one in chat: "
            "<code>deploy cash_secured_put on SPY paper capital 50000</code></p>"
            f"<h2>Strategies</h2><p>{html_module.escape(list_strategies_text()).replace(chr(10), '<br>')}</p>"
        )
        return page(
            "deployments", "strategy deployments", "premium-selling agent, paper by default",
            body, settings.default_underlying, "deployments", settings, "/deployments/page",
        )
    stats_parts: list[str] = []
    sections: list[str] = []
    total_deployed = 0.0
    total_realized = 0.0
    total_open = 0
    for deployment in deployments:
        positions = open_option_positions(journal, deployment.id)
        ledger = ledger_for(deployment, positions)
        closed = closed_option_trades(journal, deployment.id)
        realized = sum(float(item["pnl"]) for item in closed)
        total_deployed += ledger.deployed_collateral
        total_realized += realized
        total_open += len(positions)
        pct = max(0.0, min(100.0, ledger.utilization * 100.0))
        rows = [
            "<table><thead><tr><th>Contract</th><th>Expiry</th><th>Contracts</th>"
            "<th>Credit</th><th>Collateral</th></tr></thead><tbody>"
        ]
        for item in positions:
            legs = "/".join(f"{leg.side} {leg.call_or_put} {leg.strike:g}" for leg in item.legs)
            rows.append(
                f"<tr><td>{html_module.escape(item.symbol)} {html_module.escape(legs)}</td>"
                f"<td>{html_module.escape(item.expiry)}</td><td>{item.contracts:g}</td>"
                f"<td>{item.entry_credit:.2f}</td>"
                f"<td>${item.contracts * item.collateral_per_contract:,.0f}</td></tr>"
            )
        rows.append("</tbody></table>")
        table = "".join(rows) if positions else "<p>no open positions</p>"
        sections.append(
            f"<h2>{html_module.escape(deployment.id)} {html_module.escape(deployment.strategy_id)}</h2>"
            f"<p>{html_module.escape(deployment.mode)} · {html_module.escape(deployment.status)} · "
            f"symbols {html_module.escape(','.join(deployment.symbols))} · capital ${deployment.capital_cap:,.0f} · "
            f"deployed ${ledger.deployed_collateral:,.0f} · utilization {ledger.utilization:.0%} of "
            f"{ledger.target_utilization:.0%} target · closed {len(closed)} · realized ${realized:+,.0f} · "
            f"sizing scale {deployment.sizing_scale:.2f}</p>"
            f"<div style=\"background:#21262d;border-radius:6px;height:14px;max-width:560px\">"
            f"<div style=\"background:#3fb950;height:14px;border-radius:6px;width:{pct:.0f}%\"></div></div>"
            f"{table}"
        )
    stats_parts.append(
        "<div class=\"stats\">"
        f"<div class=\"stat\"><b>{len(deployments)}</b><span>deployments</span></div>"
        f"<div class=\"stat\"><b>{total_open}</b><span>open short positions</span></div>"
        f"<div class=\"stat\"><b>${total_deployed:,.0f}</b><span>collateral deployed</span></div>"
        f"<div class=\"stat\"><b>${total_realized:+,.0f}</b><span>realized PnL</span></div>"
        "</div>"
    )
    body = "".join(stats_parts + sections)
    return page(
        "deployments", "strategy deployments",
        "the premium-selling agent: feedback, lifecycle management, then new sales, every session",
        body, settings.default_underlying, "deployments", settings, "/deployments/page",
    )


def strategy_trackrecord_text(journal: Journal, deployment_id: str | None = None) -> str:
    closed = closed_option_trades(journal, deployment_id)
    scope = f"deployment {deployment_id}" if deployment_id else "all deployments"
    if not closed:
        return f"strategy track record ({scope}): no closed option trades yet"
    wins = sum(1 for item in closed if float(item["pnl"]) > 0)
    total = sum(float(item["pnl"]) for item in closed)
    lines = [
        f"strategy track record ({scope}): {len(closed)} closed, win rate {wins / len(closed):.0%} "
        f"({wins}/{len(closed)}), realized {total:+.0f}"
    ]
    by_strategy: dict[str, list[float]] = {}
    for item in closed:
        by_strategy.setdefault(str(item["strategy_id"]), []).append(float(item["pnl"]))
    for strategy_id, pnls in sorted(by_strategy.items()):
        lines.append(f"  {strategy_id}: {len(pnls)} closed, realized {sum(pnls):+.0f}")
    return "\n".join(lines)


_DEPLOY_RE = re.compile(
    r"deploy\s+([a-z_]+)(?:\s+on\s+([A-Za-z, ]+?))?(?:\s+(paper|live))?(?:\s+capital\s+(\d+(?:\.\d+)?))?$",
    re.IGNORECASE,
)


def deployments_response(text: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    lowered = text.strip().lower()
    if "track record" in lowered and ("strategy" in lowered or "deployment" in lowered):
        match = re.search(r"\b(d\d+)\b", lowered)
        return strategy_trackrecord_text(journal, match.group(1) if match else None)
    if re.search(r"\blist strategies\b|\bstrategies\b$", lowered) or lowered == "strategies":
        return list_strategies_text()
    if re.search(r"\b(list|my) deployments\b", lowered):
        return deployments_text(store, journal)
    match = re.search(r"\b(pause|resume|remove|stop) deployment\s+(d\d+)\b", lowered)
    if match:
        action, deployment_id = match.group(1), match.group(2)
        deployment = store.read_deployment(deployment_id)
        if deployment is None:
            return f"deployment {deployment_id} not found"
        if action == "remove" or (action == "stop" and "remove" in lowered):
            store.remove_deployment(deployment_id)
            journal.append(now, "deployment_removed", {"deployment_id": deployment_id})
            return f"deployment {deployment_id} removed (open positions still managed via journal book)"
        status = "paused" if action in ("pause", "stop") else "active"
        store.save_deployment(replace(deployment, status=status))
        journal.append(now, "deployment_status", {"deployment_id": deployment_id, "status": status})
        return f"deployment {deployment_id} {status}"
    if re.search(r"\brun deployments\b", lowered):
        return run_deployments(store, journal, now) or "deployments: nothing opened or closed"
    if lowered.startswith("deploy"):
        mapped = None
        if "put credit spread" in lowered or ("credit spread" in lowered and "put" in lowered):
            mapped = "put_credit_spread"
        elif "call credit spread" in lowered or ("credit spread" in lowered and "call" in lowered):
            mapped = "call_credit_spread"
        elif "covered call" in lowered or "sell call" in lowered:
            mapped = "covered_call"
        elif "sell put" in lowered or "cash-secured" in lowered or "cash secured" in lowered or "wheel" in lowered:
            mapped = "cash_secured_put"
        if mapped is not None:
            known = [symbol for symbol in store.list_underlyings() if re.search(rf"\b{symbol}\b", text, re.IGNORECASE)]
            symbols = known or ["SPY"]
            capital_match = re.search(r"capital\s+(\d+(?:\.\d+)?)", lowered)
            capital = float(capital_match.group(1)) if capital_match else 50000.0
            mode = "live" if re.search(r"\blive\b", lowered) else "paper"
            deployment = create_deployment(store, mapped, symbols, mode=mode, capital_cap=capital)
            journal.append(
                now, "deployment_created",
                {"deployment_id": deployment.id, "strategy_id": deployment.strategy_id, "symbols": list(deployment.symbols), "mode": mode, "capital_cap": capital},
            )
            return (
                f"deployment {deployment.id} created: {deployment.strategy_id} on {','.join(symbols)} "
                f"{mode} capital {capital:.0f}. It will run in the next scheduled session, or say run deployments."
            )
    match = _DEPLOY_RE.search(text.strip())
    if match and match.group(1).lower() in STRATEGY_REGISTRY:
        symbols_raw = match.group(2) or "SPY"
        symbols = [item.strip().upper() for item in re.split(r"[,\s]+", symbols_raw) if item.strip() and item.strip().lower() not in ("paper", "live")]
        mode = (match.group(3) or "paper").lower()
        capital = float(match.group(4)) if match.group(4) else 50000.0
        deployment = create_deployment(store, match.group(1), symbols, mode=mode, capital_cap=capital)
        journal.append(
            now, "deployment_created",
            {"deployment_id": deployment.id, "strategy_id": deployment.strategy_id, "symbols": list(symbols), "mode": mode, "capital_cap": capital},
        )
        return (
            f"deployment {deployment.id} created: {deployment.strategy_id} on {','.join(symbols)} "
            f"{mode} capital {capital:.0f}. It will run in the next scheduled session, or say run deployments."
        )
    return deployments_text(store, journal)
