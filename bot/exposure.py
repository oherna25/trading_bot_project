from __future__ import annotations

from bot.shapes import (
    DEFAULT_CONTRACT_MULTIPLIER,
    ChainSnapshot,
    DexProfile,
    GexProfile,
    VexProfile,
)

ONE_PERCENT_MOVE = 0.01


def _signed_exposure(call_or_put: str, greek: float | None, open_interest: float, multiplier: float) -> float:
    if greek is None:
        return 0.0
    sign = 1.0 if call_or_put == "call" else -1.0
    return sign * greek * open_interest * multiplier


def _flip_point(per_strike: dict[float, float]) -> float | None:
    cumulative = 0.0
    previous = 0.0
    previous_strike: float | None = None
    for strike in sorted(per_strike):
        cumulative += per_strike[strike]
        if previous_strike is not None and previous != 0.0:
            if cumulative == 0.0:
                return strike
            if (cumulative > 0.0) != (previous > 0.0):
                span = strike - previous_strike
                if span == 0:
                    return strike
                fraction = (0.0 - previous) / (cumulative - previous)
                return previous_strike + fraction * span
        if cumulative != 0.0:
            previous = cumulative
            previous_strike = strike
    return None


def _front_expiry(snapshot: ChainSnapshot) -> str | None:
    expiries = sorted({c.expiry for c in snapshot.contracts_list if c.gamma is not None and c.expiry})
    return expiries[0] if expiries else None


def compute_gex(
    snapshot: ChainSnapshot,
    snapshot_id: str,
    multiplier: float = DEFAULT_CONTRACT_MULTIPLIER,
    spot: float | None = None,
) -> GexProfile:
    scale = spot * spot * ONE_PERCENT_MOVE if spot else 1.0
    front = _front_expiry(snapshot)
    per_strike: dict[float, float] = {}
    total = 0.0
    front_exposure = 0.0
    call_exposure = 0.0
    put_exposure = 0.0
    for contract in snapshot.contracts_list:
        exposure = _signed_exposure(contract.call_or_put, contract.gamma, contract.open_interest, multiplier)
        per_strike[contract.strike] = per_strike.get(contract.strike, 0.0) + exposure
        total += exposure
        if front is not None and contract.expiry == front:
            front_exposure += exposure
        if contract.call_or_put == "call":
            call_exposure += exposure
        else:
            put_exposure += exposure
    scaled_strikes = {strike: value * scale for strike, value in per_strike.items()}
    return GexProfile(
        snapshot_id=snapshot_id,
        per_strike_exposure=scaled_strikes,
        total_exposure=total * scale,
        flip_point=_flip_point(per_strike),
        spot=spot,
        front_expiry=front,
        front_exposure=front_exposure * scale,
        call_exposure=call_exposure * scale,
        put_exposure=put_exposure * scale,
    )


def compute_vex(
    snapshot: ChainSnapshot,
    snapshot_id: str,
    multiplier: float = DEFAULT_CONTRACT_MULTIPLIER,
) -> VexProfile:
    per_strike: dict[float, float] = {}
    total = 0.0
    for contract in snapshot.contracts_list:
        exposure = _signed_exposure(contract.call_or_put, contract.vega, contract.open_interest, multiplier)
        per_strike[contract.strike] = per_strike.get(contract.strike, 0.0) + exposure
        total += exposure
    return VexProfile(
        snapshot_id=snapshot_id,
        per_strike_exposure=per_strike,
        total_exposure=total,
    )


def compute_dex(
    snapshot: ChainSnapshot,
    snapshot_id: str,
    multiplier: float = DEFAULT_CONTRACT_MULTIPLIER,
) -> DexProfile:
    per_strike: dict[float, float] = {}
    total = 0.0
    for contract in snapshot.contracts_list:
        if contract.delta is None:
            continue
        exposure = contract.delta * contract.open_interest * multiplier
        per_strike[contract.strike] = per_strike.get(contract.strike, 0.0) + exposure
        total += exposure
    return DexProfile(
        snapshot_id=snapshot_id,
        per_strike_exposure=per_strike,
        total_exposure=total,
    )
