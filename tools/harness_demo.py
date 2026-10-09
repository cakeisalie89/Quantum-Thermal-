#!/usr/bin/env python3
"""The generic end-to-end demonstration, and its negative twin.

One governed history, no hardware-era mode anywhere in it:

    recorded AI proposal        a fixture RESPONSE (no provider, no secret),
                                wrapped in a ProposalEnvelope around ...
    governed context            ... read-only retrieval over reviewed
                                documents, every span NOT_EVIDENCE
    ingress                     proposal.receive, written by the ingress
    capability-authorized task  the governed model path: policy, capability,
                                bounded subprocess, re-execution by a
                                separate verifier
    generic ScientificModel     thermal.slab_transient@1.0.0
    ResultBundle                captured as evidence with its field artefact
    HDF5 representation         scientific.hdf5_bundle, round-tripped
    independent verification    the series check, and the FEniCSx check in
                                its own runtime, each a governed task run by
                                an executor that is neither proposer nor
                                producer
    authority decision          a reviewer, citing the FEniCSx report (the
                                independent implementation the admission
                                policy requires)
    reconstruction              the second reader and the generic
                                consistency verifier over the same log
    packaging                   an RO-Crate of the demonstration's files

and an FMI leg through the SAME boundary: the FMU built, cited by digest,
run in its own runtime as a governed task, checked against the closed form,
decided. THE NEGATIVE TWIN is that leg with the FAULT build of the FMU: its
own invariants hold, its claim boundary is intact, governance is honest --
and the independent check FAILs it and the reviewer REJECTS it. A
demonstration that never showed a refusal would show nothing.

Nothing here is authoritative by being demonstrated: the decisions are the
authority store's, made by its rules, and this tool only reports them.

    python tools/harness_demo.py run --out demo.json \\
        [--fenicsx-python P] [--fmi-python P] [--require fenicsx,fmi]
    python tools/harness_demo.py compare A.json B.json   # native vs container
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

WS = "verification/stage10/harness_demo"
AGENT = "ai-proposer-demo"
QUERY = "independent finite-element verification of the transient slab"
SCHEMA = "harness-demo/1"


class DemoFailed(RuntimeError):
    pass


def _sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _world(base: Path):
    from qta_agent.agents import AgentRole, PrincipalKind, identity
    from qta_agent.events import EventLog
    from qta_agent.evidence import EvidenceStore
    from qta_agent.governed_model import GovernedModelRuns
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    log = EventLog(base / "log.jsonl")
    ev = EvidenceStore(base / "evidence")
    g = GovernedModelRuns(root=ROOT, log=log, evidence=ev)
    g.gov.agents.register(identity(agent_id=AGENT, instance_id=AGENT,
                                   kind=PrincipalKind.AGENT,
                                   roles={AgentRole.PROPOSER}), by="system")
    return log, ev, g


def _receive(log, ctx, index: int, parent: str | None = None):
    from qta_agent import proposals as PR
    fixture = ROOT / "integrations" / "proposals" / "recorded_fixture.jsonl"
    ad = PR.RecordedFixtureAdapter(fixture.read_text(encoding="utf-8"),
                                   source=fixture.name)
    resp = list(ad.responses(ctx))[index]

    def now(path: str):
        q = ROOT / path
        return _sha_file(q) if q.is_file() else None
    ing = PR.ProposalIngress(log, source_digest=now)
    rec = PR.envelope(resp, context=ctx, agent_id=AGENT, adapter=ad,
                      parent_proposal_id=parent,
                      iteration=0 if parent is None else 1)
    return ing, ing.receive(rec)


def _decision(g, run, chk) -> dict:
    rec = g.decide(run, chk)
    report = json.loads(g.evidence.get(chk.report_sha256))
    out = {"record_id": run.record_id, "state": rec.state.value,
           "proposer": g.authority.get(run.record_id).proposer,
           "cited_report": chk.report_sha256,
           "report_status": report["status"],
           "report_check": report["check_id"],
           "measured_ratio": (report["measured"] or {}).get("value")}
    if rec.state.value == "REJECTED":
        out["rejection"] = json.loads(g.evidence.get(
            g.authority.get(run.record_id).evidence["rejection_reason"]))
    return out


def slab_leg(log, ev, g, ctx, fenicsx: str | None, base: Path) -> dict:
    from scientific import hdf5_bundle as H
    from scientific.models.slab_transient import SlabTransientModel
    from scientific.result import ResultBundle
    ing, receipt = _receive(log, ctx, 0)
    run = ing.submit(receipt.envelope.proposal_id, g, out_dir=f"{WS}/slab")
    bundle = ResultBundle.from_record(json.loads(ev.get(run.bundle_sha256)))
    field_sha = run.governed.artifacts[f"{WS}/slab/temperature_field.bin"]
    payloads = {"temperature_field": ev.get(field_sha)}
    series = g.check(run, check_id="thermal.slab_series",
                     out_dir=f"{WS}/slab_series")
    checks = {"thermal.slab_series": json.loads(ev.get(
        series.report_sha256))["status"]}
    fem = None
    if fenicsx:
        fem = g.check(run, check_id="thermal.slab_fenicsx",
                      out_dir=f"{WS}/slab_fenicsx", runtime_python=fenicsx)
        checks["thermal.slab_fenicsx"] = json.loads(ev.get(
            fem.report_sha256))["status"]
    links = tuple(sorted(c.report_sha256 for c in (series, fem) if c))
    units = {p.name: p.unit for p in
             SlabTransientModel().parameter_schema.parameters}
    h5 = base / "slab_bundle.h5"
    h5_sha = H.write(h5, bundle, artifacts=payloads, parameter_units=units,
                     verification_links=links)
    back, arts, _ = H.read(h5)
    if back.to_record() != bundle.to_record() or arts != payloads:
        raise DemoFailed("the HDF5 representation did not round-trip")
    decision = _decision(g, run, fem or series)
    return {"proposal_id": receipt.envelope.proposal_id,
            "proposal_digest": receipt.envelope.digest(),
            "context_digest": ctx["digest"],
            "bundle_sha256": run.bundle_sha256,
            "bundle_digest": run.bundle_digest,
            "bundle_record": bundle.to_record(),
            "governed_task": run.governed.task_id,
            "checks": checks, "hdf5": {"path": h5.name, "sha256": h5_sha},
            "decision": decision}


def fmu_leg(log, ev, g, ctx, fmi: str, base: Path, *, fault: bool,
            parent: str | None = None) -> dict:
    """``parent``: the twin is its own proposal, iteration 1 of the first,
    so it binds its own idempotency key -- the same key for a different
    request (another FMU) is refused by the ledger, rightly."""
    import fmi_build
    from scientific.fmi_boundary import describe
    tag = "fault" if fault else "good"
    ing, receipt = _receive(log, ctx, 1, parent)
    built = fmi_build.build(base / f"fmu_{tag}", fault=fault)
    fmu = Path(built["fmu"])
    built["fmu"] = str(fmu.relative_to(ROOT))
    rec_path = base / f"fmu_{tag}" / "build_record.json"
    rec_path.write_text(json.dumps(built, sort_keys=True), encoding="utf-8")
    desc = describe(fmu)
    art = {"fmu_path": built["fmu"], "fmu_sha256": built["fmu_sha256"],
           "build_record_path": str(rec_path.relative_to(ROOT)),
           "build_record_sha256": _sha_file(rec_path),
           "step_s": 60.0, "runtime_python": fmi}
    run = ing.submit(receipt.envelope.proposal_id, g,
                     out_dir=f"{WS}/fmu_{tag}_run", fmu=art)
    chk = g.check(run, check_id="thermal.rc2_fmu",
                  out_dir=f"{WS}/fmu_{tag}_check")
    bundle = json.loads(ev.get(run.bundle_sha256))
    return {"fault_variant": fault, "fmu_sha256": built["fmu_sha256"],
            "proposal_id": receipt.envelope.proposal_id,
            "claim_boundary": desc.claim,
            "invariants": {i["invariant_id"]: i["holds"]
                           for i in bundle["invariants"]},
            "bundle_sha256": run.bundle_sha256,
            "governed_task": run.governed.task_id,
            "decision": _decision(g, run, chk)}


def restart(base: Path, log, ev, g) -> dict:
    """Checkpoint the authority projection, then restart from it as a new
    process would: the checkpoint audit decides the load, and the recovered
    state must equal a full replay of the log (R41)."""
    from qta_agent.checkpoint import CheckpointStore
    from qta_agent.governed_model import GovernedModelRuns
    from qta_agent.store import AuthorityStore
    cps = CheckpointStore(base / "checkpoints")
    cp = g.checkpoint(cps)
    again = GovernedModelRuns(root=ROOT, log=log, evidence=ev,
                              checkpoints=cps)
    _, cmp = AuthorityStore.recover_and_compare(
        log, cps, blobs=ev, evidence=ev, origins=again.origins)
    rec = dict(again.recovery or {})
    rec.update(checkpoint_written=cp.seq, agrees_with_full_replay=cmp[
        "agrees"], records=cmp["records"])
    return rec


def reconstruction(log_path: Path, ev_dir: Path) -> dict:
    import generic_consistency
    from qta_agent import reconstruct as rc
    from qta_agent.events import EventLog
    res = generic_consistency.verify(log_path, ev_dir)
    findings = {k: v for k, v in res.items() if k != "notes" and v}
    sub = rc.reconstruct_subsystems(EventLog(log_path))
    return {"generic_consistency_findings": findings,
            "second_reader_anomalies": sub.anomalies,
            "proposals_reconstructed": len(sub.proposals),
            "events_replayed": sub.events_replayed}


def crate(base: Path, report: dict) -> dict:
    """An RO-Crate 1.1 of the demonstration's files: packaging only."""
    files = sorted(p for p in base.rglob("*") if p.is_file()
                   and p.suffix in (".h5", ".fmu", ".json")
                   and p.name != "ro-crate-metadata.json")
    parts = [{"@id": str(p.relative_to(base)), "@type": "File",
              "name": p.name, "sha256": _sha_file(p),
              "contentSize": str(p.stat().st_size)} for p in files]
    meta = {"@context": "https://w3id.org/ro/crate/1.1/context",
            "@graph": [
                {"@id": "ro-crate-metadata.json", "@type": "CreativeWork",
                 "about": {"@id": "./"},
                 "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"}},
                {"@id": "./", "@type": "Dataset",
                 "name": "Scientific-AI harness: generic end-to-end "
                         "demonstration and its negative twin",
                 "description": "Governed proposal-to-decision history. "
                                "Packaging only: this crate adds no "
                                "evidence and decides nothing; the "
                                "decisions are the authority store's. "
                                "SIMULATION_RESULT, not measurement.",
                 "datePublished": "2026-10-08",
                 "license": {"@id": "#license-unspecified"},
                 "hasPart": [{"@id": p["@id"]} for p in parts],
                 "mentions": [{"@id": "#decisions"}]},
                {"@id": "#license-unspecified", "@type": "CreativeWork",
                 "name": "no license record exists; none invented",
                 "description": "the repository carries no license"},
                {"@id": "#decisions", "@type": "CreativeWork",
                 "name": "authority outcomes, as reported by the store",
                 "description": json.dumps(
                     {k: v["decision"]["state"] for k, v in
                      report["legs"].items() if "decision" in v},
                     sort_keys=True)},
                *parts]}
    out = base / "ro-crate-metadata.json"
    out.write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    problems = [p["@id"] for p in parts
                if _sha_file(base / p["@id"]) != p["sha256"]]
    return {"path": str(out.relative_to(ROOT)), "files": len(parts),
            "problems": problems, "sha256": _sha_file(out)}


def run(*, fenicsx: str | None, fmi: str | None, required: set) -> dict:
    from qta_agent import proposals as PR
    from qta_multiphysics.stack.rag_index import retrieve
    from scientific.backend_probe import run_environment
    if "fenicsx" in required and not fenicsx:
        raise DemoFailed("FEniCSx is required and no runtime was given")
    if "fmi" in required and not fmi:
        raise DemoFailed("FMI is required and no runtime was given")
    base = ROOT / WS
    log, ev, g = _world(base)
    ctx = PR.assemble_context(QUERY, retrieve=retrieve, k=3)
    report: dict = {"schema": SCHEMA, "workspace": WS,
                    "environment": run_environment().get("backend_status"),
                    "context": {"digest": ctx["digest"],
                                "citations": PR.citations(ctx)},
                    "legs": {}}
    report["legs"]["slab"] = slab_leg(log, ev, g, ctx, fenicsx, base)
    if fmi:
        report["legs"]["fmu"] = fmu_leg(log, ev, g, ctx, fmi, base,
                                        fault=False)
        report["legs"]["fmu_negative_twin"] = fmu_leg(
            log, ev, g, ctx, fmi, base, fault=True,
            parent=report["legs"]["fmu"]["proposal_id"])
    report["recovery"] = restart(base, log, ev, g)
    report["reconstruction"] = reconstruction(base / "log.jsonl",
                                              base / "evidence")
    _, events = log.read_verified()
    report["log"] = {"events": len(events), "head_hash": events[-1].hash}
    report["crate"] = crate(base, report)
    report["accepted"], report["why"] = judge(report, fenicsx, fmi)
    return report


def judge(report: dict, fenicsx, fmi) -> tuple:
    why = []
    legs = report["legs"]
    slab = legs["slab"]
    want = "VERIFIED" if fenicsx else "REJECTED"
    if slab["decision"]["state"] != want:
        why.append(f"slab decided {slab['decision']['state']}, expected "
                   f"{want}")
    if any(s != "PASS" for s in slab["checks"].values()):
        why.append(f"slab checks {slab['checks']}")
    if fmi:
        if legs["fmu"]["decision"]["state"] != "VERIFIED":
            why.append("the good FMU was not VERIFIED")
        twin = legs["fmu_negative_twin"]
        if twin["decision"]["state"] != "REJECTED":
            why.append("THE NEGATIVE TWIN WAS NOT REJECTED")
        if twin["decision"]["report_status"] != "FAIL":
            why.append("the negative twin's check did not FAIL")
        if not all(twin["invariants"].values()):
            why.append("the fault FMU's own invariants should hold -- only "
                       "the independent check may catch it")
    r41 = report.get("recovery") or {}
    if r41.get("mode") != "CHECKPOINT_ASSISTED" or not r41.get("healthy") \
            or not r41.get("agrees_with_full_replay") \
            or r41.get("prefix_verified") is not False:
        why.append(f"restart from the checkpoint: {r41}")
    rec = report["reconstruction"]
    if rec["generic_consistency_findings"] or rec["second_reader_anomalies"]:
        why.append(f"reconstruction findings: {rec}")
    if report["crate"]["problems"]:
        why.append(f"crate: {report['crate']['problems']}")
    return not why, why


def compare(a_path: Path, b_path: Path) -> dict:
    """Two demonstrations' slab results, under the equivalence policy."""
    from scientific import equivalence as EQ
    from scientific.result import ResultBundle
    a = json.loads(a_path.read_text(encoding="utf-8"))
    b = json.loads(b_path.read_text(encoding="utf-8"))
    ba = ResultBundle.from_record(a["legs"]["slab"]["bundle_record"])
    bb = ResultBundle.from_record(b["legs"]["slab"]["bundle_record"])
    rep = EQ.compare(ba, bb)
    return {"status": rep["status"], "decision": rep["decision"],
            "environments": rep["environments"],
            "outcomes": [a["legs"]["slab"]["decision"]["state"],
                         b["legs"]["slab"]["decision"]["state"]],
            "report": rep}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--fenicsx-python")
    r.add_argument("--fmi-python")
    r.add_argument("--require", default="")
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--out")
    args = ap.parse_args(argv)
    if args.cmd == "compare":
        res = compare(Path(args.a), Path(args.b))
        if args.out:
            Path(args.out).write_text(json.dumps(res, indent=1,
                                                 sort_keys=True) + "\n")
        print(f"slab result: {res['status']} ({res['decision']}); "
              f"outcomes {res['outcomes']}")
        return 0 if res["status"] in ("BYTE_IDENTICAL",
                                      "EQUIVALENT_AT_DECLARED_RESOLUTION") \
            and len(set(res["outcomes"])) == 1 else 1
    required = {x for x in args.require.split(",") if x}
    try:
        rep = run(fenicsx=args.fenicsx_python, fmi=args.fmi_python,
                  required=required)
    except Exception as exc:                     # noqa: BLE001
        print(f"DEMONSTRATION FAILED: {type(exc).__name__}: {exc}")
        return 1
    Path(args.out).write_text(json.dumps(rep, indent=1, sort_keys=True)
                              + "\n", encoding="utf-8")
    for name, leg in rep["legs"].items():
        print(f"{name}: {leg['decision']['state']} "
              f"({leg['decision']['report_check']} "
              f"{leg['decision']['report_status']})")
    print("demonstration: " + ("ACCEPTED" if rep["accepted"] else
                               f"NOT ACCEPTED {rep['why']}"))
    return 0 if rep["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
