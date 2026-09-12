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

import numpy as np                                              # noqa: E402
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


def test_a_failed_3d_solve_NEVER_EVALUATES_the_interpolant_past_what_it_integrated():
    """Not "the fake future is discarded" -- it is never constructed.

    NaN-ing the residual stopped the extrapolated arithmetic being BELIEVED.
    It did not stop it being PERFORMED: sol_obj.sol(tq) still ran across the
    whole requested window, and an OdeSolution asked for a time past the
    interval it covers extrapolates its final polynomial rather than
    refusing. So every quantity was computed partly over time the integrator
    never reached, and then thrown away.

    This spies on the interpolant itself and asserts the strong property: on
    a failed solve, no evaluation occurs beyond sol_obj.t[-1].
    """
    from qta_multiphysics import thermal_3d_transient as T3
    real = T3.solve_ivp
    seen = {"max_t": None, "calls": 0}

    def failing(*a, **kw):
        sol = real(*a, **kw)
        # Truncate to a genuinely shorter trajectory, as a real failure does.
        cut = max(2, len(sol.t) // 3)
        sol.t = sol.t[:cut]
        sol.y = sol.y[:, :cut]
        sol.success = False
        sol.message = "step size underflow"
        inner = sol.sol

        def watched(tq):
            seen["calls"] += 1
            arr = np.atleast_1d(np.asarray(tq, dtype=float))
            hi = float(arr.max())
            seen["max_t"] = hi if seen["max_t"] is None else max(
                seen["max_t"], hi)
            return inner(tq)

        sol.sol = watched
        return sol

    T3.solve_ivp = failing
    try:
        r = T3.solve_thermal_3d(default_config())
    finally:
        T3.solve_ivp = real

    assert r.solver_status == "failed"
    assert seen["calls"] == 0, (
        f"the interpolant was evaluated {seen['calls']} time(s) on a failed "
        f"solve, out to t={seen['max_t']}; the integrated interval ended at "
        f"{float(r.t[-1])}. A quantity that must not be trusted should not "
        "be computed")
    assert r.energy["converged"] is False
    assert r.energy["accounting"] == "UNAVAILABLE_INTEGRATION_INCOMPLETE"
    assert r.energy["integrated_to_s"] < r.energy["requested_t_end_s"], (
        "this test is not exercising a truncated trajectory")
    # What was really integrated survives, so the failure is diagnosable.
    assert r.t.size >= 2 and r.T.shape[1] == r.t.size
    assert "underflow" in r.message


def test_the_honest_path_DOES_evaluate_the_interpolant():
    """Anti-vacuity: the guard above must name a real difference.

    If solve_thermal_3d never used the interpolant, the assertion that a
    failed solve does not use it would hold for a reason that has nothing to
    do with convergence.
    """
    from qta_multiphysics import thermal_3d_transient as T3
    real = T3.solve_ivp
    seen = {"calls": 0}

    def watching(*a, **kw):
        sol = real(*a, **kw)
        inner = sol.sol

        def watched(tq):
            seen["calls"] += 1
            return inner(tq)

        sol.sol = watched
        return sol

    T3.solve_ivp = watching
    try:
        r = T3.solve_thermal_3d(default_config())
    finally:
        T3.solve_ivp = real
    assert r.solver_status == "ok"
    assert seen["calls"] > 0, (
        "the converged path does not use the interpolant either, so the "
        "failed-path assertion establishes nothing about convergence")


def test_an_honest_3d_solve_still_reports_a_residual():
    """Anti-vacuity: NaN must mean failure, not 'this path is dead'."""
    import math
    from qta_multiphysics import thermal_3d_transient as T3
    r = T3.solve_thermal_3d(default_config())
    assert r.solver_status == "ok"
    assert r.energy["converged"] is True
    assert not math.isnan(r.energy["rel_residual"])


# ===========================================================================
# THE SIBLING SWEEP.
#
# P0-6 was closed by fixing the two places the defect was FOUND: the mode
# sequence, which now refuses a failed phase, and the accounting inside
# solve_thermal_3d, which no longer quadratures a future the integrator
# never reached.
#
# Neither closed the defect CLASS. require_converged existed and had five
# call sites; this package had fifteen places that read a published number
# off a solve. The eleven below asked nothing. Each is tested separately
# because what a failed solve would have produced there is different, and
# the direction of the error is the point:
#
#   * a failed solve returns a SHORTER trajectory, so `[-1]` is the last
#     time REACHED rather than t_end;
#   * every value in it is finite, so assert_finite passes;
#   * a probe sampled early is COOLER, so a screening study reads it as a
#     negative sensitivity and a recovery study reads it as "recooled".
#
# Every stub below therefore returns usable numbers rather than nothing:
# deleting a guard must let the caller SUCCEED with a wrong answer, so that
# the test fails for the reason it was written for instead of on a missing
# attribute.
# ===========================================================================

import contextlib                                               # noqa: E402
from qta_multiphysics.mesh_3d import Grid3DConfig               # noqa: E402

#: Small enough to run several times in a test; the guard being exercised
#: does not depend on the mesh.
TINY = Grid3DConfig(nx=4, ny=4, nz=5)
TINY_REF = Grid3DConfig(nx=5, ny=5, nz=6)


@contextlib.contextmanager
def _failing(module, attr, at=1, stub=None):
    """Make the ``at``-th call to ``module.attr`` come back non-converged."""
    stub = stub or _Failed3D
    real = getattr(module, attr)
    calls = {"n": 0}

    def spy(*a, **kw):
        calls["n"] += 1
        if calls["n"] == at:
            return stub()
        return real(*a, **kw)

    setattr(module, attr, spy)
    try:
        yield calls
    finally:
        setattr(module, attr, real)


class _Failed1D:
    """A 1D solve that did not converge, reporting ordinary temperatures."""
    solver_status = "failed"

    def nv_layer_temperature_K(self):
        return 4.2

    def nv_layer_temperature_final_K(self):
        return 0.0

    def hotspot_temperature_K(self):
        return 9.9e9        # would satisfy "optical feeds thermal"

    def recool_time_s(self, th):
        return 0.0

    def final_profile(self):
        return type("P", (), {"is_finite": lambda self: True})()

    @property
    def T(self):
        import numpy as np
        return np.zeros((4, 4))


class _Failed2D:
    """A 2D solve that did not converge, reporting an ordinary field."""
    solver_status = "failed"

    def nv_layer_max_K(self):
        return 4.2

    @property
    def T_final(self):
        import numpy as np

        class _F:
            def is_finite(self):
                return True

            def radial_gradient(self):
                return np.zeros((4, 4))
        return _F()


# ------------------------------------------- the 3D convergence report ----

def test_a_failed_CI_solve_cannot_produce_a_convergence_verdict():
    """The report's own baseline. Everything else is measured against it."""
    from qta_multiphysics import convergence_3d as C3
    with _failing(C3, "solve_thermal_3d", at=1):
        with pytest.raises(SolverFailure, match="CI-mesh"):
            C3.convergence_report(default_config(), TINY, TINY_REF)


def test_a_failed_REFINED_solve_cannot_certify_mesh_convergence():
    """The refined solve IS the mesh check.

    A truncated refined trajectory is sampled earlier and reads cooler, so
    the probe difference it produces is a statement about when the solver
    stopped rather than about the mesh.
    """
    from qta_multiphysics import convergence_3d as C3
    with _failing(C3, "solve_thermal_3d", at=2):
        with pytest.raises(SolverFailure, match="refined-mesh"):
            C3.convergence_report(default_config(), TINY, TINY_REF)


def test_a_failed_TIGHTENED_solve_cannot_certify_time_convergence():
    """The third solve, reached by neither test above."""
    from qta_multiphysics import convergence_3d as C3
    with _failing(C3, "solve_thermal_3d", at=3):
        with pytest.raises(SolverFailure, match="tightened-integration"):
            C3.convergence_report(default_config(), TINY, TINY_REF)


def test_an_honest_convergence_report_runs_all_three_solves():
    """Anti-vacuity, and the reachability of the third injection point."""
    from qta_multiphysics import convergence_3d as C3
    with _failing(C3, "solve_thermal_3d", at=99) as calls:
        rep = C3.convergence_report(default_config(), TINY, TINY_REF)
    assert calls["n"] == 3, (
        f"the report took {calls['n']} solve(s); the at=3 injection above "
        "would never fire")
    assert "mesh_check" in rep


def test_the_tightened_solve_is_still_restored_when_it_is_refused():
    """The guard sits inside the try/finally that restores solver rtol.

    A guard that raised past the restoration would leave the shared config
    permanently tightened for every later caller in the process.
    """
    from qta_multiphysics import convergence_3d as C3
    cfg = default_config()
    rtol0 = cfg.solver.rtol
    with _failing(C3, "solve_thermal_3d", at=3):
        with pytest.raises(SolverFailure):
            C3.convergence_report(cfg, TINY, TINY_REF)
    assert cfg.solver.rtol == rtol0, (
        "the refused tightening left rtol scaled down for everyone else")


# ------------------------------------------------ 3D sensitivity screening --

def test_a_failed_solve_cannot_report_a_sensitivity():
    """Every perturbation goes through _rise, so this is the whole surface.

    The stub's probe series is all zeros, which reads as a large NEGATIVE
    temperature rise -- so an unguarded failure does not merely add noise,
    it flips the sign of a published sensitivity.
    """
    from qta_multiphysics import sensitivity_3d as S3
    with _failing(S3, "solve_thermal_3d", at=1):
        with pytest.raises(SolverFailure, match="probe-rise"):
            S3._rise(default_config())


def test_a_failed_PERTURBED_solve_cannot_report_a_sensitivity():
    """The base solve succeeds and the first perturbation does not.

    This is the likelier failure in practice -- a perturbed configuration is
    the one that stiffens -- and it is the case a test that only injects at
    the base call never reaches.
    """
    from qta_multiphysics import sensitivity_3d as S3
    with _failing(S3, "solve_thermal_3d", at=2):
        with pytest.raises(SolverFailure, match="probe-rise"):
            S3.sensitivity_rows(default_config())


# --------------------------------------------------- the screening adapters --

def test_the_mdao_response_refuses_a_failed_solve():
    from qta_multiphysics import thermal_3d_transient as T3
    from qta_multiphysics.stack import mdao_openmdao as MD
    with _failing(T3, "solve_thermal_3d", at=1):
        with pytest.raises(SolverFailure, match="mdao screening"):
            MD.evaluate({})


def test_the_salib_screening_response_refuses_a_failed_solve():
    from qta_multiphysics import thermal_3d_transient as T3
    from qta_multiphysics.stack import sensitivity_salib as SA
    with _failing(T3, "solve_thermal_3d", at=1):
        with pytest.raises(SolverFailure, match="salib screening"):
            SA.screening_response_K(default_config())


# ------------------------------------------------------- the heavy 3D pass --

def test_the_heavy_pass_refuses_a_failed_solve(tmp_path):
    """The one 3D consumer that solved directly and asked nothing.

    It wrote a probe timeseries, a hotspot table and an energy accounting
    from whatever came back.
    """
    from qta_multiphysics import runner_3d as R3
    with _failing(R3, "solve_thermal_3d", at=1):
        with pytest.raises(SolverFailure, match="heavy"):
            R3.write_heavy_pass(default_config(), tmp_path / "heavy")
    assert not list((tmp_path / "heavy").glob("*.csv")), (
        "the refusal still left artifacts behind")


def test_an_honest_heavy_pass_still_writes_its_outputs(tmp_path):
    """Anti-vacuity: the guard must refuse failures, not every call."""
    from qta_multiphysics import runner_3d as R3
    hdir = R3.write_heavy_pass(default_config(), tmp_path / "heavy", mesh=TINY)
    names = sorted(p.name for p in hdir.glob("*"))
    assert names == ["heavy_3d_note.json",
                     "thermal_3d_energy_accounting_heavy.csv",
                     "thermal_3d_hotspots_heavy.csv",
                     "thermal_3d_probe_timeseries_heavy.csv"], names


# ------------------------------------------- the 1D/2D verification layer --

def test_mesh_convergence_1d_refuses_a_failed_solve():
    """solver_status was recorded in a row beside the verdict, and the
    verdict was computed from the same result regardless -- verbatim the
    defect require_converged's own docstring says it was written for."""
    from qta_multiphysics import verification as V
    with _failing(V, "solve_thermal_1d", at=1, stub=_Failed1D):
        with pytest.raises(SolverFailure, match="mesh_convergence_1d"):
            V.mesh_convergence_1d(default_config())


def test_mesh_convergence_1d_refuses_a_LATER_failed_refinement():
    """The verdict compares n=200 against n=400; the third solve is the one
    a test injecting only at the first never reaches."""
    from qta_multiphysics import verification as V
    with _failing(V, "solve_thermal_1d", at=3, stub=_Failed1D):
        with pytest.raises(SolverFailure, match="n=400"):
            V.mesh_convergence_1d(default_config())


def test_mesh_convergence_2d_refuses_a_failed_solve():
    from qta_multiphysics import verification as V
    with _failing(V, "solve_thermal_2d", at=3, stub=_Failed2D):
        with pytest.raises(SolverFailure, match="mesh_convergence_2d: fine"):
            V.mesh_convergence_2d(default_config())


def test_axis_symmetry_2d_refuses_a_failed_solve():
    from qta_multiphysics import verification as V
    with _failing(V, "solve_thermal_2d", at=1, stub=_Failed2D):
        with pytest.raises(SolverFailure, match="axis_symmetry_2d"):
            V.axis_symmetry_2d(default_config())


def test_the_2d_to_1d_reduction_refuses_either_failed_side():
    """Both sides, because the check is a COMPARISON: a truncated 1D answer
    and a truncated 2D answer are equally capable of agreeing."""
    from qta_multiphysics import verification as V
    with _failing(V, "solve_thermal_1d", at=1, stub=_Failed1D):
        with pytest.raises(SolverFailure, match="1D solve"):
            V.reduction_2d_to_1d(default_config())
    with _failing(V, "solve_thermal_2d", at=1, stub=_Failed2D):
        with pytest.raises(SolverFailure, match="radial-disabled"):
            V.reduction_2d_to_1d(default_config())


def test_coupling_checks_refuse_a_failed_solve():
    """The stub reports a hotspot of 9.9e9 K, so the coupling it is asked
    about would read as confirmed."""
    from qta_multiphysics import verification as V
    with _failing(V, "solve_thermal_1d", at=1, stub=_Failed1D):
        with pytest.raises(SolverFailure, match="optical-feeds-thermal"):
            V.coupling_checks(default_config())


# -------------------------------------------------------- the UQ ensemble --

def test_a_failed_RECOVERY_solve_is_counted_rather_than_believed():
    """Mode B was checked here and the recovery solve was not.

    A failed recovery returns a shorter trajectory, so it reports a faster
    recool AND a final temperature that never finished cooling -- both in
    the direction of 'ready'. The ensemble must not abort on one bad sample,
    so the rule lands on the failure counter the module already keeps.
    """
    from qta_multiphysics import uncertainty as U
    with _failing(U, "solve_thermal_1d", at=2, stub=_Failed1D):
        _dists, summary = U.run_monte_carlo(n_samples=2, seed=7,
                                            mesh_check_fraction=0.0)
    assert summary["pde_stability_failure_count"] == 1, summary
    # A sample counted as a failure must not ALSO appear in the Mode-B
    # statistics. It did: the Mode-B values were appended before the Mode-C
    # solve was attempted, so n_evaluated counted a sample the same run had
    # already classified as a PDE failure, and the two could sum past
    # n_samples.
    assert summary["n_evaluated"] == 1, (
        "the refused sample still reached the distributions it feeds")
    assert (summary["n_evaluated"]
            + summary["pde_stability_failure_count"]) == 2


def test_a_failed_MESH_CHECK_solve_is_counted_rather_than_believed():
    """The ensemble's own mesh criterion, which is a third pair of solves.

    It is reached only when the sampled fraction fires, so a test that runs
    with mesh_check_fraction=0.0 -- as the two above must, to keep the solve
    count down -- never touches it. That is exactly how the mutation that
    removes this guard survived a matrix the rest of this file killed.

    Two solves that both stopped at the same early time agree to well within
    the 20% criterion, so an unguarded failure is counted as a PASSING mesh
    check rather than as a failed one.
    """
    from qta_multiphysics import uncertainty as U
    # Per sample: rB, rC, then the n=100 and n=200 mesh pair. The third call
    # is the first half of that pair.
    with _failing(U, "solve_thermal_1d", at=3, stub=_Failed1D) as calls:
        _dists, summary = U.run_monte_carlo(n_samples=1, seed=7,
                                            mesh_check_fraction=1.0)
    assert summary["mesh_convergence_checked"] == 1, summary
    assert summary["mesh_convergence_failure_count"] == 1, summary
    # AND THE SECOND MESH SOLVE NEVER HAPPENED.
    #
    # The count alone does not distinguish the guard from the tolerance: with
    # the guard removed, the stub's temperature happens to sit further than
    # 20% from the real one, so the comparison ALSO increments mesh_fail and
    # the run looks identical from the summary. It is not identical -- the
    # guard refuses before the pair is completed, the tolerance refuses after
    # -- and the call count is where the difference is visible. A mutation
    # removing this guard survived a version of this test that only counted.
    assert calls["n"] == 3, (
        f"the ensemble spent {calls['n']} solve(s); a refused mesh check "
        "must not pay for the second half of a comparison it cannot make")
    assert summary["pde_stability_failure_count"] == 0, (
        "the mesh check must not disqualify the sample itself")
    assert summary["n_evaluated"] == 1, summary


def test_an_honest_mesh_check_counts_no_failures():
    """Anti-vacuity for the test above, and proof the branch is reached."""
    from qta_multiphysics import uncertainty as U
    _dists, summary = U.run_monte_carlo(n_samples=1, seed=7,
                                        mesh_check_fraction=1.0)
    assert summary["mesh_convergence_checked"] == 1, (
        "the mesh-check branch never fired, so the injection above lands "
        "on a solve that is not the one it names")
    assert summary["mesh_convergence_failure_count"] == 0, summary


def test_an_honest_ensemble_counts_no_failures():
    """Anti-vacuity: the counter above must mean the injection, not the
    ordinary behaviour of a two-sample run."""
    from qta_multiphysics import uncertainty as U
    _dists, summary = U.run_monte_carlo(n_samples=2, seed=7,
                                        mesh_check_fraction=0.0)
    assert summary["pde_stability_failure_count"] == 0
    assert summary["n_evaluated"] == 2, (
        "no sample was evaluated, so the injected test above proves nothing")
