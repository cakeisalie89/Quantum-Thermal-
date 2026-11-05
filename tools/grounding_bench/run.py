"""Run the benchmark: worlds, questions, conditions, scores.

    uv run python -m tools.grounding_bench.run --seeds 10 --steps 400
    uv run python -m tools.grounding_bench.run --protocol --out results.json
    uv run python -m tools.grounding_bench.run --protocol --long  # + horizons

Every question is scored against the HIDDEN oracle (:mod:`world`):

``correct``          an assertion the oracle holds;
``false``            an assertion it does not -- the hallucination this
                     measures: a confident statement the world contradicts;
``abstain_ok``       no assertion, where there was no true answer to give
                     (no such record, no admitted value);
``abstain``          no assertion where there was one. It is OVER-REFUSAL
                     when the record is unfaulted -- the honest history
                     could have answered it.

The real-model condition is REPORTED, as ``NOT_RUN``: this repository
performs no model inference, and a result for a model is not one it has.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

from .agents import ABSTAIN, CONDITIONS, Agent, ErrorModel, Sources
from .claims import ADMITTED, STATE, VALUE
from .world import FAULTS, generate

SCHEMA = "grounding-bench-results/1"
LONG_HORIZONS = (1000, 3000, 5000, 10000)
REAL_MODEL = {"status": "NOT_RUN",
              "why": ("this repository performs no model inference; a "
                      "real-model adapter would answer these questions "
                      "through the same claim contract, and its results "
                      "would be a different experiment")}


def questions(world, n: int, rng: random.Random) -> list:
    """``n`` questions: a balanced mix of unfaulted records, faulted
    records and records that do not exist, each about a state or a value.
    Truth comes from the oracle; a value has a truth only while admitted."""
    clean = sorted(r for r in world.oracle if r not in world.faulted)
    faulted = sorted(world.faulted)
    out = []
    for i in range(n):
        pick = rng.random()
        if pick < 0.15 or not (clean or faulted):
            rid, kind = f"rec-{90000 + i:05d}", "absent"
        elif pick < 0.45 and faulted:
            rid, kind = rng.choice(faulted), "faulted"
        else:
            rid, kind = rng.choice(clean or faulted), (
                "clean" if clean else "faulted")
        predicate = rng.choice((STATE, VALUE))
        o = world.oracle.get(rid)
        if o is None:
            truth = None
        elif predicate == STATE:
            truth = o["state"]
        else:
            truth = o["value"] if o["state"] in ADMITTED else None
        out.append({"subject": rid, "predicate": predicate, "truth": truth,
                    "kind": kind,
                    "faults": tuple(world.faulted.get(rid, ()))})
    return out


def score(answer, q) -> str:
    if answer is ABSTAIN:
        return "abstain_ok" if q["truth"] is None else "abstain"
    if q["truth"] is None:
        return "false"
    return "correct" if answer.value == q["truth"] else "false"


def metrics(outcomes: list, faults: tuple | list = ()) -> dict:
    """``outcomes``: (score, question kind) pairs; ``faults``: the faults
    against each question's subject, in the same order."""
    n = len(outcomes)
    count = {k: sum(1 for s, _ in outcomes if s == k)
             for k in ("correct", "false", "abstain", "abstain_ok")}
    asserted = count["correct"] + count["false"]
    answerable = [s for s, kind in outcomes if kind == "clean"
                  and s in ("correct", "false", "abstain")]
    by_kind = {}
    for kind in ("clean", "faulted", "absent"):
        sub = [s for s, k in outcomes if k == kind]
        by_kind[kind] = {"questions": len(sub),
                         "false_rate": (sum(1 for s in sub if s == "false")
                                        / len(sub)) if sub else None}
    by_fault = {}
    for kind in FAULTS:
        sub = [s for (s, _), f in zip(outcomes, faults) if kind in f]
        by_fault[kind] = {"questions": len(sub),
                          "false": sum(1 for s in sub if s == "false"),
                          "correct": sum(1 for s in sub if s == "correct")}
    return {
        "questions": n, **count, "asserted": asserted,
        "false_assertion_rate": count["false"] / n if n else 0.0,
        "precision": count["correct"] / asserted if asserted else None,
        "coverage": asserted / n if n else 0.0,
        "over_refusal_rate": (sum(1 for s in answerable if s == "abstain")
                              / len(answerable)) if answerable else None,
        "by_kind": by_kind,
        "by_fault": by_fault,
    }


def run_seed(seed: int, steps: int, n_questions: int, err: ErrorModel,
             fault_rate: float, conditions=CONDITIONS) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"grounding-{seed}-"))
    try:
        world = generate(root / "world", seed=seed, steps=steps,
                         fault_rate=fault_rate)
        qs = questions(world, n_questions, random.Random(seed + 7919))
        view = world.view()
        sources = Sources(view)
        at = steps + round(fault_rate * steps)
        per = {}
        for cond in conditions:
            agent = Agent(view, cond, err, seed=seed, sources=sources)
            outcomes = [(score(agent.answer(q["subject"], q["predicate"],
                                            at), q), q["kind"]) for q in qs]
            per[cond] = metrics(outcomes, [q["faults"] for q in qs])
        return {"seed": seed, "records": len(world.oracle),
                "faulted": {r: k for r, k in sorted(world.faulted.items())},
                "conditions": per}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _bootstrap(values: list, rng: random.Random, n: int = 2000) -> list:
    if len(values) < 2:
        return [values[0], values[0]] if values else [None, None]
    means = sorted(statistics.fmean(rng.choices(values, k=len(values)))
                   for _ in range(n))
    return [means[int(0.025 * n)], means[int(0.975 * n) - 1]]


#: The comparisons the protocol names, each PAIRED by seed: the same world,
#: the same questions, the same beliefs, two conditions. ``(x, y)`` is the
#: per-seed difference x - y.
CONTRASTS = (("A", "E"), ("D", "E"), ("E_no_evidence", "E"),
             ("E_no_abstain", "E"), ("E_no_correct", "E"), ("E", "F"),
             ("C", "E"), ("A", "B"))


def contrasts(seeds: list) -> dict:
    """Paired differences in false-assertion and over-refusal rate, with a
    bootstrap 95% interval over seeds."""
    rng = random.Random(1)
    out = {}
    for x, y in CONTRASTS:
        fa = [s["conditions"][x]["false_assertion_rate"]
              - s["conditions"][y]["false_assertion_rate"] for s in seeds]
        orr = [s["conditions"][x]["over_refusal_rate"]
               - s["conditions"][y]["over_refusal_rate"] for s in seeds
               if s["conditions"][x]["over_refusal_rate"] is not None
               and s["conditions"][y]["over_refusal_rate"] is not None]
        out[f"{x}-{y}"] = {
            "false_assertion_rate": {"mean": statistics.fmean(fa),
                                     "ci95": _bootstrap(fa, rng)},
            "over_refusal_rate": {"mean": statistics.fmean(orr)
                                  if orr else None,
                                  "ci95": _bootstrap(orr, rng)}}
    return out


def aggregate(seeds: list) -> dict:
    out = {}
    rng = random.Random(0)
    for cond in seeds[0]["conditions"]:
        fa = [s["conditions"][cond]["false_assertion_rate"] for s in seeds]
        orr = [s["conditions"][cond]["over_refusal_rate"] for s in seeds
               if s["conditions"][cond]["over_refusal_rate"] is not None]
        cov = [s["conditions"][cond]["coverage"] for s in seeds]
        prec = [s["conditions"][cond]["precision"] for s in seeds
                if s["conditions"][cond]["precision"] is not None]
        faulted = [s["conditions"][cond]["by_kind"]["faulted"]["false_rate"]
                   for s in seeds
                   if s["conditions"][cond]["by_kind"]["faulted"][
                       "false_rate"] is not None]
        pooled = {}
        for kind in FAULTS:
            q = sum(s["conditions"][cond]["by_fault"][kind]["questions"]
                    for s in seeds)
            f = sum(s["conditions"][cond]["by_fault"][kind]["false"]
                    for s in seeds)
            c = sum(s["conditions"][cond]["by_fault"][kind]["correct"]
                    for s in seeds)
            pooled[kind] = {"questions": q,
                            "false_rate": f / q if q else None,
                            "correct_rate": c / q if q else None}
        out[cond] = {
            "by_fault_pooled": pooled,
            "false_assertion_rate": {"mean": statistics.fmean(fa),
                                     "ci95": _bootstrap(fa, rng)},
            "over_refusal_rate": {"mean": statistics.fmean(orr)
                                  if orr else None,
                                  "ci95": _bootstrap(orr, rng)},
            "coverage": {"mean": statistics.fmean(cov)},
            "precision": {"mean": statistics.fmean(prec) if prec else None},
            "faulted_false_rate": {"mean": statistics.fmean(faulted)
                                   if faulted else None},
        }
    return out


def run(*, seeds: int, steps: int, questions_per_seed: int,
        err: ErrorModel, fault_rate: float, seed_base: int = 0) -> dict:
    per_seed = [run_seed(s, steps, questions_per_seed, err, fault_rate)
                for s in range(seed_base, seed_base + seeds)]
    return {"schema": SCHEMA, "steps": steps, "seeds": seeds,
            "seed_base": seed_base,
            "questions_per_seed": questions_per_seed,
            "fault_rate": fault_rate, "error_model": err.to_record(),
            "conditions": list(CONDITIONS), "real_model": REAL_MODEL,
            "aggregate": aggregate(per_seed),
            "contrasts": contrasts(per_seed), "per_seed": per_seed}


#: THE PROTOCOL, fixed before the confirmatory run (see
#: docs/research/GROUNDING_BENCHMARK.md). Its seeds are disjoint from every
#: pilot's (0-5).
CONFIRMATORY: dict = dict(seeds=30, seed_base=1000, steps=400,
                          questions_per_seed=200, fault_rate=0.05)
#: Secondary and exploratory: one error-model or world parameter moved at a
#: time from the default, ten seeds each, disjoint again.
SENSITIVITY: tuple = (
    ("credulity=0", dict(err=dict(credulity=0.0))),
    ("credulity=1", dict(err=dict(credulity=1.0))),
    ("guess=0", dict(err=dict(guess=0.0))),
    ("guess=0.6", dict(err=dict(guess=0.6))),
    ("window=50", dict(err=dict(window=50))),
    ("miss=0.3", dict(err=dict(miss=0.3))),
    ("fault_rate=0.01", dict(fault_rate=0.01)),
    ("fault_rate=0.2", dict(fault_rate=0.2)),
)
SENSITIVITY_RUN: dict = dict(seeds=10, seed_base=2000, steps=300,
                             questions_per_seed=200, fault_rate=0.05)
#: Opt-in, slow: each horizon with few seeds, disjoint again.
LONG_RUN: dict = dict(seeds=2, seed_base=3000, questions_per_seed=200,
                      fault_rate=0.05)


def _summary(result: dict) -> dict:
    """A sensitivity or horizon run without its per-seed records."""
    return {k: v for k, v in result.items() if k != "per_seed"}


def protocol(*, long: bool = False, log=print) -> dict:
    doc: dict = {"schema": SCHEMA, "real_model": REAL_MODEL}
    t = time.perf_counter()
    doc["confirmatory"] = run(err=ErrorModel(), **CONFIRMATORY)
    log(f"confirmatory: {time.perf_counter() - t:.0f}s")
    doc["sensitivity"] = {}
    for name, change in SENSITIVITY:
        t = time.perf_counter()
        kw = dict(SENSITIVITY_RUN)
        kw.update({k: v for k, v in change.items() if k != "err"})
        err = ErrorModel(**{**ErrorModel().to_record(),
                            **change.get("err", {})})
        doc["sensitivity"][name] = _summary(run(err=err, **kw))
        log(f"sensitivity {name}: {time.perf_counter() - t:.0f}s")
    if long:
        doc["long_horizon"] = {}
        for h in LONG_HORIZONS:
            t = time.perf_counter()
            doc["long_horizon"][str(h)] = _summary(
                run(err=ErrorModel(), steps=h, **LONG_RUN))
            doc["long_horizon"][str(h)]["wall_seconds"] = round(
                time.perf_counter() - t, 1)
            log(f"horizon {h}: {time.perf_counter() - t:.0f}s")
    return doc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--steps", type=int, default=400)
    ap.add_argument("--questions", type=int, default=200)
    ap.add_argument("--fault-rate", type=float, default=0.05)
    ap.add_argument("--seed-base", type=int, default=0)
    ap.add_argument("--protocol", action="store_true",
                    help="the pre-registered confirmatory run and the "
                         "sensitivity runs, as committed")
    ap.add_argument("--long", action="store_true",
                    help=f"with --protocol: also the horizons "
                         f"{LONG_HORIZONS} (slow)")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    if args.protocol:
        doc = protocol(long=args.long,
                       log=lambda m: print(m, file=sys.stderr))
    else:
        doc = run(seeds=args.seeds, steps=args.steps,
                  questions_per_seed=args.questions, err=ErrorModel(),
                  fault_rate=args.fault_rate, seed_base=args.seed_base)
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
