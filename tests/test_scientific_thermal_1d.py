"""Thermal 1D through the ScientificModel interface: the proving case.

The adapter must change nothing about the solver (its numbers equal a direct
call's), its invariants must be computed from the run's own arrays (each one
is broken here on purpose and must say so), its artefact must be the field
it names, and its independent check must be independent in the way it
claims and no other.
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

from qta_multiphysics.thermal_1d import solve_thermal_1d  # noqa: E402
from scientific.catalog import check, models  # noqa: E402
from scientific.checks import reduction_2d  # noqa: E402
from scientific.model import ModelError, run_model_with_artifacts  # noqa: E402
from scientific.models import thermal_1d as T1  # noqa: E402
from scientific.quantity import ZeroState  # noqa: E402
from scientific.result import OutputStatus, ResultBundle  # noqa: E402
from scientific.verification import (  # noqa: E402
    Establishes, Independence, Status, VerificationError,
)

SMALL = {"n_cells": 60, "n_eval": 20}


@pytest.fixture(scope="module")
def run():
    return run_model_with_artifacts(T1.Thermal1DModel(), SMALL)


def test_the_adapter_changes_no_number(run):
    bundle, _ = run
    cfg = T1.configure(bundle.parameters)
    direct = solve_thermal_1d(cfg, source_mode="averaged", n_cells=60,
                              n_eval=20)
    assert bundle.output("nv_layer_peak_T").quantity.value == \
        direct.nv_layer_temperature_K()
    assert bundle.output("hotspot_T").quantity.value == \
        direct.hotspot_temperature_K()


def test_the_default_parameters_are_the_default_configuration():
    from qta_multiphysics.config import default_config
    params = T1.Thermal1DModel().validate({})
    assert dataclasses.asdict(T1.configure(params)) == \
        dataclasses.asdict(default_config())


def test_every_declared_invariant_is_computed_and_holds(run):
    bundle, _ = run
    declared = {i.invariant_id for i in T1.Thermal1DModel.invariants}
    assert {i.invariant_id for i in bundle.invariants} == declared
    assert bundle.all_invariants_hold, [
        (i.invariant_id, i.measured.value) for i in bundle.invariants
        if not i.holds]


def test_the_artefact_is_the_field_it_names(run):
    bundle, payloads = run
    ref = bundle.artifacts[0]
    data = payloads[ref.name]
    assert hashlib.sha256(data).hexdigest() == ref.digest
    assert data.startswith(T1.FIELD_MAGIC)
    header = json.loads(data.split(b"\n", 2)[1])
    n, nt = header["shapes"][2]
    assert (n, nt) == (60, 20)


def test_the_bundle_round_trips_and_carries_no_array(run):
    bundle, _ = run
    rec = bundle.to_record()
    assert ResultBundle.from_record(json.loads(json.dumps(rec))).digest() \
        == bundle.digest()
    assert len(json.dumps(rec)) < 20_000


def test_outputs_carry_units_and_a_resolution(run):
    bundle, _ = run
    q = bundle.output("nv_layer_peak_T").quantity
    assert q.unit == "K" and q.resolution is not None
    assert q.zero_state() is ZeroState.RESOLVED


def test_the_catalog_admits_the_model_and_only_by_its_code():
    reg = models()
    m = reg.lookup(T1.MODEL_ID, T1.MODEL_VERSION)
    assert reg.digest_of(T1.MODEL_ID, T1.MODEL_VERSION) == \
        m.implementation_digest()
    with pytest.raises(KeyError):
        check("no.such.check")


def test_the_digest_covers_the_solver_not_only_the_adapter():
    from scientific.identity import source_closure
    closure = source_closure(T1.Thermal1DModel.implementation_modules)
    assert "qta_multiphysics.thermal_1d" in closure
    assert "qta_multiphysics.material_models" in closure
    assert "qta_multiphysics.metrics" not in closure


# --- each invariant, broken ----------------------------------------------------

def _broken(monkeypatch, **changes):
    """Run the adapter on a solver result with one property corrupted."""
    real = solve_thermal_1d

    def fake(*a, **k):
        r = real(*a, **k)
        for name, fn in changes.items():
            fn(r)
        return r
    monkeypatch.setattr("qta_multiphysics.thermal_1d.solve_thermal_1d", fake)
    bundle, _ = run_model_with_artifacts(T1.Thermal1DModel(), SMALL)
    return {i.invariant_id: i.holds for i in bundle.invariants}, bundle


def test_a_failed_integration_is_reported_not_hidden(monkeypatch):
    holds, bundle = _broken(monkeypatch, status=lambda r: setattr(
        r, "solver_status", "failed"))
    assert holds["solver_converged"] is False
    assert all(o.status is OutputStatus.FAILED for o in bundle.outputs)


def test_a_non_finite_field_is_counted(monkeypatch):
    def poison(r):
        r.T[3, 5] = np.nan
    holds, bundle = _broken(monkeypatch, poison=poison)
    assert holds["finite_field"] is False
    assert bundle.invariant("finite_field").measured.value == 1.0


def test_an_energy_imbalance_is_caught(monkeypatch):
    holds, _ = _broken(monkeypatch, e=lambda r: r.energy.__setitem__(
        "rel_residual", 0.2))
    assert holds["energy_balance"] is False


def test_an_undershoot_below_the_fridge_is_caught(monkeypatch):
    def cool(r):
        r.T[0, -1] = r.cfg.fridge.T_fridge_K - 1e-3
    holds, _ = _broken(monkeypatch, cool=cool)
    assert holds["minimum_principle"] is False


def test_the_inputs_are_typed():
    with pytest.raises(ModelError):
        T1.Thermal1DModel().validate({"n_cells": 5})
    with pytest.raises(ModelError):
        T1.Thermal1DModel().validate({"T_fridge": 0.01})


# --- the independent check -------------------------------------------------------

@pytest.fixture(scope="module")
def verification(run):
    return reduction_2d.run_check(run[0], verifier_id="checker")


def test_the_reduction_check_passes_and_says_what_it_is(verification, run):
    v = verification
    assert v.status is Status.PASS, v.measured
    assert v.independence is Independence.DIFFERENT_DISCRETIZATION
    assert v.establishes is Establishes.INDEPENDENT_NUMERICAL_AGREEMENT
    assert v.subject_digest == run[0].digest()
    assert v.verifier_implementation_digest != \
        run[0].implementation_digest
    assert any("k(T)" in s for s in v.shared_components)


def test_the_check_code_is_not_the_producer_code():
    from scientific.identity import source_closure
    closure = source_closure(reduction_2d.IMPLEMENTATION_MODULES)
    assert "qta_multiphysics.thermal_2d_axisymmetric" in closure
    assert "qta_multiphysics.thermal_1d" not in closure


def test_a_check_that_reports_the_producers_digest_is_refused(run):
    v = reduction_2d.run_check(run[0], verifier_id="checker")
    rec = {**v.to_record(),
           "verifier_implementation_digest": run[0].implementation_digest}
    from scientific.verification import VerificationResult
    with pytest.raises(VerificationError, match="producer's own code"):
        VerificationResult.from_record(rec)


def test_a_disagreement_fails_the_check(run, monkeypatch):
    bundle = run[0]
    moved = dataclasses.replace(bundle.output("nv_layer_peak_T").quantity,
                                value=bundle.output(
                                    "nv_layer_peak_T").quantity.value * 1.5)
    outs = tuple(dataclasses.replace(o, quantity=moved)
                 if o.name == "nv_layer_peak_T" else o
                 for o in bundle.outputs)
    v = reduction_2d.run_check(dataclasses.replace(bundle, outputs=outs),
                               verifier_id="checker")
    assert v.status is Status.FAIL


def test_a_pulse_run_is_not_compared(run):
    params = {**run[0].parameters, "source_mode": "pulse"}
    from scientific.identity import digest
    pulse = dataclasses.replace(run[0], parameters=params,
                                parameter_digest=digest(params))
    v = reduction_2d.run_check(pulse, verifier_id="checker")
    assert v.status is Status.NOT_RUN and not v.passed
