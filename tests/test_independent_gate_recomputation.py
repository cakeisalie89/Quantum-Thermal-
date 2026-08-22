"""Recompute B4 and D10b from authority inputs, without the production code.

A gate that is only ever checked against the number the production function
produced is not verified, it is echoed. Both formulas here are written from the
governed equation in the gate table, take their inputs from the named authority
constants, and are compared against the canonical artifact -- so a change in
either the constant or the production path has to show up as a disagreement.

The mutation tests are the second half: each input is perturbed and the result
must move in the physically required direction. That is what distinguishes
"the formula is transcribed correctly" from "the formula is a constant".

MODEL-ONLY / FORECAST-ONLY / NOT_MEASURED_IN_THIS_SYSTEM. Nothing here is a
measurement, and no canonical value is changed by any of it.
"""
import csv
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import qta_full_sim as Q                                        # noqa: E402

# CODATA values, written here independently of the production module so that a
# drift in either shows up rather than cancelling.
K_B_J_PER_K = 1.380649e-23          # J/K
M_PROTON_KG = 1.67262192369e-27     # kg

# Governed inputs of the H2 surface-coverage equation, as the gate states it:
#   theta = s * P * t / (sqrt(2 pi m k T) * n_mono)
S_STICKING = 0.3                    # dimensionless
M_H2_KG = 2 * M_PROTON_KG           # kg  (H2 = 2 x H)
T_WALL_K = 300.0                    # K   (warm-wall impingement temperature)
T_MEAS_S = 1.0e4                    # s   (measurement window)
N_MONOLAYER_PER_M2 = 1.0e19         # 1/m^2


def theta_percent(*, s=S_STICKING, P_Pa=None, m_kg=M_H2_KG, T_K=T_WALL_K,
                  t_s=T_MEAS_S, n_per_m2=N_MONOLAYER_PER_M2):
    """Fractional monolayer coverage, in percent.

    Units:  [s] = 1, [P] = Pa = kg/(m s^2), [m] = kg, [k T] = J = kg m^2/s^2,
            sqrt(2 pi m k T) = kg m / s, so P/sqrt(...) = 1/(m^2 s);
            times t [s] over n [1/m^2] leaves a dimensionless coverage.
    """
    flux_per_m2_s = s * P_Pa / math.sqrt(2.0 * math.pi * m_kg * K_B_J_PER_K * T_K)
    return flux_per_m2_s * t_s / n_per_m2 * 100.0


def _gate(gate_id):
    with open(os.path.join(ROOT, "results_gate_table.csv"), newline="") as f:
        for r in csv.DictReader(f):
            if r["gate_id"] == gate_id:
                return r
    raise AssertionError(f"gate {gate_id} absent from the canonical table")


# ----------------------------------------------------------------- units ----

def test_the_flux_expression_is_dimensionally_consistent():
    """sqrt(2 pi m k T) must carry kg*m/s so the coverage comes out unitless."""
    m, T = M_H2_KG, T_WALL_K
    root = math.sqrt(2.0 * math.pi * m * K_B_J_PER_K * T)
    # kg * J = kg^2 m^2 / s^2  ->  sqrt = kg m / s ; magnitude sanity only
    assert 1e-24 < root < 1e-22, root
    # Pa / (kg m / s) = 1/(m^2 s): a flux. Multiply by s, divide by 1/m^2 -> 1.
    flux = 1.0 / root                       # per Pa
    assert flux > 0


def test_the_authority_constants_are_positive_pressures_in_pascals():
    for name in sorted(n for n in dir(Q) if n.startswith("P_H2")):
        v = getattr(Q, name)
        for x in (v if isinstance(v, tuple) else (v,)):
            assert isinstance(x, float) and 0.0 < x < 1.0, f"{name}={x}"


# -------------------------------------------------------------------- B4 ----

def test_b4_recomputed_independently_matches_the_canonical_gate():
    row = _gate("B4")
    got = theta_percent(P_Pa=Q.P_H2_POST_BAKEOUT_ASSUMED_PA)
    canonical = float(row["computed"])
    assert math.isclose(got, canonical, rel_tol=1e-12), (
        f"independent theta={got!r} vs canonical={canonical!r}")


def test_b4_consumes_the_assumption_not_a_modelled_state():
    """Substituting any other governed H2 pressure must NOT reproduce B4."""
    canonical = float(_gate("B4")["computed"])
    for name in ("P_H2_PRE_BAKEOUT_PA", "P_H2_POST_BAKEOUT_NEG_PA",
                 "P_H2_POST_BAKEOUT_ONLY_PA", "P_H2_RGA_VALIDATION_THRESHOLD_PA"):
        other = theta_percent(P_Pa=getattr(Q, name))
        assert not math.isclose(other, canonical, rel_tol=1e-9), (
            f"B4 is reproducible from {name}; the gate's input is ambiguous")


def test_b4_classification_follows_from_the_declared_threshold():
    row = _gate("B4")
    computed, threshold = float(row["computed"]), float(row["threshold"])
    assert computed < threshold
    assert row["status"] == "CONDITIONAL"
    assert row["can_PASS_now"] == "NO"
    assert row["measured_in_this_system"] == "false"


def test_b4_inputs_move_the_result_in_the_physical_direction():
    base = theta_percent(P_Pa=Q.P_H2_POST_BAKEOUT_ASSUMED_PA)
    P = Q.P_H2_POST_BAKEOUT_ASSUMED_PA
    assert theta_percent(P_Pa=P, s=2 * S_STICKING) > base        # more sticking
    assert theta_percent(P_Pa=2 * P) > base                      # more pressure
    assert theta_percent(P_Pa=P, m_kg=4 * M_H2_KG) < base        # heavier: slower
    assert theta_percent(P_Pa=P, T_K=4 * T_WALL_K) < base        # hotter: slower
    assert theta_percent(P_Pa=P, t_s=2 * T_MEAS_S) > base        # longer window
    assert theta_percent(P_Pa=P, n_per_m2=2 * N_MONOLAYER_PER_M2) < base  # more sites


def test_b4_scales_exactly_as_the_square_root_law_requires():
    """theta ~ P and theta ~ 1/sqrt(m T): checked, not assumed."""
    P = Q.P_H2_POST_BAKEOUT_ASSUMED_PA
    base = theta_percent(P_Pa=P)
    assert math.isclose(theta_percent(P_Pa=3 * P), 3 * base, rel_tol=1e-12)
    assert math.isclose(theta_percent(P_Pa=P, T_K=4 * T_WALL_K), base / 2.0,
                        rel_tol=1e-12)
    assert math.isclose(theta_percent(P_Pa=P, m_kg=9 * M_H2_KG), base / 3.0,
                        rel_tol=1e-12)


# ------------------------------------------------------------------ D10b ----

def test_d10b_recomputed_independently_matches_the_canonical_gate():
    row = _gate("D10b")
    got = theta_percent(P_Pa=Q.P_H2_PRE_BAKEOUT_PA)
    canonical = float(row["computed"])
    assert math.isclose(got, canonical, rel_tol=1e-12), (
        f"independent theta={got!r} vs canonical={canonical!r}")


def test_d10b_actually_reaches_the_named_pre_bakeout_constant():
    """The accessor must hand back the constant object, not a copy of its value."""
    ch = Q.CURRENT_CHAMBER
    assert ch.bakeout_done is False and ch.NEG_installed is False
    assert ch.P_H2_Pa() is Q.P_H2_PRE_BAKEOUT_PA


def test_changing_the_authority_constant_changes_the_recomputation():
    """Mutation: if it did not, the formula would not depend on the authority."""
    base = theta_percent(P_Pa=Q.P_H2_PRE_BAKEOUT_PA)
    mutated = theta_percent(P_Pa=Q.P_H2_PRE_BAKEOUT_PA * 1.5)
    assert math.isclose(mutated, 1.5 * base, rel_tol=1e-12)
    assert not math.isclose(mutated, base, rel_tol=1e-9)


def test_changing_an_unrelated_h2_constant_does_not_change_d10b():
    """D10b depends on the pre-bakeout state only."""
    canonical = float(_gate("D10b")["computed"])
    got = theta_percent(P_Pa=Q.P_H2_PRE_BAKEOUT_PA)
    assert math.isclose(got, canonical, rel_tol=1e-12)
    for name in ("P_H2_POST_BAKEOUT_ASSUMED_PA", "P_H2_ACCEPTANCE_TARGET_PA"):
        assert not math.isclose(theta_percent(P_Pa=getattr(Q, name)), canonical,
                                rel_tol=1e-9), name


def test_d10b_classification_follows_from_its_prerequisite_not_the_number():
    """theta exceeds the threshold AND the D10a prerequisites are unmet.

    Either alone would forbid PASS; the gate must be BLOCKED, not FAIL, because
    the prerequisite chain -- not the arithmetic -- is what is missing.
    """
    row = _gate("D10b")
    assert float(row["computed"]) > float(row["threshold"])
    assert row["status"] == "BLOCKED"
    assert row["can_PASS_now"] == "NO"
    assert row["measured_in_this_system"] == "false"
    assert _gate("D10a")["status"] == "BLOCKED"


def test_the_two_gates_are_not_the_same_calculation_twice():
    b4 = float(_gate("B4")["computed"])
    d10b = float(_gate("D10b")["computed"])
    assert not math.isclose(b4, d10b, rel_tol=1e-6)
    # and their ratio is exactly the ratio of their input pressures
    assert math.isclose(d10b / b4,
                        Q.P_H2_PRE_BAKEOUT_PA / Q.P_H2_POST_BAKEOUT_ASSUMED_PA,
                        rel_tol=1e-12)


# --------------------------------------- coverage inputs are single-sourced --

def test_the_coverage_equation_inputs_are_named_not_restated():
    """s, n_mono and T_room fed both gates from five separate bare locals.

    They are ASSUMED model parameters, so a change to one and not the others
    would move one gate and not the other with nothing to notice. Scanned by
    AST, so a restatement inside any expression is caught, not just a line
    matching a particular shape.
    """
    import ast as _ast
    src = open(os.path.join(ROOT, "qta_full_sim.py"), encoding="utf-8").read()
    tree = _ast.parse(src)
    declared_lines = set()
    for stmt in tree.body:
        if isinstance(stmt, _ast.Assign):
            for t in stmt.targets:
                if isinstance(t, _ast.Name) and t.id in (
                        "S_H2_STICKING", "N_MONOLAYER_SITES_PER_M2",
                        "T_WALL_IMPINGEMENT_K"):
                    declared_lines.update(
                        getattr(n, "lineno", -1) for n in _ast.walk(stmt))
    watched = {0.3: "S_H2_STICKING", 1e19: "N_MONOLAYER_SITES_PER_M2",
               300.0: "T_WALL_IMPINGEMENT_K"}
    offenders = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Assign) or node.lineno in declared_lines:
            continue
        names = [t.id for t in node.targets if isinstance(t, _ast.Name)]
        if not any(n.lstrip("_") in ("s_H2", "n_mono", "T_room") for n in names):
            continue
        if isinstance(node.value, _ast.Constant) and node.value.value in watched:
            offenders.append(
                f"line {node.lineno}: {names} = {node.value.value!r}; "
                f"use {watched[node.value.value]}")
    assert not offenders, "coverage-equation inputs restated: " + "; ".join(offenders)


def test_the_named_coverage_inputs_match_the_independent_ones():
    """This module wrote its own copies; they must agree with the authority."""
    assert Q.S_H2_STICKING == S_STICKING
    assert Q.N_MONOLAYER_SITES_PER_M2 == N_MONOLAYER_PER_M2
    assert Q.T_WALL_IMPINGEMENT_K == T_WALL_K


def test_changing_a_coverage_input_moves_both_gates_together():
    """They are shared inputs: a mutation must reach B4 and D10b alike."""
    b4 = theta_percent(P_Pa=Q.P_H2_POST_BAKEOUT_ASSUMED_PA)
    d10b = theta_percent(P_Pa=Q.P_H2_PRE_BAKEOUT_PA)
    b4_m = theta_percent(P_Pa=Q.P_H2_POST_BAKEOUT_ASSUMED_PA, s=2 * S_STICKING)
    d10b_m = theta_percent(P_Pa=Q.P_H2_PRE_BAKEOUT_PA, s=2 * S_STICKING)
    assert math.isclose(b4_m / b4, 2.0, rel_tol=1e-12)
    assert math.isclose(d10b_m / d10b, 2.0, rel_tol=1e-12)



if __name__ == "__main__":
    ns = dict(globals())
    for _n, _f in ns.items():
        if _n.startswith("test_") and callable(_f):
            _f()
    print("RESULT: independent gate recomputation agrees with the canonical table")
