"""A development benchmark for semantic claim verification (section 6.3).

    python tools/claim_bench.py            # print the scores
    python tools/claim_bench.py --write    # rewrite the committed results
    python tools/claim_bench.py --check    # the committed results reproduce

THE WORLD. A research objective with admitted observations, each read from
a real authority record that a second actor verified, written through the
ledger's own gates:

* ``slab-1`` reports three outputs once (one in W/m^2);
* ``slab-2`` reports one output twice, from two runs that disagree;
* ``probe-1`` reported once, from a record later withdrawn: its observation
  is not admitted evidence any more.

THE CLAIMS, AND WHERE THEIR LABELS COME FROM.

``structured``
    Generated from the observations. The label is chosen FIRST and the
    claim written to have it: a threshold three resolutions clear of the
    value on the side the label needs, half a resolution from it for
    INSUFFICIENT_EVIDENCE, between two disagreeing runs for
    CONFLICTING_EVIDENCE, a unit the evidence is not in for UNVERIFIABLE.
    The label follows from arithmetic the generator does, not from the
    checker, so it is independently established -- but only for claims
    this narrow.
``prose``
    Plausible sentences about the same evidence, most of them saying more
    than it does: a consequence, a generalization, a cause, a provenance,
    a unit conversion, a paraphrase. Their labels were assigned by the
    author of the checker, with a reason each. THEY ARE NOT HUMAN LABELS:
    a confirmatory benchmark needs labels from people who did not write
    the checker, and recruiting them is the owner's decision. The prose
    scores are a development measurement, reported as such.

THE CHECKERS.

``deterministic``
    The closed grammar and research.evaluate (claim_check.check, no
    checker). It answers UNVERIFIABLE for anything it does not read.
``lexical``
    A heuristic standing in for a fallible semantic checker: it calls a
    claim SUPPORTED when it mentions a reported output and a number near
    the reported value. It reads every claim, as a checker asked to judge
    prose would.
``combined``
    The deterministic route, with the lexical checker consulted only for
    text the grammar does not read -- ASSESSED, and never established.
``model``
    NOT_RUN. No model inference is performed here; a result for a model is
    not one this repository has.

FALSE POSITIVE means a SUPPORTED answer to a claim labelled otherwise --
the failure that matters, a plausible but unsupported claim let through.
FALSE NEGATIVE means another answer to a claim labelled SUPPORTED.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import claim_check as C  # noqa: E402
from agent_substrate import research as R  # noqa: E402
from agent_substrate.authority import Role, State  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.store import AuthorityStore  # noqa: E402

SCHEMA = "claim-bench-results/1"
RESULTS = ROOT / "docs" / "research" / "claim_bench_results.json"
OBJECTIVE = "bench"
VERDICTS = [v.value for v in C.Verdict]

#: model -> run -> output name -> (value, unit, resolution)
RUNS = {
    "slab-1": {"a": {"T_at_half": (312.4, "K", 0.2),
                     "T_max": (340.0, "K", 0.5),
                     "q_in": (1200.0, "W/m^2", 5.0)}},
    "slab-2": {"a": {"T_at_half": (305.0, "K", 0.2)},
               "b": {"T_at_half": (309.0, "K", 0.2)}},
    "probe-1": {"a": {"T_probe": (290.0, "K", 0.5)}},
}
WITHDRAWN = {("probe-1", "a")}


class World:
    def __init__(self, base: Path):
        self.log = EventLog(base / "log.jsonl")
        self.evidence = EvidenceStore(base / "evidence")
        self.ledger = R.ResearchLedger(self.log, evidence=self.evidence,
                                       actor="controller").load()
        self.ledger.register(objective_id=OBJECTIVE, statement="what do "
                             "the runs say?", owner="owner", max_steps=4,
                             max_inference_calls=1)
        for model, runs in RUNS.items():
            for run, outputs in runs.items():
                self._observe(model, run, outputs)
        for model, run in sorted(WITHDRAWN):
            self.store.transition(
                record_id=f"r-{model}-{run}", dst=State.STALE, actor="system",
                role=Role.SYSTEM, evidence={"invalidated_by":
                                            self.evidence.put(b"recalled")},
                stale_reason="the instrument was recalled")

    @property
    def store(self) -> AuthorityStore:
        return AuthorityStore(self.log, evidence=self.evidence).load()

    def _observe(self, model, run, outputs):
        rid = f"r-{model}-{run}"
        bundle = json.dumps({"model_id": model, "outputs": [
            {"name": n, "status": "OK",
             "quantity": {"value": v, "unit": u, "resolution": r}}
            for n, (v, u, r) in sorted(outputs.items())]},
            sort_keys=True).encode()
        dg = self.evidence.put(bundle)
        store = self.store
        store.create(record_id=rid, kind="measurement", proposer="ingest",
                     evidence={"result_bundle": dg})
        store.transition(record_id=rid, dst=State.UNDER_REVIEW,
                         actor="reviewer", role=Role.VERIFIER)
        store.transition(record_id=rid, dst=State.VERIFIED, actor="reviewer",
                         role=Role.VERIFIER, evidence={
                             "verification_report": self.evidence.put(
                                 f"checked {rid}".encode())})
        self.ledger.observe(observation_id=f"o-{model}-{run}",
                            objective_id=OBJECTIVE, record_id=rid,
                            bundle_bytes=bundle)

    def check(self, text, cites=(), checker=None):
        return C.check(text, cites=cites, objective_id=OBJECTIVE,
                       ledger=self.ledger, store=self.store,
                       evidence=self.evidence, checker=checker)


# ------------------------------------------------------------- the claims

def _n(x: float) -> str:
    return f"{x:.6g}"


def structured_claims() -> list:
    """Each claim's label is decided before its text is written."""
    out = []

    def add(text, label, why, cites=()):
        out.append({"family": "structured", "text": text, "label": label,
                    "label_source": "CONSTRUCTION", "why": why,
                    "cites": list(cites)})

    S, X, I, U, F = VERDICTS[0], VERDICTS[1], VERDICTS[2], VERDICTS[3], \
        VERDICTS[4]
    for model in ("slab-1",):
        for q, (v, unit, r) in sorted(RUNS[model]["a"].items()):
            o = [f"o-{model}-a"]
            add(f"{q} of {model} is below {_n(v + 3 * r)} {unit}", S,
                "threshold three resolutions above the value", o)
            add(f"{q} of {model} is above {_n(v - 3 * r)} {unit}", S,
                "threshold three resolutions below the value", o)
            add(f"the {q} of {model} remains at most {_n(v + 4 * r)} "
                f"{unit}.", S, "at most, four resolutions above", o)
            add(f"{q} of {model} is {_n(v)} {unit}", S,
                "the reported value itself", o)
            add(f"{q} of {model} is below {_n(v - 3 * r)} {unit}", X,
                "threshold three resolutions below the value", o)
            add(f"{q} of {model} is at least {_n(v + 3 * r)} {unit}", X,
                "threshold three resolutions above the value", o)
            add(f"{q} of {model} is {_n(v + 3 * r)} {unit}", X,
                "a value three resolutions off", o)
            add(f"{q} of {model} is below {_n(v + r / 2)} {unit}", I,
                "within the resolution of the value", o)
            add(f"{q} of {model} is above {_n(v - r / 2)} {unit}", I,
                "within the resolution of the value", o)
            add(f"{q} of {model} is below {_n(v + 3 * r)} furlongs", U,
                "a unit the evidence is not in")
        add(f"T_edge of {model} is below 400 K", I,
            "an output no run reports")
    lo = RUNS["slab-2"]["a"]["T_at_half"][0]
    hi = RUNS["slab-2"]["b"]["T_at_half"][0]
    add(f"T_at_half of slab-2 is below {_n((lo + hi) / 2)} K", F,
        "between two runs that disagree", ["o-slab-2-a"])
    add(f"T_at_half of slab-2 is above {_n((lo + hi) / 2)} K", F,
        "between two runs that disagree, citing the other", ["o-slab-2-b"])
    add(f"T_at_half of slab-2 is below {_n(hi + 1)} K", S,
        "above both runs")
    add(f"T_at_half of slab-2 is above {_n(hi + 1)} K", X,
        "above both runs")
    add("T_probe of probe-1 is below 300 K", I,
        "its only observation was withdrawn", ["o-probe-1-a"])
    add("T_at_half of slab-9 is below 300 K", I, "a model nothing ran")
    return out


#: (text, label, why). Assigned by the checker's author; NOT human labels.
PROSE = [
    ("T_at_half of slab-1 is below 350 K, so the panel is safe.",
     "UNVERIFIABLE", "the consequence is not something any admitted "
     "observation speaks to"),
    ("T_at_half of slab-1 stays below 350 K under every load.",
     "UNVERIFIABLE", "a generalization beyond the one condition run"),
    ("slab-1 peaks at 340 K because radiative losses dominate.",
     "UNVERIFIABLE", "a cause; the runs report values, not mechanisms"),
    ("T_max of slab-1 was confirmed below 330 K by two independent runs.",
     "CONTRADICTED", "one run, and it reports 340 K"),
    ("Two independent runs agree that T_at_half of slab-2 is 307 K.",
     "CONFLICTING_EVIDENCE", "they report 305 K and 309 K"),
    ("The measured mid-plane temperature of slab-1 is about 312 K.",
     "SUPPORTED", "a paraphrase of T_at_half, 312.4 K"),
    ("q_in of slab-1 is 1.2 kW/m^2", "SUPPORTED",
     "1200 W/m^2, in other units"),
    ("slab-1 absorbs about 1200 W/m^2 at its surface.", "SUPPORTED",
     "a paraphrase of q_in"),
    ("The probe read 290 K, well within limits.", "INSUFFICIENT_EVIDENCE",
     "the probe's record was withdrawn, and 'limits' are nobody's"),
    ("T_at_half of slab-1 is 312.4 K according to a reviewer.",
     "UNVERIFIABLE", "the attribution is not recorded anywhere"),
    ("slab-1 runs hotter than slab-2 at mid-plane.", "SUPPORTED",
     "312.4 K against 305 K and 309 K"),
    ("slab-1 is 312.4 K at the hottest point.", "CONTRADICTED",
     "the hottest point is T_max, 340 K"),
]


def prose_claims() -> list:
    return [{"family": "prose", "text": t, "label": lab,
             "label_source": "AUTHOR", "why": why, "cites": []}
            for t, lab, why in PROSE]


# ---------------------------------------------------------- the checkers

_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


def lexical(text: str, observations: list) -> tuple:
    """SUPPORTED when the text names a reported output, or a model that
    reported one, and carries a number within 2% of a reported value;
    CONTRADICTED when it names one and every number is further; otherwise
    INSUFFICIENT_EVIDENCE. Deliberately the kind of judgement a fluent
    reader makes, and as easily satisfied."""
    words = set(re.findall(r"[A-Za-z0-9_.-]+", text))
    numbers = [float(n) for n in _NUMBER.findall(text)]
    near = far = False
    for o in observations:
        names = set(o["values"]) | {o["model_id"]}
        if not words & names:
            continue
        for got in o["values"].values():
            for n in numbers:
                if abs(n - got["value"]) <= 0.02 * abs(got["value"]):
                    near = True
                else:
                    far = True
    if near:
        return "SUPPORTED", "a reported output and a number near it"
    if far:
        return "CONTRADICTED", "a reported output and no number near it"
    return "INSUFFICIENT_EVIDENCE", "nothing reported is mentioned"


def _answer(world: World, claim: dict, mode: str):
    if mode == "lexical":
        _, admitted = C.admitted_evidence(OBJECTIVE, ledger=world.ledger,
                                          store=world.store)
        verdict, _ = C.assess_with(lexical, claim["text"], admitted)
        return verdict.value, False, "ASSESSED"
    r = world.check(claim["text"], cites=claim["cites"],
                    checker=lexical if mode == "combined" else None)
    return r.verdict.value, r.established, r.basis.value


def _scores(rows: list) -> dict:
    conf = {lab: {v: 0 for v in VERDICTS} for lab in VERDICTS}
    for row in rows:
        conf[row["label"]][row["answer"]] += 1
    pos = [r for r in rows if r["label"] == "SUPPORTED"]
    neg = [r for r in rows if r["label"] != "SUPPORTED"]
    fp = [r for r in neg if r["answer"] == "SUPPORTED"]
    fn = [r for r in pos if r["answer"] != "SUPPORTED"]
    return {
        "n": len(rows),
        "exact": sum(r["answer"] == r["label"] for r in rows),
        "false_positive": len(fp), "negatives": len(neg),
        "false_negative": len(fn), "positives": len(pos),
        "established": sum(r["established"] for r in rows),
        "established_false_positive": sum(r["established"] for r in fp),
        "false_positive_texts": [r["text"] for r in fp],
        "confusion": conf,
    }


def run() -> dict:
    claims = structured_claims() + prose_claims()
    with tempfile.TemporaryDirectory() as tmp:
        world = World(Path(tmp))
        results: dict = {}
        for mode in ("deterministic", "lexical", "combined"):
            rows = []
            for c in claims:
                answer, established, basis = _answer(world, c, mode)
                rows.append({**c, "answer": answer,
                             "established": established, "basis": basis})
            entry: dict = {
                family: _scores([r for r in rows if r["family"] == family])
                for family in ("structured", "prose")}
            results[mode] = entry
            entry["rows"] = [
                {k: r[k] for k in ("family", "text", "label", "answer",
                                   "basis", "established")} for r in rows]
    return {
        "schema": SCHEMA,
        "note": "A development benchmark. Structured labels are established "
                "by construction; prose labels were assigned by the "
                "checker's author and are NOT human labels. A confirmatory "
                "benchmark needs independent human labels (owner decision).",
        "claims": len(claims),
        "label_sources": {"structured": "CONSTRUCTION", "prose": "AUTHOR"},
        "checkers": results,
        "model": "NOT_RUN",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    got = run()
    text = json.dumps(got, indent=1, sort_keys=True) + "\n"
    if a.write:
        RESULTS.write_text(text, encoding="utf-8")
    if a.check:
        if RESULTS.read_text(encoding="utf-8") != text:
            print("claim bench: the committed results do not reproduce")
            return 1
        print("claim bench: the committed results reproduce")
        return 0
    for mode in ("deterministic", "lexical", "combined"):
        for fam in ("structured", "prose"):
            s = got["checkers"][mode][fam]
            print(f"{mode:13} {fam:10} exact {s['exact']}/{s['n']}  "
                  f"FP {s['false_positive']}/{s['negatives']}  "
                  f"FN {s['false_negative']}/{s['positives']}  "
                  f"established {s['established']} "
                  f"(of them wrong: {s['established_false_positive']})")
    print("model         NOT_RUN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
