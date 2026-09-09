"""§7/§8 regression: Mode C runs source-OFF, and a failed solve grants nothing.

§7 -- run_coupled() executed a full Mode-C BDF solve with the laser still
absorbing, discarded the result, then re-solved with a zeroed-laser config
clone. The first solve was dead (it mutated neither cfg nor T_init and consumed
no RNG) and it also constructed a Mode C that violates the mode definition.

§8 -- solver_status was reported next to the metrics as a passive string while
ready_terms was computed from the same result regardless, so a non-converged
integration could still yield FORECAST_READY_IF_MEASURED.

MODEL-ONLY / FORECAST-ONLY. Software verification; not a hardware statement.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qta_multiphysics import coupled_mode_solver as CMS       # noqa: E402
from qta_multiphysics.config import default_config            # noqa: E402


# --------------------------------------------- §7: Mode C runs source OFF --

def test_mode_c_solves_only_with_the_processing_source_off():
    """Every Mode-C thermal solve must see absorbed_fraction == 0."""
    seen = []
    real = CMS.solve_thermal_1d

    def spy(cfg, **kw):
        seen.append((kw.get("t_end"), cfg.laser.absorbed_fraction))
        return real(cfg, **kw)

    CMS.solve_thermal_1d = spy
    try:
        CMS.run_coupled(default_config())
    finally:
        CMS.solve_thermal_1d = real

    rec = default_config().solver.recovery_window_s
    mode_c = [f for (t_end, f) in seen if t_end == rec]
    assert mode_c, f"no Mode-C solve observed; saw {seen}"
    assert all(f == 0.0 for f in mode_c), \
        f"a Mode-C solve ran with the source ON: absorbed_fraction={mode_c}"


def test_mode_c_is_solved_exactly_once():
    """The discarded duplicate must not come back."""
    rec = default_config().solver.recovery_window_s
    n = []
    real = CMS.solve_thermal_1d

    def spy(cfg, **kw):
        if kw.get("t_end") == rec:
            n.append(1)
        return real(cfg, **kw)

    CMS.solve_thermal_1d = spy
    try:
        CMS.run_coupled(default_config())
    finally:
        CMS.solve_thermal_1d = real
    assert sum(n) == 1, f"Mode C solved {sum(n)} times; expected exactly 1"


# -------------------------------------- §8: failure denies all authority --

class _Failed:
    """A result object shaped like a solve that did not converge."""
    solver_status = "failed"

    def __init__(self):
        import numpy as np
        self.T = np.zeros((4, 4))

    def hotspot_temperature_K(self):
        return 1.0e9

    def nv_layer_temperature_K(self):
        return 0.0

    def nv_layer_temperature_final_K(self):
        return 0.0      # would read as "cold enough" -> ready

    def recool_time_s(self, th):
        return 0.0

    def post_pulse_drift_K(self):
        return 0.0


def test_require_converged_rejects_a_failed_status():
    for bad in ("failed", "diverged", None, "", "OK"):
        obj = type("R", (), {"solver_status": bad})()
        try:
            CMS.require_converged(obj, "unit")
        except CMS.SolverFailure:
            pass
        else:
            raise AssertionError(f"solver_status={bad!r} was accepted")


def test_require_converged_accepts_ok():
    obj = type("R", (), {"solver_status": "ok"})()
    assert CMS.require_converged(obj, "unit") is obj


def test_failed_mode_b_solve_cannot_produce_readiness():
    """Adversarial: inject failure and prove readiness is unreachable.

    The injected result reports temperatures that would satisfy every
    readiness term, so only the status check can stop it.
    """
    real = CMS.solve_thermal_1d
    CMS.solve_thermal_1d = lambda cfg, **kw: _Failed()
    try:
        CMS.run_coupled(default_config())
    except CMS.SolverFailure as e:
        assert "Mode B" in str(e)
    else:
        raise AssertionError("a failed solve produced a readiness forecast")
    finally:
        CMS.solve_thermal_1d = real


def test_failed_mode_c_solve_cannot_produce_readiness():
    real = CMS.solve_thermal_1d
    rec = default_config().solver.recovery_window_s
    CMS.solve_thermal_1d = (
        lambda cfg, **kw: _Failed() if kw.get("t_end") == rec else real(cfg, **kw))
    try:
        CMS.run_coupled(default_config())
    except CMS.SolverFailure as e:
        assert "Mode C" in str(e)
    else:
        raise AssertionError("a failed recovery solve produced a readiness forecast")
    finally:
        CMS.solve_thermal_1d = real


if __name__ == "__main__":
    ns = dict(globals())
    fails = 0
    for name, fn in sorted(ns.items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:                                # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if fails else 0)


# --------------------------------------------------------------------------
# The 3D counterpart, which never inherited the rule.
#
# require_converged was written for run_coupled and applied there. For a long
# time those were the ONLY TWO call sites in the repository, while
# run_mode_sequence_3d -- whose own docstring says it runs the canonical mode
# order "exactly mirroring the 1D/2D coupled_mode_solver" -- mirrored the
# mode order, the state hand-off and the species interlocks, and not this.
# Every 3D result, the Mode-C readiness taken from it, the campaign state,
# the falsification report and the machine FSM were built on a solve nobody
# had asked about.
#
# The tests that existed asserted require_converged rejects a hand-made
# object with solver_status="failed". That proves the function works. It
# proves nothing about whether anything calls it, which was the defect.
# --------------------------------------------------------------------------

import pytest                                                   # noqa: E402
from qta_multiphysics import mode_sequence_3d as MS3            # noqa: E402
from qta_multiphysics.numerics import (                         # noqa: E402
    SolverFailure, require_converged, require_integrated,
)


class _Failed3D:
    """Shaped like a 3D solve that did not converge, reporting cold numbers.

    Every temperature here would satisfy Mode-C readiness, so only the status
    check can stop it -- the same construction the 1D tests above use.
    """
    solver_status = "failed"
    message = "step size underflow"

    def __init__(self):
        import numpy as np
        self.T = np.zeros((8, 4))
        self.t = np.linspace(0.0, 1.0, 4)
        self.energy = {"rel_residual": 0.0, "converged": False}

    def probe_timeseries_K(self):
        import numpy as np
        return np.zeros(4)                  # reads as "recooled immediately"

    def nv_layer_temperature_K(self):
        return 0.0

    def peak_temperature_K(self):
        return 0.0

    def energy_residual(self):
        return 0.0


def _seq_with_failed(monkey_at, cfg):
    real = MS3.solve_thermal_3d
    calls = {"n": 0}

    def spy(*a, **kw):
        calls["n"] += 1
        if calls["n"] == monkey_at:
            return _Failed3D()
        return real(*a, **kw)

    MS3.solve_thermal_3d = spy
    try:
        return MS3.run_mode_sequence_3d(cfg)
    finally:
        MS3.solve_thermal_3d = real


def test_a_failed_MODE_B_3d_solve_denies_the_whole_sequence():
    """The first solve. Nothing downstream of it may be built."""
    with pytest.raises(SolverFailure, match="MODE_B"):
        _seq_with_failed(1, default_config())


def test_a_failed_MODE_C_3d_solve_cannot_produce_readiness():
    """Mode C decides readiness, and Mode D is constructed only if it holds.

    The injected result recools instantly, so every readiness term is
    satisfied and only the status check can refuse it.
    """
    with pytest.raises(SolverFailure, match="MODE_C"):
        _seq_with_failed(2, default_config())


def test_a_failed_MODE_D_hold_denies_the_sequence():
    """The third solve, and the one the first two tests do not reach.

    A mutation removing this check survived a suite that injected failure
    only at solves 1 and 2 -- and survived the honest test too, because that
    test asserted nothing about the Mode D hold. A branch no test reaches is
    a branch no test defends, however green the file is.
    """
    with pytest.raises(SolverFailure, match="MODE_D"):
        _seq_with_failed(3, default_config())


def test_an_honest_3d_sequence_still_runs():
    """Anti-vacuity: a rule that refused every sequence would pass all three.

    Asserts Mode D was actually entered, so the failure-injection test above
    is known to be reaching a branch that runs rather than one that is
    skipped for unrelated reasons.
    """
    seq = MS3.run_mode_sequence_3d(default_config())
    assert seq.tB.solver_status == "ok"
    assert seq.tC.solver_status == "ok"
    assert seq.mode_c_ready is True
    assert seq.tD_hold is not None and seq.tD_hold.solver_status == "ok", (
        "Mode D was not entered, so the Mode-D failure test above is passing "
        "over a branch that never runs")


# -------------------------------- the raw-integrator half of the contract --

class _Truncated:
    """What scipy actually returns when an integration stops early.

    Not garbage: a SHORTER trajectory, every value finite. assert_finite
    passes, and a caller pairing so.y with the t_eval it asked for ends up
    holding two arrays of different lengths.
    """
    success = False
    status = -1
    message = "Required step size is less than spacing between numbers."

    def __init__(self):
        import numpy as np
        self.y = np.zeros((3, 2))
        self.t = np.linspace(0.0, 0.1, 2)


def test_require_integrated_refuses_a_truncated_trajectory():
    with pytest.raises(SolverFailure, match="did not finish"):
        require_integrated(_Truncated(), "unit")


def test_require_integrated_accepts_a_finished_one():
    ok = type("S", (), {"success": True, "status": 0, "message": ""})()
    assert require_integrated(ok, "unit") is ok


def test_the_contract_is_importable_from_the_numerics_layer():
    """Where it lives is part of the fix.

    It was defined beside the 1D coupled solver, which is where it was
    written and the only place it was applied. A rule that lives inside one
    of the things it governs is a rule the next sibling does not inherit.
    """
    from qta_multiphysics import numerics
    assert numerics.require_converged is require_converged
    # ...and still reachable where every existing caller imports it from.
    assert CMS.require_converged is require_converged


def test_a_reduction_check_will_not_compare_against_a_non_answer():
    """A reduction check compares two solvers. One of them must be an answer.

    These solves are taken outside run_mode_sequence_3d, so enforcing it
    there does not reach them: the rule has to be stated at each place that
    runs a solve for something to be concluded from.
    """
    from qta_multiphysics import reduction_checks_3d as RC
    real = RC.solve_thermal_3d
    RC.solve_thermal_3d = lambda *a, **kw: _Failed3D()
    try:
        with pytest.raises(SolverFailure, match="reduction_3d_to_1d"):
            RC.reduction_3d_to_1d(default_config())
    finally:
        RC.solve_thermal_3d = real


def test_a_failed_3d_solve_reports_no_energy_residual():
    """The accounting must not quadrature an extrapolation.

    solve_thermal_3d integrates the solver's continuous interpolant over the
    whole window. An OdeSolution evaluated past the interval it actually
    covered extrapolates the last polynomial rather than refusing, so a
    failed solve used to hand back a perfectly ordinary rel_residual --
    computed over time the integrator never reached -- beside
    solver_status="failed" for a reader to notice or not.

    assert_finite does not catch it: a failed integration returns a shorter
    trajectory, not a wrong-looking one.
    """
    import math
    from qta_multiphysics import thermal_3d_transient as T3
    real = T3.solve_ivp

    def failing(*a, **kw):
        sol = real(*a, **kw)
        sol.success = False              # everything else genuinely real
        sol.message = "step size underflow"
        return sol

    T3.solve_ivp = failing
    try:
        r = T3.solve_thermal_3d(default_config())
    finally:
        T3.solve_ivp = real

    assert r.solver_status == "failed"
    assert r.energy["converged"] is False
    assert math.isnan(r.energy["rel_residual"]), (
        f"a failed solve reported rel_residual={r.energy['rel_residual']!r}; "
        "that number is the quadrature of an extrapolation")
    assert math.isnan(r.energy["residual_J"])

    from qta_multiphysics.energy_accounting_3d import closure_ok
    assert closure_ok(r, 0.05) is False, (
        "energy closure accepted a solve that did not converge")


def test_an_honest_3d_solve_still_reports_a_residual():
    """Anti-vacuity: NaN must mean failure, not 'this path is dead'."""
    import math
    from qta_multiphysics import thermal_3d_transient as T3
    r = T3.solve_thermal_3d(default_config())
    assert r.solver_status == "ok"
    assert r.energy["converged"] is True
    assert not math.isnan(r.energy["rel_residual"])
