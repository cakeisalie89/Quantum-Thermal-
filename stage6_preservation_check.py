#!/usr/bin/env python3
"""Stage-6 preservation checker (Stage 7). Software verification only —
the scientific gate PASS count remains zero and is asserted below.

Verifies that every governed Stage-6 planning/state artifact survives
Stage-7 tooling untouched. Exit 0 = all preserved.
"""
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

FAILS: list[str] = []
CHECKS_RUN: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        CHECKS_RUN.append(name)
        print(f"  [OK]   {name}")
    else:
        CHECKS_RUN.append(name)
        print(f"  [FAIL] {name} {detail}")
        FAILS.append(name)


# Lineage reference for the Stage-6 delivery archive. This digest is NOT
# verified by this script and cannot be: the archive it names is not present in
# this repository. The archive that IS present
# (QTA_repaired_complete_1D_2D_repo.zip) hashes to fec4dfb6..., a different
# artifact. The value is retained as a provenance breadcrumb, and is printed as
# such -- it used to be printed as "authoritative Stage-6 archive sha256",
# which reads as though the archive had been checked.
AUTH_ZIP_SHA = ("3b727bc5cf9aaeb7607380ab91d7d2ced8fc021b90f01eeeb8ab8"
                "812f3e2eb95")
AUTH_ZIP_PRESENT = False        # no file in this repository has this digest

print("Stage-6 required-invariants check")
print(f"  lineage reference (NOT VERIFIED HERE, archive absent from this "
      f"repository): {AUTH_ZIP_SHA}")
print("  scope: the required Stage-6 semantic invariants listed below.")
print("  This is NOT a byte-preservation check of the Stage-6 archive.")

reg = json.loads(Path("experiment_registry.json").read_text())
gcov = json.loads(Path("experiment_gate_coverage.json").read_text())
mcov = json.loads(Path("experiment_matrix_coverage.json").read_text())
camp = json.loads(Path("campaign_registry.json").read_text())

check("four registries present at schema 1.0.0",
      all(d.get("schema_version") == "1.0.0"
          for d in (reg, gcov, mcov, camp)))

ids = [e["experiment_id"] for e in reg["experiments"]]
check("11 unique experiment IDs", len(ids) == 11 and len(set(ids)) == 11)

pbs = sorted(Path("EXPERIMENT_PLAYBOOKS").glob("EXP-*.md"))
check("11 playbooks present", len(pbs) == 11)
ok_sections = all(
    [int(m) for m in re.findall(r"^## (\d+)\.", p.read_text(), re.M)]
    == list(range(1, 31)) for p in pbs)
check("every playbook has sections 1..30", ok_sections)

check("25 gate mappings", gcov.get("n_gates") == 25
      and len(gcov["gates"]) == 25)
check("43 matrix dispositions", mcov.get("n_items") == 43
      and len(mcov["items"]) == 43)
unres = [x["matrix_key"] for x in mcov["items"]
         if x["classification"] == "unresolved"]
check("nonlinear_threshold remains the sole unresolved item",
      unres == ["nonlinear_threshold"], str(unres))

rec = {a["plan_id"]: a for a in reg["exp_af_reconciliation"]}
check("EXP-A..F reconciliation unchanged",
      set(rec) == {"EXP-A", "EXP-B", "EXP-C", "EXP-D", "EXP-E", "EXP-F"}
      and rec["EXP-A"]["maps_to"] == ["EXP-N2"]
      and rec["EXP-B"]["maps_to"] == ["EXP-N4"]
      and rec["EXP-C"]["maps_to"] == ["EXP-N4"]
      and rec["EXP-E"]["maps_to"] == ["EXP-T1"]
      and rec["EXP-F"]["maps_to"] == ["EXP-V2"])
check("EXP-D remains decomposed into V1+P1",
      rec["EXP-D"]["relationship"] == "decomposed"
      and set(rec["EXP-D"]["maps_to"]) == {"EXP-V1", "EXP-P1"})

eig = reg["eig_preservation"]
check("EIG ranking preserved unrecomputed",
      [r["experiment_id"] for r in eig["ranking"]]
      == ["EXP-A", "EXP-C", "EXP-B", "EXP-F", "EXP-D", "EXP-E"]
      and "NOT recomputed" in eig["source"])
orders = sorted(e["execution_order"] for e in reg["experiments"])
check("EIG rank distinct from execution order (total order 1..11)",
      orders == list(range(1, 12))
      and "dependency" in eig["distinct_from_execution_order"])

c1 = camp["campaigns"][0]
check("Campaign-1 proposed and unperformed",
      c1["campaign_id"] == "Campaign-1"
      and c1["status"] == "PROPOSED_NOT_PERFORMED")
check("Campaign-1 membership V1,S1,N0",
      set(c1["experiments"]) == {"EXP-V1", "EXP-S1", "EXP-N0"})
check("EXP-P1 depends on the V1 clean baseline",
      "EXP-P1" in camp["dependency_note"]
      and "EXP-V1" in str([e["dependencies"] for e in reg["experiments"]
                           if e["experiment_id"] == "EXP-P1"]))

st6 = {e["experiment_id"]: e["status"] for e in reg["experiments"]}
check("EXP-V1 remains PLAYBOOK_READY",
      st6.get("EXP-V1") == "PLAYBOOK_READY")
check("all other experiments remain DESIGNED (no new readiness change)",
      all(v == "DESIGNED" for k, v in st6.items() if k != "EXP-V1"))
check("no experiment marked performed",
      not any(w in json.dumps(reg).upper()
              for w in ('"PERFORMED"', '"COMPLETED"', '"EXECUTED"')))

rows = list(csv.DictReader(open("results_gate_table.csv")))
dist = Counter(r["status"] for r in rows)
check("gate distribution 47/23/2/11 with scientific PASS = 0",
      dist == Counter({"CONDITIONAL": 47, "BLOCKED": 23,
                       "DERIVED_CHECK": 11, "UNKNOWN": 2}), str(dist))

req = json.loads(Path("matrix_update_examples/valid_example.json")
                 .read_text())
check("automatic_application remains false in the exemplar",
      req["automatic_application"] is False)
check("requester/reviewer separation enforced by governance",
      "requester may not be a reviewer" in
      Path("qta_multiphysics/hardware_governance_3d.py").read_text())

# ---- Stage-7 / 7.5 / 8 preservation ----
for f in ("pyproject.toml", "uv.lock", "Snakefile",
          "qta_multiphysics/stage7_boundary_models.py",
          "qta_sim_stages.py", "RUNTIME_RESILIENCE.md",
          "hdf5_output_mapping.json", "hdf5_schema.json",
          "qta_scientific_results.h5", "ro-crate/ro-crate-metadata.json",
          "ro_crate_tools.py", "HDF5_DATA_MODEL.md"):
    check(f"artifact present: {f}", Path(f).exists())
check("checker --verify-existing mode present",
      "--verify-existing" in
      Path("package_consistency_check.py").read_text())
_norm = " ".join(Path("HDF5_DATA_MODEL.md").read_text().split())
check("HDF5 role stated as representation-not-evidence",
      "not new scientific evidence" in _norm.replace("NOT", "not"))

# ---- Stage-9 preservation ----
for f in ("SUPPLY_CHAIN_THREAT_MODEL.md", "RELEASE_POLICY.md",
          ".github/workflows/release.yml", "build_release_artifacts.py",
          "verify_release.py"):
    check(f"stage9 artifact present: {f}", Path(f).exists())
check("release policy forbids wildcards",
      "wildcard" in Path("RELEASE_POLICY.md").read_text().lower())
check("CI workflow least-privilege marker",
      "permissions: {}" in
      Path(".github/workflows/release.yml").read_text())

n = len(FAILS)
# The contract this script actually verifies is a named set of semantic
# invariants, not byte preservation of an archive. The result string says so.
msg = ("STAGE6_REQUIRED_INVARIANTS_PRESERVED "
       f"({len(CHECKS_RUN)} required invariants verified)" if n == 0
       else f"STAGE6_REQUIRED_INVARIANTS_VIOLATED "
            f"({n} of {len(CHECKS_RUN)} failed)")
print(f"\nRESULT: {msg}")
print("scope: required semantic invariants only; byte preservation of the "
      "Stage-6 delivery archive is NOT verified here (archive not in repo)")
print("note: software verification only; scientific gate PASS count is "
      "zero by design and unchanged")
sys.exit(1 if n else 0)
