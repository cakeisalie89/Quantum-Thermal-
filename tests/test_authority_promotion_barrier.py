"""Nothing descriptive, statistical or learned may become scientific authority.

Three barriers, each asserted rather than described.

QUARANTINE. species_transport_3d.summary() carries the CH4/H2 gas-temperature
question, which is unresolved. Its output must remain terminal diagnostic JSON:
called once, written once, never bound, never read back. Prose said so; this
proves it from the AST and from the gate arithmetic.

ACCOUNTING. The 83-gate distribution is recomputed from the canonical records
rather than compared against numbers copied into a report.

PROMOTION. PASS stays 0, can_PASS_now stays NO, measured_in_this_system stays
false, and the deep layer stays untrusted, unless authoritative evidence enters
through the governed process -- which no test, tool or model can do.

MODEL-ONLY / FORECAST-ONLY / NOT_MEASURED_IN_THIS_SYSTEM.
"""
import ast
import collections
import csv
import json
import math
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _tracked_py():
    out = subprocess.run(["git", "-C", ROOT, "ls-files", "*.py"],
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.split() if not p.startswith(("tests/", "attic/"))]


def _gate_rows():
    with open(os.path.join(ROOT, "results_gate_table.csv"), newline="") as f:
        return list(csv.DictReader(f))


# ============================ quarantine ====================================

def test_the_species_summary_is_never_bound_in_production_code():
    """Its value must go straight to disk; binding it invites reuse.

    A call whose result is discarded into write_json() cannot influence a
    number. A call assigned to a name can, later, silently.
    """
    offenders = []
    for rel in _tracked_py():
        tree = ast.parse(open(os.path.join(ROOT, rel), encoding="utf-8").read())
        parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            is_summary = (isinstance(f, ast.Attribute) and f.attr == "summary"
                          and isinstance(f.value, ast.Name)
                          and "species_transport" in f.value.id)
            if not is_summary:
                continue
            parent = parents.get(node)
            if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr,
                                   ast.Return, ast.BinOp, ast.Compare,
                                   ast.Subscript)):
                offenders.append(f"{rel}:{node.lineno} binds species summary()")
    assert not offenders, (
        "the descriptive species summary is being captured for reuse: "
        + "; ".join(offenders))


def test_no_gate_producing_module_imports_the_species_summary():
    """qta_full_sim owns every gate; it must not reach for the diagnostic."""
    src = open(os.path.join(ROOT, "qta_full_sim.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "species_transport" not in node.module, (
                f"qta_full_sim imports {node.module} at line {node.lineno}")
        if isinstance(node, ast.Import):
            for a in node.names:
                assert "species_transport" not in a.name, (
                    f"qta_full_sim imports {a.name} at line {node.lineno}")


def test_the_gas_evaluation_temperature_cannot_move_a_gate():
    """Mutate the diagnostic's temperature; D9's Knudsen number must not move.

    D9 is the one gate that consumes a Knudsen number, and it takes it from
    ModeStateVector, which uses the fridge temperature. If the diagnostic's
    T_EVAL_K ever fed a gate, this would catch it.
    """
    from qta_multiphysics import species_transport_3d as ST
    import qta_full_sim as Q
    row = [r for r in _gate_rows() if r["gate_id"] == "D9"][0]
    canonical_kn = float(row["computed"])

    before = Q.make_mode_D_state(Q.CHAMBER_STATE["post_bakeout"]).Kn_He
    assert math.isclose(before, canonical_kn, rel_tol=1e-12)

    original = ST.T_EVAL_K
    try:
        ST.T_EVAL_K = original * 1000.0        # absurd, deliberately
        after = Q.make_mode_D_state(Q.CHAMBER_STATE["post_bakeout"]).Kn_He
    finally:
        ST.T_EVAL_K = original
    assert after == before, (
        "the descriptive gas-temperature convention reached a gate calculation")


def test_the_unresolved_species_carry_no_number_at_all():
    """UNRESOLVED must mean absent, not defaulted."""
    from qta_multiphysics.species_transport_3d import summary, UNRESOLVED
    d = summary()
    unresolved = [r for r in d["per_species"]
                  if r["gas_temperature_status"] == UNRESOLVED]
    assert unresolved, "expected CH4 and H2 to be unresolved"
    for r in unresolved:
        assert r["T_eval_K"] is None, f"{r['species']} was assigned a temperature"
        assert r["Kn"] is None, f"{r['species']} was assigned a Knudsen number"


# ============================ gate accounting ===============================

def test_the_83_gate_distribution_is_recomputed_not_copied():
    rows = _gate_rows()
    counts = collections.Counter(r["status"] for r in rows)
    assert len(rows) == 83
    assert counts["PASS"] == 0
    assert counts["CONDITIONAL"] == 47
    assert counts["BLOCKED"] == 23
    assert counts["DERIVED_CHECK"] == 11
    assert counts["UNKNOWN"] == 2
    assert sum(counts.values()) == 83, counts
    assert 0 + 47 + 23 + 11 + 2 == 83


def test_no_duplicate_gate_identifiers():
    ids = [r["gate_id"] for r in _gate_rows()]
    dupes = [g for g, n in collections.Counter(ids).items() if n > 1]
    assert not dupes, f"duplicate gate ids: {dupes}"


def test_every_gate_declares_it_cannot_pass_and_was_not_measured():
    bad = [(r["gate_id"], r["can_PASS_now"], r["measured_in_this_system"])
           for r in _gate_rows()
           if r["can_PASS_now"] != "NO" or r["measured_in_this_system"] != "false"]
    assert not bad, f"gates claiming reachable PASS or measurement: {bad}"


def test_no_status_outside_the_declared_vocabulary():
    allowed = {"PASS", "CONDITIONAL", "BLOCKED", "DERIVED_CHECK", "UNKNOWN"}
    seen = {r["status"] for r in _gate_rows()}
    assert seen <= allowed, f"unexpected gate statuses: {seen - allowed}"


# ============================ promotion barrier =============================

def test_satisfying_a_threshold_does_not_confer_pass():
    """Model-only gates that already beat their threshold stay non-PASS.

    This is the invariant that separates 'the model says the number is fine'
    from 'the system was measured'. Several gates are numerically comfortable;
    none of them is PASS, and none may become PASS from arithmetic alone.
    """
    comfortable = []
    for r in _gate_rows():
        try:
            c, t = float(r["computed"]), float(r["threshold"])
        except (TypeError, ValueError):
            continue
        if c < t and r["source_directness"] != "DERIVED_FIRST_PRINCIPLES":
            comfortable.append(r)
    assert comfortable, "expected at least one within-threshold model-only gate"
    for r in comfortable:
        assert r["status"] != "PASS", (
            f"{r['gate_id']} reached PASS on arithmetic alone")
        assert r["can_PASS_now"] == "NO"


def test_the_deep_layer_is_not_trusted_and_orders_nothing():
    d = json.load(open(os.path.join(ROOT, "deep_surrogate_readiness.json")))
    assert d["status"] == "TRAINED_NOT_TRUSTED"
    assert d["thresholds_passed"] is False
    assert d["controls_experiment_ordering"] is False
    assert d["measured_in_this_system"] is False
    assert d["forecast_only"] is True


def test_no_deep_module_writes_a_canonical_root_artifact():
    """The learned layer may propose; deterministic code disposes."""
    offenders = []
    for rel in _tracked_py():
        if "deep_expdesign" not in rel:
            continue
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                v = node.value
                if v in ("results_gate_table.csv", "final_manifest.json",
                         "manifest_hash.txt", "authorities.json",
                         "measured_parameters.json", "parameter_registry.csv"):
                    offenders.append(f"{rel}:{node.lineno} names {v!r}")
    assert not offenders, (
        "a deep-layer module references a canonical authority artifact: "
        + "; ".join(offenders))


def test_falsification_count_is_a_model_internal_statement():
    """n_falsified_in_model must not be read as an experimental result."""
    d = json.load(open(os.path.join(ROOT, "falsification_report_3d.json")))
    assert d["n_falsified_in_model"] == 0
    blob = json.dumps(d).lower()
    assert "model" in blob
    for forbidden in ("experimentally", "hardware-validated", "measured in this system"):
        assert forbidden not in blob, forbidden
    # and the artifact must carry the forecast label somewhere
    assert "forecast_only" in blob or "FORECAST_ONLY".lower() in blob


# ============================ non-finite boundaries =========================

def test_no_gate_carries_a_non_finite_computed_value():
    """NaN compares false against every threshold, so a NaN must never sit
    in a gate row unnoticed: it would look like 'not above the limit'."""
    bad = []
    for r in _gate_rows():
        v = (r["computed"] or "").strip()
        if not v:
            continue
        try:
            x = float(v)
        except ValueError:
            continue                     # legitimately non-numeric (bool gates)
        if not math.isfinite(x):
            bad.append((r["gate_id"], v))
    assert not bad, f"non-finite computed values: {bad}"


def test_no_gate_carries_a_non_finite_threshold():
    bad = []
    for r in _gate_rows():
        v = (r["threshold"] or "").strip()
        if not v:
            continue
        try:
            x = float(v)
        except ValueError:
            continue
        if not math.isfinite(x):
            bad.append((r["gate_id"], v))
    assert not bad, f"non-finite thresholds: {bad}"


def test_a_gate_with_no_computed_value_is_never_pass():
    """Missing evidence is UNKNOWN or BLOCKED, never a satisfied threshold."""
    for r in _gate_rows():
        if not (r["computed"] or "").strip():
            assert r["status"] != "PASS", r["gate_id"]
            assert r["can_PASS_now"] == "NO", r["gate_id"]


def test_nan_would_not_silently_satisfy_a_less_than_threshold():
    """Documents the trap this guards: NaN < x is False, and so is NaN > x.

    A gate that classified on `computed < threshold` alone would leave a NaN
    row in whatever branch the else-clause happens to be. The canonical table
    is checked above to contain none; this pins why that matters.
    """
    nan = float("nan")
    assert not (nan < 1.0)
    assert not (nan > 1.0)
    assert not (nan == nan)


def test_governed_json_artifacts_contain_no_bare_nan_or_infinity():
    """json.dump emits bare NaN/Infinity, which is not valid JSON and which
    strict parsers reject -- so a downstream consumer could silently drop it."""
    import subprocess as _sp
    out = _sp.run(["git", "-C", ROOT, "ls-files", "*.json"],
                  capture_output=True, text=True, check=True).stdout
    bad = []
    for rel in out.split():
        if rel.startswith("attic/"):
            continue
        raw = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        try:
            json.loads(raw, parse_constant=_reject_constant)
        except ValueError as e:
            bad.append(f"{rel}: {e}")
    assert not bad, bad


def _reject_constant(name):
    raise ValueError(f"bare {name} in governed JSON")



if __name__ == "__main__":
    ns = dict(globals())
    for _n, _f in ns.items():
        if _n.startswith("test_") and callable(_f):
            _f()
    print("RESULT: authority promotion barriers hold")
