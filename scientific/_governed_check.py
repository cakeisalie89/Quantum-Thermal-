"""Subprocess entry point: run an admitted independent check on a bundle.

Launched by ``qta_agent.governed_model`` as a SEPARATE governed task, by a
different executor from the one that produced the bundle. It reads the
bundle from the governed workspace, refuses it unless its bytes hash to the
digest the caller cites (the evidence the check is about, not whatever is on
disk now), runs the check -- code with its own implementation digest -- and
writes the VerificationResult.

It writes no authority event. A VerificationResult is evidence; the
authority decision is made by a reviewer who reads it.
"""
from __future__ import annotations

import hashlib
import json
import sys


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: _governed_check.py <json-inputs>", file=sys.stderr)
        return 2
    try:
        inputs = json.loads(argv[1])
    except ValueError as exc:
        print(f"inputs are not JSON: {exc}", file=sys.stderr)
        return 2

    from qta_multiphysics.stack import workspace as WS

    from .catalog import check
    from .result import ResultBundle

    src = WS.assert_in_workspace(inputs["bundle_path"], what="reads")
    raw = src.read_bytes()
    if hashlib.sha256(raw).hexdigest() != inputs["bundle_sha256"]:
        print("the bundle on disk is not the bundle cited; refusing to "
              "verify bytes nobody asked about", file=sys.stderr)
        return 3
    bundle = ResultBundle.from_record(json.loads(raw))
    run_check, _ = check(inputs["check_id"])
    result = run_check(bundle, verifier_id=inputs["verifier_id"])
    with WS.governed_writer():
        out_dir = WS.guard_output_dir(inputs["out_dir"])
        target = out_dir / "verification.json"
        sha = WS.write_json_deterministic(target, result.to_record())
    print(json.dumps({"path": WS.relpath_in_repo(target), "sha256": sha,
                      "status": result.status.value}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
