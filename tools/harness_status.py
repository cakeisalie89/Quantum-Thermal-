#!/usr/bin/env python3
"""The Scientific-AI harness's completion status, DERIVED -- never written.

``docs/harness_contract.json`` is the definition of done: which stack
elements must be in which state and which matrix rows back each, that every
matrix row must be complete with hosted evidence covering its current
implementation, which audits must be clean, which facts about the learned
substrate and the legacy gate table must hold, and what lies outside a
software-completion claim. It states no status.

This tool derives the status from the authorities that hold the facts --
``stack.json``, ``docs/completion_matrix.json`` (its evidence state recomputed
from implementation digests by ``tools/completion_matrix.py``, not read from
prose), the audits RE-RUN here, ``docs/neural/current_status.json`` and
``docs/neural/claims.json`` -- and writes ``docs/harness_status.json`` and
``HARNESS_STATUS.md``. The outcome is exactly one of the contract's three:

* COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT -- nothing unsatisfied;
* BLOCKED -- everything unsatisfied is EXTERNALLY_BLOCKED, nothing
  actionable remains;
* INCOMPLETE -- anything else. A STAGED, DEFERRED or unclassified element
  (one the contract requires that stack.json lacks, or one stack.json has
  that the contract does not mention), a row below complete, evidence that
  was never run or predates its implementation, or an audit finding each
  keep it here.

Deterministic: the same inputs give the same bytes, and the JSON records the
digest of every input it read, so ``verify`` recomputes and compares.

    python tools/harness_status.py derive    # write both documents
    python tools/harness_status.py verify    # exit 1 unless they are current
    python tools/harness_status.py verify --require-complete
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import completion_matrix as CM  # noqa: E402

SCHEMA = "harness-status/1"
CONTRACT = Path("docs") / "harness_contract.json"
STACK = Path("stack.json")
MATRIX = Path("docs") / "completion_matrix.json"
NEURAL_STATUS = Path("docs") / "neural" / "current_status.json"
NEURAL_CLAIMS = Path("docs") / "neural" / "claims.json"
OUT_JSON = Path("docs") / "harness_status.json"
OUT_MD = Path("HARNESS_STATUS.md")
COMPLETE = "COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT"
BLOCKED = "BLOCKED"
INCOMPLETE = "INCOMPLETE"
EXTERNALLY_BLOCKED = "EXTERNALLY_BLOCKED"


def _read(rel: Path, root: Path) -> bytes:
    return (root / rel).read_bytes()


def _json(rel: Path, root: Path) -> dict:
    return json.loads(_read(rel, root).decode("utf-8"))


def _row_states(matrix: dict, root: Path) -> dict:
    def read(p):
        q = root / p
        return q.read_bytes() if q.is_file() else None

    def listdir(p):
        q = root / p
        return (sorted(str(x.relative_to(root)) for x in q.rglob("*")
                       if x.is_file()) if q.is_dir() else [])
    out = {}
    for r in matrix["rows"]:
        out[r["id"]] = {
            "classification": r["classification"],
            "evidence": CM.evidence_state(r, read, listdir),
            "residual_gaps": len(r.get("residual_gaps") or []),
            "research_frontier": sorted(
                f.get("outside", "") for f in r.get("research_frontier")
                or [] if isinstance(f, dict)),
        }
    return out


def _row_ok(st: dict) -> bool:
    return st["classification"] == CM.COMPLETE and \
        st["evidence"] == CM.EV_COVERS and st["residual_gaps"] == 0


def audits(root: Path) -> dict:
    """Each audit re-run, reduced to a verdict and the counts it rests on."""
    import claims_enforcement
    import framework_boundary
    import neural_legacy_audit
    import pass_semantics_audit
    import workflow_contract
    out = {}
    pa = pass_semantics_audit.audit(root)
    committed = json.loads((root / pass_semantics_audit.OUT).read_text(
        encoding="utf-8"))
    out["pass_semantics"] = {
        "ok": pa["current_ai_semantic_leaks"] == 0 and pa["unclassified"] == 0
        and committed == pa,
        "current_ai_semantic_leaks": pa["current_ai_semantic_leaks"],
        "unclassified": pa["unclassified"],
        "committed_report_current": committed == pa}
    na = neural_legacy_audit.audit()
    out["neural_legacy_semantics"] = {
        "ok": na["active_neural_semantic_leaks"] == 0
        and na["active_generic_semantic_leaks"] == 0
        and na["unclassified"] == 0,
        "active_neural_semantic_leaks": na["active_neural_semantic_leaks"],
        "active_generic_semantic_leaks":
            na["active_generic_semantic_leaks"],
        "unclassified": na["unclassified"]}
    fb = framework_boundary.Boundary().problems()
    out["framework_boundary"] = {"ok": not fb, "problems": len(fb)}
    cb = claims_enforcement.problems()
    out["claims_boundary"] = {"ok": not cb, "problems": len(cb)}
    wc = workflow_contract.problems()
    out["workflow_contract"] = {"ok": not wc, "problems": len(wc)}
    return out


def facts(root: Path) -> dict:
    st = _json(NEURAL_STATUS, root)
    cl = _json(NEURAL_CLAIMS, root)
    lg = st["legacy_qta_hardware_forecast"]
    fl = st["flagship"]
    return {
        "legacy_qta_pass_count_is_legacy_only": {
            "ok": lg["classification"] == "LEGACY_QTA_ONLY",
            "legacy_pass_count": lg["PASS_count"],
            "classification": lg["classification"]},
        "learned_outputs_refused": {
            "ok": cl["learned_refused_states"] == ["PROMOTED", "VERIFIED"]
            and cl["acceptance_attempt"]["refused"] is True,
            "refused_states": cl["learned_refused_states"]},
        "flagship_not_allocated_or_trained": {
            "ok": fl["real_weights_allocated"] is False
            and fl["large_model_trained"] is False,
            "real_weights_allocated": fl["real_weights_allocated"],
            "large_model_trained": fl["large_model_trained"]},
    }


def derive(root: Path = ROOT, *, audit_results: dict | None = None) -> dict:
    contract = _json(CONTRACT, root)
    stack = _json(STACK, root)
    matrix = _json(MATRIX, root)
    rows = _row_states(matrix, root)
    elements = {e["id"]: e for e in stack["elements"]}
    unsatisfied = []
    stack_out = {}
    for eid, req in sorted(contract["required_stack"].items()):
        el = elements.get(eid)
        status = el["status"] if el else "UNCLASSIFIED"
        backing = {r: rows.get(r, {"classification": "ABSENT",
                                   "evidence": CM.EV_NEVER_RUN,
                                   "residual_gaps": 0})
                   for r in req["rows"]}
        ok = status in req["accept"] and all(_row_ok(b) for b in
                                             backing.values())
        stack_out[eid] = {"status": status, "accept": req["accept"],
                          "rows": backing, "satisfied": ok}
        if not ok:
            unsatisfied.append({"kind": "stack", "id": eid,
                                "status": status,
                                "external": status == EXTERNALLY_BLOCKED})
    for eid in sorted(elements):
        if eid not in contract["required_stack"]:
            unsatisfied.append({"kind": "stack", "id": eid,
                                "status": "UNCLASSIFIED: in stack.json, "
                                          "not in the contract",
                                "external": False})
    row_ids = sorted(rows) if contract["required_rows"] == "ALL" \
        else contract["required_rows"]
    for rid in row_ids:
        st = rows[rid]
        if not _row_ok(st):
            unsatisfied.append({"kind": "row", "id": rid,
                                "status": f"{st['classification']} / "
                                          f"{st['evidence']} / "
                                          f"{st['residual_gaps']} gaps",
                                "external": st["classification"]
                                == EXTERNALLY_BLOCKED})
    au = audit_results if audit_results is not None else audits(root)
    for name in contract["required_audits"]:
        if not au.get(name, {}).get("ok"):
            unsatisfied.append({"kind": "audit", "id": name,
                                "status": "finding", "external": False})
    fa = facts(root)
    for name in contract["required_facts"]:
        if not fa.get(name, {}).get("ok"):
            unsatisfied.append({"kind": "fact", "id": name,
                                "status": "does not hold", "external": False})
    if not unsatisfied:
        outcome = COMPLETE
    elif all(u["external"] for u in unsatisfied):
        outcome = BLOCKED
    else:
        outcome = INCOMPLETE
    counts: dict = {}
    for st in rows.values():
        counts[st["classification"]] = counts.get(st["classification"], 0) + 1
    ev: dict = {}
    for st in rows.values():
        ev[st["evidence"]] = ev.get(st["evidence"], 0) + 1
    sources = {str(p): hashlib.sha256(_read(p, root)).hexdigest()
               for p in (CONTRACT, STACK, MATRIX, NEURAL_STATUS,
                         NEURAL_CLAIMS)}
    return {
        "schema": SCHEMA,
        "outcome": outcome,
        "derived_from": sources,
        "stack": stack_out,
        "matrix": {"rows": len(rows), "by_classification": dict(sorted(
            counts.items())), "by_evidence": dict(sorted(ev.items()))},
        "audits": au,
        "facts": fa,
        "unsatisfied": unsatisfied,
        "outside_this_software_completion":
            contract["outside_this_software_completion"],
        # which rows name each outside item for what they still lack
        "research_frontier": {
            o["id"]: sorted(rid for rid, st in rows.items()
                            if o["id"] in st.get("research_frontier", ()))
            for o in contract["outside_this_software_completion"]},
    }


def render(st: dict) -> str:
    lines = ["# Scientific-AI harness: completion status", "",
             "Derived by `tools/harness_status.py` from `stack.json`, the "
             "completion matrix (evidence recomputed from implementation "
             "digests), the audits re-run, and the learned-model status, "
             "against the definition of done in "
             "`docs/harness_contract.json`. Not written by hand; `verify` "
             "fails if this file differs from what the inputs give.", "",
             f"**Outcome: {st['outcome']}**", "",
             "## Required stack", "", "| element | status | backing rows | "
             "satisfied |", "|---|---|---|---|"]
    for eid, e in st["stack"].items():
        rows = ", ".join(f"{r} ({b['classification'][:8]}, "
                         f"{b['evidence'].split('_')[0]})"
                         for r, b in e["rows"].items())
        lines.append(f"| {eid} | {e['status']} | {rows} | "
                     f"{'yes' if e['satisfied'] else 'NO'} |")
    m = st["matrix"]
    lines += ["", "## Completion matrix", "",
              f"{m['rows']} rows. By classification: "
              + ", ".join(f"{k} {v}" for k, v in m["by_classification"]
                          .items())
              + ". By hosted evidence: "
              + ", ".join(f"{k} {v}" for k, v in m["by_evidence"].items())
              + ".", "", "## Audits", ""]
    for name, a in st["audits"].items():
        counts = ", ".join(f"{k} {v}" for k, v in a.items() if k != "ok")
        lines.append(f"* {name}: {'clean' if a['ok'] else 'FINDING'} "
                     f"({counts})")
    lines += ["", "## Facts", ""]
    for name, f in st["facts"].items():
        lines.append(f"* {name}: {'holds' if f['ok'] else 'DOES NOT HOLD'}")
    lines += ["", f"## Unsatisfied ({len(st['unsatisfied'])})", ""]
    for u in st["unsatisfied"]:
        lines.append(f"* {u['kind']} {u['id']}: {u['status']}"
                     + (" -- EXTERNALLY_BLOCKED" if u["external"] else ""))
    if not st["unsatisfied"]:
        lines.append("* none")
    lines += ["", "## Outside this software-completion claim", ""]
    cited = st.get("research_frontier", {})
    for o in st["outside_this_software_completion"]:
        by = cited.get(o["id"]) or []
        lines.append(f"* {o['id']} ({o['class']}): {o['statement']}"
                     + (f" -- the research frontier of {', '.join(by)}"
                        if by else ""))
    lines.append("")
    return "\n".join(lines)


def documents(st: dict) -> dict:
    """The two committed documents, exactly as ``derive`` writes them."""
    return {OUT_JSON: json.dumps(st, indent=1, sort_keys=True) + "\n",
            OUT_MD: render(st)}


def stale(root: Path, st: dict) -> list:
    """The committed documents that are not what the inputs give."""
    out = []
    for rel, want in documents(st).items():
        p = root / rel
        if not p.is_file() or p.read_text(encoding="utf-8") != want:
            out.append(str(rel))
    return out


def verify(root: Path = ROOT, *, require_complete: bool = False,
           audit_results: dict | None = None) -> tuple:
    st = derive(root, audit_results=audit_results)
    problems = [f"{rel} is not what the inputs give: run derive"
                for rel in stale(root, st)]
    if require_complete and st["outcome"] != COMPLETE:
        problems.append(f"REQUIRED: COMPLETE, derived {st['outcome']}")
    return st, problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("derive")
    v = sub.add_parser("verify")
    v.add_argument("--require-complete", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "derive":
        st = derive()
        for rel, text in documents(st).items():
            (ROOT / rel).write_text(text, encoding="utf-8")
        print(f"harness status: {st['outcome']} "
              f"({len(st['unsatisfied'])} unsatisfied)")
        return 0
    st, problems = verify(require_complete=args.require_complete)
    for line in problems:
        print(line)
    print(f"harness status: {st['outcome']} "
          f"({len(st['unsatisfied'])} unsatisfied)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
