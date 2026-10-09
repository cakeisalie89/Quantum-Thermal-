"""A local and a global sensitivity ranking of one response, compared.

One-at-a-time (OAT) differences at a base point and SALib's variance-based
Sobol total-order indices over a declared domain answer different questions:
the first how the response moves near THIS point, the second how much of its
variance over THAT domain each input carries, interactions included. They
can disagree, and when they do it is a finding about the model, not a defect
to be resolved by keeping the ranking that looks right. This module produces
both, ranks each only as far as its own resolution distinguishes
(:mod:`scientific.ranking`), and reports the comparison:

* ``IDENTICAL_AT_RESOLUTION`` / ``CONSISTENT_AT_RESOLUTION`` -- no pair
  ordered oppositely;
* ``METHODS_DISAGREE`` -- some pair both methods resolve, ordered
  oppositely; the pairs are named. Nothing chooses between them: the
  report says ``REQUIRES_HUMAN_INTERPRETATION`` and has no gate effect.

Every report carries the method identities, the parameter domain and base
point, the sample configuration, the response definition (model, version,
implementation digest, output, unit) and the comparison metric. Resolutions:
an OAT difference ``y1 - y0`` is resolved to ``r(y0) + r(y1)``, the two
outputs' own declared resolutions; a Sobol index to its bootstrap confidence
half-width (SALib's ``ST_conf``, conf_level 0.95) -- a statistical
resolution, which only more samples can narrow.

SALib is optional. Without it the global ranking is UNAVAILABLE and no other
estimator is substituted. A sensitivity is a property of this model under
these assumptions: NON_AUTHORITATIVE, never a measurement, never a gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import ranking as RK
from .quantity import Quantity

SCHEMA = "sensitivity-cross-check/1"
CONF_LEVEL = 0.95


class SensitivityRefused(ValueError):
    pass


@dataclass(frozen=True)
class Response:
    """What is being differentiated: one output of one model, evaluated."""
    model: str
    implementation_digest: str
    output: str
    unit: str
    evaluate: Callable[[dict], Quantity]

    def describe(self) -> dict:
        return {"model": self.model,
                "implementation_digest": self.implementation_digest,
                "output": self.output, "unit": self.unit}


def model_response(model, output: str, fixed: dict) -> Response:
    """A response from a ScientificModel: ``fixed`` holds every parameter
    the analysis does not vary.

    A model that offers ``evaluate_outputs`` (the outputs its ``run``
    publishes, without the run identity) is evaluated through it: an
    analysis of a few thousand points need not probe the environment a few
    thousand times. Any other model is run in full."""
    fast = getattr(model, "evaluate_outputs", None)

    def evaluate(varied: dict) -> Quantity:
        params = {**fixed, **varied}
        if fast is not None:
            _, outs, _ = fast(params)
            o = {x.name: x for x in outs}[output]
        else:
            o = model.run(params).output(output)
        if o.quantity is None:
            raise SensitivityRefused(f"{output} is {o.status.value}: "
                                     f"{o.reason}")
        return o.quantity
    probe = evaluate({})
    return Response(f"{model.model_id}@{model.model_version}",
                    model.implementation_digest(), output, probe.unit,
                    evaluate)


def _domain(base: dict, fraction: float) -> dict:
    if not 0 < fraction < 1:
        raise SensitivityRefused(f"bounds fraction {fraction!r} is not in "
                                 "(0, 1)")
    out = {}
    for k in sorted(base):
        v = float(base[k])
        if v == 0:
            raise SensitivityRefused(f"{k}: a relative domain around 0 is "
                                     "empty")
        lo, hi = sorted((v * (1 - fraction), v * (1 + fraction)))
        out[k] = [lo, hi]
    return out


def oat(resp: Response, base: dict, step: float) -> dict:
    """One-at-a-time: ``y(p_k (1 + step)) - y(p)``, each resolved to the
    sum of the two outputs' declared resolutions."""
    if not 0 < step < 1:
        raise SensitivityRefused(f"step {step!r} is not in (0, 1)")
    y0 = resp.evaluate(dict(base))
    items, rows = [], []
    for k in sorted(base):
        y1 = resp.evaluate({**base, k: float(base[k]) * (1 + step)})
        if y1.unit != y0.unit:
            raise SensitivityRefused(f"{k}: the response changed unit")
        try:
            r0, r1 = (RK.Ranked.from_quantity("y0", y0),
                      RK.Ranked.from_quantity("y1", y1))
        except RK.RankingRefused as exc:
            raise SensitivityRefused(f"OAT on {k}: {exc}") from exc
        d = y1.value - y0.value
        res = r0.resolution + r1.resolution
        items.append(RK.Ranked(k, abs(d), res,
                               f"r(y0) + r(y1): {r0.basis}"))
        rows.append({"parameter": k, "difference": d, "resolution": res})
    return {"method": {"id": "OAT", "step_fraction": step,
                       "evaluations": len(base) + 1,
                       "measure": "|y(p_k (1+step)) - y(p)|",
                       "resolution_basis": "the response's declared "
                                           "resolution at both points"},
            "rows": rows, "tiers": RK.tiers(items)}


def salib_available() -> bool:
    try:
        import SALib  # noqa: F401
    except ImportError:
        return False
    return True


def sobol(resp: Response, base: dict, fraction: float, n_base: int,
          seed: int) -> dict:
    """Sobol total-order indices over ``base (1 +- fraction)``."""
    if not salib_available():
        return {"method": {"id": "SOBOL_SALIB", "availability":
                           "UNAVAILABLE"}, "rows": [], "tiers": None}
    from importlib.metadata import version

    import numpy as np
    from SALib.analyze import sobol as analyze
    from SALib.sample import sobol as sample
    if n_base < 2 or n_base & (n_base - 1):
        raise SensitivityRefused("n_base must be a power of two (Sobol "
                                 "sequence balance)")
    dom = _domain(base, fraction)
    names = sorted(dom)
    problem = {"num_vars": len(names), "names": names,
               "bounds": [dom[k] for k in names]}
    X = sample.sample(problem, n_base, calc_second_order=False, seed=seed)
    Y = np.asarray([resp.evaluate(dict(zip(names, map(float, row)))).value
                    for row in X], dtype=float)
    if not np.all(np.isfinite(Y)):
        raise SensitivityRefused("non-finite response in the sample")
    Si = analyze.analyze(problem, Y, calc_second_order=False,
                         conf_level=CONF_LEVEL, print_to_console=False,
                         seed=seed)
    rows, items = [], []
    for i, k in enumerate(names):
        st, conf = float(Si["ST"][i]), float(Si["ST_conf"][i])
        rows.append({"parameter": k, "ST": st, "ST_conf": conf,
                     "S1": float(Si["S1"][i]),
                     "S1_conf": float(Si["S1_conf"][i])})
        items.append(RK.Ranked(k, st, abs(conf),
                               f"SALib bootstrap {CONF_LEVEL:g} confidence "
                               f"half-width, N={n_base}"))
    return {"method": {"id": "SOBOL_SALIB", "availability": "AVAILABLE",
                       "salib_version": version("SALib"),
                       "sampler": "SALib.sample.sobol, calc_second_order "
                                  "False", "n_base": n_base, "seed": seed,
                       "evaluations": int(X.shape[0]),
                       "measure": "total-order index ST",
                       "conf_level": CONF_LEVEL},
            "rows": rows, "tiers": RK.tiers(items)}


def cross_check(resp: Response, base: dict, *, fraction: float = 0.1,
                step: float = 0.1, n_base: int = 256,
                seed: int = 20261008) -> dict:
    """Both rankings and their comparison; nothing chosen between them."""
    if not base:
        raise SensitivityRefused("no parameter to vary")
    local = oat(resp, base, step)
    glob = sobol(resp, base, fraction, n_base, seed)
    report: dict[str, Any] = {
        "schema": SCHEMA, "response": resp.describe(),
        "base_point": {k: float(base[k]) for k in sorted(base)},
        "parameter_domain": _domain(base, fraction),
        "methods": [local["method"], glob["method"]],
        "rows": {"OAT": local["rows"], "SOBOL_SALIB": glob["rows"]},
        "rankings": {"OAT": [t.to_record() for t in local["tiers"]]},
        "ranking_semantics": RK.SEMANTICS,
        "interpretation": "REQUIRES_HUMAN_INTERPRETATION: OAT is local at "
                          "the base point, Sobol ST global over the domain; "
                          "a disagreement is a finding and neither ranking "
                          "is preferred here",
        "authority": "NON_AUTHORITATIVE", "automatic_gate_effect": "NONE"}
    if glob["tiers"] is None:
        report.update(status="GLOBAL_UNAVAILABLE", comparison=None)
        return report
    report["rankings"]["SOBOL_SALIB"] = [t.to_record()
                                         for t in glob["tiers"]]
    cmp = RK.compare("OAT", local["tiers"], "SOBOL_SALIB", glob["tiers"])
    report.update(status=cmp.status, comparison=cmp.to_record())
    return report
