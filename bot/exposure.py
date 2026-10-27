from __future__ import annotations

from bot.shapes import (
    DEFAULT_CONTRACT_MULTIPLIER,
    ChainSnapshot,
    GexProfile,
    VexProfile,
)


def _signed_exposure(call_or_put: str, greek: float | None, open_interest: float, multiplier: float) -> float:
    if greek is None:
        return 0.0
    sign = 1.0 if call_or_put == "call" else -1.0
    return sign * greek * open_interest * multiplier


def _flip_point(per_strike: dict[float, float]) -> float | None:
    cumulative = 0.0
    previous = 0.0
    for strike in sorted(per_strike):
        cumulative += per_strike[strike]
        if previous != 0.0 and (cumulative == 0.0 or (cumulative > 0.0) != (previous > 0.0)):
            return strike
        if cumulative != 0.0:
            previous = cumulative
    return None


def compute_gex(
    snapshot: ChainSnapshot,
    snapshot_id: str,
    multiplier: float = DEFAULT_CONTRACT_MULTIPLIER,
) -> GexProfile:
    per_strike: dict[float, float] = {}
    total = 0.0
    for contract in snapshot.contracts_list:
        exposure = _signed_exposure(contract.call_or_put, contract.gamma, contract.open_interest, multiplier)
        per_strike[contract.strike] = per_strike.get(contract.strike, 0.0) + exposure
        total += exposure
    return GexProfile(
        snapshot_id=snapshot_id,
        per_strike_exposure=per_strike,
        total_exposure=total,
        flip_point=_flip_point(per_strike),
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
