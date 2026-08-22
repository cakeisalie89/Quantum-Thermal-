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
import ast
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

#: A float literal outside this window cannot plausibly be an H2 partial
#: pressure in Pa anywhere in this repository, so it is never considered.
#: The window is deliberately wider than the declared set: the point is to
#: catch a value that is ABOUT to become a governed pressure, not only the
#: ones already named.
H2_PRESSURE_WINDOW_PA = (1e-15, 1e-6)

#: Identifier parts that mean "this name denotes a pressure".
_PRESSURE_PARTS = {"p", "pa", "pressure"}


def _identifier_parts(name):
    return [q for q in re.split(r"[^A-Za-z0-9]+", str(name)) if q]


def _names_hydrogen(name):
    """True when ``name`` carries H2 as a whole token.

    Token-wise, not substring-wise, so ``P_CH4_valve_leak_Pa`` is not hydrogen
    and ``m_H2v`` (a mass) is not either -- only ``H2`` standing alone between
    separators counts.
    """
    return any(q.upper() == "H2" for q in _identifier_parts(name))


def _names_pressure(name):
    return any(q.lower() in _PRESSURE_PARTS for q in _identifier_parts(name))


def _is_h2_pressure_name(name):
    return _names_hydrogen(name) and _names_pressure(name)


def _semantic_anchors(node, parent, enclosing_func):
    """Names that say what the literal at ``node`` MEANS, nearest first.

    Ancestry is the evidence, not the numeric value: a bare 1e-10 is a defect
    when something in its enclosing context calls it an H2 pressure, and is
    ordinary arithmetic otherwise. This is why the scan cannot be a regex over
    the source text -- ``S_vib_m2Hz = 1e-10`` and ``return 1e-10`` inside
    ``P_H2_Pa`` are the same characters and opposite verdicts.
    """
    anchors = []
    cur = node
    while cur is not None:
        par = parent.get(cur)
        if par is None:
            break
        if isinstance(par, ast.keyword) and par.arg:
            anchors.append(par.arg)                      # foo(P_H2=...)
        elif isinstance(par, ast.Dict):
            for k, v in zip(par.keys, par.values):       # {"P_H2": ...}
                if v is cur and isinstance(k, ast.Constant) and isinstance(k.value, str):
                    anchors.append(k.value)
        elif isinstance(par, ast.Assign):
            for t in par.targets:
                anchors.extend(_target_names(t))
        elif isinstance(par, ast.AnnAssign):
            anchors.extend(_target_names(par.target))
        elif isinstance(par, ast.Call):
            anchors.extend(_callee_names(par.func))      # positional args
        elif isinstance(par, ast.arguments):
            anchors.extend(a.arg for a in
                           list(par.posonlyargs) + list(par.args) + list(par.kwonlyargs))
        cur = par
    if enclosing_func is not None:
        anchors.append(enclosing_func)                   # return / default exprs
    return anchors


def _target_names(t):
    if isinstance(t, ast.Name):
        return [t.id]
    if isinstance(t, ast.Attribute):
        return [t.attr]
    if isinstance(t, (ast.Tuple, ast.List)):
        out = []
        for e in t.elts:
            out.extend(_target_names(e))
        return out
    return []


def _callee_names(f):
    if isinstance(f, ast.Name):
        return [f.id]
    if isinstance(f, ast.Attribute):
        return [f.attr]
    return []


def scan_bare_h2_pressure_literals(source, module_declares=()):
    """Report executable float literals that mean an H2 pressure but are unnamed.

    Returns a list of ``(lineno, value, anchor)``. Empty means every governed
    H2 pressure in ``source`` reaches its consumer through a named quantity.

    Coverage is by AST ancestry, so it is uniform across executable contexts:
    assignment and annotated-assignment right-hand sides, return expressions,
    positional and keyword call arguments, binary and unary and comparison and
    conditional expressions, tuples/lists/sets, dict values, comprehensions,
    nested calls, and parameter defaults. Docstrings, comments and prose are
    not float literals and are structurally invisible to it.

    ``module_declares`` names the module-level constants that ARE the
    declarations; only the statements defining exactly those names are exempt,
    since a declaration is where the literal is supposed to live. An ad-hoc
    module-level assignment to an H2-pressure-shaped name is NOT a declaration
    and is still reported.
    """
    tree = ast.parse(source)
    parent = {}
    func_of = {}
    for p_node in ast.walk(tree):
        fname = getattr(p_node, "name", None) if isinstance(
            p_node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
        for child in ast.iter_child_nodes(p_node):
            parent[child] = p_node
            if fname is not None:
                func_of[child] = fname

    def enclosing_func(n):
        cur = n
        while cur is not None:
            if cur in func_of:
                return func_of[cur]
            cur = parent.get(cur)
        return None

    declared_names = set(module_declares)
    exempt = set()
    for stmt in tree.body:
        targets = []
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                targets.extend(_target_names(t))
        elif isinstance(stmt, ast.AnnAssign):
            targets.extend(_target_names(stmt.target))
        if any(t in declared_names for t in targets):
            exempt.update(ast.walk(stmt))

    lo, hi = H2_PRESSURE_WINDOW_PA
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant)
                and isinstance(node.value, float)
                and not isinstance(node.value, bool)):
            continue
        if node in exempt or not (lo <= node.value <= hi):
            continue
        for anchor in _semantic_anchors(node, parent, enclosing_func(node)):
            if _is_h2_pressure_name(anchor):
                offenders.append((node.lineno, node.value, anchor))
                break
    return offenders


def test_no_bare_h2_pressure_literals_in_executable_consumers():
    """No unnamed H2 pressure literal survives anywhere in executable code.

    The previous version of this guard visited only ``ast.Assign`` nodes whose
    TARGET name contained "P_H2". A bare ``return 1e-10`` in ChamberState's own
    ``P_H2_Pa()`` was therefore invisible to it -- and that value fed gate
    D10b. The scan is now by ancestry over every float literal, so the context
    that makes a number a pressure is what decides, not the shape of one
    statement.
    """
    declared = {n for n in dir(Q) if n.startswith("P_H2")}
    offenders = scan_bare_h2_pressure_literals(SRC, declared)
    assert not offenders, (
        "unnamed H2 pressure literals in executable code: "
        + "; ".join(f"line {ln}: {v:g} (context {a!r})" for ln, v, a in offenders))


# ------------------------------------------- the guard itself must be tested --
# A checker is not proven by the tree it currently passes on. Each snippet below
# is source the guard MUST reject; each snippet in the false-positive test is
# source it MUST accept. Both directions are required: a guard that flags
# everything is as useless as one that flags nothing.

_MUST_CATCH = {
    "return literal in an H2 accessor":
        "def P_H2_Pa(self):\n    return 1e-10\n",
    "plain assignment to an H2 pressure name":
        "P_H2_state = 1e-10\n",
    "keyword call argument":
        "configure(P_H2_Pa=1e-10)\n",
    "positional call argument":
        "set_h2_pressure(1e-10)\n",
    "binary expression":
        "P_H2_scaled_Pa = scale * 1e-10\n",
    "dict value under an H2 key":
        'mapping = {"P_H2_Pa": 1e-10}\n',
    "conditional expression in a return":
        "def P_H2_Pa(self):\n    return 2e-12 if done else 1e-12\n",
    "annotated assignment":
        "P_H2_target_Pa: float = 2e-12\n",
    "tuple element":
        "P_H2_band_Pa = (5e-13, 2e-12)\n",
    "nested call":
        "P_H2_draw_Pa = float(max(5e-13, 2e-12))\n",
    "unary expression":
        "P_H2_delta_Pa = -1e-12\n",
    "comparison":
        "def P_H2_ok(self):\n    return self.P_H2_Pa() < 2e-12\n",
    "list comprehension element":
        "P_H2_sweep_Pa = [x * 1e-12 for x in factors]\n",
    "parameter default":
        "def dose(P_H2_Pa=5e-12):\n    return P_H2_Pa\n",
}

_MUST_NOT_CATCH = {
    "vibration spectral density sharing the value":
        "S_vib_m2Hz = 1e-10\n",
    "vibration term inside an expression":
        "Pv2 = 1e-4 * (1e-10 * 10) * 100 / (2 * 100)\n",
    "a different species' pressure":
        "P_CH4_valve_leak_Pa = 1e-11 / 0.010\n",
    "an H2 quantity that is not a pressure":
        "theta_H2 = 0.3 * P * 1e-12\n",
    "an H2 mass, not a pressure":
        "m_H2v = 2 * m_p\n",
    "a numerical tolerance":
        "if abs(Tn - Ts) < 1e-11:\n    Ts = Tn\n",
    "an outgassing rate, not a pressure":
        "Q_rate = (1e-11 if baked else 1e-9) * 0.1\n",
    "the number quoted in a docstring":
        'def f():\n    """post-bakeout P_H2 is 1e-12 Pa."""\n    return P_H2_POST_BAKEOUT_NEG_PA\n',
    "the number quoted in a comment":
        "# P_H2 pre-bakeout is 1e-10 Pa\nx = compute()\n",
    "the number quoted in prose assigned to an H2 name":
        'P_H2_note = "pre-bakeout P_H2 = 1e-10 Pa (assumed)"\n',
    "a named constant reaching its consumer":
        "def P_H2_Pa(self):\n    return P_H2_PRE_BAKEOUT_PA\n",
}


def test_guard_catches_every_executable_context():
    for label, src in _MUST_CATCH.items():
        assert scan_bare_h2_pressure_literals(src), (
            f"guard missed a bare H2 pressure literal: {label}\n{src}")


def test_guard_does_not_flag_unrelated_values():
    for label, src in _MUST_NOT_CATCH.items():
        found = scan_bare_h2_pressure_literals(src)
        assert not found, (
            f"guard produced a false positive: {label} -> {found}\n{src}")


def test_guard_exempts_the_declarations_themselves():
    """The module-level constants ARE the names; they must not self-report."""
    src = ("P_H2_PRE_BAKEOUT_PA = 1e-10\n"
           "P_H2_POST_BAKEOUT_NEG_MC_RANGE_PA = (5e-13, 2e-12)\n")
    assert not scan_bare_h2_pressure_literals(
        src, {"P_H2_PRE_BAKEOUT_PA", "P_H2_POST_BAKEOUT_NEG_MC_RANGE_PA"})


def test_guard_would_have_caught_the_defect_it_was_written_for():
    """The exact pre-fix source of ChamberState.P_H2_Pa()."""
    pre_fix = ("def P_H2_Pa(self):\n"
               "    if self.bakeout_done and self.NEG_installed:\n"
               "        return P_H2_POST_BAKEOUT_NEG_PA\n"
               "    elif self.bakeout_done:\n"
               "        return P_H2_POST_BAKEOUT_ONLY_PA\n"
               "    return 1e-10\n")
    hits = scan_bare_h2_pressure_literals(pre_fix)
    assert hits, "the guard still cannot see the defect that motivated it"
    assert hits[0][1] == 1e-10


def test_the_repository_binds_the_pre_bakeout_branch_to_its_constant():
    """ChamberState must hand D10b the named quantity, not a copy of its value."""
    ch = Q.ChamberState()
    assert ch.bakeout_done is False and ch.NEG_installed is False
    assert ch.P_H2_Pa() is Q.P_H2_PRE_BAKEOUT_PA


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
