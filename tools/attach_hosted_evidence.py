#!/usr/bin/env python3
"""Attach hosted CI evidence to completion-matrix rows -- only evidence that
is real, about the commit named, for the implementation as it was there.

A row's ``hosted_evidence`` records facts (runs, the commit they ran on, the
digest of the row's implementation files AT that commit); the validator
derives from them whether the evidence covers the implementation as it
stands now. This tool writes those facts and refuses to write anything it
cannot establish:

* every run is fetched from the GitHub API and must have run on EXACTLY the
  commit named (its head_sha), be completed, conclude success, and have no
  job that did not succeed -- a green summary over a skipped job is not
  evidence about that job;
* the implementation digest is computed from the commit's own tree (``git
  show``), never from the working tree, so a row whose files changed after
  the run cannot be stamped as covered;
* a row whose implementation at the commit has a path that does not exist
  there is digested with that absence, as the validator does.

It never decides a verdict. Run the validator afterwards; a row whose
implementation changed since the commit derives PREDATES, correctly.

    python tools/attach_hosted_evidence.py --commit SHA --rows R60 \\
        --runs 123,456 --note "what the runs covered"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import completion_matrix as CM  # noqa: E402

REPO = "cakeisalie89/Quantum-Thermal-"


class EvidenceRefused(RuntimeError):
    pass


def _gh(path: str) -> dict:
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if out.returncode != 0:
        raise EvidenceRefused(f"GitHub API {path}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def check_run(run_id: str, commit: str, fetch=_gh) -> dict:
    run = fetch(f"repos/{REPO}/actions/runs/{run_id}")
    if run.get("head_sha") != commit:
        raise EvidenceRefused(f"run {run_id} ran on {run.get('head_sha')}, "
                              f"not {commit}")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise EvidenceRefused(f"run {run_id} is {run.get('status')}/"
                              f"{run.get('conclusion')}, not completed/"
                              "success")
    jobs = fetch(f"repos/{REPO}/actions/runs/{run_id}/jobs?per_page=100")
    bad = [j["name"] for j in jobs.get("jobs", [])
           if j.get("conclusion") != "success"]
    if bad or not jobs.get("jobs"):
        raise EvidenceRefused(f"run {run_id}: jobs not successful: "
                              f"{bad or 'none listed'}")
    return {"id": str(run_id), "workflow": run.get("name"),
            "event": run.get("event"), "jobs": len(jobs["jobs"])}


def _at_commit(commit: str, root: Path = ROOT):
    def read(p):
        r = subprocess.run(["git", "show", f"{commit}:{p}"], cwd=root,
                           capture_output=True)
        return r.stdout if r.returncode == 0 else None

    def listdir(p):
        r = subprocess.run(["git", "ls-tree", "-r", "--name-only", commit,
                            "--", p], cwd=root, capture_output=True,
                           text=True)
        names = [n for n in r.stdout.splitlines() if n]
        return names if names and names != [p] else []
    return read, listdir


def attach(doc: dict, rows: list, commit: str, runs: list, note: str,
           fetch=_gh, root: Path = ROOT) -> list:
    full = subprocess.run(["git", "rev-parse", commit], cwd=root,
                          capture_output=True, text=True, check=True
                          ).stdout.strip()
    checked = [check_run(r, full, fetch) for r in runs]
    read, listdir = _at_commit(full, root)
    by_id = {r["id"]: r for r in doc["rows"]}
    out = []
    for rid in rows:
        row = by_id.get(rid)
        if row is None:
            raise EvidenceRefused(f"no row {rid}")
        digest = CM.implementation_digest(row.get("implementation") or [],
                                          read, listdir)
        row["hosted_evidence"] = {
            "runs": [c["id"] for c in checked], "commit": full,
            "implementation_sha256": digest,
            "note": note or "; ".join(f"{c['workflow']} {c['event']} "
                                      f"{c['id']}: {c['jobs']} jobs "
                                      "succeeded" for c in checked)}
        out.append((rid, digest))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--commit", required=True)
    ap.add_argument("--rows", required=True)
    ap.add_argument("--runs", required=True)
    ap.add_argument("--note", default="")
    args = ap.parse_args(argv)
    doc = json.loads(CM.MATRIX.read_text(encoding="utf-8"))
    try:
        done = attach(doc, args.rows.split(","), args.commit,
                      args.runs.split(","), args.note)
    except EvidenceRefused as exc:
        print(f"EVIDENCE REFUSED: {exc}")
        return 1
    CM.MATRIX.write_text(json.dumps(doc, indent=2, ensure_ascii=False)
                         + "\n", encoding="utf-8")
    for rid, digest in done:
        print(f"{rid}: evidence at {args.commit[:12]}, implementation "
              f"{digest[:16]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
