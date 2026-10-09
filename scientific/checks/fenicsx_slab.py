"""Independent check of the slab by finite elements in FEniCSx (dolfinx).

The producer (``scientific.models.slab_transient``) is cell-centred finite
volume with Crank-Nicolson in one dimension. This check solves the same
equations as a two-dimensional finite-element problem -- P1 Lagrange on a
quadrilateral mesh of a rectangle L x W whose lateral sides are insulated,
Crank-Nicolson in time, the convective face as a Robin term, every linear
solve a direct LU in PETSc -- so it shares the equations and the problem's
parameters with the producer and nothing else: no discretization, no
assembled operator, no solver code. It runs in its own conda-forge
environment (``tools/fenicsx_env.py``) through
``integrations/fenicsx/runner.py``, a JSON-in / JSON-out program that
imports nothing from this repository, so the producer's code cannot reach it
even by accident.

THE CRITERION. Both sides state a second-order discretization estimate --
the producer per output, the FEM solve by repeating itself at half
resolution (|T_h - T_2h| / 3, Richardson). Each becomes a Grid Convergence
Index with Roache's two-level factor of safety 3, and the two solutions must
agree within the sum: |T_FV - T_FEM| <= GCI_FV + GCI_FEM + 64 eps |T|. A
disagreement outside that is more than both methods' own errors can explain.
The FEM solution must also be laterally uniform (the 2D problem reduces to
the 1D slab only if it is), within 1e-9 K; a solve that is not has not
solved this problem and the check FAILS on it.

AVAILABILITY. Without a dolfinx interpreter (``QTA_FENICSX_PYTHON`` unset or
not runnable) the check reports NOT_RUN with the reason: an unavailable
verifier is not a passing one.

WHAT IT ESTABLISHES, AND WHAT IT DOES NOT. Agreement of two discretizations
of one model. Not experimental validation; a wrong physical model is
invisible to it.
"""
from __future__ import annotations

from typing import Any

import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ..identity import digest, implementation_digest
from ..quantity import Quantity, ResolutionClass as RC
from ..result import OutputStatus, ResultBundle
from ..verification import (
    CheckType, Establishes, Independence, Status, VerificationResult,
)
from . import quantity, resolution

CHECK_ID = "thermal.slab_fenicsx"
MODEL_ID = "thermal.slab_transient"
MODEL_VERSION = "1.0.0"
IMPLEMENTATION_MODULES = ("scientific.checks.fenicsx_slab",)
RUNNER = Path(__file__).resolve().parents[2] / "integrations" / "fenicsx" \
    / "runner.py"
ENV_VAR = "QTA_FENICSX_PYTHON"
#: The MPI transports a single-process run uses: the process itself and
#: shared memory. The pinned MPICH initialises through UCX, and UCX probes
#: whatever network devices the host has; on some hosted runners that probe
#: failed and MPI_Init aborted ("MPIDI_UCX_init_worker ... Input/output
#: error") before a line of the check ran (D-2026-124). One process needs no
#: network, so none is offered. tools/fenicsx_env.py restates this for its
#: import probe, and a test holds the two equal.
MPI_ENV = {"UCX_TLS": "self,sm"}
SAMPLE_FRACTIONS = (0.0, 0.25, 0.5, 0.75, 1.0)
SAMPLE_NAMES = ("T_at_0", "T_at_quarter", "T_at_half", "T_at_three_quarter",
                "T_at_L")
GCI_SAFETY = 3.0
LATERAL_MAX_K = 1e-9
#: the FEM resolution: elements along x and Crank-Nicolson steps; the
#: half-resolution solve that gives the estimate uses half of each
NX, NY, STEPS = 80, 4, 240
TIMEOUT_S = 1800
_EPS = sys.float_info.epsilon

SHARED = ("the slab equations: rho c T_t = k T_xx + q, insulated at x = 0, "
          "convective at x = L", "the problem's parameter values")
LIMITS = ("agreement of two discretizations of one model, not of the model "
          "with measurement",
          "a wrong model of the physics is invisible to this check")


class FenicsxUnavailable(RuntimeError):
    pass


def runner_sha256() -> str:
    return hashlib.sha256(RUNNER.read_bytes()).hexdigest()


def check_digest() -> str:
    """This module's code and the runner's bytes: the runner imports dolfinx
    and cannot be imported here, so it is identified by its file."""
    return digest({"check": implementation_digest(IMPLEMENTATION_MODULES),
                   "runner_sha256": runner_sha256()})


def interpreter() -> str:
    exe = os.environ.get(ENV_VAR, "").strip()
    if not exe:
        raise FenicsxUnavailable(f"{ENV_VAR} is not set")
    if not Path(exe).is_file() or not os.access(exe, os.X_OK):
        raise FenicsxUnavailable(f"{ENV_VAR}={exe!r} is not an executable "
                                 "file")
    return exe


def run_runner(request: dict, *, exe: str | None = None) -> dict:
    """One runner invocation: request JSON in, result JSON out, the result
    checked for the fields every caller relies on before it is returned."""
    exe = exe or interpreter()
    with tempfile.TemporaryDirectory(prefix="fenicsx-") as tmp:
        req = Path(tmp) / "request.json"
        out = Path(tmp) / "result.json"
        req.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")}
        env.update({"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
                    "PYTHONHASHSEED": "0", **MPI_ENV})
        proc = subprocess.run([exe, "-I", str(RUNNER), str(req), str(out)],
                              capture_output=True, text=True,
                              timeout=TIMEOUT_S, env=env, cwd=tmp)
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(f"the FEniCSx runner failed (exit "
                               f"{proc.returncode}): {proc.stderr[-800:]}")
        res = json.loads(out.read_text(encoding="utf-8"))
    if res.get("kind") != request["kind"] or \
            res.get("request_sha256") != hashlib.sha256(json.dumps(
                request, sort_keys=True).encode()).hexdigest():
        raise RuntimeError("the runner answered a different request")
    if not isinstance(res.get("provenance"), dict) or \
            "dolfinx" not in res["provenance"]:
        raise RuntimeError("the runner's result carries no provenance")
    return res


def slab_request(params: dict, nx: int, ny: int, steps: int) -> dict:
    keys = ("L_m", "k_W_m_K", "rho_c_J_m3_K", "q_W_m3", "h_W_m2_K",
            "T_inf_K", "T0_K", "t_end_s")
    p = {k: float(params[k]) for k in keys}
    p["width_m"] = p["L_m"] / 5.0
    return {"kind": "slab", "params": p, "nx": nx, "ny": ny, "steps": steps,
            "x_samples": [f * p["L_m"] for f in SAMPLE_FRACTIONS]}


def fem_solution(params: dict, *, exe: str | None = None) -> dict:
    """The FEM samples, their estimate, and both runs' records."""
    fine = run_runner(slab_request(params, NX, NY, STEPS), exe=exe)
    coarse = run_runner(slab_request(params, NX // 2, NY, STEPS // 2),
                        exe=exe)
    est = [abs(a - b) / 3.0 for a, b in zip(fine["T"], coarse["T"])]
    return {"T": fine["T"], "estimate": est,
            "lateral_max_abs_K": max(fine["lateral_max_abs_K"],
                                     coarse["lateral_max_abs_K"]),
            "fine": fine, "coarse": coarse}


def run_check(bundle: ResultBundle, *, verifier_id: str,
              exe: str | None = None) -> VerificationResult:
    if (bundle.model_id, bundle.model_version) != (MODEL_ID, MODEL_VERSION):
        raise ValueError(f"this check is for {MODEL_ID}@{MODEL_VERSION}, "
                         f"not {bundle.model_id}@{bundle.model_version}")
    common: dict[str, Any] = dict(
        check_id=CHECK_ID, check_type=CheckType.INDEPENDENT_IMPLEMENTATION,
        subject_digest=bundle.digest(),
        subject_model=f"{bundle.model_id}@{bundle.model_version}",
        criterion=f"max over the sample points of |T_FV - T_FEM| / "
                  f"(GCI_FV + GCI_FEM + 64 eps |T|) <= 1, GCI = "
                  f"{GCI_SAFETY:g} x each side's Richardson estimate; and "
                  f"the FEM field laterally uniform within {LATERAL_MAX_K} K",
        criterion_derivation="Roache's two-level Grid Convergence Index "
                             "(factor of safety 3) for each discretization, "
                             "summed: a difference beyond both methods' own "
                             "error bands is a disagreement",
        verifier_id=verifier_id,
        verifier_implementation_digest=check_digest(),
        producer_implementation_digest=bundle.implementation_digest,
        independence=Independence.DIFFERENT_DISCRETIZATION,
        establishes=Establishes.INDEPENDENT_NUMERICAL_AGREEMENT,
        shared_components=SHARED, limitations=LIMITS)
    outs = [bundle.output(n) for n in SAMPLE_NAMES]
    if any(o.status is not OutputStatus.OK for o in outs) or any(
            quantity(o).resolution_class is not RC.DISCRETIZATION_ESTIMATE
            for o in outs):
        return VerificationResult(
            **{**common, "limitations": LIMITS + (
                "the producer's sample outputs are not OK or state no "
                "discretization estimate; nothing to compare",)},
            status=Status.NOT_RUN, measured=None, threshold=None)
    try:
        fem = fem_solution(bundle.parameters, exe=exe)
    except FenicsxUnavailable as exc:
        return VerificationResult(
            **{**common, "limitations": LIMITS + (
                f"FEniCSx unavailable: {exc}",)},
            status=Status.NOT_RUN, measured=None, threshold=None)
    ratio = 0.0
    for o, t_fem, e_fem in zip(outs, fem["T"], fem["estimate"]):
        q = quantity(o)
        tol = GCI_SAFETY * (resolution(q) + e_fem) + 64 * _EPS * abs(t_fem)
        ratio = max(ratio, abs(q.value - t_fem) / tol)
    if fem["lateral_max_abs_K"] > LATERAL_MAX_K:
        ratio = max(ratio, math.inf)
    evidence = (digest(fem["fine"]), digest(fem["coarse"]))
    status = Status.PASS if ratio <= 1.0 else Status.FAIL
    prov = fem["fine"]["provenance"]
    return VerificationResult(
        **{**common, "limitations": LIMITS + (
            f"computed with dolfinx {prov['dolfinx']}, PETSc "
            f"{prov['petsc']}, {prov['mpi']}",)},
        status=status, evidence=evidence,
        measured=Quantity(ratio if math.isfinite(ratio) else 1e300, "1",
                          resolution=1e-6,
                          resolution_class=RC.SINGLE_EVALUATION,
                          resolution_basis="a ratio of stated values",
                          reporting_digits=6),
        threshold=Quantity(1.0, "1", exact=True))
