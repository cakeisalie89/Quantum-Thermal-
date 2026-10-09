#!/usr/bin/env python3
"""Decide each selective-Rust kernel ADOPTED or REJECTED, from measurement.

The admission mechanism (``qta_multiphysics/stack/rust_kernel.py``) has
existed since Stage 10; what did not exist was a decision. A kernel sat in
"maybe Rust": not in force, not rejected, and -- worse -- with a parity
verdict that the host's NumPy dispatch could flip (D-2026-58), so the backend
``dispatch()`` would choose was a property of the machine. This tool measures
what directive s.21 lists and writes a decision per kernel:

1. parity against the NumPy reference, bit for bit (the canonical outputs
   are compared by SHA-256, so the equivalence semantics for this path is
   byte identity), in EVERY NumPy dispatch configuration this host can
   present: native, with X86_V4 (AVX-512) disabled, and at the X86_V2
   baseline -- each in a fresh interpreter, because the dispatch is fixed
   at import;
2. performance: the best of several timed repetitions, NumPy against Rust,
   at the parity size and at a workload size;
3. workload relevance: the production call sites, found by scanning every
   tracked module outside tests for an import of the kernels or of their
   dispatcher;
4. build reproducibility: the extension's digest, which the hosted job
   compares across two builds.

THE RULE. A kernel is ADOPTED only if it is bit-identical in every measured
configuration, has at least one production call site, and is at least
``ADOPT_SPEEDUP_MIN`` times faster at the workload size. Anything else is
REJECTED, with every criterion it failed -- and REJECTED is a completed
decision: NumPy stays in force and nothing is weakened to let Rust in.

    python tools/rust_kernel_decision.py measure --python PY --out M.json
    python tools/rust_kernel_decision.py decide M.json   # writes the registry
    python tools/rust_kernel_decision.py check [M.json]  # exit 1 on drift
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DECISIONS = ROOT / "docs" / "rust_kernel_decisions.json"
SCHEMA = "rust-kernel-decisions/1"
ADOPT_SPEEDUP_MIN = 2.0
WORKLOAD_N = 1_000_000
#: NumPy 2.4 groups its x86 dispatch targets; disabling a group removes it
#: and everything above it. Names are NumPy's own.
DISPATCH_CONFIGS = {
    "native": "",
    "without_x86_v4": "X86_V4",
    "x86_v2_baseline": "X86_V4 X86_V3",
}
#: modules that may name the kernels without consuming them: the mechanism
#: itself and this tool.
NOT_CONSUMERS = {"qta_multiphysics/stack/rust_kernel.py",
                 "tools/rust_kernel_decision.py"}

_CHILD = r"""
import json, os, sys, timeit
sys.path.insert(0, sys.argv[1])
import numpy as np
from qta_multiphysics.stack import rust_kernel as R
out = {"requested_disable": os.environ.get("NPY_DISABLE_CPU_FEATURES", ""),
       "numpy_version": np.__version__,
       "simd": np.show_config(mode="dicts")["SIMD Extensions"],
       "extension_importable": R.rust_available(), "kernels": {}}
if R.rust_available():
    import qta_kernels, hashlib
    out["extension_sha256"] = hashlib.sha256(
        open(qta_kernels.qta_kernels.__file__, "rb").read()).hexdigest()
for name in sorted(R.KERNELS):
    rep = R.kernel_parity(name)
    k = {"bit_identical": rep.get("bit_identical"),
         "max_ulp_difference": rep.get("max_ulp_difference"),
         "n_test_values": rep.get("n_test_values")}
    if sys.argv[2] == "time" and R.rust_available():
        times = {}
        for n in (R.PARITY_N, int(sys.argv[3])):
            args, kw = R._test_vectors(name, n=n)
            ref, fn = R.KERNELS[name]["numpy"], R._rust_fn(name)
            reps = 5 if n > R.PARITY_N else 50
            t_np = min(timeit.repeat(lambda: ref(*args, **kw), number=reps,
                                     repeat=7)) / reps
            t_rs = min(timeit.repeat(lambda: fn(*args, **kw), number=reps,
                                     repeat=7)) / reps
            times[str(n)] = {"numpy_s": t_np, "rust_s": t_rs,
                             "speedup": t_np / t_rs}
        k["timing"] = times
    out["kernels"][name] = k
print(json.dumps(out, sort_keys=True))
"""


def call_sites(root: Path = ROOT) -> dict:
    """Tracked modules outside tests that import the kernels or their
    dispatcher, per kernel name they reference."""
    files = subprocess.run(["git", "ls-files", "*.py"], cwd=root,
                           capture_output=True, text=True,
                           check=True).stdout.split()
    found: dict = {}
    for rel in files:
        if rel.startswith("tests/") or rel in NOT_CONSUMERS:
            continue
        try:
            tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, FileNotFoundError):
            continue
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""] + [
                    f"{node.module}.{a.name}" for a in node.names]
            if any(m.endswith("rust_kernel") or m.split(".")[0] ==
                   "qta_kernels" for m in mods):
                found.setdefault(rel, getattr(node, "lineno", 0))
    return dict(sorted(found.items()))


def measure(python: str, workload_n: int = WORKLOAD_N) -> dict:
    configs = {}
    for label, disable in DISPATCH_CONFIGS.items():
        env = dict(os.environ, NPY_DISABLE_CPU_FEATURES=disable,
                   PYTHONHASHSEED="0")
        r = subprocess.run([python, "-I", "-c", _CHILD, str(ROOT),
                            "time" if label == "native" else "parity",
                            str(workload_n)], env=env, capture_output=True,
                           text=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(f"{label}: {r.stderr.strip()[-2000:]}")
        configs[label] = json.loads(r.stdout)
    return {"schema": "rust-kernel-measurement/1", "configs": configs,
            "call_sites": call_sites(), "workload_n": workload_n}


def decide(m: dict) -> dict:
    """The decision per kernel, from one measurement. Pure."""
    configs = m["configs"]
    native = configs["native"]
    if not all(c.get("extension_importable") for c in configs.values()):
        raise SystemExit("the extension was not importable in every "
                         "configuration: nothing was measured")
    out = {}
    names = sorted(native["kernels"])
    consumers = sorted(m["call_sites"])
    for name in names:
        parity = {lab: c["kernels"][name]["bit_identical"]
                  for lab, c in configs.items()}
        ulps = {lab: c["kernels"][name]["max_ulp_difference"]
                for lab, c in configs.items()}
        timing = native["kernels"][name]["timing"]
        speedup = timing[str(m["workload_n"])]["speedup"]
        failed = []
        if not all(parity.values()):
            failed.append("NOT_BIT_IDENTICAL_IN_EVERY_DISPATCH: max ulp "
                          + ", ".join(f"{k} {v}" for k, v in ulps.items()))
        if not consumers:
            failed.append("NO_PRODUCTION_CALL_SITE: no scientific path "
                          "consumes it, so no workload gains from it")
        if speedup < ADOPT_SPEEDUP_MIN:
            failed.append(f"SPEEDUP_BELOW_{ADOPT_SPEEDUP_MIN:g}X: "
                          f"{speedup:.2f}x at n={m['workload_n']}")
        out[name] = {"decision": f"RUST_KERNEL_{name}_"
                                 + ("ADOPTED" if not failed else "REJECTED"),
                     "failed_criteria": failed,
                     "parity_by_dispatch": parity,
                     "max_ulp_by_dispatch": ulps,
                     "speedup_at_workload": round(speedup, 2),
                     "speedup_at_parity_size": round(
                         timing[str(4096)]["speedup"], 2)}
    return out


def registry(m: dict) -> dict:
    kernels = decide(m)
    native = m["configs"]["native"]
    active = any(k["decision"].endswith("_ADOPTED") for k in kernels.values())
    return {
        "schema": SCHEMA,
        "admission_mechanism": "RUST_ADMISSION_MECHANISM_ADOPTED",
        "backend": "RUST_BACKEND_ACTIVE" if active
                   else "RUST_BACKEND_NOT_ACTIVE",
        "rule": f"ADOPTED only if bit-identical to the NumPy reference in "
                f"every measured dispatch configuration "
                f"({', '.join(DISPATCH_CONFIGS)}), at least one production "
                f"call site, and >= {ADOPT_SPEEDUP_MIN:g}x faster at "
                f"n={m['workload_n']}; otherwise REJECTED -- a completed "
                "decision, with NumPy in force",
        "selection": "explicit only: rust_kernel.dispatch(name, "
                     "backend='rust') for an ADOPTED kernel whose "
                     "certificate matches this process's NumPy version and "
                     "dispatch; a REJECTED kernel is refused even when "
                     "requested, and the host never chooses",
        "measured_on": {"numpy_version": native["numpy_version"],
                        "simd": native["simd"],
                        "extension_sha256": native.get("extension_sha256")},
        "call_sites": m["call_sites"],
        "kernels": kernels,
    }


def check(measurement: dict | None = None) -> list:
    doc = json.loads(DECISIONS.read_text(encoding="utf-8"))
    problems = []
    if doc.get("schema") != SCHEMA:
        problems.append(f"schema {doc.get('schema')!r}")
    now = call_sites()
    if now != doc["call_sites"]:
        problems.append(f"production call sites changed: recorded "
                        f"{doc['call_sites']}, now {now} -- re-measure and "
                        "re-decide")
    import importlib
    sys.path.insert(0, str(ROOT))
    rk = importlib.import_module("qta_multiphysics.stack.rust_kernel")
    if sorted(doc["kernels"]) != sorted(rk.KERNELS):
        problems.append(f"kernels {sorted(doc['kernels'])} != the "
                        f"mechanism's {sorted(rk.KERNELS)}")
    if measurement is not None:
        fresh = decide({**measurement, "call_sites": now})
        for name, d in fresh.items():
            rec = doc["kernels"].get(name, {})
            if rec.get("decision") != d["decision"]:
                problems.append(f"{name}: recorded {rec.get('decision')}, "
                                f"this measurement decides {d['decision']} "
                                f"({d['failed_criteria']})")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    me = sub.add_parser("measure")
    me.add_argument("--python", required=True)
    me.add_argument("--out", required=True)
    me.add_argument("--workload-n", type=int, default=WORKLOAD_N)
    de = sub.add_parser("decide")
    de.add_argument("measurement")
    ch = sub.add_parser("check")
    ch.add_argument("measurement", nargs="?")
    args = ap.parse_args(argv)
    if args.cmd == "measure":
        m = measure(args.python, args.workload_n)
        Path(args.out).write_text(json.dumps(m, indent=1, sort_keys=True)
                                  + "\n", encoding="utf-8")
        for name, d in decide(m).items():
            print(f"{name}: {d['decision']} {d['failed_criteria']}")
        return 0
    if args.cmd == "decide":
        m = json.loads(Path(args.measurement).read_text(encoding="utf-8"))
        DECISIONS.write_text(json.dumps(registry(m), indent=1,
                                        sort_keys=True) + "\n",
                             encoding="utf-8")
        print(f"wrote {DECISIONS.relative_to(ROOT)}")
        return 0
    given = (json.loads(Path(args.measurement).read_text(encoding="utf-8"))
             if args.measurement else None)
    problems = check(given)
    for p in problems:
        print(p)
    print("rust kernel decisions: " + ("OK" if not problems else
                                       f"{len(problems)} problem(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
