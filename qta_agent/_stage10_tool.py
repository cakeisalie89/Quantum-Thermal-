"""The subprocess entry point for governed Stage-10 artifact emission.

A separate module, and a separate PROCESS, on purpose. The governor applies
kernel limits to a process it does not share, so the tool cannot exhaust the
supervisor's memory, spin without being interruptible, or exit the supervisor
by exiting itself. Running this in-process would give up all three.

It writes through the Stage-10 workspace guard rather than around it, so a
governed run is subject to exactly the same write allowlist as an ungoverned
one -- the substrate adds authority, it does not replace the guard that was
already there.
"""
from __future__ import annotations

import json
import sys


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: _stage10_tool.py <json-inputs>", file=sys.stderr)
        return 2
    try:
        inputs = json.loads(argv[1])
    except ValueError as exc:
        print(f"inputs are not JSON: {exc}", file=sys.stderr)
        return 2

    from qta_multiphysics.stack import workspace as WS

    # The governed-writer scope is opened HERE, in the subprocess the
    # executor launched, and nowhere in the ungoverned tree. That is what
    # makes ``verification/stage10/governed/out`` a directory only the
    # governed path writes rather than a directory the governed path
    # happens to be the only current writer of.
    with WS.governed_writer():
        out_dir = WS.guard_output_dir(inputs["out_dir"])
        target = out_dir / inputs["name"]
        sha = WS.write_json_deterministic(target, inputs["payload"])
    print(json.dumps({"path": WS.relpath_in_repo(target), "sha256": sha},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
