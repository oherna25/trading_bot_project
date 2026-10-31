from __future__ import annotations

from collections import OrderedDict

from bot.shapes import (
    DEFAULT_CONTRACT_MULTIPLIER,
    ChainSnapshot,
    DexProfile,
    GexProfile,
    VexProfile,
)

ONE_PERCENT_MOVE = 0.01

_CACHE_LIMIT = 256
_profile_cache: OrderedDict[tuple, tuple[tuple, object]] = OrderedDict()


def _normalize_side(call_or_put: str) -> str | None:
    side = str(call_or_put).strip().lower()
    if side in ("call", "c"):
        return "call"
    if side in ("put", "p"):
        return "put"
    return None


def _signed_exposure(call_or_put: str, greek: float | None, open_interest: float, multiplier: float) -> float:
    if greek is None:
        return 0.0
    side = _normalize_side(call_or_put)
    if side is None:
        return 0.0
    sign = 1.0 if side == "call" else -1.0
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


def _cache_get(key: tuple, contracts: tuple):
    entry = _profile_cache.get(key)
    if entry is None:
        return None
    stored_contracts, value = entry
    if stored_contracts is not contracts:
        return None
    _profile_cache.move_to_end(key)
    return value


def _cache_put(key: tuple, contracts: tuple, value: object) -> None:
    _profile_cache[key] = (contracts, value)
    _profile_cache.move_to_end(key)
    while len(_profile_cache) > _CACHE_LIMIT:
        _profile_cache.popitem(last=False)


def compute_exposures(
    snapshot: ChainSnapshot,
    snapshot_id: str,
    multiplier: float = DEFAULT_CONTRACT_MULTIPLIER,
    spot: float | None = None,
) -> tuple[GexProfile, VexProfile, DexProfile]:
    gex_key = ("gex", snapshot_id, spot, multiplier)
    vex_key = ("vex", snapshot_id, multiplier)
    dex_key = ("dex", snapshot_id, multiplier)
    contracts = snapshot.contracts_list
    gex = _cache_get(gex_key, contracts)
    vex = _cache_get(vex_key, contracts)
    dex = _cache_get(dex_key, contracts)
    if gex is not None and vex is not None and dex is not None:
        return gex, vex, dex  # type: ignore[return-value]
    scale = spot * spot * ONE_PERCENT_MOVE if spot else 1.0
    front = _front_expiry(snapshot)
    gex_strikes: dict[float, float] = {}
    per_call: dict[float, float] = {}
    per_put: dict[float, float] = {}
    vex_strikes: dict[float, float] = {}
    dex_strikes: dict[float, float] = {}
    gex_total = 0.0
    vex_total = 0.0
    dex_total = 0.0
    front_exposure = 0.0
    call_exposure = 0.0
    put_exposure = 0.0
    for contract in snapshot.contracts_list:
        side = _normalize_side(contract.call_or_put)
        if side is None:
            continue
        gamma_exposure = _signed_exposure(side, contract.gamma, contract.open_interest, multiplier)
        gex_strikes[contract.strike] = gex_strikes.get(contract.strike, 0.0) + gamma_exposure
        gex_total += gamma_exposure
        if front is not None and contract.expiry == front:
            front_exposure += gamma_exposure
        if side == "call":
            call_exposure += gamma_exposure
            per_call[contract.strike] = per_call.get(contract.strike, 0.0) + gamma_exposure
        else:
            put_exposure += gamma_exposure
            per_put[contract.strike] = per_put.get(contract.strike, 0.0) + gamma_exposure
        vega_exposure = _signed_exposure(side, contract.vega, contract.open_interest, multiplier)
        vex_strikes[contract.strike] = vex_strikes.get(contract.strike, 0.0) + vega_exposure
        vex_total += vega_exposure
        if contract.delta is not None:
            delta_exposure = contract.delta * contract.open_interest * multiplier
            dex_strikes[contract.strike] = dex_strikes.get(contract.strike, 0.0) + delta_exposure
            dex_total += delta_exposure
    if gex is None:
        gex = GexProfile(
            snapshot_id=snapshot_id,
            per_strike_exposure={strike: value * scale for strike, value in gex_strikes.items()},
            total_exposure=gex_total * scale,
            flip_point=_flip_point(gex_strikes),
            spot=spot,
            front_expiry=front,
            front_exposure=front_exposure * scale,
            call_exposure=call_exposure * scale,
            put_exposure=put_exposure * scale,
            per_strike_call_exposure={strike: value * scale for strike, value in per_call.items()},
            per_strike_put_exposure={strike: value * scale for strike, value in per_put.items()},
        )
        _cache_put(gex_key, contracts, gex)
    if vex is None:
        vex = VexProfile(snapshot_id=snapshot_id, per_strike_exposure=vex_strikes, total_exposure=vex_total)
        _cache_put(vex_key, contracts, vex)
    if dex is None:
        dex = DexProfile(snapshot_id=snapshot_id, per_strike_exposure=dex_strikes, total_exposure=dex_total)
        _cache_put(dex_key, contracts, dex)
    return gex, vex, dex  # type: ignore[return-value]


def compute_gex(
    snapshot: ChainSnapshot,
    snapshot_id: str,
    multiplier: float = DEFAULT_CONTRACT_MULTIPLIER,
    spot: float | None = None,
) -> GexProfile:
    key = ("gex", snapshot_id, spot, multiplier)
    cached = _cache_get(key, snapshot.contracts_list)
    if cached is not None:
        return cached  # type: ignore[return-value]
    return compute_exposures(snapshot, snapshot_id, multiplier, spot)[0]


def compute_vex(
    snapshot: ChainSnapshot,
    snapshot_id: str,
    multiplier: float = DEFAULT_CONTRACT_MULTIPLIER,
) -> VexProfile:
    key = ("vex", snapshot_id, multiplier)
    cached = _cache_get(key, snapshot.contracts_list)
    if cached is not None:
        return cached  # type: ignore[return-value]
    return compute_exposures(snapshot, snapshot_id, multiplier)[1]


def compute_dex(
    snapshot: ChainSnapshot,
    snapshot_id: str,
    multiplier: float = DEFAULT_CONTRACT_MULTIPLIER,
) -> DexProfile:
    key = ("dex", snapshot_id, multiplier)
    cached = _cache_get(key, snapshot.contracts_list)
    if cached is not None:
        return cached  # type: ignore[return-value]
    return compute_exposures(snapshot, snapshot_id, multiplier)[2]
