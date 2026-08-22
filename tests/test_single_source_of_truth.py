"""§31: detect duplicate or conflicting definitions of governed concepts.

authorities.json registers exactly one executable authority per concept and
says every other file is a consumer. Nothing enforced that. This module scans
the tracked Python sources for literals that restate a registered authority's
value and flags any occurrence that has no declared derivation relationship.

A duplicate constant is not automatically wrong -- §31 says so explicitly --
so each known duplicate is listed in DECLARED_DERIVATIONS with the reason it
is allowed. The test fails on an UNDECLARED one. That is the property that was
missing: metrics.py carried its own literal Mode-D readiness threshold, and
nothing would have noticed if config.SolverConfig had changed.

MODEL-ONLY / FORECAST-ONLY. Software verification; not a hardware statement.
"""
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qta_multiphysics.config import default_config              # noqa: E402


def _tracked_py():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.py"],
                         capture_output=True, text=True, check=True).stdout
    return [f for f in out.split()
            if f and not f.startswith(("tests/", "attic/"))]


def _hits(pattern):
    """(file, lineno, line) for every match in tracked non-test sources."""
    rx = re.compile(pattern, re.I)
    out = []
    for rel in _tracked_py():
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if rx.search(line):
                out.append((rel, i, line.strip()))
    return out


# Each entry: concept -> (registered authority, regex for a restating literal,
#                         {file: why this duplicate is legitimate})
DECLARED_DERIVATIONS = {
    "base_temperature_K": {
        "authority": "qta_multiphysics/config.py :: FridgeConfig.T_fridge_K",
        "pattern": r"\bT_(?:fridge|base|eval|ref)\w*\s*(?::\s*float\s*)?=\s*0?\.0?10?\b",
        "allowed": {
            "qta_multiphysics/config.py":
                "the authority itself",
            "qta_multiphysics/species_transport_3d.py":
                "T_EVAL_K, documented as the Mode-D sensing-stage temperature, "
                "with its per-species applicability declared alongside it "
                "(see GAS_TEMPERATURE_SEMANTICS)",
            "qta_multiphysics/nv_spin/model.py":
                "T_ref_K default for the ZFS temperature expansion: a "
                "reference point for a linear expansion, not a second "
                "definition of the stage temperature",
            "qta_full_sim.py":
                "ModeStateVector(T_fridge_K=...) for MODE_D_SENSE. A genuine "
                "duplicate, kept because the state vector is a self-contained "
                "record; the derivation relationship is that it MUST equal "
                "FridgeConfig.T_fridge_K, and "
                "test_duplicated_values_equal_their_authority enforces it",
        },
    },
    "post_bakeout_NEG_H2_pressure_Pa": {
        "authority": "qta_full_sim.py :: P_H2_POST_BAKEOUT_NEG_PA",
        "pattern": r"\bP_H2\w*\s*(?::\s*float\s*)?=\s*1\.?0?e-12\b"
                   r"|\"P_H2_Pa\"\s*:\s*1\.?0?e-12\b"
                   r"|lambda m, c: 1\.0e-12",
        "allowed": {
            "qta_full_sim.py":
                "the authority itself",
            "qta_multiphysics/cryopanel_dynamics_3d.py":
                "P_H2_RESIDUAL_PA. No qta_multiphysics module imports "
                "qta_full_sim -- the package deliberately does not depend on "
                "the top-level script, whose import executes a full run -- so "
                "the modelled post-bakeout+NEG pressure is restated here. The "
                "derivation relationship is that it MUST equal "
                "P_H2_POST_BAKEOUT_NEG_PA, enforced below",
            "qta_multiphysics/machine_fsm.py":
                "base_ctx['P_H2_Pa'], the FSM's post-bakeout context. Same "
                "dependency-direction reason; same equality requirement",
            "qta_multiphysics/campaign_state_3d.py":
                "the campaign readiness context's P_H2_Pa. Same "
                "dependency-direction reason; same equality requirement",
            "qta_multiphysics/measurement_ingest_3d.py":
                "the P_H2_Pa diagnostic predictor. Same dependency-direction "
                "reason; same equality requirement",
        },
    },
    "mode_d_readiness_threshold_K": {
        "authority": "qta_multiphysics/config.py :: SolverConfig."
                     "mode_d_temp_threshold_K",
        "pattern": r"\b(?:th|thresh\w*|mode_d_temp_threshold_K)\s*(?::\s*float\s*)?=\s*0\.0?50\b",
        "allowed": {
            "qta_multiphysics/config.py": "the authority itself",
        },
    },
    "solver_rtol": {
        "authority": "qta_multiphysics/config.py :: SolverConfig.rtol",
        "pattern": r"\brtol\s*(?::\s*float\s*)?=\s*1\.?0?e-0?6\b",
        "allowed": {
            "qta_multiphysics/config.py": "the authority itself",
            "qta_multiphysics/gas_transport_1d.py":
                "solve_gas_transport_1d(rtol=...) keyword default. A genuine "
                "duplicate; the derivation relationship is that it MUST equal "
                "SolverConfig.rtol, enforced by "
                "test_duplicated_values_equal_their_authority",
        },
    },
}


def test_no_undeclared_duplicate_of_a_registered_authority_value():
    problems = []
    for concept, spec in DECLARED_DERIVATIONS.items():
        for rel, line_no, line in _hits(spec["pattern"]):
            if rel not in spec["allowed"]:
                problems.append(
                    f"{concept}: {rel}:{line_no} restates the value registered "
                    f"to {spec['authority']} with no declared derivation\n"
                    f"    {line}")
    assert not problems, "\n".join(problems)


def test_declared_derivations_are_still_real():
    """An allowlist entry for a file that no longer restates the value is
    itself drift -- it would hide a future reintroduction."""
    stale = []
    for concept, spec in DECLARED_DERIVATIONS.items():
        seen = {rel for rel, _, _ in _hits(spec["pattern"])}
        for rel in spec["allowed"]:
            if rel not in seen:
                stale.append(f"{concept}: {rel} is allowlisted but no longer "
                             "restates the value")
    assert not stale, "\n".join(stale)


# ------------------------------- concepts with a single canonical inventory --

def test_gate_identifiers_agree_between_table_and_registry():
    import csv
    ids = [r["gate_id"] if "gate_id" in r else r.get("gid") or r.get("id")
           for r in csv.DictReader(open(ROOT / "results_gate_table.csv",
                                        newline=""))]
    ids = [i for i in ids if i]
    assert len(ids) == len(set(ids)), "duplicate gate identifiers in the table"
    assert len(ids) == 83, f"gate count drifted to {len(ids)}"


def test_pass_semantics_are_zero_everywhere_they_are_stated():
    import csv
    rows = list(csv.DictReader(open(ROOT / "results_gate_table.csv", newline="")))
    assert sum(r["status"] == "PASS" for r in rows) == 0
    manifest = json.loads((ROOT / "final_manifest.json").read_text())
    assert manifest["canonical_state"]["PASS"] == 0
    assert manifest["canonical_state"]["all_can_PASS_now"] == "NO"


def test_heat_switch_semantics_have_one_meaning():
    """IL-04: sensing requires the SC heat switch OPEN, in every path."""
    from qta_multiphysics.machine_fsm import INTERLOCKS
    from qta_multiphysics.state_machine_3d import device_state
    il04 = {i.id: i for i in INTERLOCKS}["IL-04"]
    assert "not OPEN" in il04.description, il04.description
    assert device_state("MODE_D").heat_switch_state == "OPEN"


def test_mode_species_assignments_have_one_authority():
    from qta_multiphysics import mode_sequence_3d as M
    b = set(M.CANONICAL_ACTIVE.get("MODE_B", ()))
    d = set(M.CANONICAL_ACTIVE.get("MODE_D", ()))
    assert b == {"C13_CH4"}, b
    assert d == {"He3", "He4"}, d
    assert not (b & d)


def test_duplicated_values_equal_their_authority():
    """A declared duplicate is only legitimate while it still agrees.

    Allowlisting a duplicate records that it exists; this asserts it has not
    drifted from the value it duplicates. Without this the allowlist would be
    a way to hide exactly the defect §31 is about.
    """
    import inspect
    from qta_multiphysics import gas_transport_1d
    cfg = default_config()

    sig = inspect.signature(gas_transport_1d.solve_gas_transport_1d)
    assert sig.parameters["rtol"].default == cfg.solver.rtol, (
        "gas_transport_1d rtol default has drifted from SolverConfig.rtol")

    src = (ROOT / "qta_full_sim.py").read_text(encoding="utf-8")
    m = re.search(r"T_fridge_K\s*=\s*([0-9.eE+-]+)", src)
    assert m, "T_fridge_K restatement not found in qta_full_sim.py"
    assert float(m.group(1)) == cfg.fridge.T_fridge_K, (
        f"qta_full_sim T_fridge_K={m.group(1)} has drifted from "
        f"FridgeConfig.T_fridge_K={cfg.fridge.T_fridge_K}")

    # Post-bakeout+NEG H2 pressure: one authority, four package-side restatements
    import qta_full_sim as Q
    canonical = Q.P_H2_POST_BAKEOUT_NEG_PA
    from qta_multiphysics import cryopanel_dynamics_3d
    assert cryopanel_dynamics_3d.P_H2_RESIDUAL_PA == canonical, (
        f"cryopanel P_H2_RESIDUAL_PA={cryopanel_dynamics_3d.P_H2_RESIDUAL_PA} "
        f"has drifted from P_H2_POST_BAKEOUT_NEG_PA={canonical}")
    # Found by the §14 AST scanner rather than a regex, so a restatement in a
    # context the regex did not anticipate (a lambda body, a dict value on its
    # own line) cannot slip past.
    sys.path.insert(0, str(ROOT / "tests"))
    from test_h2_pressure_authority import scan_bare_h2_pressure_literals
    for rel in ("qta_multiphysics/machine_fsm.py",
                "qta_multiphysics/campaign_state_3d.py",
                "qta_multiphysics/measurement_ingest_3d.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        found = scan_bare_h2_pressure_literals(text)
        assert found, f"no P_H2 restatement found in {rel}"
        for lineno, value, anchor in found:
            assert value == canonical, (
                f"{rel}:{lineno} restates {anchor}={value}, which has drifted "
                f"from P_H2_POST_BAKEOUT_NEG_PA={canonical}")


def test_governed_output_count_has_one_source():
    """88 governed outputs: hdf5_output_mapping.json is the inventory.

    The number appears in prose in several documents; it must be derived from
    one place, and the inventory must be self-consistent.
    """
    mapping = json.loads((ROOT / "hdf5_output_mapping.json").read_text())
    assert len(mapping["outputs"]) == mapping["n_governed"] == 88
    assert (mapping["n_csv_tables"] + mapping["n_json_native"]
            + len(mapping["exempt"])) == mapping["n_governed"], (
        f'{mapping["n_csv_tables"]} csv + {mapping["n_json_native"]} json + '
        f'{len(mapping["exempt"])} exempt != {mapping["n_governed"]} governed')


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
                print(f"FAIL {name}: {str(e)[:300]}")
    raise SystemExit(1 if fails else 0)
