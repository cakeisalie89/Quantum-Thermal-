#!/usr/bin/env python3
"""The production-readiness matrix, held to the tree and summarized.

docs/security/readiness_matrix.json says, capability by capability, whether
it is IMPLEMENTED (built, tested locally), HOSTED_TESTED (a hosted run at a
commit with this implementation passed), DEPLOYMENT_REQUIRED (a deployment
must provide it; DEPLOYMENT.md says how to check), EXTERNALLY_BLOCKED (an
owner action or outside credential) or UNVERIFIED. This refuses a matrix
that cites a test, a specification or a ledger record that does not exist,
a HOSTED_TESTED row without the runs and commit that make it so, a blocked
row that names no owner action, and a deployment row that does not point at
the deployment contract. The summary says whether the contract is met; it
is not met while any row is EXTERNALLY_BLOCKED or UNVERIFIED.

    python tools/readiness.py check
    python tools/readiness.py summary
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "docs" / "security" / "readiness_matrix.json"
LEDGER = ROOT / "docs" / "DEFECT_LEDGER.md"
STATUSES = ("IMPLEMENTED", "HOSTED_TESTED", "DEPLOYMENT_REQUIRED",
            "EXTERNALLY_BLOCKED", "UNVERIFIED")


def load(path: Path = MATRIX) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def problems(doc: dict, root: Path = ROOT) -> list:
    out = []
    if set(doc.get("statuses", {})) != set(STATUSES):
        out.append(f"statuses are {sorted(doc.get('statuses', {}))}, not "
                   f"{sorted(STATUSES)}")
    ledger = (root / LEDGER.relative_to(ROOT)).read_text(encoding="utf-8")
    seen = set()
    for r in doc.get("rows", []):
        rid = r.get("id")
        if rid in seen:
            out.append(f"{rid}: listed twice")
        seen.add(rid)
        st = r.get("status")
        if st not in STATUSES:
            out.append(f"{rid}: status {st!r}")
            continue
        for p in r.get("tests", []) + r.get("mutation_specs", []):
            if not (root / p).is_file():
                out.append(f"{rid}: cites {p}, which does not exist")
        if not re.search(rf"^## {re.escape(r.get('ledger', ''))} ", ledger,
                         re.MULTILINE):
            out.append(f"{rid}: ledger record {r.get('ledger')!r} is not "
                       "in the defect ledger")
        note = r.get("note", "")
        if st == "HOSTED_TESTED":
            h = r.get("hosted") or {}
            if not h.get("runs") or not re.fullmatch(
                    r"[0-9a-f]{40}", str(h.get("commit", ""))):
                out.append(f"{rid}: HOSTED_TESTED names no hosted runs at a "
                           "commit")
        if st in ("IMPLEMENTED", "HOSTED_TESTED") and not r.get("tests"):
            out.append(f"{rid}: {st} cites no test")
        if st == "EXTERNALLY_BLOCKED" and "owner action" not in note \
                and "production_registry" not in note:
            out.append(f"{rid}: EXTERNALLY_BLOCKED names no owner action or "
                       "external dependency")
        if st == "DEPLOYMENT_REQUIRED" and "DEPLOYMENT.md" not in note \
                and "cgroups" not in note:
            out.append(f"{rid}: DEPLOYMENT_REQUIRED does not point at the "
                       "deployment contract")
    return out


def summary(doc: dict) -> dict:
    counts = {s: 0 for s in STATUSES}
    for r in doc["rows"]:
        counts[r["status"]] += 1
    met = counts["EXTERNALLY_BLOCKED"] == 0 and counts["UNVERIFIED"] == 0 \
        and counts["IMPLEMENTED"] == 0
    return {"rows": len(doc["rows"]), "by_status": counts,
            "contract_met": met,
            "verdict": ("PRODUCTION_READINESS_CONTRACT_MET" if met else
                        "PRODUCTION_READINESS_CONTRACT_NOT_MET")}


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    doc = load()
    found = problems(doc)
    if args[:1] == ["check"]:
        for f in found:
            print(f"  - {f}")
        print("readiness matrix " + ("holds" if not found else
                                     f"BROKEN ({len(found)} problem(s))"))
        return 1 if found else 0
    s = summary(doc)
    print(json.dumps(s, indent=1, sort_keys=True))
    return 0 if not found else 1


if __name__ == "__main__":
    sys.exit(main())
