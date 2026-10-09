"""Subprocess entry point: run one admitted model under governance.

Launched by ``qta_agent.governed_model`` through the governed executor --
policy, capability, bounded subprocess -- never called in-process. It
resolves the model in the closed catalog, validates the parameters, runs it,
and writes the ResultBundle and its artefacts into the governed workspace.

It has no handle on the event log and writes no authority event. What it
produces is a claim about a computation; whether the claim is accepted is
decided elsewhere, by others.
"""
from __future__ import annotations

import json
import sys


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: _governed_run.py <json-inputs>", file=sys.stderr)
        return 2
    try:
        inputs = json.loads(argv[1])
    except ValueError as exc:
        print(f"inputs are not JSON: {exc}", file=sys.stderr)
        return 2

    from qta_multiphysics.stack import workspace as WS

    from .catalog import models
    from .model import run_model_with_artifacts

    model = models().lookup(inputs["model_id"], inputs["model_version"])
    bundle, payloads = run_model_with_artifacts(model, inputs["parameters"])
    with WS.governed_writer():
        out_dir = WS.guard_output_dir(inputs["out_dir"])
        for name, data in sorted(payloads.items()):
            target = WS.assert_in_workspace(out_dir / f"{name}.bin")
            target.write_bytes(data)
        sha = WS.write_json_deterministic(out_dir / "bundle.json",
                                          bundle.to_record())
    print(json.dumps({"path": WS.relpath_in_repo(out_dir / "bundle.json"),
                      "sha256": sha, "bundle_digest": bundle.digest(),
                      "all_invariants_hold": bundle.all_invariants_hold},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
