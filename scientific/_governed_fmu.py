"""Subprocess entry point: run the thermal_rc2 FMU under governance.

Launched by ``qta_agent.governed_model`` through the governed executor. The
FMU and its build record are cited by digest: the bytes on disk must hash
to what the caller cites, or nothing runs -- the result is about THAT
archive, not whatever sits at the path now. The FMU is read at the FMI
boundary, executed by fmpy in the runtime the task names (a declared,
recorded input; the governed environment inherits nothing), and its
ResultBundle written into the governed workspace.

It writes no authority event. An FMU result is a SIMULATION_RESULT and
NON_AUTHORITATIVE until an independent check and a reviewer say otherwise.
"""
from __future__ import annotations

import hashlib
import json
import sys


def _cited(WS, path: str, sha: str, what: str) -> bytes:
    raw = WS.assert_in_workspace(path, what="reads").read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha:
        raise SystemExit(f"the {what} on disk is not the {what} cited; "
                         "refusing to run bytes nobody asked about")
    return raw


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: _governed_fmu.py <json-inputs>", file=sys.stderr)
        return 2
    try:
        inputs = json.loads(argv[1])
    except ValueError as exc:
        print(f"inputs are not JSON: {exc}", file=sys.stderr)
        return 2

    from qta_multiphysics.stack import workspace as WS

    from .checks.fmu_rc2 import fmu_bundle

    try:
        _cited(WS, inputs["fmu_path"], inputs["fmu_sha256"], "FMU")
        record = json.loads(_cited(WS, inputs["build_record_path"],
                                   inputs["build_record_sha256"],
                                   "build record"))
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 3
    fmu = WS.assert_in_workspace(inputs["fmu_path"], what="reads")
    bundle = fmu_bundle(fmu, inputs["parameters"], step_s=inputs["step_s"],
                        build_record=record, exe=inputs["runtime_python"])
    with WS.governed_writer():
        out_dir = WS.guard_output_dir(inputs["out_dir"])
        sha = WS.write_json_deterministic(out_dir / "bundle.json",
                                          bundle.to_record())
    print(json.dumps({"path": WS.relpath_in_repo(out_dir / "bundle.json"),
                      "sha256": sha, "bundle_digest": bundle.digest(),
                      "all_invariants_hold": bundle.all_invariants_hold},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
