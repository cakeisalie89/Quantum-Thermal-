#!/usr/bin/env python3
"""The slab's mid-plane temperature, with a transparent error budget.

    python tools/slab_uq.py            # print the budget
    python tools/slab_uq.py --write    # record docs/assurance/slab_uq.json
    python tools/slab_uq.py --check    # re-derive it and compare (CI)

THE MODEL AND THE POINT. ``thermal.slab_transient`` at the documented point
the research campaign asks about (tools/adversarial_eval.SLAB_RUN): a 5 cm
slab with k = 15 W/m/K, rho c = 3.6 MJ/m^3/K, q = 200 kW/m^3 and h = 50
W/m^2/K, after 600 s. The output is T_at_half.

THE CONTRIBUTIONS, each said to be measured, estimated, assumed or not
established (scientific.uncertainty):

- discretization, ESTIMATED: the model's own Richardson estimate;
- discretization, MEASURED: the run against the closed-form eigenfunction
  series (scientific.checks.slab_series), which shares no code with the
  solver. It replaces the estimate in the total, and the estimate is held
  to it;
- the series reference's own error, ESTIMATED: from its terms and its
  quadrature, said to be beneath the discretization and not bounded here;
- rounding, ESTIMATED: the model's own bound, 64 eps max|T|;
- parameters, ASSUMED: declared normal spreads on every physical input,
  propagated linearly and by Monte Carlo and compared;
- model form and measurement, NOT_ESTABLISHED: no temperature of any real
  slab was observed.

Everything here is MODEL_ONLY and NON_AUTHORITATIVE. The parametric
interval is exactly as good as the declared spreads, which are
illustrative and not measured.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scientific import uncertainty as U  # noqa: E402
from scientific.checks.slab_series import series_temperature  # noqa: E402
from scientific.models import slab_transient as ST  # noqa: E402

RESULTS = ROOT / "docs" / "assurance" / "slab_uq.json"
SCHEMA = "slab-error-budget/1"
OUTPUT = "T_at_half"
#: the documented point of tools/adversarial_eval.SLAB_RUN, restated so this
#: tool imports nothing of the governed layer
POINT = {"L_m": 0.05, "T0_K": 300.0, "T_inf_K": 300.0, "h_W_m2_K": 50.0,
         "k_W_m_K": 15.0, "n_cells": 80, "n_steps": 240, "q_W_m3": 200000.0,
         "rho_c_J_m3_K": 3600000.0, "t_end_s": 600.0}
#: Declared, not measured: illustrative one-sigma spreads of the kind a
#: handbook value carries, so that the propagation has something to carry.
SPREADS = {"k_W_m_K": 0.05 * 15.0, "rho_c_J_m3_K": 0.05 * 3.6e6,
           "h_W_m2_K": 0.10 * 50.0, "q_W_m3": 0.05 * 2.0e5,
           "T_inf_K": 1.0, "T0_K": 1.0, "L_m": 0.005 * 0.05}
SAMPLES = 1000
SEED = 20261031


def _half(p: dict) -> float:
    """T_at_half from the model's own solver at the run's resolution: what
    the model publishes, without the half-resolution run and the run
    identity a parameter sweep need not repeat."""
    x, T, _ = ST._solve(p, p["n_cells"], p["n_steps"])
    return ST._samples(p, x, T)[ST.SAMPLE_NAMES.index(OUTPUT)]


def _admissible(p: dict) -> bool:
    return all(p[k] > 0 for k in ("k_W_m_K", "rho_c_J_m3_K", "h_W_m2_K",
                                  "L_m", "T_inf_K", "T0_K")) \
        and p["q_W_m3"] >= 0


def derive(*, samples: int = SAMPLES, seed: int = SEED) -> dict:
    model = ST.SlabTransientModel()
    bundle = model.run(POINT)
    out = bundle.output(OUTPUT).quantity
    if out is None or out.resolution is None:
        raise SystemExit(f"{OUTPUT} has no value with a resolution at the "
                         "documented point; there is nothing to budget")
    reference = series_temperature(POINT, [0.5 * POINT["L_m"]])[0]
    measured = abs(out.value - reference)
    inputs = {k: U.Distribution(v, "declared: an illustrative one-sigma "
                                   "spread, not measured")
              for k, v in SPREADS.items()}
    lin = U.linear(_half, POINT, inputs)
    mc = U.monte_carlo(_half, POINT, inputs, n=samples, seed=seed,
                       admissible=_admissible)
    agreement = U.compare(lin, mc)
    coverage = U.estimate_bounds(out.resolution, measured)

    b = U.Budget(model=f"{model.model_id}@{model.model_version}",
                 output=OUTPUT, unit=out.unit, value=out.value)
    b.add(U.Contribution(
        "discretization", "NUMERICAL", U.ESTIMATED, out.resolution,
        out.resolution_basis,
        "the model's own estimate, published with the output"))
    b.add(U.Contribution(
        "discretization_against_the_series", "NUMERICAL", U.MEASURED,
        measured, "|T(finite volume) - T(eigenfunction series)| at L/2",
        f"the estimate {coverage['verdict'].lower().replace('_', ' ')} "
        f"(measured / estimated = {coverage['ratio']:.7f})",
        supersedes="discretization"))
    b.add(U.Contribution(
        "series_reference", "NUMERICAL", U.ESTIMATED, 0.0,
        "terms summed until the next decay factor underflows; the initial "
        "condition projected by 64-point Gauss-Legendre quadrature",
        "taken as beneath the discretization error and not bounded here: "
        "the measured difference includes it"))
    b.add(U.Contribution(
        "rounding", "NUMERICAL", U.ESTIMATED,
        64 * sys.float_info.epsilon * abs(out.value),
        "64 eps |T|, the model's own rounding bound",
        "far beneath the discretization error"))
    b.add(U.Contribution(
        "parameters", "PARAMETRIC", U.ASSUMED,
        0.5 * (mc["p97_5"] - mc["p2_5"]),
        f"Monte Carlo, {mc['samples']} samples, seed {seed}: half the "
        "central 95% interval",
        "conditional on the declared input spreads, which are illustrative "
        "and not measured; the linear propagation is the cross-check"))
    b.add(U.Contribution(
        "model_form", "MODEL_FORM", U.NOT_ESTABLISHED, None,
        "nothing compares the model with a real slab",
        "constant properties, no radiation, uniform heating: the "
        "applicability the model declares"))
    b.add(U.Contribution(
        "measurement", "MEASUREMENT", U.NOT_ESTABLISHED, None,
        "no temperature was measured", "MODEL_ONLY"))
    return {
        "schema": SCHEMA, "point": POINT, "spreads": SPREADS,
        "model": {"id": model.model_id, "version": model.model_version,
                  "implementation_digest": model.implementation_digest()},
        "reference": {"check": "thermal.slab_series", "value": reference,
                      "unit": "K"},
        "estimate_covers_measured": coverage,
        "propagation": {"linear": lin, "monte_carlo": mc,
                        "agreement": agreement},
        "budget": b.to_record(),
        "authority": "NON_AUTHORITATIVE",
        "status": "MODEL_ONLY: no physical observation; the parametric "
                  "interval is conditional on declared spreads",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="re-derive and compare with the record")
    args = ap.parse_args(argv)
    out = derive()
    text = json.dumps(out, indent=1, sort_keys=True) + "\n"
    if args.check:
        problems = _differences(json.loads(text), json.loads(
            RESULTS.read_text(encoding="utf-8")))
        for line in problems:
            print(line)
        print(f"slab error budget: {len(problems)} difference(s) from "
              f"{RESULTS.relative_to(ROOT)}")
        return 1 if problems else 0
    if args.write:
        RESULTS.write_text(text, encoding="utf-8")
    print(text)
    return 0 if math.isfinite(out["budget"]["total"]["half_width"]) else 1


def _differences(a, b, path: str = "") -> list:
    """Where ``a`` and ``b`` differ, floats to 1e-9 relative: the same
    arithmetic on another BLAS may differ in its last bits."""
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            return [f"{path}: keys {sorted(set(a) ^ set(b))}"]
        return [d for k in sorted(a) for d in
                _differences(a[k], b[k], f"{path}.{k}")]
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: {len(a)} items, recorded {len(b)}"]
        return [d for i, (x, y) in enumerate(zip(a, b))
                for d in _differences(x, y, f"{path}[{i}]")]
    if isinstance(a, float) and isinstance(b, float):
        ok = math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
    else:
        ok = a == b
    return [] if ok else [f"{path}: {a!r}, recorded {b!r}"]


if __name__ == "__main__":
    sys.exit(main())
