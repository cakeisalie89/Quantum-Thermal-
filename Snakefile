# QTA Stage-7 Snakemake workflow. Wraps the EXISTING authoritative
# commands; rewrites no scientific solver. MODEL-ONLY / FORECAST-ONLY.
# Software-verification results only — the scientific gate PASS count is
# zero and is protected by the wrapped checkers themselves.
#
# Usage:
#   snakemake -n full_verification            # dry run
#   snakemake --cores 1 full_verification     # everything
#   snakemake --cores 1 clean_workspace       # remove generated workspace
#
# Workspace: all generated artifacts go under verification/snakemake/
# (never the canonical source tree). Verification-only rules never touch
# the canonical manifest. --cores 1 is the supported invocation: canonical
# generation is single-writer by design (no parallel writes to one file).

import hashlib, json, os
from pathlib import Path

WS = "verification/snakemake"
SRC_OUTPUTS = [l.split()[1] for l in []]  # populated at rule level
EXEMPT = {"deep_surrogate_readiness.json"}

def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()

rule full_verification:
    input:
        f"{WS}/env_verified.json",
        f"{WS}/registries_validated.json",
        f"{WS}/invariants_validated.json",
        f"{WS}/tests_fast.json",
        f"{WS}/package_checker.txt",
        f"{WS}/manuscript_checker.txt",
        f"{WS}/canonical_outputs.done.json",
        f"{WS}/outputs_validated.json",
        f"{WS}/gate_table_validated.json",
        f"{WS}/roadmap_validated.txt",
        f"{WS}/manifest_verified.json",
    output:
        f"{WS}/reproducibility_report.json"
    run:
        rep = {"workflow": "stage7-snakemake", "inputs": {}}
        for f in input:
            rep["inputs"][os.path.basename(f)] = _sha(f)
        ov = json.loads(Path(f"{WS}/outputs_validated.json").read_text())
        rep["governed_output_comparison"] = ov
        rep["note"] = ("software verification only; scientific gate PASS "
                       "count remains zero")
        Path(output[0]).write_text(json.dumps(rep, indent=1, sort_keys=True))

rule env_verified:
    output: f"{WS}/env_verified.json"
    run:
        import sys, platform
        import numpy, scipy, qutip
        d = {"python": sys.version.split()[0],
             "platform": platform.machine(),
             "numpy": numpy.__version__, "scipy": scipy.__version__,
             "qutip": qutip.__version__}
        assert d["numpy"] == "2.4.4" and d["scipy"] == "1.17.1" \
            and d["qutip"] == "5.2.1", d
        Path(output[0]).write_text(json.dumps(d, indent=1, sort_keys=True))

rule registries_validated:
    input:
        "experiment_registry.json", "experiment_gate_coverage.json",
        "experiment_matrix_coverage.json", "campaign_registry.json",
        "validation_matrix_update_request.schema.json",
    output: f"{WS}/registries_validated.json"
    run:
        d = {}
        for f in input:
            doc = json.loads(Path(f).read_text())
            d[f] = {"sha256": _sha(f),
                    "schema_version": doc.get("schema_version")}
        assert all(v["schema_version"] in ("1.0.0",)
                   for k, v in d.items()
                   if not k.endswith("schema.json"))
        Path(output[0]).write_text(json.dumps(d, indent=1, sort_keys=True))

rule invariants_validated:
    output: f"{WS}/invariants_validated.json"
    shell:
        "python3 tests/test_mode_species_3d.py > {output}.log 2>&1 && "
        "python3 tests/test_machine_fsm.py >> {output}.log 2>&1 && "
        "python3 -c \"import json,hashlib;"
        "json.dump({{'invariant_suites': ['mode_species_3d','machine_fsm'],"
        "'log_sha256': hashlib.sha256(open('{output}.log','rb').read())"
        ".hexdigest()}}, open('{output}','w'), indent=1)\""

rule tests_fast:
    output: f"{WS}/tests_fast.json"
    shell:
        "python3 tests/test_stage6_roadmap.py > {output}.log 2>&1 && "
        "python3 tests/test_hardware_governance.py >> {output}.log 2>&1 && "
        "python3 tests/test_measurement_ingest.py >> {output}.log 2>&1 && "
        "python3 tests/test_campaign_uncertainty.py >> {output}.log 2>&1 && "
        ".venv/bin/python -m pytest "
        "tests/test_stage7_boundary.py -q >> {output}.log 2>&1 && "
        "python3 -c \"import json,hashlib;"
        "json.dump({{'suites': ['stage6_roadmap','hardware_governance',"
        "'measurement_ingest','campaign_uncertainty','stage7_boundary'],"
        "'log_sha256': hashlib.sha256(open('{output}.log','rb').read())"
        ".hexdigest()}}, open('{output}','w'), indent=1)\""

rule package_checker:
    output: f"{WS}/package_checker.txt"
    shell:
        "python3 package_consistency_check.py > {output} 2>&1 && "
        "grep -q 'RESULT: PASS' {output}"

rule manuscript_checker:
    output: f"{WS}/manuscript_checker.txt"
    shell:
        "python3 manuscript_consistency_check.py > {output} 2>&1 && "
        "grep -q 'RESULT: PASS' {output}"

rule canonical_outputs:
    # generates a FRESH output set inside the workspace via the
    # authoritative runner-based full pipeline (identical code path to
    # qta_full_sim.py, parameterized to the workspace); the canonical
    # source tree is never written.
    output: f"{WS}/canonical_outputs.done.json"
    run:
        import subprocess, sys
        od = f"{WS}/outputs"
        r = subprocess.run([sys.executable, "snakemake_sim_entry.py", od],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
        files = sorted(os.listdir(od))
        Path(output[0]).write_text(json.dumps(
            {"n_outputs": len(files),
             "hashes": {f: _sha(os.path.join(od, f)) for f in files}},
            indent=1, sort_keys=True))

rule outputs_validated:
    input: f"{WS}/canonical_outputs.done.json"
    output: f"{WS}/outputs_validated.json"
    run:
        done = json.loads(Path(input[0]).read_text())
        same = diff = 0; mism = []
        for name, h in done["hashes"].items():
            if name in EXEMPT: continue
            if Path(name).exists() and _sha(name) == h: same += 1
            else: diff += 1; mism.append(name)
        rep = {"byte_identical": same, "mismatched": diff,
               "mismatch_names": mism}
        Path(output[0]).write_text(json.dumps(rep, indent=1,
                                              sort_keys=True))
        assert diff == 0, mism

rule gate_table_validated:
    input: "results_gate_table.csv"
    output: f"{WS}/gate_table_validated.json"
    run:
        import csv
        from collections import Counter
        rows = list(csv.DictReader(open(input[0])))
        dist = Counter(r["status"] for r in rows)
        assert dist == Counter({"CONDITIONAL": 47, "BLOCKED": 23,
                                "DERIVED_CHECK": 11, "UNKNOWN": 2}), dist
        Path(output[0]).write_text(json.dumps(
            {"n_gates": len(rows), "distribution": dict(dist),
             "scientific_PASS_count": 0}, indent=1, sort_keys=True))

rule roadmap_validated:
    output: f"{WS}/roadmap_validated.txt"
    shell:
        "python3 stage6_preservation_check.py > {output} 2>&1 && "
        "grep -q 'RESULT: PRESERVED' {output}"

rule manifest_verified:
    input: "final_manifest.json", "manifest_hash.txt"
    output: f"{WS}/manifest_verified.json"
    run:
        man = json.loads(Path(input[0]).read_text())
        stored = Path(input[1]).read_text().split("sha256:")[1].split()[0]
        calc = _sha(input[0])
        bad = [e["filename"] for e in man["files"]
               if not Path(e["filename"]).exists()
               or _sha(e["filename"]) != e["sha256"]]
        rep = {"entries": len(man["files"]), "mismatches": len(bad),
               "detached_hash_ok": stored.strip() == calc}
        Path(output[0]).write_text(json.dumps(rep, indent=1,
                                              sort_keys=True))
        assert not bad and rep["detached_hash_ok"]

rule clean_workspace:
    # removes ONLY the generated workspace; never authoritative sources
    shell: "rm -rf verification/snakemake"
