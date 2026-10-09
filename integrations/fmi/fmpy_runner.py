"""Run an FMU with fmpy -- an FMI runtime this repository did not write.

RUNS ONLY IN THE ISOLATED FMI RUNTIME (``tools/isolated_runtime.py create
fmi`` creates it
from ``integrations/fmi/runtime.lock``, every wheel by hash). It imports
nothing from this repository: one JSON request on argv[1], one JSON result
on argv[2]. The harness reads the FMU's description itself
(``scientific/fmi_boundary.py``); this program is the second, independent
reader and the only thing that executes the binary.

Requests:

* ``validate``  -- fmpy's own validation of the archive and description.
* ``describe``  -- fmpy's parse of every variable (name, causality,
                   variability, type, unit through its declared type).
* ``simulate``  -- instantiate, set parameters, initialise, step to t_end at
                   a fixed communication step with constant inputs, read
                   the outputs.
* ``state``     -- FMI-P1: step to t_split, save the state in memory and as
                   serialized bytes, step on to t_end; restore each and
                   replay; return all three trajectories' end values.
* ``refusals``  -- FMI-P2 and instantiation: a step from the wrong time, a
                   non-positive step, a parameter set after initialisation,
                   a non-finite input and a wrong instantiation token must
                   each be refused by the FMU.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys

import fmpy
from fmpy import extract, read_model_description
from fmpy.fmi3 import FMU3Slave
from fmpy.validation import validate_fmu

OUTPUTS = ("T1", "T2", "E_in", "E_out", "E_stored")


def _open(fmu: str, token: str | None = None):
    md = read_model_description(fmu)
    d = extract(fmu)
    vr = {v.name: v.valueReference for v in md.modelVariables}
    s = FMU3Slave(guid=token if token is not None else md.instantiationToken,
                  unzipDirectory=d,
                  modelIdentifier=md.coSimulation.modelIdentifier,
                  instanceName="harness")
    return md, vr, s


def _init(s, vr, start: dict, n_sub: int | None):
    s.instantiate()
    floats = {k: v for k, v in start.items()}
    if floats:
        s.setFloat64([vr[k] for k in floats], list(floats.values()))
    if n_sub is not None:
        s.setInt32([vr["n_sub"]], [int(n_sub)])
    s.enterInitializationMode(startTime=0.0)
    s.exitInitializationMode()


def _read(s, vr) -> dict:
    vals = s.getFloat64([vr[k] for k in OUTPUTS])
    return dict(zip(OUTPUTS, vals))


def _run(s, vr, t0: float, t1: float, h: float) -> float:
    t = t0
    n = int(round((t1 - t0) / h))
    for i in range(n):
        s.doStep(t, h)
        t = t0 + (i + 1) * h
    return t


def validate(req):
    return {"problems": list(validate_fmu(req["fmu"]))}


def describe(req):
    md = read_model_description(req["fmu"])
    out = []
    for v in md.modelVariables:
        unit = getattr(v, "unit", None) or (
            v.declaredType.unit if v.declaredType is not None else None)
        out.append({"name": v.name, "value_reference": v.valueReference,
                    "type": v.type, "causality": v.causality,
                    "variability": v.variability, "unit": unit})
    return {"fmi_version": md.fmiVersion,
            "instantiation_token": md.instantiationToken,
            "model_identifier": md.coSimulation.modelIdentifier,
            "can_get_set_state": bool(md.coSimulation.canGetAndSetFMUstate),
            "can_serialize_state":
                bool(md.coSimulation.canSerializeFMUstate),
            "units": sorted(u.name for u in md.unitDefinitions),
            "variables": out}


def simulate(req):
    md, vr, s = _open(req["fmu"])
    _init(s, vr, req["start"], req.get("n_sub"))
    t_end = _run(s, vr, 0.0, float(req["t_end"]), float(req["step"]))
    out = _read(s, vr)
    s.terminate()
    s.freeInstance()
    return {"t_end": t_end, "outputs": out}


def state(req):
    md, vr, s = _open(req["fmu"])
    _init(s, vr, req["start"], req.get("n_sub"))
    h, t_split, t_end = (float(req[k]) for k in ("step", "t_split", "t_end"))
    t = _run(s, vr, 0.0, t_split, h)
    saved = s.getFMUState()
    blob = bytes(s.serializeFMUState(saved))
    _run(s, vr, t, t_end, h)
    direct = _read(s, vr)
    s.setFMUState(saved)
    _run(s, vr, t_split, t_end, h)
    from_memory = _read(s, vr)
    restored = s.deserializeFMUState(blob)
    s.setFMUState(restored)
    _run(s, vr, t_split, t_end, h)
    from_bytes = _read(s, vr)
    # a corrupted serialization must be refused, never restored
    bad = bytearray(blob)
    bad[0] ^= 0xFF
    try:
        s.deserializeFMUState(bytes(bad))
        corrupt_refused = False
    except Exception:
        corrupt_refused = True
    s.terminate()
    s.freeInstance()
    return {"direct": direct, "from_memory": from_memory,
            "from_bytes": from_bytes,
            "serialized_bytes": len(blob),
            "serialized_sha256": hashlib.sha256(blob).hexdigest(),
            "corrupt_serialization_refused": corrupt_refused}


def refusals(req):
    out = {}

    def attempt(name, fn):
        try:
            fn()
            out[name] = False
        except Exception:
            out[name] = True

    md, vr, s = _open(req["fmu"])
    _init(s, vr, {}, None)
    s.doStep(0.0, 10.0)
    attempt("step_from_wrong_time", lambda: s.doStep(999.0, 10.0))
    attempt("non_positive_step", lambda: s.doStep(10.0, 0.0))
    attempt("parameter_after_initialisation",
            lambda: s.setFloat64([vr["C1"]], [1.0]))
    attempt("non_finite_input", lambda: s.setFloat64([vr["Q"]], [math.nan]))
    s.terminate()
    s.freeInstance()
    _, _, wrong = _open(req["fmu"],
                        token="{00000000-0000-0000-0000-000000000000}")
    attempt("wrong_instantiation_token", wrong.instantiate)
    return {"refused": out}


def main(argv) -> int:
    req = json.loads(open(argv[1], encoding="utf-8").read())
    fn = {"validate": validate, "describe": describe, "simulate": simulate,
          "state": state, "refusals": refusals}[req["kind"]]
    res = fn(req)
    res["kind"] = req["kind"]
    res["request_sha256"] = hashlib.sha256(
        json.dumps(req, sort_keys=True).encode()).hexdigest()
    res["runtime"] = {"fmpy": fmpy.__version__,
                      "python": sys.version.split()[0]}
    with open(argv[2], "w", encoding="utf-8") as fh:
        json.dump(res, fh, sort_keys=True, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
