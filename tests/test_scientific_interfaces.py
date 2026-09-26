"""The Phase-2 interfaces refuse what they exist to refuse.

Each rule in ``scientific/`` is exercised on an input that breaks it and on a
control that must pass, so a rule that could not fail shows up as a test
that cannot fail. The thermal-1D adapter, the governed path and the import
isolation are tested in their own modules.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scientific.identity import IdentityError, digest  # noqa: E402
from scientific.model import (  # noqa: E402
    Applicability, InvariantSpec, ModelBase, ModelError, Parameter,
    ParameterSchema, run_model,
)
from scientific.observation import (  # noqa: E402
    Observation, ObservationError, ObservationKind as K,
)
from scientific.quantity import (  # noqa: E402
    Quantity, QuantityError, ResolutionClass as RC, UncertaintyClass as UC,
    ZeroState, indistinguishable,
)
from scientific.registry import ModelRegistry, RegistryError  # noqa: E402
from scientific.result import (  # noqa: E402
    ArtifactRef, BundleError, InvariantResult, Output, OutputStatus,
    ResultBundle,
)
from scientific.run_identity import (  # noqa: E402
    RunIdentity, environment_digest, may_reuse,
)
from scientific.verification import (  # noqa: E402
    CheckType, Establishes, Independence, Status, VerificationError,
    VerificationResult,
)

Z = "0" * 64
A = "a" * 64
B = "b" * 64


# --- Quantity: the generic form of D-2026-69 ----------------------------------

def _ledger(value):
    """The quantity D-2026-69 is about, as the mechanism represents it."""
    return Quantity(value, "J", resolution=8.1e-27,
                    resolution_class=RC.ACCUMULATED_BOUND,
                    resolution_basis="n*eps*sum|t_i|, forward error bound of "
                                     "the running sum at row 2 (D-2026-69)",
                    reporting_digits=10, uncertainty_class=UC.NUMERICAL)


def test_the_d_2026_69_crossing_is_one_result_not_two():
    here, there = _ledger(1.615587134e-27), _ledger(0.0)
    assert here.zero_state() is there.zero_state() is \
        ZeroState.BELOW_RESOLUTION
    assert indistinguishable(here, there)
    assert "below resolution" in here.formatted()
    assert "1.615587134" not in here.formatted()


def test_a_computed_zero_is_not_an_exact_zero():
    assert _ledger(0.0).zero_state() is not ZeroState.EXACT_ZERO
    exact = Quantity(0.0, "W/m^2", exact=True)
    assert exact.zero_state() is ZeroState.EXACT_ZERO


def test_a_resolved_value_prints_its_digits():
    q = _ledger(3.0e-25)
    assert q.zero_state() is ZeroState.RESOLVED
    assert q.formatted().startswith("3.000000000e-25")


def test_no_stated_resolution_cannot_claim_a_zero_state():
    q = Quantity(0.0, "J")
    assert q.zero_state() is ZeroState.RESOLUTION_NOT_STATED
    with pytest.raises(QuantityError, match="no stated resolution"):
        indistinguishable(q, _ledger(0.0))


@pytest.mark.parametrize("kw,match", [
    ({"value": float("nan")}, "finite"),
    ({"value": True}, "number"),
    ({"unit": ""}, "unit"),
    ({"resolution": 1e-3}, "without its class"),
    ({"resolution": 1e-3, "resolution_class": RC.SOLVER_TOLERANCE},
     "where it came from"),
    ({"resolution_class": RC.SOLVER_TOLERANCE}, "no resolution"),
    ({"resolution": -1.0, "resolution_class": RC.SOLVER_TOLERANCE,
      "resolution_basis": "x"}, ">= 0"),
    ({"reporting_digits": 0}, "1..17"),
    ({"exact": True, "resolution": 1.0,
      "resolution_class": RC.SOLVER_TOLERANCE, "resolution_basis": "x"},
     "no numerical floor"),
])
def test_a_malformed_quantity_is_refused(kw, match):
    base = {"value": 1.0, "unit": "K"}
    with pytest.raises(QuantityError, match=match):
        Quantity(**{**base, **kw})


def test_a_quantity_round_trips_and_its_zero_state_is_checked():
    q = _ledger(1e-27)
    assert Quantity.from_record(q.to_record()) == q
    rec = {**q.to_record(), "zero_state": "RESOLVED"}
    with pytest.raises(QuantityError, match="the values say"):
        Quantity.from_record(rec)


def test_units_must_agree_to_compare():
    with pytest.raises(QuantityError, match="units differ"):
        indistinguishable(_ledger(0.0), replace(_ledger(0.0), unit="eV"))


# --- Observation kinds ----------------------------------------------------------

def _q():
    return Quantity(1.0, "K")


def test_a_simulation_result_never_becomes_a_measurement():
    for kind in (K.RAW_OBSERVATION, K.PROCESSED_OBSERVATION):
        with pytest.raises(ObservationError):
            Observation("o2", kind, _q(), "model thermal_1d@1",
                        transformation="relabel",
                        derived_from=(("o1", K.SIMULATION_RESULT),))


def test_a_raw_observation_is_not_derived():
    with pytest.raises(ObservationError, match="not derived"):
        Observation("o", K.RAW_OBSERVATION, _q(), "thermometer",
                    transformation="x", derived_from=(("i", K.RAW_OBSERVATION),))
    Observation("o", K.RAW_OBSERVATION, _q(), "thermometer")   # control


def test_processed_needs_only_measured_inputs():
    Observation("p", K.PROCESSED_OBSERVATION, _q(), "pipeline",
                transformation="dark subtraction",
                derived_from=(("r", K.RAW_OBSERVATION),))           # control
    with pytest.raises(ObservationError):
        Observation("p", K.PROCESSED_OBSERVATION, _q(), "pipeline",
                    transformation="dark subtraction",
                    derived_from=(("r", K.RAW_OBSERVATION),
                                  ("s", K.SYNTHETIC_OBSERVATION)))


def test_a_calibration_must_cite_a_measurement():
    with pytest.raises(ObservationError, match="cite a measured input"):
        Observation("c", K.CALIBRATED_PARAMETER, _q(), "fit",
                    transformation="least squares", calibration="LM fit",
                    derived_from=(("s", K.SIMULATION_RESULT),))
    with pytest.raises(ObservationError, match="calibration provenance"):
        Observation("c", K.CALIBRATED_PARAMETER, _q(), "fit",
                    transformation="least squares",
                    derived_from=(("r", K.RAW_OBSERVATION),))
    obs = Observation("c", K.CALIBRATED_PARAMETER, _q(), "fit",
                      transformation="least squares", calibration="LM fit",
                      derived_from=(("r", K.RAW_OBSERVATION),
                                    ("s", K.SIMULATION_RESULT)))
    assert not obs.measured


def test_a_simulated_measurement_is_synthetic():
    o = Observation("y", K.SYNTHETIC_OBSERVATION, _q(), "forward model",
                    transformation="sensor model",
                    derived_from=(("s", K.SIMULATION_RESULT),))
    assert not o.measured


def test_derived_kinds_need_lineage_and_a_transformation():
    with pytest.raises(ObservationError, match="derived from"):
        Observation("d", K.DERIVED_STATISTIC, _q(), "mean")
    with pytest.raises(ObservationError, match="transformation"):
        Observation("d", K.DERIVED_STATISTIC, _q(), "mean",
                    derived_from=(("r", K.RAW_OBSERVATION),))


# --- ResultBundle -------------------------------------------------------------

def _inv(holds=True, iid="energy_balance"):
    return InvariantResult(iid, holds, Quantity(1e-4, "1"), "|r| <= 1e-3",
                           Quantity(1e-3, "1"), "solver rtol x 1000")


def _bundle(**kw):
    params = kw.pop("parameters", {"n": 10})
    base = dict(model_id="m", model_version="1", implementation_digest=A,
                parameter_digest=digest(params), environment_digest=B,
                parameters=params,
                outputs=(Output("T", OutputStatus.OK, Quantity(4.0, "K")),),
                invariants=(_inv(),), convergence={"status": "ok"})
    base.update(kw)
    return ResultBundle(**base)


def test_a_bundle_round_trips_by_digest():
    b = _bundle(artifacts=(ArtifactRef("field", A, "application/x-npz", 10),))
    again = ResultBundle.from_record(b.to_record())
    assert again == b and again.digest() == b.digest()


def test_a_bundle_refuses_an_inline_array():
    with pytest.raises(BundleError, match="arrays go in an artefact"):
        _bundle(convergence={"T": [1.0] * 200})
    _bundle(convergence={"series": [1.0, 2.0, 3.0]})           # control


def test_a_bundle_is_always_a_simulation_result():
    with pytest.raises(BundleError, match="SIMULATION_RESULT"):
        _bundle(observation_kind=K.RAW_OBSERVATION)


def test_a_bundle_carries_the_parameters_its_digest_names():
    with pytest.raises(BundleError, match="parameter_digest"):
        _bundle(parameter_digest=digest({"n": 11}))


def test_a_failed_output_says_why_and_carries_no_value():
    _bundle(outputs=(Output("T", OutputStatus.FAILED, reason="diverged"),))
    with pytest.raises(BundleError, match="carries no value"):
        Output("T", OutputStatus.FAILED, Quantity(1.0, "K"), reason="x")
    with pytest.raises(BundleError, match="say why"):
        Output("T", OutputStatus.UNDEFINED)
    with pytest.raises(BundleError, match="needs a Quantity"):
        Output("T", OutputStatus.OK)


@pytest.mark.parametrize("kw,match", [
    ({"implementation_digest": "x"}, "not a sha256"),
    ({"outputs": ()}, "reports nothing"),
    ({"outputs": (Output("T", OutputStatus.OK, Quantity(1.0, "K")),) * 2},
     "repeat"),
    ({"invariants": (_inv(), _inv())}, "repeat"),
    ({"convergence": {"x": float("inf")}}, "JSON"),
])
def test_a_malformed_bundle_is_refused(kw, match):
    with pytest.raises(BundleError, match=match):
        _bundle(**kw)


def test_a_bundle_record_of_another_schema_is_refused():
    rec = {**_bundle().to_record(), "schema_version": 2}
    with pytest.raises(BundleError, match="schema_version"):
        ResultBundle.from_record(rec)
    rec = {**_bundle().to_record(), "verified": True}
    with pytest.raises(BundleError, match="extra"):
        ResultBundle.from_record(rec)


def test_all_invariants_hold_needs_at_least_one():
    assert _bundle().all_invariants_hold
    assert not _bundle(invariants=(_inv(False),)).all_invariants_hold
    assert not _bundle(invariants=()).all_invariants_hold


# --- VerificationResult -------------------------------------------------------

def _vr(**kw):
    base = dict(check_id="reduction", check_type=CheckType.
                INDEPENDENT_IMPLEMENTATION, subject_digest=A,
                subject_model="m@1", status=Status.PASS,
                measured=Quantity(0.01, "1"), criterion="rel <= 0.15",
                threshold=Quantity(0.15, "1"),
                criterion_derivation="verification.py reduction criterion",
                verifier_id="checker", verifier_implementation_digest=B,
                producer_implementation_digest=A,
                independence=Independence.DIFFERENT_DISCRETIZATION,
                establishes=Establishes.INDEPENDENT_NUMERICAL_AGREEMENT,
                shared_components=("material models",),
                limitations=("shares k(T), Cp(T)",))
    base.update(kw)
    return VerificationResult(**base)


def test_independence_with_the_producers_digest_is_refused():
    with pytest.raises(VerificationError, match="producer's own code"):
        _vr(verifier_implementation_digest=A)


def test_an_independent_check_must_say_how_it_is_independent():
    with pytest.raises(VerificationError, match="what kind"):
        _vr(independence=Independence.NONE)


def test_shared_components_must_be_stated_as_limitations():
    with pytest.raises(VerificationError, match="states no limitation"):
        _vr(limitations=())


def test_converged_is_not_experimentally_validated():
    with pytest.raises(VerificationError, match="not experimentally"):
        _vr(establishes=Establishes.EXPERIMENTAL_VALIDATION)
    with pytest.raises(VerificationError, match="only a comparison"):
        _vr(establishes=Establishes.EXPERIMENTAL_VALIDATION,
            observations=(("r", K.RAW_OBSERVATION),))
    with pytest.raises(VerificationError, match="not experimentally"):
        _vr(check_type=CheckType.COMPARISON_WITH_MEASUREMENT,
            establishes=Establishes.EXPERIMENTAL_VALIDATION,
            observations=(("s", K.SIMULATION_RESULT),))
    _vr(check_type=CheckType.COMPARISON_WITH_MEASUREMENT,
        establishes=Establishes.EXPERIMENTAL_VALIDATION,
        observations=(("r", K.RAW_OBSERVATION),))                   # control


def test_a_verdict_needs_a_measurement():
    with pytest.raises(VerificationError, match="nothing measured"):
        _vr(measured=None)
    _vr(measured=None, status=Status.NOT_RUN, threshold=None)       # control


def test_independent_agreement_needs_independence():
    with pytest.raises(VerificationError, match="not independent"):
        _vr(check_type=CheckType.CONVERGENCE,
            independence=Independence.NONE)


def test_a_verification_result_round_trips():
    v = _vr()
    assert VerificationResult.from_record(v.to_record()) == v


# --- the model contract ---------------------------------------------------------

class _Toy(ModelBase):
    model_id = "toy"
    model_version = "1"
    implementation_modules = ("scientific.quantity",)
    parameter_schema = ParameterSchema((
        Parameter("x", "float", unit="m", minimum=0.0, default=1.0),
        Parameter("mode", "str", choices=("a", "b"), default="a"),
    ))
    invariants = (InvariantSpec("energy_balance", "d", "|r| <= 1e-3"),)
    applicability = Applicability("a toy")

    def __init__(self, bundle_hook=None):
        self.hook = bundle_hook

    def run(self, inputs):
        b = _bundle(parameters=inputs,
                    implementation_digest=self.implementation_digest(),
                    model_id=self.model_id, model_version=self.model_version)
        return self.hook(b) if self.hook else b


def test_run_model_returns_a_bundle_true_to_the_model():
    b = run_model(_Toy(), {"x": 2})
    assert b.parameters == {"x": 2.0, "mode": "a"}


@pytest.mark.parametrize("inputs,match", [
    ({"x": -1.0}, "< 0.0"), ({"y": 1.0}, "unknown"), ({"x": "1"}, "number"),
    ({"mode": "c"}, "not in"), ({"x": float("nan")}, "finite"),
])
def test_inputs_are_typed_and_bounded(inputs, match):
    with pytest.raises(ModelError, match=match):
        run_model(_Toy(), inputs)


@pytest.mark.parametrize("hook,match", [
    (lambda b: replace(b, invariants=()), "not computed"),
    (lambda b: replace(b, invariants=(_inv(), _inv(iid="extra"))),
     "undeclared"),
    (lambda b: replace(b, implementation_digest=Z), "implementation digest"),
    (lambda b: replace(b, model_version="2"), "not the model that ran"),
    (lambda b: replace(b, parameters={"x": 3.0, "mode": "a"},
                       parameter_digest=digest({"x": 3.0, "mode": "a"})),
     "not the validated inputs"),
])
def test_a_bundle_untrue_to_its_model_is_refused(hook, match):
    with pytest.raises(ModelError, match=match):
        run_model(_Toy(hook), {"x": 2})


# --- the registry -----------------------------------------------------------------

def test_the_registry_refuses_other_code_under_the_same_name():
    reg = ModelRegistry()
    d = reg.register(_Toy())
    assert reg.register(_Toy()) == d                       # idempotent
    other = _Toy()
    other.implementation_modules = ("scientific.observation",)
    with pytest.raises(RegistryError, match="bump the version"):
        reg.register(other)


def test_the_registry_is_default_deny():
    with pytest.raises(RegistryError, match="not registered"):
        ModelRegistry().lookup("toy", "1")


def test_a_model_without_an_identity_is_not_registered():
    m = _Toy()
    m.implementation_modules = ()
    with pytest.raises(RegistryError, match="no implementation identity"):
        ModelRegistry().register(m)


def test_a_model_whose_code_changed_is_not_looked_up():
    reg = ModelRegistry()
    m = _Toy()
    reg.register(m)
    m.implementation_modules = ("scientific.observation",)
    with pytest.raises(RegistryError, match="changed since"):
        reg.lookup("toy", "1")


# --- run identity -------------------------------------------------------------------

def _rid(**kw):
    base = dict(model_id="m", model_version="1", implementation_digest=A,
                parameter_digest=B, environment_digest=environment_digest())
    base.update(kw)
    return RunIdentity(**base)


def test_an_identical_run_with_intact_evidence_is_reused():
    assert may_reuse(_rid(), _rid(), prior_evidence_intact=True) == (True, "")


@pytest.mark.parametrize("kw,field", [
    ({"implementation_digest": Z}, "implementation_digest"),
    ({"parameter_digest": Z}, "parameter_digest"),
    ({"environment_digest": Z}, "environment_digest"),
    ({"seeds": (7,)}, "seeds"),
    ({"workflow_revision": "abc"}, "workflow_revision"),
    ({"upstream_evidence": (A,)}, "upstream_evidence"),
    ({"model_version": "2"}, "model_version"),
])
def test_any_identity_difference_refuses_reuse_and_names_it(kw, field):
    ok, why = may_reuse(_rid(), _rid(**kw), prior_evidence_intact=True)
    assert not ok and field in why


def test_reuse_needs_the_evidence_not_the_name():
    ok, why = may_reuse(_rid(), _rid(), prior_evidence_intact=False)
    assert not ok and "evidence" in why


def test_upstream_evidence_order_does_not_change_the_identity():
    assert _rid(upstream_evidence=(A, B)).digest() == \
        _rid(upstream_evidence=(B, A)).digest()


def test_a_run_identity_refuses_a_non_digest():
    with pytest.raises(IdentityError):
        _rid(parameter_digest="not-a-digest")


class _WithField(_Toy):
    """A toy that hands over artefact bytes, optionally the wrong ones."""

    def __init__(self, payload=b"field-bytes", ref_of=b"field-bytes"):
        super().__init__()
        self.payload, self.ref_of = payload, ref_of

    def run_with_artifacts(self, inputs):
        import hashlib
        ref = ArtifactRef("field", hashlib.sha256(self.ref_of).hexdigest(),
                          "application/octet-stream", len(self.ref_of))
        b = replace(self.run(inputs), artifacts=(ref,))
        return b, ({} if self.payload is None else {"field": self.payload})


def test_artefact_bytes_must_be_the_ones_referenced():
    from scientific.model import run_model_with_artifacts
    b, p = run_model_with_artifacts(_WithField(), {})              # control
    assert p == {"field": b"field-bytes"}
    with pytest.raises(ModelError, match="not the ones"):
        run_model_with_artifacts(_WithField(payload=b"other-bytes!"), {})
    with pytest.raises(ModelError, match="bytes given for"):
        run_model_with_artifacts(_WithField(payload=None), {})
