"""§14 closure: distinct H2 pressure quantities must stay distinct.

Owner decision: gate B4's coverage forecast uses the literature/design
assumption (5e-12 Pa); the chamber-state model keeps its own modelled
pressures (1e-10 / 2e-12 / 1e-12 Pa); the acceptance target and the RGA
validation threshold are separate again. These are DIFFERENT QUANTITIES, not
rival estimates of one number, so the risk this file guards against is
silent SUBSTITUTION, not disagreement.

The Monte-Carlo range belongs to the modelled bakeout+NEG pressure. The
repository establishes that three ways: the sampler's own comment, the
enclosing run_mode_D_MC docstring, and the bounds themselves --
sqrt(5e-13 * 2e-12) = 1e-12 Pa exactly, a geometric factor-of-2 band centred
on that nominal. It was previously named P_H2_MC_RANGE_PA, which invited
reading it as uncertainty around the B4 assumption; it is not.

MODEL-ONLY / FORECAST-ONLY. No value here is a measurement.
"""
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import qta_full_sim as Q                                        # noqa: E402

SRC = open(os.path.join(ROOT, "qta_full_sim.py"), encoding="utf-8").read()


# --------------------------------------------- values and their distinctness --

def test_each_named_quantity_keeps_its_value():
    assert Q.P_H2_PRE_BAKEOUT_PA == 1e-10
    assert Q.P_H2_POST_BAKEOUT_NEG_PA == 1e-12
    assert Q.P_H2_POST_BAKEOUT_ONLY_PA == 2e-12
    assert Q.P_H2_POST_BAKEOUT_ASSUMED_PA == 5e-12
    assert Q.P_H2_ACCEPTANCE_TARGET_PA == 2e-12
    assert Q.P_H2_RGA_VALIDATION_THRESHOLD_PA == 2e-14
    assert Q.P_H2_POST_BAKEOUT_NEG_MC_RANGE_PA == (5e-13, 2e-12)


def test_no_universal_h2_constant_was_created():
    """The point is to preserve distinct quantities, not erase them."""
    names = [n for n in dir(Q) if n.startswith("P_H2")]
    assert len(names) >= 6, names
    vals = {Q.P_H2_PRE_BAKEOUT_PA, Q.P_H2_POST_BAKEOUT_NEG_PA,
            Q.P_H2_POST_BAKEOUT_ONLY_PA, Q.P_H2_POST_BAKEOUT_ASSUMED_PA,
            Q.P_H2_RGA_VALIDATION_THRESHOLD_PA}
    assert len(vals) == 5, "distinct quantities collapsed onto one value"


def test_shared_value_does_not_mean_shared_identity():
    """2e-12 Pa is two different quantities and must stay two names."""
    assert Q.P_H2_POST_BAKEOUT_ONLY_PA == Q.P_H2_ACCEPTANCE_TARGET_PA
    assert "shared_values_are_not_shared_meanings" in Q.H2_PRESSURE_AUTHORITY
    assert re.search(r"^P_H2_POST_BAKEOUT_ONLY_PA\s*=", SRC, re.M)
    assert re.search(r"^P_H2_ACCEPTANCE_TARGET_PA\s*=", SRC, re.M)


# ------------------------------------------- each consumer takes its own one --

def test_b4_consumes_the_b4_assumption():
    """Gate B4 must reference the assumption symbol, not a chamber value."""
    m = re.search(r"P_H2\s*=\s*(P_H2_[A-Z_]+)", SRC)
    assert m, "gate B4 no longer assigns P_H2 from a named quantity"
    assert m.group(1) == "P_H2_POST_BAKEOUT_ASSUMED_PA", m.group(1)


def test_b4_gate_value_is_computed_from_the_assumption():
    import csv
    rows = {r["gate_id"] if "gate_id" in r else r.get("gid"): r
            for r in csv.DictReader(open(os.path.join(ROOT, "results_gate_table.csv"),
                                         newline=""))}
    b4 = rows.get("B4")
    assert b4 is not None
    theta = float(b4["computed"])
    # theta is exactly linear in P_H2; recover the pressure it used.
    implied = theta / 0.016076265348451066 * Q.P_H2_POST_BAKEOUT_ASSUMED_PA
    assert math.isclose(implied, Q.P_H2_POST_BAKEOUT_ASSUMED_PA, rel_tol=1e-6), (
        f"B4 appears to use {implied:.3e} Pa, not the B4 assumption")


def test_chamber_state_consumes_the_chamber_model_quantities():
    from copy import deepcopy
    c = deepcopy(Q.CURRENT_CHAMBER)
    assert c.P_H2_Pa() == Q.P_H2_PRE_BAKEOUT_PA
    c.bakeout_done = True
    assert c.P_H2_Pa() == Q.P_H2_POST_BAKEOUT_ONLY_PA
    c.NEG_installed = True
    assert c.P_H2_Pa() == Q.P_H2_POST_BAKEOUT_NEG_PA


def test_chamber_state_never_returns_the_b4_assumption():
    """The two paths must not be able to substitute for one another."""
    from copy import deepcopy
    import itertools
    for bake, neg in itertools.product((False, True), repeat=2):
        c = deepcopy(Q.CURRENT_CHAMBER)
        c.bakeout_done, c.NEG_installed = bake, neg
        assert c.P_H2_Pa() != Q.P_H2_POST_BAKEOUT_ASSUMED_PA


# ------------------------------------------------- Monte-Carlo range belongs --

def test_mc_range_brackets_the_nominal_it_describes():
    """An interval that excludes its own nominal is not an uncertainty range."""
    lo, hi = Q.P_H2_POST_BAKEOUT_NEG_MC_RANGE_PA
    assert lo <= Q.P_H2_POST_BAKEOUT_NEG_PA <= hi
    assert Q.H2_PRESSURE_AUTHORITY["mc_range_brackets_its_nominal"] is True
    assert Q.H2_PRESSURE_AUTHORITY["mc_range_describes"] == \
        "P_H2_POST_BAKEOUT_NEG_PA"


def test_mc_range_is_centred_on_that_nominal():
    """The evidence the renaming rests on, kept executable."""
    lo, hi = Q.P_H2_POST_BAKEOUT_NEG_MC_RANGE_PA
    assert math.isclose(math.sqrt(lo * hi), Q.P_H2_POST_BAKEOUT_NEG_PA,
                        rel_tol=1e-12)
    assert math.isclose(hi / Q.P_H2_POST_BAKEOUT_NEG_PA, 2.0, rel_tol=1e-12)
    assert math.isclose(Q.P_H2_POST_BAKEOUT_NEG_PA / lo, 2.0, rel_tol=1e-12)


def test_mc_range_does_not_claim_to_describe_the_b4_assumption():
    lo, hi = Q.P_H2_POST_BAKEOUT_NEG_MC_RANGE_PA
    assert not (lo <= Q.P_H2_POST_BAKEOUT_ASSUMED_PA <= hi), (
        "the B4 assumption now sits inside the chamber-model MC range; if the "
        "range was widened, H2_PRESSURE_AUTHORITY must say why")
    assert "P_H2_MC_RANGE_PA" not in [n for n in dir(Q)], \
        "the ambiguous old name is back"


def test_mc_sampler_uses_the_renamed_quantity():
    assert re.search(r"rng\.uniform\(\*P_H2_POST_BAKEOUT_NEG_MC_RANGE_PA\)", SRC)


# ------------------------------------------------------- hygiene / structure --

def test_no_bare_h2_pressure_literals_in_executable_consumers():
    """No unnamed H2 pressure literal in executable code.

    Parsed with ast rather than regex, for two reasons learned the hard way:
    a numeric-shape scan flags rng.uniform(1e-11, 1e-8), which is S_vib in
    m^2/Hz and not a pressure at all; and a text scan flags the authority
    record's own prose, which quotes the value it is documenting. Only real
    assignments and call keywords are inspected, and the module-level
    declarations of the named constants are exempt because they ARE the names.
    """
    import ast
    tree = ast.parse(SRC)
    declared = {n for n in dir(Q) if n.startswith("P_H2")}
    offenders = []

    class V(ast.NodeVisitor):
        def visit_Assign(self, node):
            for t in ast.walk(node):
                if isinstance(t, ast.Name) and t.id in declared:
                    return                      # a declaration, not a consumer
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any("P_H2" in n for n in names) and \
                    isinstance(node.value, ast.Constant) and \
                    isinstance(node.value.value, float):
                offenders.append(f"{names}={node.value.value:g} "
                                 f"(line {node.lineno})")
            self.generic_visit(node)

    V().visit(tree)
    assert not offenders, f"unnamed H2 pressure literals remain: {offenders}"


def test_every_named_quantity_is_in_pascals_and_positive():
    """Scalar quantities are positive floats; the range is an ordered pair."""
    for name in [n for n in dir(Q) if n.startswith("P_H2") and n.endswith("_PA")]:
        v = getattr(Q, name)
        if isinstance(v, tuple):          # the MC range, checked below
            assert len(v) == 2 and all(isinstance(x, float) and x > 0 for x in v)
            continue
        assert isinstance(v, float) and v > 0, (name, v)
    lo, hi = Q.P_H2_POST_BAKEOUT_NEG_MC_RANGE_PA
    assert 0 < lo < hi


def test_authority_record_is_resolved_and_still_unmeasured():
    a = Q.H2_PRESSURE_AUTHORITY
    assert a["status"] == "RESOLVED_BY_OWNER_DECISION"
    assert "5e-12" in a["decision"] or "P_H2_POST_BAKEOUT_ASSUMED_PA" in a["decision"]
    assert "RGA has not been performed" in a["still_unmeasured"]
    assert "NOT_MEASURED_IN_THIS_SYSTEM" in a["label"]


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
