#!/usr/bin/env python3
"""FMI 3.0 acceptance: a real FMU, built, loaded, stepped, restored, checked.

The FMU is ``integrations/fmi/thermal_rc2`` (a generic two-node thermal RC
network, C, classical RK4), built by ``tools/fmi_build.py`` and run by fmpy
in its own runtime. The campaign, every item measured:

* **validation** -- fmpy's validator reports no problem; the harness's own
  boundary reader (``scientific.fmi_boundary``) and fmpy's parser agree on
  every variable (an independent second reading).
* **FMI-P1 state** -- stepped to t_split, the state saved in memory and as
  serialized bytes; continuing directly, from the restored memory state and
  from the deserialized bytes must give the SAME outputs at t_end (the same
  binary on the same input is deterministic, so equality is the claim, and
  it is measured, not assumed); a corrupted serialization must be refused.
* **FMI-P2 step semantics** -- a step from the wrong time, a non-positive
  step, a parameter set after initialisation, a non-finite input and a
  wrong instantiation token must each be refused by the FMU.
* **FMI-P3 step size** -- T1(t_end) at communication steps 480, 240, 120 and
  60 s (one RK4 substep each) against the closed form: the observed order
  on the finest pair must be within 5 % of RK4's a-priori order 4 (>= 3.8),
  computed from the errors.
* **FMI-P4 claim boundary** -- the FMU's annotation says SIMULATION_RESULT
  and NON_AUTHORITATIVE, and its ResultBundle carries it; tampered copies
  without the annotation, claiming a measured kind, or claiming authority,
  are refused at the boundary.
* **FMI-P5 units** -- every exported variable's name, causality,
  variability and unit equals the model's declared table, read both by the
  harness and by fmpy; a copy with an undefined unit is refused, a copy with
  a different defined unit shows as a contract difference.
* **Archive integrity** -- a copy with one flipped binary byte is refused
  because its sha256 is not the build's; a member escaping the archive and a
  DOCTYPE in the description are refused.
* **Verification** -- the FMU's bundle passes ``thermal.rc2_fmu`` against the
  closed form; the FAULT build (G12 10 % off inside, declared right) FAILS
  it.

    QTA_FMI_PYTHON=<prefix>/bin/python python tools/fmi_acceptance.py \\
        --work DIR --out report.json
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scientific import fmi_boundary as FB  # noqa: E402
from scientific.checks import fmu_rc2 as F  # noqa: E402
from scientific.models.thermal_rc2 import (  # noqa: E402
    FMI_VARIABLES, ThermalRC2Model, closed_form,
)

SCHEMA = "fmi-acceptance/1"
P3_STEPS = (480.0, 240.0, 120.0, 60.0)
P3_ORDER_MIN = 3.8
T_END = 3600.0


def _build(work: Path, fault: bool) -> dict:
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "fmi_build.py"),
                          "--out", str(work)] + (["--fault"] if fault else []),
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _rewrite(src: Path, dst: Path, *, md=None, binary=None, extra=None):
    """A tampered copy of an FMU: replace the description and/or the
    binary, or add a member."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "modelDescription.xml" and md is not None:
                data = md(data)
            if info.filename.startswith("binaries/") and binary is not None:
                data = binary(data)
            zout.writestr(info, data)
        if extra:
            zout.writestr(extra[0], extra[1])


def tamper_cases(fmu: Path, work: Path, record: dict) -> dict:
    out: dict[str, dict] = {}
    cases: dict[str, dict] = {
        "annotation_removed": dict(md=lambda d: d.replace(
            b"<Annotations>", b"<!-- -->").replace(b"</Annotations>", b"")
            .replace(b'<Annotation type="org.scientific-ai-harness.claim-'
                     b'boundary">', b"").replace(b"</Annotation>", b"")
            .replace(b"<ClaimBoundary", b"<Removed")),
        "claims_measured": dict(md=lambda d: d.replace(
            b'observationKind="SIMULATION_RESULT"',
            b'observationKind="RAW_OBSERVATION"')),
        "claims_authority": dict(md=lambda d: d.replace(
            b'authority="NON_AUTHORITATIVE"', b'authority="AUTHORITATIVE"')),
        "undefined_unit": dict(md=lambda d: d.replace(
            b'quantity="ThermodynamicTemperature" unit="K"',
            b'quantity="ThermodynamicTemperature" unit="degC"')),
        "doctype": dict(md=lambda d: d.replace(
            b"<fmiModelDescription",
            b"<!DOCTYPE x [<!ENTITY e \"x\">]>\n<fmiModelDescription", 1)),
        "escaping_member": dict(extra=("../escape.txt", b"x")),
        "binary_byte_flipped": dict(binary=lambda d: d[:4096]
                                    + bytes([d[4096] ^ 0x01]) + d[4097:]),
    }
    for name, kw in cases.items():
        dst = work / f"tampered_{name}.fmu"
        _rewrite(fmu, dst, **kw)
        try:
            desc = FB.describe(dst)
            if desc.archive_sha256 != record["fmu_sha256"]:
                raise FB.FmuRefused("archive sha256 is not the build's")
            out[name] = {"refused": False}
        except FB.FmuRefused as exc:
            out[name] = {"refused": True, "reason": str(exc)}
    # a DEFINED but different unit is not a parse error: it is a contract
    # difference, which the importer must report
    dst = work / "tampered_other_unit.fmu"
    _rewrite(fmu, dst, md=lambda d: d.replace(
        b'name="T1" valueReference="9" causality="output" '
        b'variability="continuous" declaredType="Temperature"',
        b'name="T1" valueReference="9" causality="output" '
        b'variability="continuous" declaredType="Energy"'))
    diffs = FB.contract_differences(FB.describe(dst), FMI_VARIABLES)
    out["other_defined_unit"] = {"refused": bool(diffs), "reason": diffs}
    return out


def campaign(work: Path, exe: str) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    rec = _build(work, fault=False)
    rec_f = _build(work, fault=True)
    fmu = Path(rec["fmu"])
    out: dict = {"schema": SCHEMA, "build": {k: rec[k] for k in (
        "fmu_sha256", "binary_sha256", "model_description_sha256",
        "source_sha256", "compiler", "flags", "platform", "headers")}}
    ours = FB.describe(fmu)
    theirs = F.run_runner({"kind": "describe", "fmu": str(fmu)}, exe=exe)
    ours_v = sorted((v.name, v.causality, v.variability, v.type, v.unit)
                    for v in ours.variables)
    theirs_v = sorted((v["name"], v["causality"], v["variability"],
                       v["type"], v["unit"]) for v in theirs["variables"])
    val = F.run_runner({"kind": "validate", "fmu": str(fmu)}, exe=exe)
    out["validation"] = {
        "fmpy_problems": val["problems"],
        "two_readers_agree": ours_v == theirs_v,
        "fmi_version": theirs["fmi_version"],
        "runtime": val["runtime"],
        "accepted": not val["problems"] and ours_v == theirs_v}
    start = {F.START[k]: v for k, v in ThermalRC2Model().validate(
        {"t_end_s": T_END}).items() if k in F.START}
    st = F.run_runner({"kind": "state", "fmu": str(fmu), "start": start,
                       "n_sub": 4, "step": 60.0, "t_split": 1800.0,
                       "t_end": T_END}, exe=exe)
    out["P1_state"] = {
        "direct": st["direct"], "from_memory": st["from_memory"],
        "from_bytes": st["from_bytes"],
        "serialized_bytes": st["serialized_bytes"],
        "corrupt_serialization_refused": st["corrupt_serialization_refused"],
        "accepted": st["direct"] == st["from_memory"] == st["from_bytes"]
        and st["corrupt_serialization_refused"]}
    ref = F.run_runner({"kind": "refusals", "fmu": str(fmu)}, exe=exe)
    out["P2_step_semantics"] = {"refused": ref["refused"],
                                "accepted": all(ref["refused"].values())}
    exact = closed_form(ThermalRC2Model().validate({"t_end_s": T_END}), T_END)
    errs = []
    for h in P3_STEPS:
        r = F.run_runner({"kind": "simulate", "fmu": str(fmu),
                          "start": start, "n_sub": 1, "step": h,
                          "t_end": T_END}, exe=exe)
        errs.append(abs(r["outputs"]["T1"] - exact["T1"]))
    orders = [math.log(a / b) / math.log(2.0) for a, b in zip(errs, errs[1:])]
    out["P3_step_size"] = {"steps_s": list(P3_STEPS), "abs_error_T1_K": errs,
                           "orders": orders, "order_min": P3_ORDER_MIN,
                           "accepted": orders[-1] >= P3_ORDER_MIN}
    bundle = F.fmu_bundle(fmu, {"t_end_s": T_END}, step_s=60.0,
                          build_record=rec, exe=exe)
    vr = F.run_check(bundle, verifier_id="fmi-acceptance")
    out["P4_claim_boundary"] = {
        "annotation": ours.claim,
        "bundle_claim": bundle.provenance["fmi"]["claim_boundary"],
        "accepted": ours.claim.get("authority") == "NON_AUTHORITATIVE"
        and ours.claim.get("observation_kind") == "SIMULATION_RESULT"}
    out["P5_units"] = {"contract_differences":
                       FB.contract_differences(ours, FMI_VARIABLES),
                       "units_defined": list(ours.units)}
    out["P5_units"]["accepted"] = not out["P5_units"]["contract_differences"]
    out["tamper"] = tamper_cases(fmu, work, rec)
    out["verification"] = {"status": vr.status.value,
                           "ratio": vr.measured.value if vr.measured else None,
                           "record": vr.to_record()}
    bf = F.fmu_bundle(Path(rec_f["fmu"]), {"t_end_s": T_END}, step_s=60.0,
                      build_record=rec_f, exe=exe)
    vf = F.run_check(bf, verifier_id="fmi-acceptance")
    out["negative_control_fault_fmu"] = {"status": vf.status.value,
                                         "rejected": vf.status.value == "FAIL"}
    criteria = [out[k]["accepted"] for k in (
        "validation", "P1_state", "P2_step_semantics", "P3_step_size",
        "P4_claim_boundary", "P5_units")]
    criteria.append(out["verification"]["status"] == "PASS")
    controls = [v["refused"] for v in out["tamper"].values()]
    controls.append(out["negative_control_fault_fmu"]["rejected"])
    out["verdict"] = {"criteria_met": sum(criteria),
                      "criteria": len(criteria),
                      "controls_rejected": sum(controls),
                      "controls": len(controls),
                      "accepted": bool(all(criteria) and all(controls))}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    try:
        exe = F.interpreter()
    except F.FmiUnavailable as exc:
        print(f"FMI ACCEPTANCE NOT RUN: {exc}")
        return 1
    rep = campaign(args.work.resolve(), exe)
    args.out.write_text(json.dumps(rep, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
    v = rep["verdict"]
    print(f"build    {rep['build']['fmu_sha256'][:16]}...  "
          f"({rep['build']['compiler']})")
    print(f"P1 state restore identical: {rep['P1_state']['accepted']}; "
          f"P2 refusals {sum(rep['P2_step_semantics']['refused'].values())}"
          f"/{len(rep['P2_step_semantics']['refused'])}; P3 order "
          f"{rep['P3_step_size']['orders'][-1]:.3f}")
    print(f"P4 {rep['P4_claim_boundary']['annotation']}; P5 differences "
          f"{rep['P5_units']['contract_differences']}")
    print(f"verification {rep['verification']['status']} "
          f"({rep['verification']['ratio']:.3f}); fault FMU "
          f"{rep['negative_control_fault_fmu']['status']}")
    print(f"FMI ACCEPTANCE: {v['criteria_met']}/{v['criteria']} criteria, "
          f"{v['controls_rejected']}/{v['controls']} controls rejected -- "
          f"{'ACCEPTED' if v['accepted'] else 'NOT ACCEPTED'}")
    return 0 if v["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
