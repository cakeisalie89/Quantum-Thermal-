#!/usr/bin/env python3
"""Which numbers in the canonical outputs were decided by a clip.

WHY THIS EXISTS

``gas_transport_profile.csv`` stated ``0.000000000e+00`` for methane in all
120 cells. The Mode-C solve had not produced zero: it had produced values
between -1.9e+01 and -1.8e-10 -- noise, in an integrator whose own absolute
tolerance is 1e3 -- and ``np.clip(so.y, 0.0, None)`` turned every one of them
into an exact zero, which ``%.9e`` then wrote out to ten significant figures.
On a runner whose BLAS dispatch differs the same noise lands positive, the
clip leaves it alone, and the file states 1.6e-02 instead. Two machines, one
model, two different claims about residual contamination at Mode D entry, and
nothing in either artefact marking the number as one the solve cannot see.

The defect was not the zero. It was that the file carried a value and not the
resolution of the method that produced it, so a species absent by design
(helium in Mode C) and a species present but unresolved (methane) were written
identically.

WHAT IS MEASURED RATHER THAN ASSERTED

A clip that moves a value is a value the method could not produce inside the
physical range. So every ``numpy.clip`` in the scientific tree is instrumented
during a real canonical run, and what comes back is a measurement: which sites
move values, how many, and how far. Reading the source would have found the
same two sites; it would not have established that the other nine never fire,
which is the half of the statement that makes "the defect is confined to gas
transport" a measurement instead of an impression.

WHAT IS REVIEWED RATHER THAN DERIVED

Whether a moving clip reaches a serialised file, and what states the
resolution there. That is a judgement about the output contract and cannot be
read off a stack frame. It is committed in ``docs/clip_provenance.json``, and
the mechanical parts around it are checked:

  * a site that moves values and is not in the inventory fails. A new clip
    cannot start deciding serialised numbers silently;
  * an inventory entry whose site the run never reaches fails, so the table
    cannot drift into describing a past version of the code;
  * ``moves`` must agree with the measurement, in both directions;
  * a moving clip that reaches an output must name what declares the
    resolution there. "It is only noise" is exactly the claim that needs the
    floor written down, because noise and a small measurement are the same
    digits.

A run in which nothing was instrumented is a failure, not a pass.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
import tempfile
import traceback

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
INVENTORY = ROOT / "docs" / "clip_provenance.json"
TREE = "/qta_multiphysics/"


class ScopeError(RuntimeError):
    """The measurement covered nothing, so its verdict means nothing."""


def _site(root: pathlib.Path):
    """The innermost frame inside the scientific tree: ``path:function``.

    Keyed by function rather than by line so that editing anything above a
    clip does not invalidate the inventory. Two clips in one function share an
    entry, which is correct: they share the judgement about what that function
    does to its output.
    """
    for fr in reversed(traceback.extract_stack()[:-2]):
        if TREE in fr.filename:
            rel = pathlib.Path(fr.filename).relative_to(root).as_posix()
            return f"{rel}:{fr.name}"
    return None


def measure(root: pathlib.Path):
    """Run the canonical pipeline with every clip in the tree observed."""
    rec = collections.defaultdict(
        lambda: {"calls": 0, "moving_calls": 0, "elements": 0,
                 "elements_moved": 0, "max_displacement": 0.0,
                 "worst_raw": None, "worst_written": None})
    real = np.clip

    def observed(a, a_min, a_max, *args, **kw):
        out = real(a, a_min, a_max, *args, **kw)
        key = _site(root)
        if key is None:
            return out
        r = rec[key]
        r["calls"] += 1
        try:
            arr = np.asarray(a, dtype=float)
            res = np.asarray(out, dtype=float)
            moved = arr != res
            r["elements"] += int(arr.size)
            n = int(moved.sum())
            if n:
                r["moving_calls"] += 1
                r["elements_moved"] += n
                d = np.abs(arr - res)
                worst = int(d.argmax())
                if float(d.ravel()[worst]) > r["max_displacement"]:
                    r["max_displacement"] = float(d.ravel()[worst])
                    r["worst_raw"] = float(arr.ravel()[worst])
                    r["worst_written"] = float(res.ravel()[worst])
        except (TypeError, ValueError):
            # A clip over something that is not a float array still counts as
            # a call; it simply has no displacement to report.
            pass
        return out

    np.clip = observed
    try:
        # The script's own directory is what Python puts on the path, so the
        # package the pipeline lives in has to be asked for explicitly.
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        # The SAME three entry points qta_full_sim.py drives, and for the same
        # reason the scope of this measurement has to match the scope of its
        # claim: run_all alone reaches nine clip sites and would have reported
        # "every clip in the scientific tree" about the 1D/2D layer only,
        # leaving the integrated and 3D layers unmeasured and unmentioned.
        # (--deep is off in the canonical pipeline, so the deep SBI layer is
        # outside this measurement and outside what it claims.)
        from qta_multiphysics.integrated_layers import run_integrated_layers
        from qta_multiphysics.runner_3d import run_3d_all
        import qta_multiphysics.runner as runner
        with tempfile.TemporaryDirectory(prefix="clip-provenance-") as d:
            out = pathlib.Path(d)
            runner.run_all(out, verbose=False)
            run_integrated_layers(profile="ci", output_dir=out, seed=42)
            run_3d_all(out, heavy=False)
    finally:
        np.clip = real

    if not rec:
        raise ScopeError(
            "no clip anywhere in the scientific tree was observed during a "
            "full canonical run; the instrument measured nothing, and a "
            "verdict drawn from an empty measurement is not a verdict")
    return dict(rec)


def check(measured, inventory):
    """Reconcile the measurement against the committed judgements."""
    problems = []
    declared = inventory.get("sites", {})

    for site in sorted(set(measured) - set(declared)):
        m = measured[site]
        problems.append(
            f"{site}: observed but not in the inventory "
            f"({m['elements_moved']} of {m['elements']} elements moved); a "
            "clip nothing describes may already be deciding serialised "
            "numbers")
    for site in sorted(set(declared) - set(measured)):
        problems.append(
            f"{site}: in the inventory but never reached by a canonical run; "
            "the table is describing a past version of the code")

    for site in sorted(set(declared) & set(measured)):
        m, d = measured[site], declared[site]
        moves = m["elements_moved"] > 0
        if bool(d.get("moves")) != moves:
            problems.append(
                f"{site}: inventory says moves={d.get('moves')!r}, the run "
                f"measured {m['elements_moved']} moved elements")
            continue
        if not moves:
            continue
        reaches = d.get("reaches_output") or []
        if reaches and not d.get("resolution_declared"):
            problems.append(
                f"{site}: moves values that reach {reaches} and names nothing "
                "that states the resolution there")
        if not d.get("test"):
            problems.append(
                f"{site}: moves values and names no regression test")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="record the measurement into the inventory, keeping "
                         "the reviewed judgements already there")
    ap.add_argument("--json", action="store_true",
                    help="print the measurement")
    args = ap.parse_args(argv)

    try:
        measured = measure(ROOT)
    except ScopeError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(measured, indent=1, sort_keys=True))

    inventory = (json.loads(INVENTORY.read_text())
                 if INVENTORY.exists() else {"sites": {}})

    if args.write:
        sites = {}
        for site, m in sorted(measured.items()):
            prior = inventory.get("sites", {}).get(site, {})
            sites[site] = {
                "moves": m["elements_moved"] > 0,
                "elements_moved": m["elements_moved"],
                "elements": m["elements"],
                "max_displacement": m["max_displacement"],
                "worst_raw": m["worst_raw"],
                "worst_written": m["worst_written"],
                "what": prior.get("what", "UNREVIEWED"),
                "reaches_output": prior.get("reaches_output", []),
                "resolution_declared": prior.get("resolution_declared"),
                "test": prior.get("test"),
            }
        inventory["sites"] = sites
        inventory.setdefault(
            "note",
            "Measured by tools/clip_provenance.py during a full canonical "
            "run. 'moves', the counts and the displacements are measured; "
            "'what', 'reaches_output', 'resolution_declared' and 'test' are "
            "reviewed judgements. A clip that moves a value produced a number "
            "the method could not produce inside the physical range.")
        INVENTORY.write_text(
            json.dumps(inventory, indent=1, sort_keys=True) + "\n")
        print(f"wrote {INVENTORY.relative_to(ROOT)} ({len(sites)} sites)")

    problems = check(measured, inventory)
    moving = sorted(s for s, m in measured.items() if m["elements_moved"])
    print(f"{len(measured)} clip sites observed in the scientific tree; "
          f"{len(moving)} move values")
    for site in moving:
        m = measured[site]
        print(f"  {site}: {m['elements_moved']}/{m['elements']} elements, "
              f"max displacement {m['max_displacement']:.6e} "
              f"({m['worst_raw']:.6e} -> {m['worst_written']:.6e})")
    if problems:
        print(f"\nREFUSED: {len(problems)} problems", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("inventory agrees with the measurement")
    return 0


if __name__ == "__main__":
    sys.exit(main())
