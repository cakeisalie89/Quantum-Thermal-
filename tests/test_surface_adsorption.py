"""Langmuir capture: the surface-adsorption model, its check, its laws.

Three things are held here:

* THE LAWS. The impingement flux is the Hertz-Knudsen law, written here a
  second way (pressure over the root of 2 pi m k T) and compared to the
  model's (n vbar / 4) within a few ulp; the capture step is the closed-form
  solution of dN/dt = s F (1 - N / N_cap), restated here and compared bit
  for bit over a grid that includes every guarded edge.
* THE MODEL. Every declared invariant is computed and holds, and each is
  broken on purpose and seen to fail.
* THE CHECK. It is independent in the way it claims, passes on agreement,
  fails on a disagreement of the kind it exists for, and does not compare
  what it cannot.
"""
from __future__ import annotations

import ast
import dataclasses
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import repo_content_audit as RCA  # noqa: E402

from scientific.catalog import check, models  # noqa: E402
from scientific.checks import langmuir_rk4 as RK  # noqa: E402
from scientific.identity import source_closure  # noqa: E402
from scientific.model import ModelError, run_model  # noqa: E402
from scientific.models import surface_adsorption as SA  # noqa: E402
from scientific.result import OutputStatus  # noqa: E402
from scientific.verification import (  # noqa: E402
    Establishes, Independence, Status,
)

BASE = {"pressure_Pa": 1e-4, "T_gas_K": 300.0, "mass_amu": 17.035,
        "sticking": 0.55, "capacity_per_m2": 1e19, "window_s": 1.0,
        "n_windows": 3}
CASES = [
    BASE,
    {**BASE, "pressure_Pa": 1e-12, "mass_amu": 2.016, "sticking": 0.01,
     "n_windows": 10},
    {**BASE, "sticking": 0.0},
    {**BASE, "pressure_Pa": 0.0},
    {**BASE, "window_s": 0.0},
    {**BASE, "pressure_Pa": 1e-9, "T_gas_K": 77.0, "mass_amu": 28.0,
     "sticking": 0.9, "capacity_per_m2": 1e18, "initial_coverage": 0.99,
     "window_s": 1e-3, "n_windows": 7},
    {**BASE, "pressure_Pa": 1.0, "sticking": 1.0, "window_s": 1e6,
     "n_windows": 10000},
]


# -- the laws ----------------------------------------------------------------

def _capture_reference(N, flux, s, Nc, dt):
    """The closed-form capture over one window of constant flux, restated:
    N(dt) = Nc - (Nc - N) exp(-s F dt / Nc), refused for a negative window,
    the identity for no sticking, no flux or no time, and never above
    capacity."""
    if dt < 0:
        raise ValueError("dt_s must be >= 0")
    if s <= 0.0 or flux <= 0.0 or dt == 0.0:
        return N
    N = Nc - (Nc - N) * math.exp(-s * flux * dt / Nc)
    return min(N, Nc)


GRID_P = (0.0, 1e-12, 3.7e-9, 1e-6, 1e-4, 2.5e-1, 1e3)
GRID_T = (4.2, 77.0, 300.0, 1234.5)
GRID_M = (2.016, 3.016, 17.035, 44.0)


def test_the_flux_is_the_hertz_knudsen_law():
    """p / sqrt(2 pi m k T), a different arrangement of the same law, agrees
    with n vbar / 4 to a few ulp at every grid point -- and is exactly zero
    where the pressure is."""
    n = 0
    for p in GRID_P:
        for T in GRID_T:
            for m in GRID_M:
                got = SA.impingement_flux(SA.number_density(p, T), T, m)
                want = p / math.sqrt(2.0 * math.pi * m * SA.AMU * SA.K_B * T)
                assert got == pytest.approx(want, rel=8 * 2.0 ** -52,
                                            abs=0.0), (p, T, m)
                n += 1
    assert n == len(GRID_P) * len(GRID_T) * len(GRID_M)


def test_the_flux_check_would_see_a_wrong_mass_unit():
    """CONTROL: the comparison above is tight enough to see the mass in
    grams rather than kilograms."""
    p, T, m = 1e-4, 300.0, 17.035
    got = SA.impingement_flux(SA.number_density(p, T), T, m * 1e3)
    want = p / math.sqrt(2.0 * math.pi * m * SA.AMU * SA.K_B * T)
    assert got != pytest.approx(want, rel=1e-3)


@pytest.mark.parametrize("N0", [0.0, 1e10, 5e18, 1e19])
@pytest.mark.parametrize("dt", [0.0, 1e-3, 1.0, 1e4, 1e9])
@pytest.mark.parametrize("s", [0.0, 0.01, 0.55, 1.0])
def test_the_capture_step_is_the_closed_form_bit_for_bit(N0, dt, s):
    for flux in (0.0, 1.3e5, 2.9e17, 4.4e22):
        assert SA.langmuir_capture(N0, flux, s, 1e19, dt) == \
            _capture_reference(N0, flux, s, 1e19, dt)


def test_capture_never_leaves_a_surface_above_capacity():
    """An over-full inventory handed in is brought to capacity, not past
    it."""
    for N in (1e19 * (1 + 1e-12), 1.5e19):
        got = SA.langmuir_capture(N, 1e17, 0.5, 1e19, 1.0)
        assert got == 1e19 == _capture_reference(N, 1e17, 0.5, 1e19, 1.0)


def test_a_negative_window_is_refused():
    with pytest.raises(ValueError, match="dt_s must be >= 0"):
        SA.langmuir_capture(0.0, 1e17, 0.5, 1e19, -1.0)


def test_the_generic_model_carries_no_retired_vocabulary_in_its_code():
    """Every identifier and every string constant of the model module --
    its docstring aside -- read with the repository content audit's own
    patterns; and it imports no retired module."""
    src = (ROOT / "scientific/models/surface_adsorption.py").read_text(
        encoding="utf-8")
    code = ast.parse(src)
    words = [n.id for n in ast.walk(code) if isinstance(n, ast.Name)] + [
        n.value for n in ast.walk(code)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n is not code.body[0].value]
    text = "\n".join(words)
    assert [m.group(0) for _, rx in RCA._TERMS
            for m in rx.finditer(text)] == []
    retired = RCA.retired_from_inventory(ROOT)[1]
    imported = {n.module or "" for n in ast.walk(code)
                if isinstance(n, ast.ImportFrom) and n.level == 0}
    assert not imported & retired


# -- the model ---------------------------------------------------------------

@pytest.fixture(scope="module")
def model():
    return SA.SurfaceAdsorptionModel()


@pytest.mark.parametrize("inputs", CASES)
def test_every_declared_invariant_is_computed_and_holds(model, inputs):
    b = run_model(model, inputs)
    assert {i.invariant_id for i in b.invariants} == \
        {i.invariant_id for i in model.invariants}
    assert all(i.holds for i in b.invariants), \
        [(i.invariant_id, i.measured) for i in b.invariants if not i.holds]
    assert all(o.status is OutputStatus.OK for o in b.outputs)


def test_the_outputs_are_the_functions_values(model):
    b = run_model(model, BASE)
    F = SA.impingement_flux(SA.number_density(1e-4, 300.0), 300.0, 17.035)
    N = 0.0
    for _ in range(3):
        N = SA.langmuir_capture(N, F, 0.55, 1e19, 1.0)
    assert b.output("impingement_flux").quantity.value == F
    assert b.output("final_inventory").quantity.value == N
    assert b.output("final_coverage").quantity.value == N / 1e19
    assert b.output("admitted_fluence").quantity.value == F + F + F
    assert b.output("impingement_flux").quantity.unit == "m^-2 s^-1"


def test_a_saturated_surface_reaches_capacity_and_no_further(model):
    b = run_model(model, CASES[-1])
    assert b.output("final_coverage").quantity.value == 1.0


def test_the_catalog_admits_the_model_and_its_check():
    m = models().lookup(SA.MODEL_ID, SA.MODEL_VERSION)
    run, dig = check("surface_adsorption.rk4_pressure_form")
    assert run is RK.run_check and dig() == RK.check_digest()
    assert [c.check_id for c in m.independent_checks] == [RK.CHECK_ID]


def test_the_digest_covers_the_model_and_not_the_check(model):
    closure = source_closure(model.implementation_modules)
    assert "scientific.models.surface_adsorption" in closure
    assert not any(".checks" in m for m in closure), sorted(closure)
    assert all(m.startswith("scientific.") for m in closure), sorted(closure)
    assert model.implementation_digest() != RK.check_digest()


@pytest.mark.parametrize("bad", [
    {"pressure_Pa": -1.0}, {"sticking": 1.5}, {"T_gas_K": float("nan")},
    {"n_windows": 0}, {"n_windows": 2.0}, {"initial_coverage": 1.01},
    {"presure_Pa": 1e-4},
])
def test_the_inputs_are_typed_and_bounded(model, bad):
    with pytest.raises(ModelError):
        run_model(model, {**BASE, **bad})


def test_a_missing_physical_input_is_refused_not_defaulted(model):
    for name in ("pressure_Pa", "T_gas_K", "mass_amu", "sticking",
                 "capacity_per_m2", "window_s"):
        with pytest.raises(ModelError, match=f"missing parameter {name}"):
            run_model(model, {k: v for k, v in BASE.items() if k != name})


def _broken(monkeypatch, model, step, inputs=BASE):
    monkeypatch.setattr(SA, "langmuir_capture", step)
    return {i.invariant_id: i.holds for i in run_model(model,
                                                       inputs).invariants}


def test_an_overshoot_past_capacity_is_caught(monkeypatch, model):
    held = _broken(monkeypatch, model,
                   lambda N, F, s, c, dt: c * (1.0 + 1e-9))
    assert held["bounded"] is False


def test_a_window_that_loses_inventory_is_caught(monkeypatch, model):
    real = SA.langmuir_capture
    calls = []

    def step(N, F, s, c, dt):
        calls.append(1)
        return real(N, F, s, c, dt) * (0.5 if len(calls) == 2 else 1.0)
    held = _broken(monkeypatch, model, step)
    assert held["capture_only"] is False


def test_a_capture_beyond_the_incident_fluence_is_caught(monkeypatch, model):
    real = SA.langmuir_capture
    held = _broken(monkeypatch, model,
                   lambda N, F, s, c, dt: real(N, 10 * F, s, c, dt))
    assert held["within_incidence"] is False


def test_a_step_that_does_not_compose_is_caught(monkeypatch, model):
    """A step that is not the solution of this law, yet bounded, monotone
    and (with dt < 1, dt^1.5 < dt) within the incident fluence: only the
    comparison with one long window can see it."""
    def step(N, F, s, c, dt):
        return c - (c - N) * math.exp(-s * F * dt ** 1.5 / c)
    held = _broken(monkeypatch, model, step,
                   {**BASE, "window_s": 0.004, "n_windows": 4})
    assert held["composition"] is False


def test_a_non_finite_value_fails_every_output(monkeypatch, model):
    monkeypatch.setattr(SA, "langmuir_capture",
                        lambda N, F, s, c, dt: float("nan"))
    b = run_model(model, BASE)
    held = {i.invariant_id: i.holds for i in b.invariants}
    assert held["finite"] is False
    assert all(o.status is OutputStatus.FAILED for o in b.outputs)


# -- the check ---------------------------------------------------------------

@pytest.mark.parametrize("inputs", CASES)
def test_the_check_passes_on_agreement_and_says_what_it_is(model, inputs):
    b = run_model(model, inputs)
    v = RK.run_check(b, verifier_id="test-verifier")
    assert v.status is Status.PASS, v.measured
    assert v.independence is Independence.DIFFERENT_DISCRETIZATION
    assert v.establishes is Establishes.INDEPENDENT_NUMERICAL_AGREEMENT
    assert v.subject_digest == b.digest()
    assert v.verifier_implementation_digest != b.implementation_digest
    assert any("law" in s for s in v.shared_components)


def test_the_check_shares_no_code_with_the_producer():
    closure = source_closure(RK.IMPLEMENTATION_MODULES)
    assert "scientific.checks.langmuir_rk4" in closure
    assert not any(m.startswith("scientific.models") for m in closure), \
        sorted(closure)


def test_the_check_is_for_the_model_as_it_is_now():
    """The subject is stated in the check, not imported; a new model version
    must be reviewed against the check before the check will speak to it."""
    assert (RK.MODEL_ID, RK.MODEL_VERSION) == (SA.MODEL_ID, SA.MODEL_VERSION)


def test_the_pressure_form_is_the_same_flux():
    for p in GRID_P:
        for T in GRID_T:
            for m in GRID_M:
                a = RK.pressure_form_flux(p, T, m)
                b = SA.impingement_flux(SA.number_density(p, T), T, m)
                assert a == b or abs(a - b) <= 1e-14 * max(a, b)


@pytest.mark.parametrize("wrong,inputs", [
    ("flux", {**BASE, "pressure_Pa": 1e-9}),
    ("capture", {**BASE, "pressure_Pa": 1e-9}),
    # saturated: the inventory is N_cap whatever the flux, so only the
    # flux comparison can see the flux error
    ("flux", CASES[-1]),
])
def test_a_producer_error_fails_the_check(monkeypatch, model, wrong,
                                          inputs):
    """A flux 1e-5 off, or a capture rate 1e-3 off -- each a disagreement
    the check exists to see."""
    if wrong == "flux":
        real = SA.impingement_flux
        monkeypatch.setattr(SA, "impingement_flux",
                            lambda n, T, m: real(n, T, m) * (1 + 1e-5))
    else:
        monkeypatch.setattr(
            SA, "langmuir_capture",
            lambda N, F, s, c, dt: c - (c - N) * math.exp(-s * F * dt / c
                                                          * 1.001))
    b = run_model(model, inputs)
    v = RK.run_check(b, verifier_id="test-verifier")
    assert v.status is Status.FAIL, v.measured


@pytest.mark.parametrize("window", [1e-3, 1e-6])
def test_a_difference_inside_the_stated_resolution_does_not_fail(model,
                                                                 window):
    """A small capture onto a nearly full surface: the producer's N - N0 is a
    difference of large numbers, and the check compares it at the resolution
    the producer states, not at digits it never claimed."""
    b = run_model(model, {**CASES[5], "window_s": window, "n_windows": 1})
    v = RK.run_check(b, verifier_id="t")
    assert v.status is Status.PASS, v.measured


def test_a_failed_output_is_not_compared(monkeypatch, model):
    monkeypatch.setattr(SA, "langmuir_capture",
                        lambda N, F, s, c, dt: float("inf"))
    b = run_model(model, BASE)
    v = RK.run_check(b, verifier_id="t")
    assert v.status is Status.NOT_RUN and v.measured is None


def test_the_check_refuses_another_model():
    from scientific.models import slab_transient as ST
    b = run_model(SA.SurfaceAdsorptionModel(), BASE)
    b = dataclasses.replace(b, model_id=ST.MODEL_ID)
    with pytest.raises(ValueError, match="this check is for"):
        RK.run_check(b, verifier_id="t")
