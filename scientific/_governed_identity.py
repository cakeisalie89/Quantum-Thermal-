"""Subprocess entry point: the run identity of a proposed model run.

Launched by ``qta_agent.governed_model`` before a run, so a reuse decision is
made against what the run WOULD be here -- this implementation digest, these
validated parameters, this environment -- computed on the scientific side,
where the model is known, and handed to the authority side as a document.
It runs nothing and decides nothing: the comparison with a prior result, and
the check that the prior's evidence is intact, happen in the bridge.
"""
from __future__ import annotations

import json
import sys


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: _governed_identity.py <json-inputs>", file=sys.stderr)
        return 2
    try:
        inputs = json.loads(argv[1])
    except ValueError as exc:
        print(f"inputs are not JSON: {exc}", file=sys.stderr)
        return 2

    from qta_multiphysics.stack import workspace as WS

    from .catalog import models
    from .model import run_identity_for

    model = models().lookup(inputs["model_id"], inputs["model_version"])
    identity = run_identity_for(model, model.validate(inputs["parameters"]))
    with WS.governed_writer():
        out_dir = WS.guard_output_dir(inputs["out_dir"])
        target = out_dir / "identity.json"
        sha = WS.write_json_deterministic(target, identity.to_record())
    print(json.dumps({"path": WS.relpath_in_repo(target), "sha256": sha,
                      "identity_digest": identity.digest()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
