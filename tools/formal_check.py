#!/usr/bin/env python3
"""Model-check the TLA+ specifications in docs/formal (Assurance B).

    python tools/formal_check.py run    --jar PATH  # check, write the record
    python tools/formal_check.py verify --jar PATH  # check, compare with it
    python tools/formal_check.py check              # no Java: is the record
                                                    # complete, as expected,
                                                    # and of these specs?
    python tools/formal_check.py fetch  --dest DIR  # tla2tools.jar, pinned

WHAT A RESULT HERE MEANS

TLC explores every state of a FINITE model -- two requests, three actors,
one crash -- under the assumptions the specification states, fairness
included. "No error" says the stated invariants hold in every reachable
state of that model and the stated liveness properties hold of every fair
behaviour. It does not say the Python implementation is correct: the
specifications are written by hand, and tests/test_formal_conformance.py is
what connects them to the code (trace replay, and the authority edge table
compared edge for edge). See docs/formal/README.md.

EACH PROPERTY IS SHOWN ABLE TO FAIL

Every specification has a ``Bug`` constant selecting deliberately broken
variants, and each variant must be refuted by the property it targets. An
invariant no variant can violate is not shown to say anything.

WHAT IS RECORDED

``docs/formal/results.json``: per model and variant, the outcome, the
property violated, the number of distinct states, and the counterexample's
actions and observed variables -- except for a liveness violation, where
TLC's timed liveness schedule makes the count and the lasso irreproducible
and only the verdict is recorded. ``docs/formal/traces.jsonl``: random
behaviours of the correct models (TLC's simulation mode, seeded), which the
conformance tests replay against the implementation. TLC runs with one
worker and fixed seeds, so both are reproducible; ``verify`` reruns TLC and
compares.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FORMAL = ROOT / "docs" / "formal"
RESULTS = FORMAL / "results.json"
TRACES = FORMAL / "traces.jsonl"
SCHEMA = "formal-results/1"

#: v1.7.4 is the newest STABLE release; v1.8.0 is a rolling pre-release
#: whose bytes change under its tag, so it cannot be pinned by digest.
JAR_VERSION = "1.7.4"
JAR_URL = ("https://github.com/tlaplus/tlaplus/releases/download/"
           f"v{JAR_VERSION}/tla2tools.jar")
JAR_SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"

PASS = "PASS"
TEMPORAL = "TEMPORAL"

#: Each model: its constants (TLA+ expressions, bound through a generated
#: MC module), what TLC checks, what each broken variant must violate, which
#: variables a trace keeps, and how many simulated behaviours to record.
MODELS: tuple[dict[str, Any], ...] = (
    {"spec": "AuthorityPromotion",
     "constants": {"Actors": '{"a1", "a2", "a3"}'},
     "invariants": ("TypeOK", "SeparationOfDuties",
                    "PromotedOnlyFromVerified",
                    "PromotedUnderAPolicyInForce"),
     "properties": ("TerminalIsFinal",),
     "deadlock": False,
     "variants": {"none": PASS,
                  "self_verify": "SeparationOfDuties",
                  "review_promotes": "SeparationOfDuties",
                  "withdrawn_policy": "PromotedUnderAPolicyInForce"},
     "observe": ("state", "proposer", "policy"),
     "simulate": (25, 10)},
    {"spec": "LogAppend",
     "constants": {"Procs": '{"p1", "p2"}', "PerProc": "2", "MaxCrash": "1"},
     "invariants": ("ChainIntact", "NothingAfterATornLine", "NoDuplicate",
                    "Exclusive"),
     "properties": ("EveryIntentRecorded",),
     "deadlock": True,
     "variants": {"none": PASS, "no_lock": "ChainIntact",
                  "append_over_torn": "NothingAfterATornLine",
                  "no_idempotent_decide": "NoDuplicate",
                  "no_repair": TEMPORAL},
     "observe": (), "simulate": None},
    {"spec": "InferenceBudget",
     "constants": {"Reqs": '{"r1", "r2"}', "MaxAttempts": "2",
                   "Reserve_": "2", "Usage": "1", "Cap": "3", "MaxTime": "5"},
     "invariants": ("NeverOverspent", "AtMostOneOpen", "AtMostOneOK",
                    "DispatchedOnce", "NothingSettledInFlight",
                    "HeldIsTheLedger"),
     "properties": ("EverySettled",),
     "deadlock": True,
     "variants": {"none": PASS, "check_held_only": "NeverOverspent",
                  "double_dispatch": "DispatchedOnce",
                  "late_dispatch": "NothingSettledInFlight",
                  "recover_ignores_lease": "NothingSettledInFlight",
                  "no_recovery": TEMPORAL},
     "observe": ("held", "now"),
     "simulate": (25, 25)},
    {"spec": "CrashRetry",
     "constants": {"Workers": '{"w1", "w2"}', "MaxAdversity": "2"},
     "invariants": ("OneTaskPerKey", "RecordedOnce",
                    "RecordedByTheLeaseHolder", "ExternalEffectsAtMostOnce"),
     "properties": ("ReachesAnOutcome",),
     "deadlock": True,
     "variants": {"none": PASS, "rebind": "OneTaskPerKey",
                  "no_lease_check": "RecordedByTheLeaseHolder",
                  "rerun_external": "ExternalEffectsAtMostOnce",
                  "no_recover": TEMPORAL},
     "observe": (), "simulate": None},
    {"spec": "Invalidation",
     "constants": {
         "Records": '{"r1", "r2", "r3", "r4", "r5"}',
         # r2 rests on r1, r3 on r2, r4 on r3; r5 on nothing. r3 is
         # SUPERSEDED: it cannot go stale, and r4 behind it must be reached.
         "Deps": '[r1 |-> {}, r2 |-> {"r1"}, r3 |-> {"r2"}, '
                 'r4 |-> {"r3"}, r5 |-> {}]',
         "Roots": '{"r1"}',
         "Initial": '[r1 |-> "VERIFIED", r2 |-> "PROMOTED", '
                    'r3 |-> "SUPERSEDED", r4 |-> "PROMOTED", '
                    'r5 |-> "PROMOTED"]',
         "MaxCrash": "1", "MaxReverify": "1"},
     "invariants": ("PlanIsComplete",),
     "properties": ("EventuallyConsistent", "JustifiedStaleness"),
     "deadlock": True,
     "variants": {"none": PASS, "direct_only": "PlanIsComplete",
                  "stop_at_skipped": "PlanIsComplete", "no_settle": TEMPORAL,
                  "overeager": "JustifiedStaleness"},
     "observe": ("origin", "st", "pending"),
     "simulate": (25, 14)},
    {"spec": "ContextFreshness",
     "constants": {"MaxVersion": "3"},
     "invariants": ("DecidedOnlyWhenFresh",),
     "properties": (),
     "deadlock": False,
     "variants": {"none": PASS, "check_at_build": "DecidedOnlyWhenFresh",
                  "check_then_act": "DecidedOnlyWhenFresh",
                  "ignore_withdrawal": "DecidedOnlyWhenFresh"},
     "observe": ("version", "withdrawn", "contexts"),
     "simulate": (25, 10)},
)


class FormalError(Exception):
    """The record cannot be produced or does not hold."""


# ------------------------------------------------------------ TLA+ values

_TOKEN = re.compile(r'\s*(?:(?P<str>"(?:[^"\\]|\\.)*")|(?P<int>-?\d+)|'
                    r'(?P<sym><<|>>|\|->|:>|@@|[{}\[\](),])|'
                    r'(?P<id>[A-Za-z_][A-Za-z0-9_]*))')


def _tokens(text: str) -> list:
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise FormalError("cannot read a TLA+ value at "
                              f"{text[pos:][:40]!r}")
        pos = m.end()
        kind = m.lastgroup or ""
        out.append((kind, m.group(kind)))
    return out


def parse_value(text: str):
    """A TLA+ value as TLC prints it, as JSON-able data: a tuple is a list,
    a set {"$set": [...]}, a record a dict, and a function ``a :> b @@ ...``
    {"$fn": [[a, b], ...]}."""
    toks = _tokens(text)
    try:
        val, i = _value(toks, 0)
    except IndexError:
        raise FormalError(f"truncated TLA+ value {text[:60]!r}") from None
    if i != len(toks):
        raise FormalError(f"trailing input in {text[:60]!r}")
    return val


def _value(t, i):
    kind, tok = t[i]
    if kind == "str":
        return json.loads(tok), i + 1
    if kind == "int":
        return int(tok), i + 1
    if kind == "id" and tok in ("TRUE", "FALSE"):
        return tok == "TRUE", i + 1
    if tok == "<<":
        items, i = _items(t, i + 1, ">>")
        return items, i
    if tok == "{":
        items, i = _items(t, i + 1, "}")
        return {"$set": sorted(items, key=json.dumps)}, i
    if tok == "[":
        rec = {}
        i += 1
        while t[i][1] != "]":
            key = t[i][1]
            if t[i + 1][1] != "|->":
                raise FormalError(f"expected |-> after {key!r}")
            rec[key], i = _value(t, i + 2)
            if t[i][1] == ",":
                i += 1
        return rec, i + 1
    if tok == "(":
        first, i = _value(t, i + 1)
        if t[i][1] == ")":
            return first, i + 1
        pairs = []
        while True:
            if t[i][1] != ":>":
                raise FormalError("expected :> in a function")
            v, i = _value(t, i + 1)
            pairs.append([first, v])
            if t[i][1] == ")":
                return {"$fn": pairs}, i + 1
            if t[i][1] != "@@":
                raise FormalError("expected @@ in a function")
            first, i = _value(t, i + 1)
    raise FormalError(f"unexpected {tok!r}")


def _items(t, i, close):
    items = []
    while t[i][1] != close:
        v, i = _value(t, i)
        items.append(v)
        if t[i][1] == ",":
            i += 1
    return items, i + 1


def _state(lines: list, observe) -> dict:
    """``/\\ var = value`` lines (continuations joined) -> {var: value}."""
    text = "\n".join(lines)
    out = {}
    for part in re.split(r"(?m)^/\\ ", text):
        part = part.strip()
        if not part:
            continue
        var, _, val = part.partition(" = ")
        if observe is None or var in observe or var == "last":
            out[var] = parse_value(" ".join(val.split()))
    return out


def counterexample(stdout: str, observe) -> tuple:
    """The trace TLC printed: a list of states, and the index a lasso loops
    back to (liveness), or None."""
    lines = stdout.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines)
                     if ln.startswith("Error: The behavior up to this point")
                     or ln.startswith("Error: The following behavior"))
    except StopIteration:
        return [], None
    states: list = []
    cur: list = []
    loop = None
    for ln in lines[start + 1:]:
        if re.match(r"^\d+ states generated", ln) or \
                ln.startswith("Finished in") or ln.startswith("The number"):
            break
        back = re.match(r"^Back to state:? (\d+)", ln)
        if back:
            loop = int(back.group(1)) - 1
        m = re.match(r"^State (\d+): (.*)$", ln)
        if m or back or not ln.strip():
            if cur:
                states.append(_state(cur, observe))
                cur = []
            inner = re.search(r"Back to state:? (\d+)", m.group(2)) \
                if m else None
            if inner:
                loop = int(inner.group(1)) - 1
            elif m and m.group(2).strip() == "Stuttering":
                loop = len(states) - 1          # stays in the last state
            continue
        if ln.startswith("/\\ ") or (cur and ln.startswith(" ")):
            cur.append(ln)
    if cur:
        states.append(_state(cur, observe))
    return states, loop


def simulation_trace(text: str, observe) -> list:
    """One trace file TLC's simulation mode wrote: STATE_n == /\\ ...

    A stuttering step -- the model at rest, at its time horizon or with
    nothing left to do -- repeats the state, ``last`` included, and would
    read as the last action happening again; it is dropped."""
    blocks: list = []
    cur: list | None = None
    for ln in text.splitlines():
        if re.match(r"^STATE_\d+ ==", ln):
            if cur is not None:
                blocks.append(cur)
            cur = []
        elif cur is not None and (ln.startswith("/\\ ") or
                                  ln.startswith(" ")):
            cur.append(ln)
    if cur:
        blocks.append(cur)
    states: list = []
    for block in blocks:
        whole = _state(block, None)
        if states and whole == states[-1][0]:
            continue
        states.append((whole, _state(block, observe)))
    return [kept for _whole, kept in states]


# ------------------------------------------------------------- running TLC

def jar_digest(jar: Path) -> str:
    return hashlib.sha256(jar.read_bytes()).hexdigest()


def require_jar(jar: Path) -> Path:
    if not jar.is_file():
        raise FormalError(f"no tla2tools.jar at {jar}; fetch it with "
                          "`formal_check.py fetch`")
    got = jar_digest(jar)
    if got != JAR_SHA256:
        raise FormalError(f"{jar} is sha256 {got}, not the pinned "
                          f"tla2tools {JAR_VERSION} ({JAR_SHA256})")
    return jar


def _mc(model: dict, bug: str, simulate: bool = False) -> tuple:
    """The generated MC module and its configuration. A simulated behaviour
    is cut off at a depth, so liveness is not asked of it: that is the
    exhaustive check's to answer."""
    defs = "\n".join(f"mc_{k} == {v}" for k, v in model["constants"].items())
    module = (f"---- MODULE MC ----\nEXTENDS {model['spec']}\n{defs}\n"
              f'mc_Bug == "{bug}"\n====\n')
    lines = ["CONSTANTS"]
    lines += [f"  {k} <- mc_{k}" for k in model["constants"]]
    lines += ["  Bug <- mc_Bug", "SPECIFICATION Spec"]
    if model["invariants"]:
        lines.append("INVARIANTS " + " ".join(model["invariants"]))
    if model["properties"] and not simulate:
        lines.append("PROPERTIES " + " ".join(model["properties"]))
    lines.append(f"CHECK_DEADLOCK {'TRUE' if model['deadlock'] else 'FALSE'}")
    return module, "\n".join(lines) + "\n"


def _tlc(jar: Path, model: dict, bug: str, work: Path,
         simulate=None) -> str:
    work.mkdir(parents=True)
    shutil.copy(FORMAL / f"{model['spec']}.tla", work)
    module, cfg = _mc(model, bug, simulate=bool(simulate))
    (work / "MC.tla").write_text(module)
    (work / "MC.cfg").write_text(cfg)
    cmd = ["java", "-XX:+UseParallelGC", "-cp", str(jar), "tlc2.TLC",
           "-workers", "1", "-fp", "1", "-seed", "1", "-nowarning",
           "-metadir", str(work / "meta"), "-config", "MC.cfg"]
    if simulate:
        num, depth = simulate
        cmd += ["-simulate", f"file={work / 'sim' / 'tr'},num={num}",
                "-depth", str(depth)]
        (work / "sim").mkdir()
    proc = subprocess.run(cmd + ["MC.tla"], cwd=work, capture_output=True,
                          text=True, timeout=1800)
    return proc.stdout + proc.stderr


def check_one(jar: Path, model: dict, bug: str, work: Path) -> dict:
    out = _tlc(jar, model, bug, work)
    m = re.search(r"Error: (?:Invariant|Action property) (\w+) is violated",
                  out)
    if "Model checking completed. No error has been found." in out:
        outcome, violated = PASS, None
    elif m:
        outcome, violated = "VIOLATED", m.group(1)
    elif "Temporal properties were violated" in out:
        outcome, violated = "VIOLATED", TEMPORAL
    else:
        raise FormalError(f"{model['spec']}/{bug}: TLC did not finish:\n"
                          + out[-3000:])
    counts = re.findall(r"(\d+) states generated, (\d+) distinct states", out)
    if violated == TEMPORAL:
        # TLC checks liveness on its own timed schedule, so how far it got
        # before reporting -- and which lasso it reports -- is not
        # reproducible. The verdict is; only the verdict is recorded.
        return {"spec": model["spec"], "variant": bug,
                "expected": model["variants"][bug],
                "outcome": outcome, "violated": violated,
                "distinct_states": None, "trace": [], "loops_to": None}
    trace, loop = counterexample(out, model["observe"]) \
        if outcome != PASS else ([], None)
    return {"spec": model["spec"], "variant": bug,
            "expected": model["variants"][bug],
            "outcome": outcome, "violated": violated,
            "distinct_states": int(counts[-1][1]) if counts else None,
            "trace": trace, "loops_to": loop}


def simulate_one(jar: Path, model: dict, work: Path) -> list:
    out = _tlc(jar, model, "none", work, simulate=model["simulate"])
    if "Error:" in out:
        raise FormalError(f"{model['spec']}: simulation found an error:\n"
                          + out[-3000:])
    traces = []
    for f in sorted((work / "sim").iterdir(),
                    key=lambda p: [int(x) for x in p.name.split("_")[1:]]):
        traces.append(simulation_trace(f.read_text(), model["observe"]))
    return traces


def spec_digests() -> dict:
    return {m["spec"]: hashlib.sha256(
        (FORMAL / f"{m['spec']}.tla").read_bytes()).hexdigest()
        for m in MODELS}


def derive(jar: Path) -> tuple:
    require_jar(jar)
    rows, sims = [], {}
    with tempfile.TemporaryDirectory(prefix="formal-") as tmp:
        for model in MODELS:
            for bug in model["variants"]:
                rows.append(check_one(jar, model, bug,
                                      Path(tmp) / f"{model['spec']}-{bug}"))
            if model["simulate"]:
                sims[model["spec"]] = simulate_one(
                    jar, model, Path(tmp) / f"{model['spec']}-sim")
    results = {"schema": SCHEMA, "tla2tools": {"version": JAR_VERSION,
                                               "sha256": JAR_SHA256},
               "specs": spec_digests(), "results": rows}
    traces = {"schema": SCHEMA, "specs": spec_digests(), "traces": sims}
    return results, traces


# --------------------------------------------------------------- checking

def problems(results: dict, traces: dict | None = None) -> list:
    """Why the committed record does not hold, without running TLC."""
    out = []
    if results.get("schema") != SCHEMA:
        out.append(f"results schema is {results.get('schema')!r}")
    if results.get("tla2tools") != {"version": JAR_VERSION,
                                    "sha256": JAR_SHA256}:
        out.append("the record was not made with the pinned tla2tools")
    want = spec_digests()
    if results.get("specs") != want:
        stale = sorted(k for k in want
                       if results.get("specs", {}).get(k) != want[k])
        out.append(f"specifications changed since they were checked: {stale}")
    rows = {(r["spec"], r["variant"]): r for r in results.get("results", ())}
    for model in MODELS:
        for bug, expected in model["variants"].items():
            r = rows.pop((model["spec"], bug), None)
            if r is None:
                out.append(f"{model['spec']}/{bug}: not checked")
                continue
            got = PASS if r["outcome"] == PASS else r["violated"]
            if got != expected or r["expected"] != expected:
                out.append(f"{model['spec']}/{bug}: expected {expected}, "
                           f"TLC said {got}")
            if r["outcome"] != PASS and r["violated"] != TEMPORAL and \
                    not r["trace"]:
                out.append(f"{model['spec']}/{bug}: a violation without "
                           "its counterexample")
            if bug == "none" and not r["distinct_states"]:
                out.append(f"{model['spec']}: no states explored")
    for spec, bug in rows:
        out.append(f"{spec}/{bug}: recorded but not a model here")
    if traces is not None:
        if traces.get("specs") != want:
            out.append("simulated traces are of other specifications")
        for model in MODELS:
            if model["simulate"] and not traces.get("traces", {}).get(
                    model["spec"]):
                out.append(f"{model['spec']}: no simulated traces")
    return out


def _dump(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True) + "\n"


def _dump_compact(traces: dict) -> str:
    """One behaviour per line: a derived artefact, read by tests."""
    lines = [json.dumps({k: v for k, v in traces.items() if k != "traces"},
                        sort_keys=True)]
    for spec in sorted(traces["traces"]):
        for t in traces["traces"][spec]:
            lines.append(json.dumps({"spec": spec, "trace": t},
                                    sort_keys=True, separators=(",", ":")))
    return "\n".join(lines) + "\n"


def load_traces(text: str) -> dict:
    head, *rows = [json.loads(ln) for ln in text.splitlines() if ln]
    head["traces"] = {}
    for row in rows:
        head["traces"].setdefault(row["spec"], []).append(row["trace"])
    return head


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--jar", type=Path, required=True)
    sub.add_parser("check")
    p = sub.add_parser("fetch")
    p.add_argument("--dest", type=Path, required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "fetch":
            args.dest.mkdir(parents=True, exist_ok=True)
            target = args.dest / f"tla2tools-{JAR_VERSION}.jar"
            with urllib.request.urlopen(JAR_URL, timeout=120) as r:
                data = r.read()
            got = hashlib.sha256(data).hexdigest()
            if got != JAR_SHA256:
                raise FormalError(f"{JAR_URL} served sha256 {got}, not "
                                  f"{JAR_SHA256}; refusing it")
            target.write_bytes(data)
            print(target)
            return 0
        if args.cmd == "check":
            found = problems(json.loads(RESULTS.read_text()),
                             load_traces(TRACES.read_text()))
        else:
            results, traces = derive(args.jar)
            found = problems(results, traces)
            if args.cmd == "run":
                RESULTS.write_text(_dump(results))
                TRACES.write_text(_dump_compact(traces))
            else:
                if _dump(results) != RESULTS.read_text():
                    found.append("TLC's results differ from "
                                 "docs/formal/results.json")
                if _dump_compact(traces) != TRACES.read_text():
                    found.append("TLC's simulated traces differ from "
                                 "docs/formal/traces.json")
    except FormalError as exc:
        print(f"formal_check: {exc}", file=sys.stderr)
        return 2
    for p in found:
        print(f"PROBLEM: {p}")
    if not found:
        print(f"formal_check: {len(MODELS)} specifications, "
              f"{sum(len(m['variants']) for m in MODELS)} models, "
              "each as expected")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
