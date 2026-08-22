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
