from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, Holding, OperatorProfile, OptionContract
from bot.store import SnapshotStore

DEMO_SYMBOLS = ("SPY", "QQQ")
DEMO_SOURCE = "demo_seed"
_SPOTS = {"SPY": 600.0, "QQQ": 520.0}
_STEPS = {"SPY": 5.0, "QQQ": 5.0}
_OFFSETS = (-20.0, -15.0, -10.0, -5.0, 0.0, 5.0, 10.0, 15.0, 20.0)


def _friday_on_or_after(day, min_dte: int) -> str:
    target = day + timedelta(days=min_dte)
    target += timedelta(days=(4 - target.weekday()) % 7)
    return target.isoformat()


def _contracts(spot: float, step: float, expiries: tuple[str, ...]) -> tuple[OptionContract, ...]:
    contracts = []
    for expiry in expiries:
        for offset in _OFFSETS:
            strike = spot + offset
            moneyness = offset / step
            call_delta = max(0.05, min(0.95, 0.50 - moneyness * 0.09))
            for side in ("call", "put"):
                delta = call_delta if side == "call" else -(1.0 - call_delta)
                base_gamma = max(0.01, 0.06 - abs(offset) * 0.0015)
                gamma = base_gamma * 1.4 if side == "call" else base_gamma * 0.8
                if side == "call":
                    oi = max(400.0, 2800.0 + offset * 120.0)
                else:
                    oi = max(400.0, 2800.0 - offset * 80.0)
                intrinsic = max(0.0, (spot - strike) if side == "call" else (strike - spot))
                extrinsic = max(6.5, 9.5 - abs(offset) * 0.15)
                price = intrinsic + extrinsic
                contracts.append(
                    OptionContract(
                        strike=strike,
                        expiry=expiry,
                        call_or_put=side,
                        open_interest=oi,
                        gamma=gamma,
                        vega=0.12,
                        delta=round(delta, 2),
                        charm=0.01 if side == "call" else -0.01,
                        vanna=0.02,
                        price=round(price, 2),
                        bid=round(price - 0.10, 2),
                        ask=round(price + 0.10, 2),
                        iv=0.24 + abs(offset) * 0.001,
                    )
                )
    return tuple(contracts)


def seed_demo(
    store: SnapshotStore,
    journal: Journal,
    now: datetime | None = None,
) -> list[str]:
    moment = now or datetime.now(timezone.utc)
    seeded = []
    for symbol in DEMO_SYMBOLS:
        if store.read_latest(symbol) is not None:
            seeded.append(symbol)
            continue
        spot = _SPOTS[symbol]
        step = _STEPS[symbol]
        expiries = tuple(_friday_on_or_after(moment.date(), dte) for dte in (10, 17, 24))
        contracts = _contracts(spot, step, expiries)
        closes = [spot - 8.0, spot - 5.0, spot - 2.0, spot - 4.0, spot]
        for index, close in enumerate(closes):
            stamp = moment - timedelta(days=len(closes) - 1 - index)
            store.save(
                ChainSnapshot(
                    underlying=symbol,
                    timestamp_utc=stamp,
                    source_name=DEMO_SOURCE,
                    latency_ms=0.0,
                    contracts_list=contracts,
                )
            )
            store.save_bar(
                symbol,
                Bar(
                    interval="1d",
                    timestamp_utc=stamp,
                    open=close - 1.0,
                    high=close + 2.0,
                    low=close - 2.0,
                    close=close,
                    volume=1_000_000.0,
                ),
            )
        for day_offset in range(25):
            day = (moment - timedelta(days=24 - day_offset)).date().isoformat()
            store.save_iv(symbol, day, 0.18 + (day_offset % 8) * 0.02)
        store.save_iv(symbol, moment.date().isoformat(), 0.26)
        seeded.append(symbol)
    if store.read_profile() is None:
        store.save_profile(
            OperatorProfile(
                risk_tolerance="moderate",
                portfolio_size=100000.0,
                knowledge="intermediate",
                options_ok=True,
            )
        )
    if not store.read_holdings():
        store.save_holdings([Holding(symbol="SPY", kind="stock", side="long", qty=100.0)])
    if not store.list_deployments():
        from bot.deployments import create_deployment

        create_deployment(store, "put_credit_spread", ("SPY",), mode="paper", capital_cap=40000.0)
    journal.append(moment, "demo_seeded", {"symbols": seeded, "source": DEMO_SOURCE, "keys_required": False})
    return seeded
