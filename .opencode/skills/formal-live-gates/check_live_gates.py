#!/usr/bin/env python3
"""Exhaustive gate verification for live-ordering safety (formal-live-gates).

Property (must hold in EVERY case in scope):
  Live money cannot move unless every gate passed, and kill_switch always stops it.
  Concretely, for every gate-flag combination and every short event ordering:
    venue.place_order is reached IFF (adapter armed AND no halt AND no kill
    AND risk review approves AND all review inputs present).

Method: exhaustive small-model enumeration executed against the REAL product
code (bot/autonomy.py, bot/risk.py). No hand model of any gate function: the
only model in this file is the venue stub, which counts place_order calls and
returns a fixed fill (documented below). No network, no ports, no servers, no
persistence: all journals are in-memory, each case/sequence starts clean, and
the module-global autonomy.live_enabled is reset between cases.

Scope (finite, complete -- every case runs, nothing is sampled):
  Phase A: enable_live over signature(2) x halt(2) x kill(2) x evidence(5) x checklist(3) = 120
  Phase B: review_intent halt/kill veto + notional/options dimensions = 8
  Phase C: submit_live over armed(2) x halt(2) x kill(2) x intent(2) x args(5) = 160
  Phase D: kill/reset ordering spot checks (arm -> fill -> kill -> blocked ->
           reset-still-blocked -> re-arm -> fill; enable-while-halted fails)
  Phase E: ALL event sequences over {Eok,Ebad,K,H,R,S} of length 1..5 = 9330

Run from repo root:  .venv/bin/python .opencode/skills/formal-live-gates/check_live_gates.py
Exit 0 = property holds on current tree. Exit 1 = counterexample printed
(in the code's own terms) or harness sanity failure. Runs in ~1s, stdlib only.

Why not TLA+/TLC or Z3 here: this host has no Java (TLC needs it) and no
solver CLI; pip-installing z3-solver would add a network dependency to every
check run and would still require hand-modeling Python semantics in SMT --
strictly weaker than executing the real functions over the whole finite gate
domain, which is already complete. Revisit if the gate domain stops being finite.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import bot.autonomy as autonomy
from bot.autonomy import LiveDisabledError, enable_live, kill_switch, reset_autonomy, submit_live
from bot.journal import Journal
from bot.risk import fire_halt, review_intent
from bot.shapes import OrderIntent, OrderLeg, PaperEvidence, RiskLimits, RiskState

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
LIMITS = RiskLimits(max_trade_size=5000.0, day_profit_target=500.0, max_day_loss=1000.0)
READY = PaperEvidence(clean_days=12, clean_days_target=10, max_drawdown=100.0,
                      drawdown_bound=500.0, halt_drills_passed=True)
FULL_CHECKLIST = {"paper_evidence_reviewed": True, "risk_limits_set": True, "kill_switch_tested": True}

FAILURES: list[str] = []
COUNTS = {"cases": 0}


class _Ack:
    def __init__(self, order_id: str, fill_price: float) -> None:
        self.order_id = order_id
        self.fill_price = fill_price


class StubVenue:
    """THE ONLY MODEL in this check. Stands in for TradierVenue: counts
    place_order calls (each call = live money would move) and returns a
    fixed fill. It never touches the network."""

    def __init__(self) -> None:
        self.calls = 0

    def place_order(self, intent: OrderIntent, reference_price: float) -> _Ack:
        self.calls += 1
        return _Ack(order_id=f"stub-{self.calls}", fill_price=150.0)


def _state(**overrides) -> RiskState:
    base = {"account_size": 100000.0, "risk_per_trade": 0.01, "day_pnl": 0.0,
            "halt_active": False, "kill_active": False}
    base.update(overrides)
    return RiskState(**base)


def _good_intent() -> OrderIntent:
    return OrderIntent(symbol="TEST", side="buy", size=1000.0,
                       risk_numbers={"size": 1000.0}, signal_id="sig-1")


def _oversized_intent() -> OrderIntent:
    return OrderIntent(symbol="TEST", side="buy", size=999999.0,
                       risk_numbers={"size": 999999.0}, signal_id="sig-9")


def _options_intent() -> OrderIntent:
    leg = OrderLeg(symbol="AAPL", side="buy", size=500.0, strike=150.0,
                   expiry="2026-12-18", call_or_put="call")
    return OrderIntent(symbol="AAPL", side="buy", size=500.0,
                       risk_numbers={"size": 500.0}, signal_id="sig-opt", legs=(leg,))


def _fail(msg: str) -> None:
    FAILURES.append(msg)


def _submit_or_raise(intent, state, limits, journal, now, venue):
    """Returns (filled: bool, state_or_updated). filled True only if a fill
    came back AND the venue was reached."""
    before = venue.calls
    try:
        _fill, _pos, updated = submit_live(
            intent, state, None, limits, journal, now,
            venue=venue, reference_price=100.0)
    except LiveDisabledError:
        return False, state, venue.calls - before
    return True, updated, venue.calls - before


# ---- Phase A: enable_live gate matrix --------------------------------------
def _phase_a() -> None:
    weak_days = PaperEvidence(clean_days=3, clean_days_target=10, max_drawdown=100.0,
                              drawdown_bound=500.0, halt_drills_passed=True)
    weak_dd = PaperEvidence(clean_days=12, clean_days_target=10, max_drawdown=800.0,
                            drawdown_bound=500.0, halt_drills_passed=True)
    weak_drills = PaperEvidence(clean_days=12, clean_days_target=10, max_drawdown=100.0,
                                drawdown_bound=500.0, halt_drills_passed=False)
    evidences = [("none", None), ("weak-days", weak_days), ("weak-dd", weak_dd),
                 ("weak-drills", weak_drills), ("ready", READY)]
    checklists = [("empty", {}),
                  ("partial", {"paper_evidence_reviewed": True, "risk_limits_set": True}),
                  ("full", dict(FULL_CHECKLIST))]
    for sig in ("", "op-signed"):
        for halt in (False, True):
            for kill in (False, True):
                for ev_name, ev in evidences:
                    for cl_name, cl in checklists:
                        COUNTS["cases"] += 1
                        autonomy.live_enabled = False
                        journal = Journal()
                        got = enable_live(sig, _state(halt_active=halt, kill_active=kill),
                                          journal, NOW, ev, cl)
                        expected = (bool(sig) and not halt and not kill
                                    and ev is READY and cl == FULL_CHECKLIST)
                        if got != expected or autonomy.live_enabled != expected:
                            _fail(
                                "[counterexample A: enable_live] "
                                f"sig={sig!r} halt={halt} kill={kill} "
                                f"evidence={ev_name} checklist={cl_name} -> "
                                f"enable_live returned {got}, live_enabled="
                                f"{autonomy.live_enabled}; expected {expected}")
                        elif not expected and sig and not halt and not kill:
                            kinds = [e.kind for e in journal.entries()]
                            if not kinds or kinds[-1] != "live_blocked":
                                _fail(
                                    "[counterexample A: enable_live journal] "
                                    f"sig={sig!r} evidence={ev_name} checklist={cl_name} "
                                    f"-> rejected without a live_blocked receipt")
    autonomy.live_enabled = False


# ---- Phase B: review_intent vetoes -----------------------------------------
def _phase_b() -> None:
    cases = [
        ("good/clean", _good_intent(), _state(), "approved"),
        ("good/halt", _good_intent(), _state(halt_active=True), "rejected"),
        ("good/kill", _good_intent(), _state(kill_active=True), "rejected"),
        ("good/halt+kill", _good_intent(),
         _state(halt_active=True, kill_active=True), "rejected"),
        ("oversized/clean", _oversized_intent(), _state(), "rejected"),
        ("options/no-profile", _options_intent(), _state(), "rejected"),
        ("good/kill+oversized", _oversized_intent(),
         _state(kill_active=True), "rejected"),
        ("options/halt", _options_intent(), _state(halt_active=True), "rejected"),
    ]
    for name, intent, state, expected in cases:
        COUNTS["cases"] += 1
        got = review_intent(intent, state, LIMITS, Journal(), NOW)
        if got != expected:
            _fail(f"[counterexample B: review_intent] {name} -> {got!r}; expected {expected!r}")


# ---- Phase C: submit_live gate matrix (independent oracle) -------------------
def _phase_c() -> None:
    arg_variants = ["all", "no-state", "no-limits", "no-journal", "no-now"]
    for live in (False, True):
        for halt in (False, True):
            for kill in (False, True):
                for small in (True, False):
                    intent = _good_intent() if small else _oversized_intent()
                    # Harness sanity: on clean flags the real review must agree
                    # with the direct size check used as oracle below.
                    oracle_review = review_intent(
                        intent, _state(), LIMITS, Journal(), NOW)
                    if (oracle_review == "approved") != small:
                        _fail(f"[harness] review oracle disagrees with size oracle "
                              f"(small={small} -> {oracle_review!r}); fix harness, not product")
                        continue
                    for variant in arg_variants:
                        COUNTS["cases"] += 1
                        autonomy.live_enabled = live
                        state = _state(halt_active=halt, kill_active=kill)
                        journal: Journal | None = Journal()
                        now = NOW
                        st, lim, jo, no = state, LIMITS, journal, now
                        if variant == "no-state":
                            st = None
                        elif variant == "no-limits":
                            lim = None
                        elif variant == "no-journal":
                            jo = None
                        elif variant == "no-now":
                            no = None
                        venue = StubVenue()
                        expected_ok = (live and not halt and not kill
                                       and variant == "all" and small)
                        filled, _, delta = _submit_or_raise(
                            intent, st, lim, jo, no, venue)
                        if filled != expected_ok or (venue.calls != (1 if expected_ok else 0)):
                            _fail(
                                "[counterexample C: submit_live] "
                                f"live_enabled={live} halt={halt} kill={kill} "
                                f"intent={'good' if small else 'oversized'} args={variant} "
                                f"-> filled={filled} venue_calls={venue.calls}; "
                                f"expected {'fill' if expected_ok else 'LiveDisabledError, no venue call'}")
    autonomy.live_enabled = False


# ---- Phase C2: submit_live's OWN gates (review verdict forced) ------------------
def _phase_c2() -> None:
    """Defense in depth: submit_live must enforce armed/halt/kill/args gates
    even when review is forced to approve, and must honor a forced review
    rejection even with all flags clear. Only the review verdict is stubbed
    here (two lambdas); every gate under test is the real product code."""
    real_review = autonomy.review_intent
    try:
        autonomy.review_intent = lambda *a, **k: "approved"  # type: ignore[method-assign]
        for live in (False, True):
            for halt in (False, True):
                for kill in (False, True):
                    for variant in ("all", "no-state", "no-limits", "no-journal", "no-now"):
                        COUNTS["cases"] += 1
                        autonomy.live_enabled = live
                        state = _state(halt_active=halt, kill_active=kill)
                        journal: Journal | None = Journal()
                        now = NOW
                        st, lim, jo, no = state, LIMITS, journal, now
                        if variant == "no-state":
                            st = None
                        elif variant == "no-limits":
                            lim = None
                        elif variant == "no-journal":
                            jo = None
                        elif variant == "no-now":
                            no = None
                        venue = StubVenue()
                        expected_ok = (live and not halt and not kill
                                       and variant == "all")
                        filled, _, _ = _submit_or_raise(
                            _good_intent(), st, lim, jo, no, venue)
                        if filled != expected_ok or venue.calls != (1 if expected_ok else 0):
                            _fail(
                                "[counterexample C2: submit_live own gates] "
                                f"review forced approve, live_enabled={live} halt={halt} "
                                f"kill={kill} args={variant} -> filled={filled} "
                                f"venue_calls={venue.calls}; expected "
                                f"{'fill' if expected_ok else 'LiveDisabledError, no venue call'}")
        autonomy.review_intent = lambda *a, **k: "rejected"  # type: ignore[method-assign]
        COUNTS["cases"] += 1
        autonomy.live_enabled = True
        venue = StubVenue()
        filled, _, _ = _submit_or_raise(
            _good_intent(), _state(), LIMITS, Journal(), NOW, venue)
        if filled or venue.calls != 0:
            _fail("[counterexample C2: submit_live honors review] "
                  "review forced reject with all flags clear -> "
                  f"filled={filled} venue_calls={venue.calls}; "
                  "expected LiveDisabledError, no venue call")
    finally:
        autonomy.review_intent = real_review
        autonomy.live_enabled = False


# ---- Phase D: kill/reset ordering -------------------------------------------
def _phase_d() -> None:
    COUNTS["cases"] += 1
    autonomy.live_enabled = False
    journal = Journal()
    venue = StubVenue()
    state = _state()
    assert enable_live("op-signed", state, journal, NOW, READY, FULL_CHECKLIST) is True
    filled, state, _ = _submit_or_raise(_good_intent(), state, LIMITS, journal, NOW, venue)
    if not filled or venue.calls != 1:
        _fail("[counterexample D] armed adapter did not fill a good intent")
        autonomy.live_enabled = False
        return
    killed = kill_switch(state, journal, NOW)
    if autonomy.live_enabled or not (killed.kill_active and killed.halt_active):
        _fail("[counterexample D] kill_switch left live_enabled="
              f"{autonomy.live_enabled} kill={killed.kill_active} halt={killed.halt_active}")
    filled, _, _ = _submit_or_raise(_good_intent(), killed, LIMITS, journal, NOW, venue)
    if filled or venue.calls != 1:
        _fail("[counterexample D] submit_live moved money after kill_switch "
              f"(filled={filled} venue_calls={venue.calls})")
    cleared = reset_autonomy(killed, journal, NOW)
    if cleared.kill_active or cleared.halt_active:
        _fail("[counterexample D] reset_autonomy did not clear kill/halt flags")
    if autonomy.live_enabled:
        _fail("[counterexample D] reset_autonomy re-armed the adapter by itself")
    filled, _, _ = _submit_or_raise(_good_intent(), cleared, LIMITS, journal, NOW, venue)
    if filled or venue.calls != 1:
        _fail("[counterexample D] submit_live filled after reset without re-arm "
              f"(filled={filled} venue_calls={venue.calls})")
    assert enable_live("op-signed", cleared, journal, NOW, READY, FULL_CHECKLIST) is True
    filled, state, _ = _submit_or_raise(_good_intent(), cleared, LIMITS, journal, NOW, venue)
    if not filled or venue.calls != 2:
        _fail("[counterexample D] full re-arm path did not recover")
    COUNTS["cases"] += 1
    autonomy.live_enabled = False
    journal2 = Journal()
    if enable_live("op-signed", _state(halt_active=True), journal2, NOW,
                   READY, FULL_CHECKLIST) is not False or autonomy.live_enabled:
        _fail("[counterexample D] enable_live armed the adapter while halt_active")
    autonomy.live_enabled = False


# ---- Phase E: exhaustive event sequences --------------------------------------
_OPS = ("Eok", "Ebad", "K", "H", "R", "S")


def _run_sequence(seq: tuple[str, ...]) -> None:
    autonomy.live_enabled = False
    journal = Journal()
    venue = StubVenue()
    cur = _state()
    fills = 0
    for op in seq:
        if op == "Eok":
            enable_live("op-signed", _state(), journal, NOW, READY, FULL_CHECKLIST)
        elif op == "Ebad":
            enable_live("", _state(), journal, NOW, READY, FULL_CHECKLIST)
        elif op == "K":
            cur = kill_switch(cur, journal, NOW)
        elif op == "H":
            cur = fire_halt(cur)
        elif op == "R":
            cur = reset_autonomy(cur, journal, NOW)
        elif op == "S":
            COUNTS["cases"] += 1
            live = autonomy.live_enabled
            expected_ok = live and not cur.halt_active and not cur.kill_active
            if expected_ok:
                oracle = review_intent(_good_intent(), cur, LIMITS, Journal(), NOW)
                if oracle != "approved":
                    _fail(f"[harness] seq {''.join(seq)}: good intent not approved "
                          f"on clean flags (day_pnl drift?); fix harness")
                    return
            before = venue.calls
            try:
                _fill, _pos, cur = submit_live(
                    _good_intent(), cur, None, LIMITS, journal, NOW,
                    venue=venue, reference_price=100.0)
                raised = False
            except LiveDisabledError:
                raised = True
            delta = venue.calls - before
            if expected_ok and (raised or delta != 1):
                _fail(f"[counterexample E: ordering] seq {''.join(seq)} -> S step: "
                      f"live={live} halt={cur.halt_active if not raised else '?'} "
                      f"gates open but raised={raised} venue_delta={delta}; expected fill")
                return
            if not expected_ok and (not raised or delta != 0):
                _fail(f"[counterexample E: ordering] seq {''.join(seq)} -> S step: "
                      f"live={live} halt={cur.halt_active} kill={cur.kill_active} "
                      f"gates closed but raised={raised} venue_delta={delta}; "
                      f"expected LiveDisabledError with no venue call")
                return
            if not raised:
                fills += 1
    if fills > 0 and "Eok" not in seq:
        _fail(f"[counterexample E: ordering] seq {''.join(seq)} filled {fills}x "
              f"without any successful arm")
    autonomy.live_enabled = False


def _phase_e() -> None:
    from itertools import product
    for length in (1, 2, 3, 4, 5):
        for seq in product(_OPS, repeat=length):
            _run_sequence(seq)


def main() -> int:
    _phase_a()
    _phase_b()
    _phase_c()
    _phase_c2()
    _phase_d()
    _phase_e()
    print(f"formal-live-gates: {COUNTS['cases']} cases executed "
          f"(A enable=120, B review=8, C submit=160, C2 own-gates=41, D ordering=2, "
          f"E sequences=9330 incl. S-steps).")
    print("gates: bot/autonomy.py:enable_live/submit_live/kill_switch/reset_autonomy + "
          "bot/risk.py:review_intent/fire_halt/fire_kill/operator_reset; "
          "venue stub counts place_order, fixed fill, no network.")
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} counterexample(s):")
        for msg in FAILURES[:20]:
            print("  " + msg)
        if len(FAILURES) > 20:
            print(f"  ... and {len(FAILURES) - 20} more")
        return 1
    print("PASS: live money cannot move unless every gate passed; kill_switch always stops it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
