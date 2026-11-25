"""A design of experiments over an admitted ScientificModel, run by OpenMDAO
and checked case by case against a direct run of the same model.

WHAT IT IS. An exploration: how declared outputs move as declared parameters
vary over a range the caller states. Every case is a SIMULATION_RESULT of
the model under its own assumptions; the report is NOT_A_RECOMMENDATION and
has no authority. Choosing a design from it is a person's decision.

WHAT IS REFUSED, before anything runs:

* a design variable the model does not declare, or one that is not numeric;
* a range that leaves the parameter's declared bounds -- the model's
  applicability ends there, and a DOE outside it would explore an
  extrapolation as if it were the model;
* a response that is not a numeric output of the model.

THE CHECK. OpenMDAO sits between the sample plan and the model: it turns
inputs into its own arrays, calls the component, and hands outputs back. Any
of those steps could coerce a value. So every case is ALSO run directly
through the model with the same inputs, and the two outputs compared byte
for byte (``float.hex``). The expected verdict is BITWISE_IDENTITY: the same
implementation on the same inputs in the same process. Anything else is a
defect in the plumbing, reported, never averaged away.

THE PLAN. A Latin hypercube from ``numpy.random.Generator(PCG64(seed))``,
computed here and handed to OpenMDAO's ``ListGenerator``, so the plan is a
property of the seed and of this file rather than of an OpenMDAO version.

OpenMDAO is optional (the ``uq`` extra). Without it the exploration is
UNAVAILABLE; no other driver is substituted.
"""
from __future__ import annotations

import hashlib
import json
import math

SCHEMA = "openmdao-doe/1"
LABEL = "EXPLORATION_ONLY NOT_A_RECOMMENDATION"


class DOERefused(ValueError):
    pass


def available() -> bool:
    try:
        import openmdao.api  # noqa: F401
    except ImportError:
        return False
    return True


def _admit(model, design: dict, responses: tuple) -> dict:
    params = {p.name: p for p in model.parameter_schema.parameters}
    out = {}
    for name, rng in design.items():
        p = params.get(name)
        if p is None:
            raise DOERefused(f"{name} is not a parameter of "
                             f"{model.model_id}")
        if p.kind not in ("float", "int"):
            raise DOERefused(f"{name} is {p.kind}, not numeric")
        lo, hi = (float(v) for v in rng)
        if not lo < hi:
            raise DOERefused(f"{name}: empty range [{lo}, {hi}]")
        if (p.minimum is not None and lo < p.minimum) or \
                (p.maximum is not None and hi > p.maximum):
            raise DOERefused(
                f"{name}: [{lo}, {hi}] leaves the declared bounds "
                f"[{p.minimum}, {p.maximum}] -- outside them the model does "
                "not apply")
        out[name] = (lo, hi, p.unit, p.kind)
    if not responses:
        raise DOERefused("no response named")
    return out


def latin_hypercube(design: dict, n: int, seed: int) -> list:
    """``n`` points, one per stratum on every axis, from a seeded PCG64."""
    import numpy as np
    rng = np.random.Generator(np.random.PCG64(seed))
    names = sorted(design)
    cols = []
    for _ in names:
        u = (rng.permutation(n) + rng.random(n)) / n
        cols.append(u)
    pts = []
    for i in range(n):
        pt = {}
        for name, col in zip(names, cols):
            lo, hi, _unit, kind = design[name]
            v = lo + float(col[i]) * (hi - lo)
            pt[name] = int(round(v)) if kind == "int" else v
        pts.append(pt)
    return pts


def _outputs(model, params: dict, responses: tuple) -> dict:
    bundle = model.run(params)
    got = {}
    for r in responses:
        o = bundle.output(r)
        if o.quantity is None:
            raise DOERefused(f"{r} is {o.status.value}: {o.reason}")
        got[r] = (float(o.quantity.value), o.quantity.unit)
    return got


def cross_check(model, seen: list, responses: tuple, names: list) -> tuple:
    """Re-run every case directly and compare with what came back through
    OpenMDAO, byte for byte. ``(rows, mismatched case indices)``."""
    rows, mismatches = [], []
    for i, (params, got) in enumerate(seen):
        direct = _outputs(model, params, responses)
        same = all(float(got[r][0]).hex() == float(direct[r][0]).hex()
                   and got[r][1] == direct[r][1] for r in responses)
        if not same:
            mismatches.append(i)
        rows.append({"case": i,
                     "inputs": {n: params[n] for n in names},
                     "outputs": {r: {"value": got[r][0], "unit": got[r][1]}
                                 for r in responses},
                     "direct_run_identical": same})
    return rows, mismatches


def run_doe(model, base: dict, design: dict, responses: tuple, *,
            n_samples: int = 8, seed: int = 20261013) -> dict:
    """Run the plan through OpenMDAO, cross-check every case directly, and
    return the report. Raises :class:`DOERefused` before running anything
    inadmissible."""
    spec = _admit(model, design, tuple(responses))
    plan = latin_hypercube(spec, n_samples, seed)
    if not available():
        return {"schema": SCHEMA, "availability": "UNAVAILABLE",
                "label": LABEL, "reason": "openmdao is not installed"}
    import numpy as np
    import openmdao
    import openmdao.api as om

    seen: list = []
    names = sorted(spec)

    class ModelComponent(om.ExplicitComponent):
        def setup(self):
            for n in names:
                self.add_input(n, val=float(spec[n][0]))
            for r in responses:
                self.add_output(r, val=0.0)

        def compute(self, inputs, outputs):
            params = dict(base)
            for n in names:
                v = float(inputs[n][0])
                params[n] = int(round(v)) if spec[n][3] == "int" else v
            got = _outputs(model, params, tuple(responses))
            seen.append((params, got))
            for r in responses:
                outputs[r] = got[r][0]

    prob = om.Problem(reports=False)
    prob.model.add_subsystem("m", ModelComponent(), promotes=["*"])
    for n in names:
        prob.model.add_design_var(n, lower=spec[n][0], upper=spec[n][1])
    cases = [[(n, np.array([float(p[n])])) for n in names] for p in plan]
    prob.driver = om.DOEDriver(om.ListGenerator(cases))
    prob.setup()
    prob.run_driver()
    prob.cleanup()

    rows, mismatches = cross_check(model, seen, tuple(responses), names)
    plan_ok = len(seen) == len(plan) and all(
        all(math.isclose(float(s[0][n]), float(p[n]), rel_tol=0, abs_tol=0)
            for n in names) for s, p in zip(seen, plan))
    report = {
        "schema": SCHEMA, "availability": "AVAILABLE", "label": LABEL,
        "model": {"model_id": model.model_id,
                  "model_version": model.model_version,
                  "implementation_digest": model.implementation_digest()},
        "base": dict(sorted(base.items())),
        "design": {n: {"lower": spec[n][0], "upper": spec[n][1],
                       "unit": spec[n][2]} for n in names},
        "responses": list(responses),
        "plan": {"method": "Latin hypercube, numpy PCG64", "seed": seed,
                 "n_samples": n_samples,
                 "executed_as_planned": plan_ok},
        "cases": rows,
        "cross_check": {
            "comparison": "every case re-run directly, outputs compared by "
                          "float.hex",
            "verdict": ("BITWISE_IDENTITY" if not mismatches and plan_ok
                        else "MISMATCH"),
            "mismatched_cases": mismatches},
        "driver": {"openmdao": openmdao.__version__},
        "authority": "NON_AUTHORITATIVE",
    }
    report["digest"] = hashlib.sha256(json.dumps(
        {k: v for k, v in report.items() if k != "driver"},
        sort_keys=True).encode()).hexdigest()
    return report
