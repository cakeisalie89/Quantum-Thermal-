"""AI proposes, the harness decides: the ingress boundary, tried from both
sides.

From the front: a proposal is parsed under a closed schema, refused for
every field a proposer could use to claim authority, carries no
credential, and is content-addressed. From behind: once received it is a
record in the log (written by the ingress, never by the agent), it reaches
execution only through the governed path with its own agent as submitter,
and that agent can do nothing a proposer cannot -- decide, verify, sign as
a verifier, execute the check, or write outside governed execution. The
second reader reads every receipt again under rules restated in its own
words.
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent import proposals as PR  # noqa: E402
from qta_agent import reconstruct as rc  # noqa: E402
from qta_agent.agents import AgentRole, PrincipalKind, identity  # noqa: E402
from qta_agent.authority import Role, State, TransitionError  # noqa: E402
from qta_agent.canonical import digest  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_model import (  # noqa: E402
    CHECK_WORKER_ID, REVIEWER_ID, GovernedModelRuns, ModelRunRefused,
)
from qta_agent.store import StoreError  # noqa: E402

WS = "verification/stage10/_pytest_proposals"
FIXTURE = ROOT / "integrations" / "proposals" / "recorded_fixture.jsonl"
FIXTURE_TEXT = FIXTURE.read_text(encoding="utf-8")
AGENT = "ai-proposer-fixture"


def _context(hits=None):
    hits = hits if hits is not None else [{
        "path": "STACK.md", "line_start": 1, "line_end": 3,
        "source_sha256": digest("STACK.md"), "text": "reviewed text",
        "evidence_status": PR.EVIDENCE_STATUS}]
    return PR.assemble_context("slab", retrieve=lambda q, k: {"hits": hits})


def _responses():
    return list(PR.RecordedFixtureAdapter(FIXTURE_TEXT).responses({}))


def _record(i=0, **over):
    rec = PR.envelope(_responses()[i], context=_context(), agent_id=AGENT,
                      adapter=PR.RecordedFixtureAdapter(FIXTURE_TEXT))
    rec.update(over)
    return PR.seal(rec) if "proposal_id" not in over else rec


# ------------------------------------------------------------------ schema

def test_every_fixture_response_makes_a_valid_envelope():
    for i, _ in enumerate(_responses()):
        env = PR.ProposalEnvelope.parse(_record(i))
        assert env.record["authority"] == "NON_AUTHORITATIVE"
        assert env.proposal_id == PR.proposal_id_of(env.record)
        assert env.record["source"]["adapter"] == "recorded-fixture"


def test_the_id_is_the_content_and_cannot_be_chosen():
    rec = _record()
    rec["iteration"] = 0
    bad = dict(rec, proposal_id="prop-" + "0" * 64)
    with pytest.raises(PR.ProposalRefused, match="digest of the proposal"):
        PR.ProposalEnvelope.parse(bad)


def _mutated(path, value):
    rec = copy.deepcopy(_record())
    d = rec
    for k in path[:-1]:
        d = d[k]
    if value is KeyError:
        del d[path[-1]]
    else:
        d[path[-1]] = value
    return PR.seal(rec)


@pytest.mark.parametrize("path, value, why", [
    (("authority",), "AUTHORITATIVE", "never authoritative"),
    (("authority",), "VERIFIED", "never authoritative"),
    (("schema",), "proposal-envelope/0", "schema"),
    (("kind",), "APPEND_TRANSITION", "kind"),
    (("agent_id",), "", "agent_id"),
    (("source", "adapter"), "trusted-core", "adapter"),
    (("source", "secret"), "x", "unknown"),
    (("context", "digest"), "abc", "context.digest"),
    (("context", "retrieved"), [{"path": "/etc/passwd", "line_start": 1,
                                 "line_end": 1,
                                 "source_sha256": "0" * 64}], "relative"),
    (("context", "retrieved"), [{"path": "a.md", "line_start": 3,
                                 "line_end": 1,
                                 "source_sha256": "0" * 64}], "line range"),
    (("rationale_sha256",), "a rationale in clear", "digest"),
    (("parent_proposal_id",), "prop-" + "1" * 64, "iteration 0"),
    (("iteration",), 2, "iteration 0"),
    (("iteration",), True, "iteration"),
    (("created_by", "input_digest"), "x", "input_digest"),
    (("request", "model_id"), "../../etc", "not a name"),
    (("request", "parameters"), [1, 2], "parameters"),
    (("request", "extra"), 1, "unknown"),
    (("request",), KeyError, "missing"),
    (("timestamp",), "now", "unknown"),
])
def test_a_malformed_envelope_is_refused(path, value, why):
    with pytest.raises(PR.ProposalRefused, match=why):
        PR.ProposalEnvelope.parse(_mutated(path, value))


@pytest.mark.parametrize("key", ["status", "verdict", "verified",
                                 "signature", "evidence", "out_dir",
                                 "transition", "Promoted"])
def test_an_authority_bearing_key_anywhere_is_refused(key):
    rec = _record()
    rec["request"]["parameters"][key] = "VERIFIED"
    with pytest.raises(PR.ProposalRefused, match="cannot carry"):
        PR.ProposalEnvelope.parse(PR.seal(rec))


@pytest.mark.parametrize("secret", [
    "sk-ant-api03-" + "a" * 40, "Bearer " + "b" * 30,
    "-----BEGIN RSA PRIVATE KEY-----", "AKIA" + "C" * 16,
    "ghp_" + "d" * 36, "xoxb-" + "1" * 20])
def test_a_credential_is_refused_and_never_stored(secret, tmp_path):
    resp = dict(_responses()[2])
    resp["request"] = dict(resp["request"], question=f"use {secret}")
    with pytest.raises(PR.ProposalRefused, match="credential"):
        PR.envelope(resp, context=_context(), agent_id=AGENT,
                    adapter=PR.RecordedFixtureAdapter(FIXTURE_TEXT))
    log = EventLog(tmp_path / "log.jsonl")
    rec = _record(2)
    rec["request"]["question"] = f"use {secret}"
    with pytest.raises(PR.ProposalRefused):
        PR.ProposalIngress(log).receive(PR.seal(rec))
    assert secret not in (tmp_path / "log.jsonl").read_text() \
        if (tmp_path / "log.jsonl").exists() else True


def test_non_json_oversized_and_deep_records_are_refused():
    rec = _record()
    rec["request"]["parameters"]["L_m"] = float("nan")
    with pytest.raises(PR.ProposalRefused):
        PR.ProposalEnvelope.parse(rec)
    rec = _record(2)
    rec["request"]["question"] = "x" * (PR.MAX_RECORD_BYTES + 1)
    with pytest.raises(PR.ProposalRefused):
        PR.ProposalEnvelope.parse(PR.seal(rec))
    deep: dict = {}
    d = deep
    for _ in range(PR.MAX_DEPTH + 2):
        d["a"] = {}
        d = d["a"]
    rec = _record()
    rec["request"]["parameters"]["nested"] = deep
    with pytest.raises(PR.ProposalRefused, match="deeper"):
        PR.ProposalEnvelope.parse(PR.seal(rec))
    with pytest.raises(PR.ProposalRefused):
        PR.ProposalEnvelope.parse(["not", "a", "record"])


def test_a_code_patch_is_a_digest_outside_authority():
    resp = {"kind": "CODE_PATCH", "provider": "any", "model": "any",
            "rationale": "r", "request": {"patch_sha256": "e" * 64,
                                          "files": ["scientific/x.py"],
                                          "summary": "s"}}
    env = PR.ProposalEnvelope.parse(PR.envelope(
        resp, context=_context(), agent_id=AGENT,
        adapter=PR.ExternalClientAdapter(lambda c: resp)))
    assert env.kind == "CODE_PATCH"
    resp["request"]["patch_sha256"] = "diff --git a b"
    with pytest.raises(PR.ProposalRefused, match="by digest"):
        PR.envelope(resp, context=_context(), agent_id=AGENT,
                    adapter=PR.ExternalClientAdapter(lambda c: resp))


# ---------------------------------------------------------------- adapters

def test_an_external_client_is_held_to_the_same_envelope():
    seen = {}

    def client(ctx):
        seen.update(ctx)
        return {"kind": "MODEL_RUN", "provider": "some-provider",
                "model": "some-model-2", "rationale": "because",
                "request": {"model_id": "thermal.rc2_network",
                            "model_version": "1.0.0", "parameters": {}}}
    ad = PR.ExternalClientAdapter(client)
    ctx = _context()
    [resp] = list(ad.responses(ctx))
    env = PR.ProposalEnvelope.parse(PR.envelope(resp, context=ctx,
                                                agent_id=AGENT, adapter=ad))
    assert env.record["source"] == {"adapter": "external-client",
                                    "provider": "some-provider",
                                    "model": "some-model-2"}
    assert set(seen) == {"query", "hits", "evidence_status"}
    assert seen["evidence_status"] == PR.EVIDENCE_STATUS


@pytest.mark.parametrize("resp", [
    {"kind": "MODEL_RUN", "provider": None, "model": None, "rationale": "r",
     "request": {"model_id": "m", "model_version": "1", "parameters": {}},
     "authority": "VERIFIED"},
    {"kind": "MODEL_RUN", "provider": None, "model": None, "rationale": "r",
     "request": {"model_id": "m", "model_version": "1",
                 "parameters": {"verified": True}}},
    "VERIFIED",
])
def test_a_client_claiming_authority_is_refused(resp):
    ad = PR.ExternalClientAdapter(lambda c: resp)
    with pytest.raises(PR.ProposalRefused):
        for r in ad.responses(_context()):
            PR.envelope(r, context=_context(), agent_id=AGENT, adapter=ad)


def test_the_trusted_core_names_no_provider_and_imports_no_client():
    src = (ROOT / "qta_agent" / "proposals.py").read_text().lower()
    for name in ("anthropic", "openai", "gemini", "import requests",
                 "import httpx", "urllib.request", "api_key"):
        assert name not in src, name


# ----------------------------------------------------------------- context

def test_retrieved_text_is_stamped_not_evidence_and_cited():
    ctx = _context()
    assert ctx["evidence_status"] == PR.EVIDENCE_STATUS
    assert ctx["generation"] == "NONE"
    assert PR.citations(ctx) == [{"line_end": 3, "line_start": 1,
                                  "path": "STACK.md",
                                  "source_sha256": digest("STACK.md")}]
    with pytest.raises(PR.ProposalRefused, match="stamped"):
        _context([{"path": "a", "line_start": 1, "line_end": 1,
                   "source_sha256": "0" * 64, "text": "t",
                   "evidence_status": "EVIDENCE"}])


def test_governed_retrieval_over_the_real_corpus():
    from qta_multiphysics.stack.rag_index import retrieve
    ctx = PR.assemble_context("finite element slab verification",
                              retrieve=retrieve, k=3)
    assert ctx["hits"] and all(h["source_sha256"] for h in ctx["hits"])
    assert PR.assemble_context("finite element slab verification",
                               retrieve=retrieve, k=3)["digest"] == \
        ctx["digest"], "retrieval is deterministic"


def test_a_stale_citation_is_refused(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    ing = PR.ProposalIngress(log, source_digest=lambda p: "f" * 64)
    with pytest.raises(PR.ProposalRefused, match="stale"):
        ing.receive(_record())
    assert not (tmp_path / "log.jsonl").exists() or \
        PR.received(log) == {}


# ----------------------------------------------------------------- receipt

def test_receipt_is_durable_idempotent_and_written_by_the_ingress(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    ing = PR.ProposalIngress(log)
    rec = _record()
    first = ing.receive(rec)
    again = ing.receive(rec)
    assert not first.duplicate and again.duplicate
    assert again.seq == first.seq
    _, events = log.read_verified()
    receipts = [e for e in events if e.action == PR.ACT_PROPOSAL_RECEIVE]
    assert len(receipts) == 1
    assert receipts[0].actor == PR.INGRESS_ID != AGENT
    assert PR.received(log)[first.envelope.proposal_id]["agent_id"] == AGENT


def test_an_unreadable_history_is_refused_not_projected_empty(tmp_path):
    """A first draft of received() discarded read_verified's report, so a log
    with a broken chain gave an empty projection -- every proposal silently
    'never received' -- instead of a refusal."""
    log = EventLog(tmp_path / "log.jsonl")
    PR.ProposalIngress(log).receive(_record())
    PR.ProposalIngress(log).receive(_record(1))
    lines = log.path.read_text().splitlines(keepends=True)
    lines[0] = lines[0].replace('"proposal.receive"', '"proposal.recieve"')
    log.path.write_text("".join(lines))
    with pytest.raises(Exception, match="(?i)hash|chain|seq"):
        PR.received(EventLog(tmp_path / "log.jsonl"))


def test_the_agent_cannot_be_its_own_ingress(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    with pytest.raises(PR.ProposalRefused, match="cannot be the ingress"):
        PR.ProposalIngress(log, actor=AGENT).receive(_record())


def test_a_projection_refuses_one_id_with_another_content(tmp_path):
    """The id is the content's digest, so 'one id, two contents' is a forged
    id -- refused by the parse before any comparison could matter."""
    log = EventLog(tmp_path / "log.jsonl")
    rec = _record()
    PR.ProposalIngress(log).receive(rec)
    other = copy.deepcopy(rec)
    other["iteration"] = 0
    other["request"]["parameters"]["L_m"] = 0.06
    log.append(actor=PR.INGRESS_ID, action=PR.ACT_PROPOSAL_RECEIVE,
               target=rec["proposal_id"],
               payload={"envelope": other, "envelope_digest": digest(other),
                        "received_by": PR.INGRESS_ID,
                        "authority": PR.AUTHORITY})
    with pytest.raises(PR.ProposalRefused):
        PR.received(log)


# ------------------------------------------------- the governed submission

@pytest.fixture(scope="module")
def world():
    base = ROOT / WS
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    log = EventLog(base / "log.jsonl")
    g = GovernedModelRuns(root=ROOT, log=log,
                          evidence=EvidenceStore(base / "evidence"))
    g.gov.agents.register(identity(agent_id=AGENT, instance_id=AGENT,
                                   kind=PrincipalKind.AGENT,
                                   roles={AgentRole.PROPOSER}), by="system")
    ing = PR.ProposalIngress(log)
    receipt = ing.receive(_record(0))
    diag = ing.receive(_record(2))
    run = ing.submit(receipt.envelope.proposal_id, g, out_dir=f"{WS}/run")
    chk = g.check(run, check_id="thermal.slab_series",
                  out_dir=f"{WS}/check")
    yield g, ing, log, receipt, diag, run, chk
    if base.exists():
        shutil.rmtree(base)


def test_a_received_proposal_runs_under_governance_as_its_agent(world):
    g, _, _, receipt, _, run, chk = world
    rec = g.authority.get(run.record_id)
    assert rec.proposer == AGENT
    assert rec.state is State.PROPOSED
    assert json.loads(g.evidence.get(chk.report_sha256))["status"] == "PASS"
    assert run.governed.task_id


def test_a_resubmitted_proposal_executes_nothing_twice(world):
    g, ing, log, receipt, _, run, _ = world
    again = ing.submit(receipt.envelope.proposal_id, g,
                       out_dir=f"{WS}/run")
    assert again.governed.task_id == run.governed.task_id
    assert again.record_id == run.record_id
    _, events = log.read_verified()
    creates = [e for e in events if e.action == "task.create"
               and e.target == run.governed.task_id]
    assert len(creates) == 1


def test_recovery_finds_a_received_but_unsubmitted_proposal(world, tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    ing = PR.ProposalIngress(log)
    r = ing.receive(_record(0))
    # the process died here; a new one reads the log
    pending = PR.ProposalIngress(log).pending(lambda pid: False)
    assert [e.proposal_id for e in pending] == [r.envelope.proposal_id]
    assert PR.ProposalIngress(log).pending(lambda pid: True) == []


def test_only_a_received_model_run_is_executed(world):
    g, ing, _, _, diag, _, _ = world
    with pytest.raises(PR.ProposalRefused, match="DIAGNOSTIC"):
        ing.submit(diag.envelope.proposal_id, g, out_dir=f"{WS}/d")
    with pytest.raises(PR.ProposalRefused, match="never received"):
        ing.submit("prop-" + "9" * 64, g, out_dir=f"{WS}/n")


def test_an_fmu_proposal_never_names_its_own_binary(world, tmp_path):
    g, _, _, _, _, _, _ = world
    log = EventLog(tmp_path / "log.jsonl")
    ing = PR.ProposalIngress(log)
    r = ing.receive(_record(1))
    with pytest.raises(PR.ProposalRefused, match="admitted"):
        ing.submit(r.envelope.proposal_id, g, out_dir=f"{WS}/f")


def test_the_agent_cannot_decide_its_own_result(world):
    g, _, _, _, _, run, chk = world
    with pytest.raises(ModelRunRefused, match="cannot decide"):
        g.decide(run, chk, reviewer=AGENT)


def test_the_agent_cannot_move_authority_through_the_store(world):
    g, _, _, _, _, run, _ = world
    for dst in (State.UNDER_REVIEW, State.VERIFIED, State.PROMOTED):
        with pytest.raises((TransitionError, StoreError)):
            g.authority.transition(record_id=run.record_id, dst=dst,
                                   actor=AGENT, role=Role.VERIFIER)


def test_the_agent_cannot_execute_the_check_or_act_as_verifier(world):
    g, _, _, _, _, run, _ = world
    with pytest.raises(ModelRunRefused, match="proposed or ran"):
        g.check(run, check_id="thermal.slab_series", out_dir=f"{WS}/x",
                worker=AGENT)
    for role in (AgentRole.EXECUTOR, AgentRole.VERIFIER):
        with pytest.raises(Exception):
            g.gov.agents.require(AGENT, role)


def test_the_decision_traces_to_governed_evidence_not_the_proposal(world):
    """Decided by a reviewer from governed evidence alone. The series check
    is an ANALYTIC_REFERENCE -- evidence, but not what the current admission
    policy admits on (an INDEPENDENT_IMPLEMENTATION; the FEniCSx check is
    one, exercised where its runtime exists) -- so the decision is REJECTED
    for exactly that reason, and nothing the proposal carried is cited."""
    g, _, _, receipt, _, run, chk = world
    rec = g.decide(run, chk)
    assert rec.state is State.REJECTED
    reason = json.loads(g.evidence.get(
        g.authority.get(run.record_id).evidence["rejection_reason"]))
    assert reason["problems"] == ["the report is not an independent check"]
    cited = set(g.authority.get(run.record_id).evidence.values())
    pid = receipt.envelope.proposal_id
    assert pid not in cited and receipt.envelope.digest() not in cited
    for h in receipt.envelope.citations:
        assert h["source_sha256"] not in cited
    report = json.loads(g.evidence.get(chk.report_sha256))
    assert report["verifier_id"] == CHECK_WORKER_ID != AGENT


# ------------------------------------------------------------ second reader

def test_the_second_reader_agrees_with_the_ingress(world):
    _, _, log, _, _, _, _ = world
    recon = rc.reconstruct_subsystems(log)
    mine = {pid: {"digest": r["digest"], "agent_id": r["agent_id"],
                  "kind": r["kind"]}
            for pid, r in PR.received(log).items()}
    assert rc.compare_subsystems({"proposals": mine}, recon) == ()
    assert not [a for a in recon.anomalies if "proposal" in a]


def _forge(tmp_path, mutate_payload, actor=PR.INGRESS_ID):
    log = EventLog(tmp_path / "log.jsonl")
    rec = _record()
    payload = {"envelope": rec, "envelope_digest": digest(rec),
               "received_by": PR.INGRESS_ID, "authority": PR.AUTHORITY}
    mutate_payload(payload)
    log.append(actor=actor, action=PR.ACT_PROPOSAL_RECEIVE,
               target=rec["proposal_id"], payload=payload)
    return rc.reconstruct_subsystems(log).anomalies


def test_a_receipt_in_another_writers_name_is_a_finding(tmp_path):
    found = _forge(tmp_path, lambda p: p.update(received_by="someone-else"))
    assert any("received_by" in a for a in found)
    found = _forge(tmp_path / "b", lambda p: p.update(received_by=AGENT),
                   actor=AGENT)
    assert any("wrote its own receipt" in a for a in found)


@pytest.mark.parametrize("mutate, finding", [
    (lambda p: p["envelope"].update(authority="VERIFIED"),
     "claiming authority"),
    (lambda p: p["envelope"]["request"]["parameters"].update(
        verdict="PASS"), "authority-bearing"),
    (lambda p: p.update(envelope_digest="0" * 64), "stated digest"),
    (lambda p: p["envelope"].update(extra=1), "fields differ"),
    (lambda p: p["envelope"].update(proposal_id="prop-" + "a" * 64),
     "not the digest"),
    (lambda p: p.pop("envelope"), "no envelope"),
])
def test_a_forged_receipt_is_a_finding_for_the_second_reader(
        tmp_path, mutate, finding):
    assert any(finding in a for a in _forge(tmp_path, mutate))


def test_a_receipt_whose_digest_is_not_its_envelopes_is_refused(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    rec = _record()
    log.append(actor=PR.INGRESS_ID, action=PR.ACT_PROPOSAL_RECEIVE,
               target=rec["proposal_id"],
               payload={"envelope": rec, "envelope_digest": "0" * 64,
                        "received_by": PR.INGRESS_ID,
                        "authority": PR.AUTHORITY})
    with pytest.raises(PR.ProposalRefused, match="digest mismatch"):
        PR.received(log)


def test_a_receipt_filed_under_another_id_is_refused(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    rec = _record()
    log.append(actor=PR.INGRESS_ID, action=PR.ACT_PROPOSAL_RECEIVE,
               target="prop-" + "b" * 64,
               payload={"envelope": rec, "envelope_digest": digest(rec),
                        "received_by": PR.INGRESS_ID,
                        "authority": PR.AUTHORITY})
    with pytest.raises(PR.ProposalRefused, match="target"):
        PR.received(log)


def test_the_second_reader_refuses_a_proposer_picking_up_its_own_claim(
        tmp_path):
    """D-2026-105, read the second time: a record.transition taking a claim
    PROPOSED -> UNDER_REVIEW by its own proposer, appended straight into the
    log past the store, is refused on replay."""
    log = EventLog(tmp_path / "log.jsonl")
    log.append(actor=AGENT, action="record.create", target="r1",
               payload={"record_id": "r1", "kind": "claim",
                        "proposer": AGENT, "state": "PROPOSED",
                        "evidence": {}, "depends_on": [],
                        "policy_id": None, "idempotency_key": None})
    log.append(actor=AGENT, action="record.transition", target="r1",
               payload={"record_id": "r1", "src": "PROPOSED",
                        "dst": "UNDER_REVIEW", "role": "VERIFIER",
                        "evidence": {}, "policy_id": None,
                        "stale_reason": None, "edge_reason": "",
                        "idempotency_key": None})
    recon = rc.reconstruct(log)
    assert any("I4" in u or "proposer" in u.lower()
               for u in recon.unauthorized), recon.unauthorized


def test_a_decision_interrupted_after_the_pickup_resumes(world):
    """A crash between the reviewer's pickup and the verdict leaves the
    record UNDER_REVIEW; deciding again goes on to the verdict instead of
    failing on a pickup that already happened."""
    import dataclasses
    g, _, _, _, _, run, chk = world
    rid = f"{run.record_id}-resume"
    g.authority.create(record_id=rid, kind="scientific_result",
                       proposer=AGENT,
                       evidence={"result_bundle": run.bundle_sha256})
    g.authority.transition(record_id=rid, dst=State.UNDER_REVIEW,
                           actor=REVIEWER_ID, role=Role.VERIFIER)
    rec = g.decide(dataclasses.replace(run, record_id=rid), chk)
    assert rec.state is State.REJECTED
