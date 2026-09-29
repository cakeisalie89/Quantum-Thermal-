"""Thermal 2D axisymmetric through the ScientificModel interface (Phase 4).

As for thermal 1D: the adapter changes no number, its invariants are computed
from the run's own arrays (each broken here on purpose), its artefact is the
field it names, and its independent check -- the 3D Cartesian solver with
adiabatic sides -- is independent in the way it claims, speaks only to the
problem it can solve, and reports NOT_RUN for the rest.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_multiphysics.thermal_2d_axisymmetric import solve_thermal_2d  # noqa: E402
from scientific.catalog import check, models  # noqa: E402
from scientific.checks import reduction_3d  # noqa: E402
from scientific.identity import digest, source_closure  # noqa: E402
from scientific.model import ModelError, run_model_with_artifacts  # noqa: E402
from scientific.models import thermal_1d as T1  # noqa: E402
from scientific.models import thermal_2d as T2  # noqa: E402
from scientific.quantity import ZeroState  # noqa: E402
from scientific.result import OutputStatus, ResultBundle  # noqa: E402
from scientific.verification import (  # noqa: E402
    Establishes, Independence, Status,
)

SMALL = {"n_r": 16, "n_z": 24, "n_eval": 10}
ADIABATIC = {**SMALL, "lateral_boundary": "adiabatic"}


@pytest.fixture(scope="module")
def run():
    return run_model_with_artifacts(T2.Thermal2DModel(), ADIABATIC)


@pytest.fixture(scope="module")
def cold():
    return run_model_with_artifacts(T2.Thermal2DModel(), SMALL)


def test_the_adapter_changes_no_number(run):
    bundle, _ = run
    cfg = T2.configure(bundle.parameters)
    direct = solve_thermal_2d(cfg, source_mode="averaged",
                              t_end=cfg.solver.recovery_window_s, n_r=16,
                              n_z=24, n_eval=10, lateral_adiabatic=True)
    assert bundle.output("nv_layer_peak_T").quantity.value == \
        direct.nv_layer_max_K()
    assert bundle.output("nv_layer_mean_T").quantity.value == \
        direct.nv_layer_mean_K()
    assert bundle.output("max_T").quantity.value == direct.max_T_K()


def test_the_window_is_the_solvers_own_default(run):
    bundle, _ = run
    cfg = T2.configure(bundle.parameters)
    assert bundle.convergence["t_end_s"] == cfg.solver.recovery_window_s
    assert T2.window_s(cfg, "pulse") == cfg.solver.pulse_window_s


def test_the_lateral_boundary_is_the_one_declared(run, cold):
    """The two boundaries are different problems: with no lateral sink the
    whole disc warms, and the final field's coldest cell says so."""
    hot = json.loads(run[1][T2.FIELD_ARTIFACT].split(b"\n", 2)[1])
    assert hot["shapes"][3] == [16, 24]
    tf_adiabatic = _fields(run)["T_final_K"].min()
    tf_cold = _fields(cold)["T_final_K"].min()
    assert tf_adiabatic > tf_cold + 1.0, (tf_adiabatic, tf_cold)


def test_the_default_parameters_are_the_default_configuration():
    from qta_multiphysics.config import default_config
    params = T2.Thermal2DModel().validate({})
    assert dataclasses.asdict(T2.configure(params)) == \
        dataclasses.asdict(default_config())
    assert params["lateral_boundary"] == "cold_contact"


def test_the_two_thermal_models_map_parameters_the_same_way():
    """Written out twice so the identities stay independent; held equal
    here so they cannot drift."""
    p = {"T_fridge_K": 0.5, "kapitza_coeff_W_m2_K4": 7.0}
    assert dataclasses.asdict(T2.configure(p)) == \
        dataclasses.asdict(T1.configure(p))


@pytest.mark.parametrize("which", ["run", "cold"])
def test_every_declared_invariant_is_computed_and_holds(which, request):
    bundle, _ = request.getfixturevalue(which)
    declared = {i.invariant_id for i in T2.Thermal2DModel.invariants}
    assert {i.invariant_id for i in bundle.invariants} == declared
    assert bundle.all_invariants_hold, [
        (i.invariant_id, i.measured.value) for i in bundle.invariants
        if not i.holds]


def _fields(run) -> dict:
    data = run[1][T2.FIELD_ARTIFACT]
    assert data.startswith(T2.FIELD_MAGIC)
    _, head, body = data.split(b"\n", 2)
    header = json.loads(head)
    out, off = {}, 0
    for name, shape in zip(header["layout"], header["shapes"]):
        n = int(np.prod(shape)) if shape else 1
        out[name] = np.frombuffer(body[off:off + 8 * n],
                                  dtype="<f8").reshape(shape)
        off += 8 * n
    assert off == len(body)
    return out


def test_the_artefact_is_the_field_it_names(run):
    bundle, payloads = run
    ref = bundle.artifacts[0]
    assert hashlib.sha256(payloads[ref.name]).hexdigest() == ref.digest
    f = _fields(run)
    assert f["T_peak_K"].shape == f["T_final_K"].shape == (16, 24)
    assert float(f["T_peak_K"].max()) == \
        bundle.output("max_T").quantity.value


def test_the_bundle_round_trips_and_carries_no_array(run):
    bundle, _ = run
    rec = bundle.to_record()
    assert ResultBundle.from_record(json.loads(json.dumps(rec))).digest() \
        == bundle.digest()
    assert len(json.dumps(rec)) < 20_000


def test_outputs_carry_units_and_a_resolution(run):
    q = run[0].output("nv_layer_peak_T").quantity
    assert q.unit == "K" and q.resolution is not None
    assert q.zero_state() is ZeroState.RESOLVED


def test_the_catalog_admits_the_model_and_its_check():
    reg = models()
    m = reg.lookup(T2.MODEL_ID, T2.MODEL_VERSION)
    assert reg.digest_of(T2.MODEL_ID, T2.MODEL_VERSION) == \
        m.implementation_digest()
    fn, dg = check(reduction_3d.CHECK_ID)
    assert fn is reduction_3d.run_check and dg() == reduction_3d.check_digest()


def test_the_digest_covers_the_solver_and_not_the_check():
    closure = source_closure(T2.Thermal2DModel.implementation_modules)
    assert "qta_multiphysics.thermal_2d_axisymmetric" in closure
    assert "qta_multiphysics.material_models" in closure
    assert "qta_multiphysics.thermal_3d_transient" not in closure
    assert "qta_multiphysics.metrics" not in closure


def test_the_inputs_are_typed():
    m = T2.Thermal2DModel()
    for bad in ({"n_r": 5}, {"lateral_boundary": "open"},
                {"disable_radial": True}, {"source_mode": "cw"}):
        with pytest.raises(ModelError):
            m.validate(bad)


# --- each invariant, broken ----------------------------------------------------

def _broken(monkeypatch, **changes):
    real = solve_thermal_2d

    def fake(*a, **k):
        r = real(*a, **k)
        for fn in changes.values():
            fn(r)
        return r
    monkeypatch.setattr(
        "qta_multiphysics.thermal_2d_axisymmetric.solve_thermal_2d", fake)
    bundle, _ = run_model_with_artifacts(T2.Thermal2DModel(), SMALL)
    return {i.invariant_id: i.holds for i in bundle.invariants}, bundle


def test_a_failed_integration_is_reported_not_hidden(monkeypatch):
    holds, bundle = _broken(monkeypatch, s=lambda r: setattr(
        r, "solver_status", "failed"))
    assert holds["solver_converged"] is False
    assert all(o.status is OutputStatus.FAILED for o in bundle.outputs)


@pytest.mark.parametrize("field", ["T_final", "T_peak"])
def test_a_non_finite_field_is_counted(monkeypatch, field):
    def poison(r):
        getattr(r, field).values[2, 3] = np.nan
    holds, bundle = _broken(monkeypatch, poison=poison)
    assert holds["finite_field"] is False
    assert bundle.invariant("finite_field").measured.value == 1.0


@pytest.mark.parametrize("residual", [0.2, -0.2])
def test_an_energy_imbalance_is_caught(monkeypatch, residual):
    """Either sign: energy created and energy lost are both imbalances."""
    holds, _ = _broken(monkeypatch, e=lambda r: r.energy.__setitem__(
        "rel_residual", residual))
    assert holds["energy_balance"] is False


def test_an_energy_imbalance_inside_the_criterion_holds(monkeypatch):
    """Control on the bound: 0.09 is inside the borrowed 0.10."""
    holds, _ = _broken(monkeypatch, e=lambda r: r.energy.__setitem__(
        "rel_residual", -0.09))
    assert holds["energy_balance"] is True


@pytest.mark.parametrize("field", ["T_final", "T_peak"])
def test_an_undershoot_below_the_fridge_is_caught(monkeypatch, field):
    def cool(r):
        getattr(r, field).values[0, -1] = r.cfg.fridge.T_fridge_K - 1e-3
    holds, _ = _broken(monkeypatch, cool=cool)
    assert holds["minimum_principle"] is False


# --- the independent check -------------------------------------------------------

@pytest.fixture(scope="module")
def verification(run):
    return reduction_3d.run_check(run[0], verifier_id="checker")


def test_the_3d_check_passes_and_says_what_it_is(verification, run):
    v = verification
    assert v.status is Status.PASS, v.measured
    assert v.measured.value < 0.05
    assert v.independence is Independence.DIFFERENT_DISCRETIZATION
    assert v.establishes is Establishes.INDEPENDENT_NUMERICAL_AGREEMENT
    assert v.subject_digest == run[0].digest()
    assert v.verifier_implementation_digest != run[0].implementation_digest
    assert any("4/pi" in s for s in v.limitations)


def test_the_check_code_is_not_the_producer_code():
    closure = source_closure(reduction_3d.IMPLEMENTATION_MODULES)
    assert "qta_multiphysics.thermal_3d_transient" in closure
    assert "qta_multiphysics.thermal_2d_axisymmetric" not in closure


def _with_nv(bundle, factor):
    q = bundle.output("nv_layer_peak_T").quantity
    moved = dataclasses.replace(q, value=q.value * factor)
    return dataclasses.replace(bundle, outputs=tuple(
        dataclasses.replace(o, quantity=moved)
        if o.name == "nv_layer_peak_T" else o for o in bundle.outputs))


def test_the_check_solves_the_window_the_producer_solved(run, monkeypatch):
    """The comparison is like-for-like in time as well as in boundary: the
    3D solve is asked for the producer's window, derived from the
    configuration rather than read back from the bundle."""
    seen = {}

    def spy(*a, **k):
        seen.update(k)
        return _Solve3D()
    monkeypatch.setattr(
        "qta_multiphysics.thermal_3d_transient.solve_thermal_3d", spy)
    reduction_3d.run_check(run[0], verifier_id="checker")
    assert seen["t_end"] == run[0].convergence["t_end_s"]
    assert seen["transverse"] == "gaussian"
    assert seen["source_mode"] == "averaged"


def test_a_disagreement_fails_the_check(run):
    v = reduction_3d.run_check(_with_nv(run[0], 1.5), verifier_id="checker")
    assert v.status is Status.FAIL


class _Solve3D:
    """A converged 3D result whose beam-axis NV-layer peak is 10 K."""
    solver_status = "ok"

    def nv_layer_temperature_K(self):
        return 10.0


@pytest.mark.parametrize("rel,status", [(0.09, Status.PASS),
                                        (0.11, Status.FAIL)])
def test_the_bound_is_the_borrowed_one(run, monkeypatch, rel, status):
    """Just inside and just outside 0.10, on either side of the 3D value:
    the criterion is the one declared, measured relative to the producer."""
    monkeypatch.setattr(
        "qta_multiphysics.thermal_3d_transient.solve_thermal_3d",
        lambda *a, **k: _Solve3D())
    nv2 = run[0].output("nv_layer_peak_T").quantity.value
    for nv2_new in (10.0 / (1 - rel), 10.0 / (1 + rel)):
        v = reduction_3d.run_check(_with_nv(run[0], nv2_new / nv2),
                                   verifier_id="c")
        assert v.status is status, (nv2_new, v.measured.value)
        assert v.measured.value == pytest.approx(rel)


def test_a_cold_contact_run_is_not_compared(cold):
    v = reduction_3d.run_check(cold[0], verifier_id="checker")
    assert v.status is Status.NOT_RUN and not v.passed
    assert any("cold_contact" in s for s in v.limitations)


def test_a_pulse_run_is_not_compared(run):
    params = {**run[0].parameters, "source_mode": "pulse"}
    pulse = dataclasses.replace(run[0], parameters=params,
                                parameter_digest=digest(params))
    v = reduction_3d.run_check(pulse, verifier_id="checker")
    assert v.status is Status.NOT_RUN


def test_a_failed_output_is_not_compared(run):
    failed = dataclasses.replace(run[0], outputs=tuple(
        dataclasses.replace(o, status=OutputStatus.FAILED, quantity=None,
                            reason="x") for o in run[0].outputs))
    v = reduction_3d.run_check(failed, verifier_id="checker")
    assert v.status is Status.NOT_RUN


def test_the_check_refuses_another_model(run):
    other = dataclasses.replace(run[0], model_id=T1.MODEL_ID)
    with pytest.raises(ValueError, match="this check is for"):
        reduction_3d.run_check(other, verifier_id="checker")
