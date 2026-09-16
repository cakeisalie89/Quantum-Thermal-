"""The subprocess entry point for the governed Stage-10 digest index.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

THE SECOND GOVERNED TOOL, AND WHY A SECOND ONE MATTERS

The first governed tool writes a JSON payload the caller handed it. That is
the safest thing a tool can do, and it made the governed path's guarantees
hard to distinguish from a well-behaved ``json.dump``: re-execution could
not disagree, because the output was a pure function of an argument that was
already in the task record.

This tool's output is a function of the WORKSPACE. It reads files that other
Stage-10 rules produced and derives an index over their bytes. Three
consequences follow, and each of them is a property the first tool could not
exercise:

  * re-execution is a real comparison. Re-running it re-reads the files, so a
    file that changed between execution and verification produces a digest
    disagreement rather than the same constant twice.
  * it has a genuine failure mode. A named file that is missing, unreadable
    or outside the workspace fails the run, and a FAILED run never reaches
    verification.
  * it needs a READ guard, not only a write guard. A tool that hashes a path
    the caller names can be pointed at ``results_gate_table.csv`` or at
    ``/etc/passwd``, and either would put a digest of a file the substrate
    must not mediate into a provenance chain. The write allowlist says
    nothing about that, because nothing is being written.

The index is not a gate, not a manifest and not a canonical output. It says
what the workspace contained when a governed run looked at it, which is
provenance and not validity.
"""
from __future__ import annotations

import json
import sys


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: _stage10_index_tool.py <json-inputs>", file=sys.stderr)
        return 2
    try:
        inputs = json.loads(argv[1])
    except ValueError as exc:
        print(f"inputs are not JSON: {exc}", file=sys.stderr)
        return 2

    from qta_multiphysics.stack import workspace as WS

    files = inputs["files"]
    if not isinstance(files, list) or not files:
        # A zero-file index is the vacuous-success shape this repository has
        # already been bitten by once: "0 of 0 files hashed" reads exactly
        # like a complete index and certifies nothing at all.
        print("files must be a non-empty list", file=sys.stderr)
        return 2

    entries = []
    for rel in files:
        if not isinstance(rel, str):
            print(f"file entry is not a string: {rel!r}", file=sys.stderr)
            return 2
        # READ guard. Same allowlist as the write guard, and for the same
        # reason: the substrate may look at the Stage-10 workspace and
        # nowhere else. A path that escapes by symlink resolves out and is
        # refused here rather than being hashed under the name it was given.
        try:
            target = WS.assert_in_workspace(rel, what="reads")
        except ValueError as exc:
            print(f"refusing to index {rel}: {exc}", file=sys.stderr)
            return 3
        if not target.is_file():
            print(f"no such file to index: {rel}", file=sys.stderr)
            return 4
        data = target.read_bytes()
        entries.append({
            "path": WS.relpath_in_repo(target),
            "sha256": WS.sha256_file(target),
            "bytes": len(data),
        })

    entries.sort(key=lambda e: e["path"])
    index = {
        "label": "MODEL_ONLY / FORECAST_ONLY",
        "automatic_gate_effect": "NONE",
        "n_files": len(entries),
        "files": entries,
        "does_not_mean": (
            "an index records which bytes were present when a governed run "
            "read them. It is not a gate, not a manifest, and not evidence "
            "that any of the indexed files is scientifically valid"),
    }

    with WS.governed_writer():
        out_dir = WS.guard_output_dir(inputs["out_dir"])
        target = out_dir / inputs["name"]
        sha = WS.write_json_deterministic(target, index)
    print(json.dumps({"path": WS.relpath_in_repo(target), "sha256": sha,
                      "n_files": len(entries)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
