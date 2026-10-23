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
    release candidate           built from the commit and verified by the
                                portable verifier to PROVENANCE_STRUCTURE;
                                the release gate refuses a digest-only run.
                                The live signature is the hosted
                                supply-chain job's (GitHub OIDC); here it is
                                NOT_CHECKED and the report says so
    hostile inputs              a tampered candidate, a forged admission, the
                                proposer as its own reviewer, a decision
                                under a withdrawn policy: each refused, and
                                the demonstration fails if one is not

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
    from agent_substrate.agents import AgentRole, PrincipalKind, identity
    from agent_substrate.events import EventLog
    from agent_substrate.evidence import EvidenceStore
    from agent_substrate.governed_model import GovernedModelRuns
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
    from agent_substrate import proposals as PR
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


def slab_leg(log, ev, g, ctx, fenicsx: str | None, base: Path,
             keep: dict | None = None) -> dict:
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
    if keep is not None:
        keep.update(run=run, check=fem or series)
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
    from agent_substrate.checkpoint import CheckpointStore
    from agent_substrate.governed_model import GovernedModelRuns
    from agent_substrate.store import AuthorityStore
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
    from agent_substrate import reconstruct as rc
    from agent_substrate.events import EventLog
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


def release_leg(base: Path) -> dict:
    """The candidate this commit would release, verified as far as a run
    without an OIDC identity can: integrity and provenance structure.
    Signature, signer and authorization are NOT_CHECKED here, and the gate
    refuses a digest-only verification (D-2026-143)."""
    import supply_chain as SC
    out = base / "release-candidate"
    built = SC.build(out, allow_dirty=True)
    policy = ROOT / "docs" / "supply_chain_ci_policy.json"
    rep = SC.verify(out, SC.load_policy(policy), mode="digests")
    try:
        SC.admit(out, policy, mode="digests", trust_config=None)
        gate = {"admitted": True, "reason": ""}
    except SC.Refused as exc:
        gate = {"admitted": False, "reason": str(exc)}
    return {"commit": built["commit"], "dirty": built["dirty"],
            "deterministic_payload": built["deterministic_payload"],
            "levels": {k: v["status"] for k, v in rep["levels"].items()},
            "highest_established": rep["highest_established"],
            "release_authorized": rep["release_authorized"],
            "source_tree": rep["source_tree"], "gate": gate,
            "signature": {
                "status": "NOT_CHECKED_HERE",
                "live_provider": "UNTESTED in this run: the hosted "
                                 "supply-chain job signs with its OIDC "
                                 "identity and verifies online, offline "
                                 "and from a network namespace"}}


def hostile_leg(base: Path, g, slab: dict) -> dict:
    """Malicious inputs, each through the real component it attacks; every
    one must be refused, and the reason is recorded."""
    import offline_verify as OV
    from agent_substrate import reconstruct as rc
    from agent_substrate import result_rules
    from agent_substrate.authority import Role, State
    from agent_substrate.events import EventLog
    from agent_substrate.evidence import EvidenceStore
    from agent_substrate.governed_model import (REVIEWER_ID,
                                                GovernedModelRuns,
                                                ModelRunRefused)
    from agent_substrate.governed_tasks import POLICY_ID, SUBMITTER_ID
    from agent_substrate.policy import PolicyRequest, PolicyStore, \
        UnknownPolicy
    from agent_substrate.store import StoreError
    out: dict = {}

    def refused(name, fn, errors):
        try:
            fn()
            out[name] = {"refused": False, "reason": ""}
        except errors as exc:
            out[name] = {"refused": True, "reason": str(exc)[:300]}

    def copy_world(name: str) -> Path:
        dst = base / "hostile" / name
        if dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True)
        shutil.copy(base / "log.jsonl", dst / "log.jsonl")
        shutil.copytree(base / "evidence", dst / "evidence")
        return dst

    # 1. a release candidate with one byte changed after it was built
    rc_copy = base / "hostile" / "release-candidate"
    if rc_copy.exists():
        shutil.rmtree(rc_copy)
    shutil.copytree(base / "release-candidate", rc_copy)
    z = bytearray((rc_copy / "source.zip").read_bytes())
    z[200] ^= 0x01
    (rc_copy / "source.zip").write_bytes(bytes(z))
    refused("tampered_release_candidate", lambda: OV.verify_bundle(
        rc_copy, OV.load_policy(ROOT / "docs" / "supply_chain_ci_policy.json"),
        mode="digests"), OV.Refused)

    # 2. a VERIFIED line citing the genuine report's fields in other bytes
    w = copy_world("forged-admission")
    gw = GovernedModelRuns(root=ROOT, log=EventLog(w / "log.jsonl"),
                           evidence=EvidenceStore(w / "evidence"))
    gw.authority.create(record_id="hostile-forged", kind=result_rules.KIND,
                        proposer=SUBMITTER_ID,
                        evidence={"result_bundle": slab["bundle_sha256"]})
    gw.authority.transition(record_id="hostile-forged",
                            dst=State.UNDER_REVIEW, actor=REVIEWER_ID,
                            role=Role.VERIFIER)
    genuine = slab["decision"]["cited_report"]
    forged = EvidenceStore(w / "evidence").put(json.dumps(json.loads(
        EvidenceStore(w / "evidence").get(genuine)), indent=2).encode())
    EventLog(w / "log.jsonl").append(
        actor=REVIEWER_ID, action="record.transition", target="hostile-forged",
        payload={"record_id": "hostile-forged", "src": "UNDER_REVIEW",
                 "dst": "VERIFIED", "role": "VERIFIER",
                 "evidence": {"verification_report": forged},
                 "policy_id": None, "stale_reason": None,
                 "edge_reason": "forged", "idempotency_key": None,
                 "admission_policy": result_rules.CURRENT_POLICY})
    refused("forged_admission", lambda: GovernedModelRuns(
        root=ROOT, log=EventLog(w / "log.jsonl"),
        evidence=EvidenceStore(w / "evidence")), StoreError)
    out["forged_admission"]["second_reader_flags_it"] = any(
        "hostile-forged" in u for u in rc.reconstruct(
            EventLog(w / "log.jsonl"),
            evidence=EvidenceStore(w / "evidence")).unauthorized)

    # 3. the proposer as its own reviewer
    run, check = slab["_run"], slab["_check"]
    refused("proposer_as_reviewer", lambda: g.decide(
        run, check, reviewer=run.submitter), ModelRunRefused)

    # 4. a decision under the governed policy after its owner withdrew it
    w = copy_world("withdrawn-policy")
    ps = PolicyStore(EventLog(w / "log.jsonl")).load()
    ps.withdraw(POLICY_ID, actor="owner", reason="demonstration: revoked")
    refused("decision_under_a_withdrawn_policy", lambda: ps.decide_and_record(
        POLICY_ID, PolicyRequest(action="stage10.execute", subject=AGENT,
                                 role="SUBMITTER", resource="any",
                                 task_id="hostile"), actor=AGENT),
        UnknownPolicy)
    return out


def run(*, fenicsx: str | None, fmi: str | None, required: set) -> dict:
    from agent_substrate import proposals as PR
    from agent_substrate.rag_index import retrieve
    from scientific.backend_probe import run_environment
    if "fenicsx" in required and not fenicsx:
        raise DemoFailed("FEniCSx is required and no runtime was given")
    if "fmi" in required and not fmi:
        raise DemoFailed("FMI is required and no runtime was given")
    base = ROOT / WS
    log, ev, g = _world(base)
    ctx = PR.assemble_context(QUERY, retrieve=retrieve, k=3)
    report: dict = {"schema": SCHEMA, "workspace": WS,
                    # What this run could demonstrate. Without FEniCSx the
                    # slab is decided on the series check alone (REJECTED);
                    # without FMI the FMU legs do not run. An ACCEPTED run of
                    # the smaller scope is a demonstration of refusal, not
                    # of admission, and the report says which it is.
                    "scope": {"fenicsx_check": bool(fenicsx),
                              "fmu_legs": bool(fmi),
                              "required": sorted(required)},
                    "environment": run_environment().get("backend_status"),
                    "context": {"digest": ctx["digest"],
                                "citations": PR.citations(ctx)},
                    "legs": {}}
    kept: dict = {}
    report["legs"]["slab"] = slab_leg(log, ev, g, ctx, fenicsx, base,
                                      keep=kept)
    if fmi:
        report["legs"]["fmu"] = fmu_leg(log, ev, g, ctx, fmi, base,
                                        fault=False)
        report["legs"]["fmu_negative_twin"] = fmu_leg(
            log, ev, g, ctx, fmi, base, fault=True,
            parent=report["legs"]["fmu"]["proposal_id"])
    report["release"] = release_leg(base)
    report["hostile"] = hostile_leg(
        base, g, {**report["legs"]["slab"], "_run": kept["run"],
                  "_check": kept["check"]})
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
    rel = report.get("release") or {}
    if rel.get("highest_established") != "PROVENANCE_STRUCTURE" \
            or rel.get("release_authorized") is not False \
            or (rel.get("gate") or {}).get("admitted") is not False:
        why.append(f"release candidate: {rel}")
    hostile = report.get("hostile") or {}
    if not hostile or not all(c.get("refused") for c in hostile.values()):
        why.append(f"A HOSTILE INPUT WAS NOT REFUSED: {hostile}")
    elif not hostile["forged_admission"].get("second_reader_flags_it"):
        why.append("the second reader did not flag the forged admission")
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
    # Which demonstration each run WAS. Two runs of the smaller scope agree
    # about a refusal; that is not a reproduction of an admission, and the
    # comparison says which one it is rather than leaving it to prose.
    return {"status": rep["status"], "decision": rep["decision"],
            "environments": rep["environments"],
            "outcomes": [a["legs"]["slab"]["decision"]["state"],
                         b["legs"]["slab"]["decision"]["state"]],
            "scopes": [a.get("scope"), b.get("scope")],
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
              f"outcomes {res['outcomes']}; scopes {res['scopes']}")
        return 0 if res["status"] in ("BYTE_IDENTICAL",
                                      "EQUIVALENT_AT_DECLARED_RESOLUTION") \
            and len(set(res["outcomes"])) == 1 \
            and res["scopes"][0] == res["scopes"][1] else 1
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
    sc = rep["scope"]
    scope = ("FEniCSx check " + ("run" if sc["fenicsx_check"] else
                                 "NOT run -- the slab decided without it")
             + "; FMU legs " + ("run" if sc["fmu_legs"] else "NOT run"))
    print("demonstration: " + ("ACCEPTED" if rep["accepted"] else
                               f"NOT ACCEPTED {rep['why']}")
          + f" (scope: {scope})")
    return 0 if rep["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
