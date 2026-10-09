"""The thermal_rc2 FMU, run through FMI, checked against the closed form.

Two halves, kept apart on purpose:

* ``fmu_bundle`` turns an FMU run into a ResultBundle like any other model's.
  The FMU is read at the boundary first (``scientific.fmi_boundary``: archive,
  XML, units, claim annotation) and refused there if anything is outside it;
  it is then executed by fmpy in its own runtime
  (``integrations/fmi/fmpy_runner.py``), twice -- at a communication step h
  and at h / 2 -- so each output carries a DISCRETIZATION_ESTIMATE for a
  fourth-order method, |y_h/2 - y_h| / 15 (Richardson). The bundle is a
  SIMULATION_RESULT (a ResultBundle cannot be anything else), its
  implementation identity is the FMU archive's sha256, and its provenance
  carries the FMU's own claim boundary: NON_AUTHORITATIVE.
* ``run_check`` compares that bundle with ``scientific.models.thermal_rc2``'s
  closed form -- a separately written solution of the same equations, by
  eigen-decomposition rather than Runge-Kutta. The criterion is each
  output's Grid Convergence Index (Roache's two-level factor of safety 3 on
  the FMU's own estimate) plus the closed form's stated resolution.

A PASS is agreement of two implementations of one model. It is evidence; it
admits nothing. Whether the FMU's result is accepted for anything is the
authority layer's decision, citing this check by digest.
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

from .. import fmi_boundary as FB
from ..identity import digest, implementation_digest
from ..models.thermal_rc2 import (
    FMI_VARIABLES, MODEL_ID as REF_MODEL_ID, OUTPUTS, UNITS, ThermalRC2Model,
)
from ..quantity import Quantity, ResolutionClass as RC
from ..result import InvariantResult, Output, OutputStatus, ResultBundle
from ..run_identity import RunIdentity
from ..verification import (
    CheckType, Establishes, Independence, Status, VerificationResult,
)
from . import quantity, resolution

CHECK_ID = "thermal.rc2_fmu"
FMU_MODEL_ID = "fmi.thermal_rc2"
FMU_MODEL_VERSION = "1.0.0"
IMPLEMENTATION_MODULES = ("scientific.checks.fmu_rc2",)
RUNNER = Path(__file__).resolve().parents[2] / "integrations" / "fmi" \
    / "fmpy_runner.py"
ENV_VAR = "QTA_FMI_PYTHON"
GCI_SAFETY = 3.0
RK4_RICHARDSON = 15.0          # 2^4 - 1
TIMEOUT_S = 600
_EPS = sys.float_info.epsilon

#: harness parameter name -> FMI start variable
START = {"C1_J_K": "C1", "C2_J_K": "C2", "G12_W_K": "G12", "G2a_W_K": "G2a",
         "T1_0_K": "T1_0", "T2_0_K": "T2_0", "Q_W": "Q", "Tamb_K": "Tamb"}
SHARED = ("the two-node network equations",)
LIMITS = ("agreement of two implementations of one model, not of the model "
          "with measurement",
          "a wrong model of the physics is invisible to this check")


class FmiUnavailable(RuntimeError):
    pass


def check_digest() -> str:
    return implementation_digest(IMPLEMENTATION_MODULES)


def interpreter() -> str:
    exe = os.environ.get(ENV_VAR, "").strip()
    if not exe:
        raise FmiUnavailable(f"{ENV_VAR} is not set")
    if not Path(exe).is_file() or not os.access(exe, os.X_OK):
        raise FmiUnavailable(f"{ENV_VAR}={exe!r} is not an executable file")
    return exe


def run_runner(request: dict, *, exe: str | None = None) -> dict:
    fmu = request.get("fmu")
    if fmu is not None and not Path(fmu).is_absolute():
        # The runner works in a scratch directory of its own, so a relative
        # path names nothing there -- refused here, by name, rather than as
        # a FileNotFoundError from inside the other runtime (D-2026-112).
        raise ValueError(f"the FMU path {fmu!r} is relative; the FMI "
                         "runner runs in its own scratch directory and "
                         "needs an absolute path")
    exe = exe or interpreter()
    with tempfile.TemporaryDirectory(prefix="fmi-") as tmp:
        req = Path(tmp) / "request.json"
        out = Path(tmp) / "result.json"
        req.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")}
        proc = subprocess.run([exe, "-I", str(RUNNER), str(req), str(out)],
                              capture_output=True, text=True,
                              timeout=TIMEOUT_S, env=env, cwd=tmp)
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(f"the FMI runner failed (exit "
                               f"{proc.returncode}): {proc.stderr[-800:]}")
        res = json.loads(out.read_text(encoding="utf-8"))
    if res.get("kind") != request["kind"] or \
            res.get("request_sha256") != hashlib.sha256(json.dumps(
                request, sort_keys=True).encode()).hexdigest():
        raise RuntimeError("the FMI runner answered a different request")
    return res


def fmu_bundle(fmu: Path | str, params: dict, *, step_s: float,
               build_record: dict, exe: str | None = None) -> ResultBundle:
    """Run the FMU at ``step_s`` and ``step_s / 2`` (one RK4 substep per
    communication step) and return its ResultBundle. Refuses an FMU that
    fails the boundary or whose bytes are not the ones the build recorded."""
    desc = FB.describe(fmu)
    if desc.archive_sha256 != build_record.get("fmu_sha256"):
        raise FB.FmuRefused("the FMU's sha256 is not the one its build "
                            "recorded")
    diffs = FB.contract_differences(desc, FMI_VARIABLES)
    if diffs:
        raise FB.FmuRefused(f"variable contract differs: {diffs}")
    p = ThermalRC2Model().validate(params)
    start = {START[k]: p[k] for k in START}
    runs = []
    for h in (step_s, step_s / 2.0):
        runs.append(run_runner({"kind": "simulate", "fmu": str(fmu),
                                "start": start, "n_sub": 1, "step": h,
                                "t_end": p["t_end_s"]}, exe=exe))
    coarse, fine = runs[0]["outputs"], runs[1]["outputs"]
    outputs = []
    for name in OUTPUTS:
        v = fine[name]
        est = abs(v - coarse[name]) / RK4_RICHARDSON
        if not math.isfinite(v):
            outputs.append(Output(name, OutputStatus.FAILED,
                                  reason="non-finite FMU output"))
            continue
        outputs.append(Output(name, OutputStatus.OK, Quantity(
            float(v), UNITS[name], resolution=max(est, 64 * _EPS * abs(v)),
            resolution_class=RC.DISCRETIZATION_ESTIMATE,
            resolution_basis=f"Richardson, fourth order: |y(h={step_s / 2:g}"
                             f" s) - y(h={step_s:g} s)| / 15",
            reporting_digits=12)))
    runtime = runs[1]["runtime"]
    env_digest = digest({"fmi_runtime": runtime,
                         "binary_sha256": desc.binary_sha256})
    identity = RunIdentity(
        model_id=FMU_MODEL_ID, model_version=FMU_MODEL_VERSION,
        implementation_digest=desc.archive_sha256,
        parameter_digest=digest(p), environment_digest=env_digest)
    return ResultBundle(
        model_id=FMU_MODEL_ID, model_version=FMU_MODEL_VERSION,
        implementation_digest=desc.archive_sha256,
        parameter_digest=digest(p),
        environment_digest=env_digest,
        parameters=dict(p), outputs=tuple(outputs),
        invariants=_invariants(outputs, desc.claim),
        convergence={"method": "classical RK4 inside the FMU, one substep "
                               "per communication step",
                     "steps_s": [step_s, step_s / 2.0]},
        solver_config={"n_sub": 1},
        warnings=("an FMU result: SIMULATION_RESULT, NON_AUTHORITATIVE",),
        artifacts=(),
        provenance={"fmi": {"archive_sha256": desc.archive_sha256,
                            "binary_sha256": desc.binary_sha256,
                            "claim_boundary": desc.claim,
                            "build": {k: build_record[k] for k in
                                      ("compiler", "flags", "platform")},
                            "runtime": runtime},
                    "run_identity": identity.to_record()})


def _invariants(outputs: list, claim: dict) -> tuple:
    """What the FMU's own outputs must satisfy, computed from them.

    The energy balance is held to the outputs' OWN declared resolutions:
    E_in - E_out - E_stored is a sum of three estimates, each within its
    resolution of the exact value, whose exact sum is zero -- so the gap
    may be at most three times the sum of the three resolutions (the GCI
    factor of safety) plus rounding. Nothing here is a tolerance chosen
    for this FMU."""
    ok = {o.name: o.quantity for o in outputs
          if o.status is OutputStatus.OK}
    nonfinite = len(OUTPUTS) - len(ok)
    inv = [InvariantResult(
        "finite", nonfinite == 0, Quantity(float(nonfinite), "1", exact=True),
        "non-finite or failed output count == 0",
        Quantity(0.0, "1", exact=True), "counted over the five outputs")]
    if nonfinite == 0:
        e_in, e_out, e_st = ok["E_in"], ok["E_out"], ok["E_stored"]
        gap = abs(e_in.value - e_out.value - e_st.value)
        bound = GCI_SAFETY * (e_in.resolution + e_out.resolution
                              + e_st.resolution) + 64 * _EPS * max(
            abs(e_in.value), abs(e_out.value), abs(e_st.value))
        inv.append(InvariantResult(
            "energy_balance", bool(gap <= bound), Quantity(gap, "J"),
            "|E_in - E_out - E_stored| <= 3 (r_in + r_out + r_stored) "
            "+ 64 eps max|E|", Quantity(bound, "J"),
            "the exact balance is zero; each term is within its own "
            "declared resolution of its exact value"))
    held = (claim.get("authority") == FB.REQUIRED_AUTHORITY
            and claim.get("observation_kind") == "SIMULATION_RESULT")
    inv.append(InvariantResult(
        "claim_boundary", held, Quantity(0.0 if held else 1.0, "1",
                                         exact=True),
        "the FMU's annotation is NON_AUTHORITATIVE and SIMULATION_RESULT",
        Quantity(0.0, "1", exact=True),
        "read from modelDescription.xml at the boundary"))
    return tuple(inv)


def run_check(bundle: ResultBundle, *, verifier_id: str) -> VerificationResult:
    if (bundle.model_id, bundle.model_version) != (FMU_MODEL_ID,
                                                   FMU_MODEL_VERSION):
        raise ValueError(f"this check is for {FMU_MODEL_ID}@"
                         f"{FMU_MODEL_VERSION}, not {bundle.model_id}@"
                         f"{bundle.model_version}")
    claim = bundle.provenance.get("fmi", {}).get("claim_boundary", {})
    if claim.get("authority") != FB.REQUIRED_AUTHORITY or \
            claim.get("scientific_model") != REF_MODEL_ID:
        raise ValueError("the bundle does not carry the FMU's claim "
                         "boundary for this model")
    common: dict[str, Any] = dict(
        check_id=CHECK_ID, check_type=CheckType.INDEPENDENT_IMPLEMENTATION,
        subject_digest=bundle.digest(),
        subject_model=f"{bundle.model_id}@{bundle.model_version}",
        criterion="max over the outputs of |y_FMU - y_closed| / (GCI_FMU + "
                  "r_closed) <= 1, GCI = 3 x the FMU's Richardson estimate",
        criterion_derivation="Roache's two-level Grid Convergence Index "
                             "(factor of safety 3) on the FMU's fourth-order "
                             "estimate, plus the closed form's stated "
                             "rounding resolution",
        verifier_id=verifier_id,
        verifier_implementation_digest=check_digest(),
        producer_implementation_digest=bundle.implementation_digest,
        independence=Independence.DIFFERENT_IMPLEMENTATION,
        establishes=Establishes.INDEPENDENT_NUMERICAL_AGREEMENT,
        shared_components=SHARED, limitations=LIMITS)
    outs = [bundle.output(n) for n in OUTPUTS]
    if any(o.status is not OutputStatus.OK for o in outs):
        return VerificationResult(
            **{**common, "limitations": LIMITS + (
                "the FMU's outputs are not OK; nothing to compare",)},
            status=Status.NOT_RUN, measured=None, threshold=None)
    ref = ThermalRC2Model().run(bundle.parameters)
    ratio = 0.0
    for o in outs:
        r = quantity(ref.output(o.name))
        q = quantity(o)
        tol = GCI_SAFETY * resolution(q) + resolution(r)
        ratio = max(ratio, abs(q.value - r.value) / tol)
    status = Status.PASS if ratio <= 1.0 else Status.FAIL
    return VerificationResult(
        **common, status=status, evidence=(ref.digest(),),
        measured=Quantity(ratio if math.isfinite(ratio) else 1e300, "1",
                          resolution=1e-6,
                          resolution_class=RC.SINGLE_EVALUATION,
                          resolution_basis="a ratio of stated values",
                          reporting_digits=6),
        threshold=Quantity(1.0, "1", exact=True))
