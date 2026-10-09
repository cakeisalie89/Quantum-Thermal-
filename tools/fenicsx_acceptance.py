#!/usr/bin/env python3
"""FEniCSx acceptance: is the finite-element path a working, independent
verifier? Measured, in its own environment, with controls that must fail.

FEniCSx (dolfinx) is adopted in this harness as an INDEPENDENT NUMERICAL
VERIFIER -- it produces VerificationResults, never results with authority.
Adoption rests on this campaign, run on the environment
``tools/fenicsx_env.py`` creates from its lock:

A. Manufactured-solution convergence. -k lap(u) = f on the unit square with
   P1 and with P2 elements, and rho c u_t = k u_xx + f with P1 and
   Crank-Nicolson at dt ~ h. The L2 error's observed order on the finest
   pair must be within 5 % of the a-priori order (2 for P1, 3 for P2,
   Ciarlet; 2 for CN + P1 with dt ~ h): 1.9, 2.85, 1.9. The orders are
   computed from the errors, never asserted.
B. Reduction. The generic slab solved as a 2D problem with insulated lateral
   sides must (i) be laterally uniform within 1e-9 K and (ii) agree with the
   closed-form series within its own Grid Convergence Index; and the
   primary finite-volume model's result must pass the FEniCSx check
   (``scientific.checks.fenicsx_slab``).
C. Energy balance from the FEM fields: |E_in - E_out - dU| / max(|E_in|,
   |dU|) <= 1e-9 (Crank-Nicolson with the trapezoid boundary flux is
   conservative; the bound is accumulated rounding with margin).
D. Determinism. The same request twice: value-equivalence within 64 eps |T|
   is the claim; whether the two fields are also byte-identical is measured
   and reported, and is not claimed beyond this run.
E. Negative controls, each of which MUST be rejected: the FEM slab with a
   10 % wrong conductivity against the series; a finite-volume result
   computed with a 10 % wrong conductivity (declaring the right one)
   against FEniCSx; an MMS study whose source term uses the wrong
   conductivity (its order must collapse below the threshold).
F. Provenance: dolfinx, basix, UFL, PETSc, MPI, the solver options, every
   mesh digest, the lock's and the runner's sha256.

Writes one deterministic JSON report and exits 0 only when every criterion
holds and every control is rejected.

    QTA_FENICSX_PYTHON=<prefix>/bin/python \\
        python tools/fenicsx_acceptance.py --out report.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scientific.checks import fenicsx_slab as FX  # noqa: E402
from scientific.checks import slab_series as SS  # noqa: E402
import scientific.models.slab_transient as ST  # noqa: E402

SCHEMA = "fenicsx-acceptance/1"
ORDER_MIN = {"P1": 1.9, "P2": 2.85, "CN_P1": 1.9}
ENERGY_MAX = 1e-9
LATERAL_MAX_K = 1e-9
SLAB = {"L_m": 0.05, "k_W_m_K": 15.0, "rho_c_J_m3_K": 3.6e6,
        "q_W_m3": 2.0e5, "h_W_m2_K": 50.0, "T_inf_K": 300.0, "T0_K": 300.0,
        "t_end_s": 600.0}
_EPS = sys.float_info.epsilon


def _orders(levels: list) -> list:
    return [math.log(a["l2_error"] / b["l2_error"]) / math.log(a["h"] / b["h"])
            for a, b in zip(levels, levels[1:])]


def mms(exe: str, kind: str, req: dict, threshold: float) -> dict:
    res = FX.run_runner({"kind": kind, **req}, exe=exe)
    orders = _orders(res["levels"])
    return {"request": req, "levels": res["levels"], "orders": orders,
            "threshold": threshold,
            "accepted": bool(orders[-1] >= threshold),
            "provenance": res["provenance"]}


def slab_vs_series(exe: str, params: dict) -> dict:
    fem = FX.fem_solution(params, exe=exe)
    xs = [f * params["L_m"] for f in FX.SAMPLE_FRACTIONS]
    ref = SS.series_temperature(params, xs)
    ratio = max(abs(t - r) / (FX.GCI_SAFETY * e + 64 * _EPS * abs(r))
                for t, r, e in zip(fem["T"], ref, fem["estimate"]))
    return {"T_fem": fem["T"], "T_series": ref, "estimate": fem["estimate"],
            "ratio_to_gci": ratio, "accepted": bool(ratio <= 1.0),
            "lateral_max_abs_K": fem["lateral_max_abs_K"],
            "lateral_accepted": bool(fem["lateral_max_abs_K"]
                                     <= LATERAL_MAX_K),
            "energy": fem["fine"]["energy"],
            "energy_accepted": bool(abs(fem["fine"]["energy"]
                                        ["residual_rel"]) <= ENERGY_MAX),
            "mesh_sha256": [fem["fine"]["mesh_sha256"],
                            fem["coarse"]["mesh_sha256"]],
            "field_sha256": fem["fine"]["field_sha256"]}


def campaign(exe: str) -> dict:
    out: dict = {"schema": SCHEMA}
    # A. manufactured solutions
    out["A_mms"] = {
        "P1": mms(exe, "mms_steady", {"k": 2.0, "degree": 1,
                                      "n": [8, 16, 32, 64]}, ORDER_MIN["P1"]),
        "P2": mms(exe, "mms_steady", {"k": 2.0, "degree": 2,
                                      "n": [8, 16, 32]}, ORDER_MIN["P2"]),
        "CN_P1": mms(exe, "mms_transient",
                     {"k": 1.0, "rho_c": 1.0, "t_end": 0.5,
                      "steps_per_cell": 1, "n": [16, 32, 64, 128]},
                     ORDER_MIN["CN_P1"]),
    }
    # B. reduction to the 1D slab and agreement with the series
    red = slab_vs_series(exe, SLAB)
    out["B_reduction"] = {k: v for k, v in red.items() if k != "energy"}
    primary = ST.SlabTransientModel().run({**SLAB, "n_cells": 80,
                                           "n_steps": 240})
    vr = FX.run_check(primary, verifier_id="fenicsx-acceptance", exe=exe)
    out["B_primary_check"] = {"status": vr.status.value,
                              "ratio": vr.measured.value if vr.measured
                              else None, "verification": vr.to_record()}
    # C. energy
    out["C_energy"] = {"residual_rel": red["energy"]["residual_rel"],
                       "max": ENERGY_MAX,
                       "accepted": red["energy_accepted"],
                       "terms": red["energy"]}
    # D. determinism
    req = FX.slab_request(SLAB, FX.NX, FX.NY, FX.STEPS)
    r1 = FX.run_runner(req, exe=exe)
    r2 = FX.run_runner(req, exe=exe)
    diff = max(abs(a - b) for a, b in zip(r1["T"], r2["T"]))
    bound = 64 * _EPS * max(abs(t) for t in r1["T"])
    out["D_determinism"] = {
        "claim": "DETERMINISTIC_VALUE_EQUIVALENCE",
        "max_abs_difference_K": diff, "bound_K": bound,
        "accepted": bool(diff <= bound),
        "byte_identical_observed": r1["field_sha256"] == r2["field_sha256"],
        "note": "byte identity is reported for this run on this build; it "
                "is not claimed for another host or build"}
    # E. negative controls -- each must be REJECTED
    wrong = {**SLAB, "k_W_m_K": SLAB["k_W_m_K"] * 1.10}
    fem_wrong = FX.fem_solution(wrong, exe=exe)
    xs = [f * SLAB["L_m"] for f in FX.SAMPLE_FRACTIONS]
    ref = SS.series_temperature(SLAB, xs)
    r_wrong = max(abs(t - r) / (FX.GCI_SAFETY * e + 64 * _EPS * abs(r))
                  for t, r, e in zip(fem_wrong["T"], ref,
                                     fem_wrong["estimate"]))
    orig = ST._solve
    try:
        ST._solve = lambda p, n, m: orig(
            {**p, "k_W_m_K": p["k_W_m_K"] * 1.10}, n, m)
        bad = ST.SlabTransientModel().run({**SLAB, "n_cells": 80,
                                           "n_steps": 240})
    finally:
        ST._solve = orig
    vr_bad = FX.run_check(bad, verifier_id="fenicsx-acceptance", exe=exe)
    mms_bad = FX.run_runner({"kind": "mms_steady", "k": 2.0, "degree": 1,
                             "n": [8, 16, 32, 64],
                             "source_k_factor": 1.10}, exe=exe)
    bad_orders = _orders(mms_bad["levels"])
    out["E_negative_controls"] = {
        "fem_wrong_conductivity_vs_series": {
            "ratio_to_gci": r_wrong, "rejected": bool(r_wrong > 1.0)},
        "fv_wrong_conductivity_vs_fenicsx": {
            "status": vr_bad.status.value,
            "rejected": vr_bad.status.value == "FAIL"},
        "mms_wrong_source": {"orders": bad_orders,
                             "rejected": bool(bad_orders[-1]
                                              < ORDER_MIN["P1"])},
    }
    # F. provenance
    out["F_provenance"] = {
        "runtime": out["A_mms"]["P1"]["provenance"],
        "runner_sha256": FX.runner_sha256(),
        "lock_sha256": hashlib.sha256(
            (ROOT / "integrations" / "fenicsx" / "environment.lock")
            .read_bytes()).hexdigest(),
        "check_digest": FX.check_digest(),
        "slab_problem": SLAB}
    criteria = [out["A_mms"][k]["accepted"] for k in ("P1", "P2", "CN_P1")]
    criteria += [red["accepted"], red["lateral_accepted"],
                 out["B_primary_check"]["status"] == "PASS",
                 out["C_energy"]["accepted"],
                 out["D_determinism"]["accepted"]]
    controls = [v["rejected"] for v in out["E_negative_controls"].values()]
    out["verdict"] = {
        "criteria_met": sum(criteria), "criteria": len(criteria),
        "controls_rejected": sum(controls), "controls": len(controls),
        "accepted": bool(all(criteria) and all(controls))}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    try:
        exe = FX.interpreter()
    except FX.FenicsxUnavailable as exc:
        print(f"FENICSX ACCEPTANCE NOT RUN: {exc}")
        return 1
    rep = campaign(exe)
    args.out.write_text(json.dumps(rep, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
    v = rep["verdict"]
    a = rep["A_mms"]
    print(f"A  orders  P1 {a['P1']['orders'][-1]:.3f}  P2 "
          f"{a['P2']['orders'][-1]:.3f}  CN+P1 {a['CN_P1']['orders'][-1]:.3f}")
    print(f"B  FEM vs series {rep['B_reduction']['ratio_to_gci']:.3f} of GCI;"
          f" lateral {rep['B_reduction']['lateral_max_abs_K']:.2e} K; FV "
          f"primary vs FEniCSx {rep['B_primary_check']['status']}")
    print(f"C  energy residual {rep['C_energy']['residual_rel']:.2e}")
    print(f"D  repeat max |dT| {rep['D_determinism']['max_abs_difference_K']}"
          f" K; byte-identical observed "
          f"{rep['D_determinism']['byte_identical_observed']}")
    print(f"E  controls rejected {v['controls_rejected']}/{v['controls']}")
    print(f"FENICSX ACCEPTANCE: {v['criteria_met']}/{v['criteria']} criteria,"
          f" {v['controls_rejected']}/{v['controls']} controls rejected -- "
          f"{'ACCEPTED' if v['accepted'] else 'NOT ACCEPTED'}")
    return 0 if v["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
