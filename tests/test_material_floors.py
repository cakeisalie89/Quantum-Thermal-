"""§5/§15 regression: declared floors, and per-mode gas-temperature semantics.

§5 -- diamond_cp and diamond_k carry floors documented as "small ... for
numerical safety". Measured against the raw models they guard, the Cp floor
exceeds the Debye model below 0.407 K and the k floor exceeds the
boundary-limited model below 0.794 K. Both crossovers are far above this
machine's operating point: at the 10 mK stage the floors exceed the physical
models by 6.8e4x and 5.0e5x, and the Mode-C readiness threshold (50 mK) sits
deep inside the floored regime. The floors are UNCHANGED here -- the repository
has no authority for better cryogenic diamond properties -- but they must stay
declared, and a future floor must not silently come to dominate.

§15 -- species_transport_3d evaluated every species at the 10 mK Mode-D
sensing-stage temperature, including the Mode-B methane precursor, which
decided its Knudsen regime.

MODEL-ONLY / FORECAST-ONLY. Software verification; not a hardware statement.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qta_multiphysics import material_models as MM              # noqa: E402
from qta_multiphysics.config import default_config              # noqa: E402
from qta_multiphysics.units import DIAMOND_DENSITY_KG_M3        # noqa: E402


# ------------------------------------------------- §5 crossovers and ratios --

def test_crossovers_are_where_the_floor_equals_the_raw_model():
    t_cp = MM.cp_floor_crossover_K()
    t_k = MM.k_floor_crossover_K()
    assert math.isclose(float(MM._cp_raw(t_cp)), MM.CP_FLOOR_J_KG_K, rel_tol=1e-9)
    assert math.isclose(float(MM._k_raw(t_k)), MM.K_FLOOR_W_M_K, rel_tol=1e-9)


def test_floors_are_declared_as_dominant_in_the_operating_regime():
    """The floors dominate at 10 mK; the report must say so, not hide it."""
    r = MM.floor_report()
    assert r["dominant_in_canonical_regime"] is True
    row = next(t for t in r["table"] if t["T_K"] == 0.010)
    assert row["cp_floor_dominates"] and row["k_floor_dominates"]
    assert row["cp_floor_over_raw"] > 1e4
    assert row["k_floor_over_raw"] > 1e5


def test_mode_c_readiness_threshold_is_inside_the_floored_regime():
    """The governed 50 mK threshold is not evaluated on the cited physics."""
    th = default_config().solver.mode_d_temp_threshold_K
    assert th < MM.cp_floor_crossover_K()
    assert th < MM.k_floor_crossover_K()
    row = next(t for t in MM.floor_report()["table"] if t["T_K"] == th)
    assert row["cp_floor_dominates"] and row["k_floor_dominates"]


def test_floor_class_below_crossover_is_not_called_regularization():
    r = MM.floor_report()
    for f in r["floors"].values():
        assert f["class_below_crossover"] == MM.FLOOR_CLASS_EFFECTIVE_PROPERTY
        assert f["class_above_crossover"] == MM.FLOOR_CLASS_REGULARIZATION


def test_floors_distort_diffusivity_by_the_reported_factor():
    """Raw alpha is flat (k and Cp both ~T^3); the floors break that."""
    for T in (0.010, 0.050):
        a_raw = float(MM._k_raw(T)) / (DIAMOND_DENSITY_KG_M3 * float(MM._cp_raw(T)))
        a_flr = float(MM.diamond_k(T)) / (DIAMOND_DENSITY_KG_M3 * float(MM.diamond_cp(T)))
        assert math.isclose(a_raw, 0.0385, rel_tol=0.02), a_raw
        assert a_flr / a_raw > 7.0, f"expected ~7.4x inflation, got {a_flr / a_raw}"


def test_no_authoritative_replacement_is_invented():
    r = MM.floor_report()
    assert "NO_AUTHORITATIVE_REPLACEMENT_IN_REPOSITORY" in r["authority_status"]
    assert MM.CP_FLOOR_J_KG_K == 1.0e-6      # unchanged
    assert MM.K_FLOOR_W_M_K == 1.0e-3        # unchanged


def test_affected_predictions_are_named():
    r = MM.floor_report()
    blob = " ".join(r["affected_predictions"]).lower()
    assert "recool" in blob or "recovery" in blob
    assert "diffusivity" in blob


# --- the general guard: a numerical floor may not dominate undeclared -------

def test_a_guard_cannot_dominate_a_scientific_regime_without_being_declared():
    """Every floor whose crossover lands inside a modelled temperature must
    appear in floor_report() and be classed as an effective property there.

    This is the property that failed silently: the floors were introduced as
    guards, the operating point moved below their crossovers, and nothing
    forced anyone to notice.
    """
    r = MM.floor_report()
    modelled_lo = min(MM.MODE_TEMPERATURE_PROBES_K)
    declared = r["floors"]
    for name, crossover in (("diamond_cp", MM.cp_floor_crossover_K()),
                            ("diamond_k", MM.k_floor_crossover_K())):
        if crossover > modelled_lo:
            assert name in declared, f"{name} floor dominates but is undeclared"
            assert declared[name]["crossover_K"] == crossover
            assert declared[name]["class_below_crossover"] == \
                MM.FLOOR_CLASS_EFFECTIVE_PROPERTY


def test_floor_declaration_reaches_the_model_provenance():
    from qta_multiphysics import materials_3d
    from qta_multiphysics.mesh_3d import Grid3DConfig, StructuredGrid3D
    cfg = default_config()
    grid = StructuredGrid3D(cfg, Grid3DConfig(nx=6, ny=6, nz=6))
    d = materials_3d.summary(cfg, grid)
    assert "property_floor_declaration" in d, \
        "a consumer of a sub-kelvin prediction cannot see the floors"
    assert d["property_floor_declaration"]["dominant_in_canonical_regime"]


# ------------------------------------------- §15 gas-temperature semantics --

def test_mode_b_methane_is_not_evaluated_at_the_sensing_temperature():
    from qta_multiphysics import species_transport_3d as ST
    mode, _basis, status, T = ST.GAS_TEMPERATURE_SEMANTICS["C13_CH4"]
    assert mode == "MODE_B"
    assert status == ST.UNRESOLVED
    assert T is None, "a Mode-B gas temperature was assigned without authority"


def test_unresolved_species_report_a_span_not_a_regime():
    from qta_multiphysics import species_transport_3d as ST
    row = next(r for r in ST.summary()["per_species"] if r["species"] == "C13_CH4")
    assert row["T_eval_K"] is None
    assert row["Kn"] is None
    assert row["regime"] == "PARAMETERIZED_UNRESOLVED"
    assert row["regime_is_temperature_sensitive"] is True
    assert set(row["regimes_spanned"]) >= {"TRANSITIONAL", "MOLECULAR_FLOW"}


def test_helium_stays_resolved_at_the_sensing_stage():
    from qta_multiphysics import species_transport_3d as ST
    for sp in ("He3", "He4"):
        row = next(r for r in ST.summary()["per_species"] if r["species"] == sp)
        assert row["gas_temperature_status"] == ST.RESOLVED
        assert row["T_eval_K"] == default_config().fridge.T_fridge_K
        assert row["regime"] == "MOLECULAR_FLOW"


def test_residual_hydrogen_classification_is_robust_across_the_span():
    """Unresolved temperature, but the answer does not depend on it."""
    from qta_multiphysics import species_transport_3d as ST
    row = next(r for r in ST.summary()["per_species"] if r["species"] == "H2")
    assert row["gas_temperature_status"] == ST.UNRESOLVED
    assert row["regime"] == "MOLECULAR_FLOW"
    assert row["regime_is_temperature_sensitive"] is False


def test_summary_lists_its_unresolved_temperatures():
    from qta_multiphysics import species_transport_3d as ST
    assert ST.summary()["unresolved_gas_temperatures"] == ["C13_CH4", "H2"]


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
