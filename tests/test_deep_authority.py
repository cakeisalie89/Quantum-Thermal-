"""§21/§22/§23 regression: the deep layer's trust boundary and its degenerates.

§21 -- forward_matrix() interpolates coherence on an 80-node grid while
clean_forward() evaluates it exactly. The repository asserted "<1e-3 ...
(verified)" in a source comment with no executable check. Measured, inside the
declared tau_c prior, the error reaches 0.78 in a quantity bounded in [0, 1].

§22 -- OOD detection existed but was only used to write a report. An ordinary
posterior_query() consulted neither readiness nor distribution membership, so
an out-of-distribution query was answered by silent extrapolation.

§23 -- degenerate training inputs (empty, single-row, zero-variance,
NaN/inf) produced usable-looking Standardizer and OODModel objects instead of
failing closed.

MODEL-ONLY / FORECAST-ONLY. Software verification; never a physics claim.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qta_multiphysics.deep_expdesign import (                    # noqa: E402
    DENIED_NOT_READY, DENIED_OOD, TRUSTED, TRUST_REQUIREMENTS, fit_ood,
    trust_state)
from qta_multiphysics.deep_expdesign.likelihood_model import (   # noqa: E402
    DegenerateInputError, Standardizer, interpolation_equivalence_report)

READY = {"trained": True, "compared_against_direct_mc": True,
         "thresholds_passed": True, "calibration": {"passed": True}}


def _ood():
    rng = np.random.default_rng(0)
    return fit_ood(rng.normal(size=(200, 3))), rng.normal(size=(1, 3))


# ------------------------------------------------------- §22 trust gating --

def test_every_requirement_is_enforced_individually():
    ood, ctx = _ood()
    assert trust_state(None, READY, ood=ood, context=ctx)["state"] == TRUSTED
    for req, key in (("trained", "trained"),
                     ("validation_ran", "compared_against_direct_mc"),
                     ("thresholds_passed", "thresholds_passed")):
        bad = dict(READY); bad[key] = False
        st = trust_state(None, bad, ood=ood, context=ctx)
        assert not st["trusted"], req
        assert req in st["failed_requirements"], st


def test_calibration_failure_denies_authority():
    ood, ctx = _ood()
    bad = dict(READY, calibration={"passed": False})
    st = trust_state(None, bad, ood=ood, context=ctx)
    assert st["state"] == DENIED_NOT_READY
    assert "calibration_passed" in st["failed_requirements"]


def test_missing_readiness_record_denies_rather_than_defaults():
    ood, ctx = _ood()
    st = trust_state(None, None, ood=ood, context=ctx)
    assert not st["trusted"]
    assert st["state"] == DENIED_NOT_READY


def test_out_of_distribution_query_is_denied():
    ood, _ = _ood()
    far = np.full((1, 3), 50.0)
    st = trust_state(None, READY, ood=ood, context=far)
    assert st["state"] == DENIED_OOD
    assert st["ood"]["is_ood"] is True


def test_omitting_the_ood_model_cannot_buy_trust():
    """The bypass: no detector supplied must not read as in-distribution."""
    st = trust_state(None, READY)
    assert st["state"] == DENIED_OOD, st
    assert st["checks"]["in_distribution"] is False


def test_half_a_check_is_no_check():
    ood, _ = _ood()
    assert not trust_state(None, READY, ood=ood)["trusted"]
    assert not trust_state(None, READY, context=np.zeros((1, 3)))["trusted"]


def test_denial_names_the_failed_requirement():
    st = trust_state(None, {}, ood=None, context=None)
    assert set(st["requirements"]) == set(TRUST_REQUIREMENTS)
    assert st["failed_requirements"], "a denial must say what failed"


def test_raw_posterior_query_declares_it_asserts_no_trust():
    import inspect
    from qta_multiphysics.deep_expdesign import inference
    src = inspect.getsource(inference.posterior_query)
    assert "NOT_EVALUATED" in src or "asserts" in src


# ------------------------------------------ §21 interpolation equivalence --

def test_interpolation_error_is_measured_not_asserted():
    r = interpolation_equivalence_report(sequences=("hahn",),
                                         measurement_times=(1e-3,),
                                         n_probe=101)
    assert "measured_worst_abs_error" in r
    assert r["per_case"], "no measurement was recorded"
    assert r["reference"].endswith("filter_function_coherence") or \
        "filter_function_coherence" in r["reference"]


def test_interpolation_claim_is_classified_by_the_measurement():
    r = interpolation_equivalence_report(sequences=("hahn", "xy8"),
                                         measurement_times=(1e-3,),
                                         n_probe=101)
    assert r["claimed_tolerance"] == 1e-3
    if r["measured_worst_abs_error"] >= r["claimed_tolerance"]:
        assert r["classification"] == "NOT_VALIDATED_AT_CURRENT_GRID_RESOLUTION"
        assert r["meets_claimed_tolerance"] is False
    else:
        assert r["classification"] == "VALIDATED_REDUCED_NUMERICAL_REPRESENTATION"


def test_the_stale_verified_claim_is_withdrawn_not_merely_reworded():
    """The comment may quote the old claim; it must not still assert it."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "qta_multiphysics"
           / "deep_expdesign" / "simulator_adapter.py").read_text(encoding="utf-8")
    assert "values are stable to" not in src, \
        "the original stability assertion is still live"
    assert "That claim was never executable" in src, \
        "the withdrawal must be explicit, so the correction is auditable"
    assert "interpolation_equivalence_report" in src, \
        "the comment must point at the executable measurement that replaced it"


# ------------------------------------------------ §23 degenerate fail-close --

def test_standardizer_refuses_non_finite_input():
    for bad in (np.full((50, 3), np.nan), np.full((50, 3), np.inf)):
        try:
            Standardizer.fit(bad)
        except DegenerateInputError:
            pass
        else:
            raise AssertionError("NaN/inf produced a standardizer")


def test_standardizer_refuses_empty_single_row_and_zero_variance():
    for bad in (np.zeros((0, 3)), np.ones((1, 3)), np.ones((50, 3))):
        try:
            Standardizer.fit(bad)
        except DegenerateInputError:
            pass
        else:
            raise AssertionError(f"degenerate input accepted: shape {bad.shape}")


def test_standardizer_still_fits_legitimate_data():
    A = np.random.default_rng(0).normal(size=(64, 3))
    st = Standardizer.fit(A)
    assert np.all(np.isfinite(st.mean)) and np.all(st.std > 0)
    assert np.allclose(st.transform(A).mean(0), 0.0, atol=1e-12)


def test_ood_fit_refuses_degenerate_context_matrices():
    for bad in (np.zeros((0, 3)), np.zeros((1, 3)), np.ones((50, 3)),
                np.full((50, 3), np.nan), np.full((50, 3), np.inf)):
        try:
            fit_ood(bad)
        except DegenerateInputError:
            pass
        else:
            raise AssertionError(f"degenerate OOD fit accepted: {bad.shape}")


def test_ood_fit_still_works_on_legitimate_contexts():
    ood, ctx = _ood()
    s = ood.score(ctx)
    assert np.all(np.isfinite(s["mahalanobis"]))


def test_a_degenerate_fit_can_never_become_a_trusted_surrogate():
    """The §23 end state: no impossible condition yields VALIDATED_SURROGATE."""
    try:
        bad_ood = fit_ood(np.ones((50, 3)))
    except DegenerateInputError:
        return                      # refused at the fit, which is the point
    raise AssertionError(f"degenerate OOD model was produced: {bad_ood}")


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
