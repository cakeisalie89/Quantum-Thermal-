#!/usr/bin/env python3
"""The third status system: what scientific and model evidence exists.

Three registries answer three different questions, and none may answer for
another:

* ``docs/harness_status.json`` (``tools/harness_status.py``): are the
  software contracts implemented, tested and hosted?
* ``docs/security/readiness_matrix.json`` (``tools/readiness.py``): is the
  system ready for production?
* ``docs/scientific_model_evidence.json`` (this tool): what has actually been
  computed, checked, measured or asked of a model, and with what limits?

A green software contract is not a model result. A model result is not a
scientific verification, and none of those is a measurement of the world.
Neither of the other two tools reads this file, and this tool writes nothing
but it. ``tests/test_model_evidence.py`` holds all three to that.

What this registry can say about a real model is bounded on purpose. A run is
counted only from a bundle in ``docs/model_runs/`` that names the owner's
authorization (OD-1), the commit, the log it produced and its cost. Even then
it is RECORDED_NOT_VERIFIED: a bundle says a run happened, and nothing here
checks what the model said against the world.

    uv run python tools/model_evidence.py derive   # write the registry
    uv run python tools/model_evidence.py verify   # fail if it is stale
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "docs" / "scientific_model_evidence.json"
SCHEMA = "scientific-model-evidence/1"
RUNS = ROOT / "docs" / "model_runs"
BENCH = ROOT / "docs" / "research" / "grounding_bench_results.json"
NEURAL = ROOT / "docs" / "neural" / "current_status.json"
FIXTURE_CYCLE = "tests/test_gate_b_acceptance.py"

STATUSES = {
    "NOT_RUN": "nothing of this kind has been run",
    "NONE": "nothing of this kind exists in this repository",
    "FIXTURE_ONLY": "exercised end to end with a scripted model; no "
                    "language model was involved",
    "SCRIPTED_AGENT_EVIDENCE_ONLY": "measured on scripted agents with an "
                                    "explicit error model; a property of "
                                    "those agents, not of any language model",
    "RECORDED_NOT_VERIFIED": "a run is recorded with its authorization, log "
                             "and cost; what the model said is not checked "
                             "against the world here",
    "PARTIAL": "some of the claims of this kind are held; which ones are "
               "listed",
}
_BUNDLE_KEYS = {"schema", "run_id", "authorization", "commit", "log_sha256",
                "requests", "cost_micro", "provider_claimed", "note"}
_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")


class Refused(Exception):
    """An input this registry cannot state honestly."""


def _bundles(root: Path) -> list:
    runs = root / RUNS.relative_to(ROOT)
    out = []
    for p in sorted(runs.glob("*.json")) if runs.is_dir() else []:
        b = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(b, dict) or set(b) != _BUNDLE_KEYS:
            raise Refused(f"{p.name}: a run bundle has exactly "
                          f"{sorted(_BUNDLE_KEYS)}")
        if b["schema"] != "model-run-bundle/1":
            raise Refused(f"{p.name}: schema {b['schema']!r}")
        if "OD-1" not in str(b["authorization"]):
            raise Refused(f"{p.name}: names no owner authorization (OD-1); "
                          "a live run nobody authorized is not evidence, it "
                          "is an incident")
        if not _COMMIT.match(str(b["commit"])) or \
                not _DIGEST.match(str(b["log_sha256"])):
            raise Refused(f"{p.name}: commit or log digest malformed")
        for k in ("requests", "cost_micro"):
            if isinstance(b[k], bool) or not isinstance(b[k], int) or \
                    b[k] < 0:
                raise Refused(f"{p.name}: {k} is not a count")
        out.append(b)
    return out


def derive(root: Path = ROOT) -> dict:
    bench = json.loads((root / BENCH.relative_to(ROOT)).read_text("utf-8"))
    neural = json.loads((root / NEURAL.relative_to(ROOT)).read_text("utf-8"))
    runs = _bundles(root)
    agg = bench["confirmatory"]["aggregate"]
    entries = [
        {"id": "SME-01", "kind": "REAL_MODEL_INFERENCE",
         "status": "RECORDED_NOT_VERIFIED" if runs else "NOT_RUN",
         "runs": [b["run_id"] for b in runs],
         "requests": sum(b["requests"] for b in runs),
         "cost_micro": sum(b["cost_micro"] for b in runs),
         "source": "docs/model_runs/",
         "note": ("no provider has been chosen (OD-1) and the live worker "
                  "refuses; no language model has been called by this "
                  "harness" if not runs else
                  "runs recorded under the owner's authorization; their "
                  "answers are not checked against the world here")},
        {"id": "SME-02", "kind": "FIXTURE_MODEL_CYCLE",
         "status": "FIXTURE_ONLY" if (root / FIXTURE_CYCLE).is_file()
         else "NOT_RUN",
         "source": FIXTURE_CYCLE,
         "note": "a scripted provider answers through the inference gateway "
                 "and the governed path; it shows the plumbing holds, and "
                 "nothing about how a language model behaves"},
        {"id": "SME-03", "kind": "GROUNDING_BENCHMARK",
         "status": "SCRIPTED_AGENT_EVIDENCE_ONLY",
         "source": "docs/research/grounding_bench_results.json",
         "facts": {
             "agent_alone_false_assertion_rate":
                 round(agg["A"]["false_assertion_rate"]["mean"], 4),
             "gated_false_assertion_rate":
                 round(agg["D"]["false_assertion_rate"]["mean"], 4),
             "real_model": bench["real_model"]["status"]},
         "note": "Tranche G's pre-registered benchmark: scripted agents, a "
                 "hidden oracle; the real-model condition was not run"},
        {"id": "SME-04", "kind": "LEARNED_MODEL_NF_1T", "status": "PARTIAL",
         "source": "docs/neural/current_status.json",
         "facts": {
             "flagship_claims": sorted(neural["flagship"]["claims_held"]),
             "development_member_claims":
                 sorted(neural["development"]["claims_held"])},
         "note": "the flagship is an architecture, verified by parameter "
                 "count and meta-validation; it is neither allocated nor "
                 "trained. A small development member was trained and "
                 "evaluated. It is a scientific-feature model, not a "
                 "language model"},
        {"id": "SME-05", "kind": "SYMBOLIC_OR_FORMAL_CHECK", "status": "NONE",
         "source": "-",
         "note": "no symbolic engine or proof checker is installed (OD-5); "
                 "no scientific statement here is formally proved"},
        {"id": "SME-06", "kind": "REAL_WORLD_MEASUREMENT", "status": "NONE",
         "source": "-",
         "note": "this repository holds no measurement of a physical "
                 "system; simulation agreement is not measurement"},
    ]
    for e in entries:
        if e["status"] not in STATUSES:
            raise Refused(f"{e['id']}: status {e['status']!r}")
    return {
        "schema": SCHEMA,
        "label": "what scientific and model evidence exists, and its limits",
        "note": ("Derived by tools/model_evidence.py. Neither the software "
                 "completion status nor the production readiness matrix "
                 "reads this file, and this file reads neither of them."),
        "statuses": STATUSES,
        "verdict": {"REAL_MODEL_EVIDENCE": entries[0]["status"]},
        "entries": entries,
    }


def render(doc: dict) -> str:
    return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    doc = render(derive())
    if args[:1] == ["derive"]:
        REGISTRY.write_text(doc, encoding="utf-8")
        print(f"wrote {REGISTRY.relative_to(ROOT)}: real-model evidence "
              f"{json.loads(doc)['verdict']['REAL_MODEL_EVIDENCE']}")
        return 0
    held = REGISTRY.read_text(encoding="utf-8") if REGISTRY.is_file() else ""
    if held != doc:
        print("scientific and model evidence registry is STALE: run "
              "tools/model_evidence.py derive")
        return 1
    print("scientific and model evidence registry holds: real-model "
          f"evidence {json.loads(doc)['verdict']['REAL_MODEL_EVIDENCE']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
