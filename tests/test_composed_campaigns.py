"""Composed adversarial campaigns (section 7): attacks on the interactions.

docs/assurance/campaigns.json registers ten campaigns crossing several
subsystems each, with the seven things the directive asks of one: the
invariant, a valid control, the adversarial input, the expected outcome,
the independent observable evidence, the regression test (or that none was
needed) and the risk that remains. Six are held by composed tests that
already existed; the registry names them, and this file checks every named
test exists. Four crossed subsystems no test had joined, and are here:

K1  correct but old evidence -> a current-world claim -> no re-observation
K2  a poisoned memory -> a recalled summary -> a model's claim -> an
    attempt to verify it
K3  a policy change -> a context built under the old one -> a delayed
    proposal -> its authorization
K4  a forged event -> a checkpoint made to match it -> an independent
    witness

Each one counts its attempts, and the registry records the counts; the
test that computes them checks the registry says the same, so a number in
the registry is measured, not claimed.
"""
from __future__ import annotations

import ast
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_freshness as TF  # noqa: E402
import test_inference_gateway as TG  # noqa: E402

from agent_substrate import checkpoint as CP  # noqa: E402
from agent_substrate import claim_check as C  # noqa: E402
from agent_substrate import inference as I  # noqa: E402
from agent_substrate import research as R  # noqa: E402
from agent_substrate import witness as W  # noqa: E402
from agent_substrate.authority import Role, State  # noqa: E402
from agent_substrate.canonical import digest  # noqa: E402
from agent_substrate.context import ContextBuilder, Tier, record_context  # noqa: E402
from agent_substrate.events import ChainState, EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.evidence_terms import Property  # noqa: E402
from agent_substrate.memory import MemoryStore  # noqa: E402
from agent_substrate.memory_trust import assess  # noqa: E402
from agent_substrate.policy import (  # noqa: E402
    ANY, Effect, PolicyError, PolicyRequest, PolicyStore, document, rule,
)
from agent_substrate.reconstruct import reconstruct_subsystems  # noqa: E402
from agent_substrate.store import AuthorityStore  # noqa: E402
from agent_substrate.trust_labels import (  # noqa: E402
    TrustError, label_of_memory, require,
)
from tools import claim_bench as B  # noqa: E402

REGISTRY = ROOT / "docs" / "assurance" / "campaigns.json"
FIELDS = ("invariant", "control", "adversarial_input", "expected_outcome",
          "evidence", "regression", "remaining_risk")


def _registry() -> dict:
    return {c["id"]: c for c in
            json.loads(REGISTRY.read_text(encoding="utf-8"))["campaigns"]}


def _counted(cid: str, attempts: list) -> None:
    """``attempts`` is ``(what, accepted)`` per adversarial attempt. None
    may be accepted, and the registry must say exactly what was tried."""
    accepted = [what for what, ok in attempts if ok]
    assert accepted == [], f"{cid}: accepted {accepted}"
    got = {"attempts": len(attempts), "accepted": 0,
           "tried": [what for what, _ in attempts]}
    assert _registry()[cid]["measured"] == got, (cid, got)


def _refused(fn, *errors) -> bool:
    try:
        fn()
    except errors:
        return True
    return False


# ------------------------------------------------------------------ K1

@pytest.fixture
def fw(tmp_path, monkeypatch):
    clock = TF.Clock(TF.T0)
    monkeypatch.setattr(time, "time", clock)
    return TF.World(tmp_path, clock)


def _claim(fw, text, cites):
    return C.check(text, cites=cites, objective_id="o1", ledger=fw.led,
                   store=AuthorityStore(fw.log, evidence=fw.evidence).load(),
                   evidence=fw.evidence)


def test_k1_old_evidence_is_not_a_current_fact_until_it_is_observed_again(
        fw):
    claim = "T_probe of probe-1 is below 400 K"
    TF._decided(fw)                                 # INTERVAL, one hour
    control = _claim(fw, claim, ["obs-1"])
    assert (control.verdict, control.established) == (C.Verdict.SUPPORTED,
                                                      True)

    fw.clock.t = TF.T0 + 2 * TF.HOUR               # the reading is old now
    stale = _claim(fw, claim, ["obs-1"])
    attempts = [
        ("a current-world claim on an outdated reading",
         stale.verdict is C.Verdict.SUPPORTED or stale.established),
        ("a conclusion on the outdated verdict",
         not _refused(lambda: fw.led.set_status(
             "o1", "CONCLUDED", reason="h1 holds", basis=["h1"]),
             R.ResearchError)),
        ("the same verdict re-recorded on the same outdated reading",
         not _refused(lambda: fw.led.set_status(
             "h1", "SUPPORTED", reason="still", basis=["obs-1"]),
             R.ResearchError)),
    ]
    assert stale.citation == "INTACT"               # authentic, and old
    assert stale.admission["obs-1"].startswith("its standing is UNKNOWN")
    assert stale.verdict is C.Verdict.INSUFFICIENT_EVIDENCE

    # a re-observation renews what the reading supported, and nothing else
    fw.measured("m2", 352.0, TF.T0 + 2 * TF.HOUR)
    fw.observe("obs-2", "m2")
    renewed = _claim(fw, claim, ["obs-2"])
    assert (renewed.verdict, renewed.established) == (C.Verdict.SUPPORTED,
                                                      True)
    assert fw.led.evaluate_hypothesis("h1") == "SUPPORTED"
    assert fw.led.get("h1").basis == ("obs-2",)
    # evidence about the world when it was measured, never the world now
    assert Property.CURRENT_WORLD not in renewed.label.properties
    assert fw.second_reader() == []
    _counted("K1", attempts)


# ------------------------------------------------------------------ K2

def test_k2_a_poisoned_memory_becomes_no_evidence_however_it_travels(
        tmp_path):
    w = B.World(tmp_path)
    mem = MemoryStore(w.log, evidence=w.evidence).load()
    bundle = w.ledger.get("o-slab-1-a").body["result_bundle"]
    # a note citing the real bytes and misreporting them, and a summary of
    # it: both pass every check of what they cite
    mem.remember(memory_id="m-poison", text="T_at_half of slab-1 is 290 K",
                 author="agent-x", derived_from=(bundle,),
                 confidence="certain")
    mem.summarize(memory_id="m-summary", author="agent-y",
                  text="slab-1 runs at 290 K at mid-plane",
                  summarizes=("m-poison",))
    b = ContextBuilder(task_id="t-k2", purpose="answer what slab-1 does")
    hits = mem.recall("slab-1 T_at_half", k=5)
    assert {h.evidence_status for h in hits} == {
        "REMEMBERED_TEXT_NOT_EVIDENCE"}
    for h in hits:
        b.add(item_id=h.memory_id, tier=Tier.MEMORY, text=h.text,
              source=h.memory_id)
    ctx = b.build(budget_bytes=10_000)
    record_context(w.log, ctx.manifest, actor="context-builder")
    shown = [i.item_id for i in ctx.manifest.shown(Tier.MEMORY)]
    assert "m-poison" in shown                       # the attack reached it

    # the model repeats what it was shown, citing the real observation
    said = w.check("T_at_half of slab-1 is 290 K", cites=["o-slab-1-a"])
    trust = assess(w.log, "m-poison", evidence=w.evidence)
    attempts = [
        ("the model's claim from the recalled note",
         said.verdict is C.Verdict.SUPPORTED or said.established),
        ("the note read as semantically supported",
         not _refused(lambda: require(label_of_memory(mem.get("m-poison")),
                                      Property.SEMANTICALLY_SUPPORTED),
                      TrustError)),
        ("the summary read as semantically supported",
         not _refused(lambda: require(label_of_memory(mem.get("m-summary")),
                                      Property.SEMANTICALLY_SUPPORTED),
                      TrustError)),
        ("the note recorded as a research observation",
         not _refused(lambda: w.ledger.observe(
             observation_id="o-from-memory", objective_id=B.OBJECTIVE,
             record_id="m-poison", bundle_bytes=w.evidence.get(bundle)),
             R.ResearchError)),
    ]
    assert said.citation == "INTACT"                 # what it cites is real
    assert said.verdict is C.Verdict.CONTRADICTED    # and says otherwise
    # the bytes it cites verify; that is not the note being true
    assert trust.answers["evidence_supported"].holds
    assert trust.answers["factually_established"].value == "NOT_ESTABLISHED"

    honest = w.check("T_at_half of slab-1 is 312.4 K", cites=["o-slab-1-a"])
    assert (honest.verdict, honest.established) == (C.Verdict.SUPPORTED, True)
    assert reconstruct_subsystems(w.log).anomalies == []
    _counted("K2", attempts)


# ------------------------------------------------------------------ K3

def _two_actions(version: int, *, deny_run: bool = False):
    every: dict = dict(subjects=(ANY,), roles=(ANY,), resources=(ANY,))
    rules = [rule(rule_id="infer", effect=Effect.ALLOW, actions=("infer",),
                  **every),
             rule(rule_id="run", effect=Effect.ALLOW, actions=("run_model",),
                  **every)]
    if deny_run:
        rules.insert(0, rule(rule_id="no-runs", effect=Effect.DENY,
                             actions=("run_model",), **every))
    return document(policy_id=TG.POLICY, version=version, rules=rules)


def test_k3_a_proposal_is_authorized_under_the_policy_in_force_not_its_own(
        tmp_path, monkeypatch):
    monkeypatch.setattr(TG, "_policy", lambda version=1: _two_actions(1))
    w = TG.World(tmp_path)
    out = w.infer()                                  # under version 1
    assert out.ok
    run = PolicyRequest(action="run_model", subject=TG.CALLER,
                        role="proposer", resource="thermal.slab_transient")
    control = w.policies.evaluate(TG.POLICY, run)
    assert control.allowed                           # what v1 would say
    # a decision taken under v1 whose verdict v2 gives too: only the digest
    # it cites says which version it was taken under
    infer = PolicyRequest(action="infer", subject=TG.CALLER, role="proposer",
                          resource="b1")
    old = w.policies.evaluate(TG.POLICY, infer)

    w.policies.publish(_two_actions(2, deny_run=True), actor="owner")
    decided = w.policies.decide_and_record(TG.POLICY, run, actor="scheduler")
    v1 = _two_actions(1).digest()
    assert w.policies.evaluate(TG.POLICY, infer).allowed == old.allowed
    forged = dict(old.to_record())
    body = {k: v for k, v in forged.items() if k != "at_seq"}
    attempts = [
        ("the delayed proposal authorized under the context's policy",
         decided.allowed),
        ("a second call with the context built under the old policy",
         not _refused(lambda: w.gateway().infer(
             w.request(key="k2"), I.render_prompt(w.context)),
             I.InferenceRefused)),
    ]
    assert decided.policy_digest != v1 and decided.rule_id == "no-runs"
    assert w.context.manifest.policy_digest == v1    # what it was shown
    # a decision citing the superseded version, appended around the store
    w.log.append(actor="scheduler", action="policy.decision",
                 target=infer.resource,
                 payload={"decision": forged, "decision_digest":
                          digest(body)})
    attempts.append(("a decision recorded under the superseded version",
                     not _refused(lambda: PolicyStore(w.log).load(),
                                  PolicyError)))
    found = reconstruct_subsystems(w.log).anomalies
    assert any("other than the version in force" in a for a in found), found
    _counted("K3", attempts)


# ------------------------------------------------------------------ K4

def _test_key(label: str) -> bytes:
    """A deterministic TEST secret, as test_witness uses: no production
    witness exists, and this is not one."""
    return hashlib.sha256(f"test-witness-key:{label}".encode()).digest()


def _rechain(log: EventLog, seq: int, edit) -> None:
    """Edit record ``seq`` and re-chain every record after it, head witness
    included: a rewrite every local check accepts."""
    recs = [json.loads(x) for x in
            log.path.read_text(encoding="utf-8").splitlines()]
    prev = recs[seq - 1]["hash"]
    for r in recs[seq:]:
        if r["seq"] == seq:
            edit(r)
        r["prev_hash"] = prev
        r["hash"] = digest({k: v for k, v in r.items() if k != "hash"})
        prev = r["hash"]
    log.path.write_text("".join(json.dumps(r, sort_keys=True) + "\n"
                                for r in recs), encoding="utf-8")
    log._write_head(ChainState(recs[-1]["seq"], recs[-1]["hash"]))


def test_k4_a_forged_history_with_its_own_checkpoint_is_caught_by_a_witness(
        tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    ev = EvidenceStore(tmp_path / "evidence")
    store = AuthorityStore(log, evidence=ev).load()
    store.create(record_id="r1", kind="measurement", proposer="ingest",
                 evidence={"result_bundle": ev.put(b"reading")})
    store.transition(record_id="r1", dst=State.UNDER_REVIEW,
                     actor="reviewer", role=Role.VERIFIER)
    store.transition(record_id="r1", dst=State.VERIFIED, actor="reviewer",
                     role=Role.VERIFIER, evidence={"verification_report":
                                                   ev.put(b"checked")})
    checkpoints = CP.CheckpointStore(tmp_path / "checkpoints")
    checkpoints.write(CP.create(log, state_digest=store.state_digest()))
    _, events = log.read_verified()
    clock = TF.Clock(1_800_000_000.0)
    wit = W.Witness("w1", _test_key("w1"), tmp_path / "w1",
                    clock=clock)
    origin = W.origin_of("k4", events)
    wit.provision(W.Checkpoint(origin, 0, events[0].hash))
    head = events[-1]
    cosig = wit.add_checkpoint(W.Checkpoint(origin, head.seq, head.hash),
                               W.proof_records(events, 0, head.seq))
    root = W.TrustRoot(origin=origin, witnesses={"w1": wit.public_key},
                       quorum=1)
    assert W.verify_witnessed(events, [cosig], root).problems == []  # control

    # who verified the record is rewritten, the chain re-made, and a fresh
    # checkpoint written over the honest one, as a writer with the disk can
    verified_at = next(e.seq for e in events if e.action == "record.transition"
                       and e.payload.get("dst") == "VERIFIED")
    _rechain(log, verified_at, lambda r: r.update(actor="reviewer-b"))
    shutil.rmtree(tmp_path / "checkpoints")
    forged_store = AuthorityStore(log, evidence=ev).load()
    checkpoints = CP.CheckpointStore(tmp_path / "checkpoints")
    checkpoints.write(CP.create(log, state_digest=forged_store.state_digest()))
    report, forged = log.read_verified()
    rep = W.verify_witnessed(forged, [cosig], root)
    attempts = [
        ("the rewritten history accepted against the witness",
         not any(p.startswith("FORKED") for p in rep.problems)),
    ]
    # every local check accepts it: that is the attack
    assert report.ok
    CP.check_against(log, checkpoints.latest())
    assert forged_store.get("r1").state is State.VERIFIED
    assert reconstruct_subsystems(log).anomalies == []
    assert any(p.startswith("FORKED") for p in rep.problems), rep.problems
    _counted("K4", attempts)


# ------------------------------------------------------------ the registry

def _test_names(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    names |= {t.id for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name)}
    return names


def test_every_campaign_states_all_seven_and_names_tests_that_exist():
    doc = json.loads(REGISTRY.read_text(encoding="utf-8"))
    ids = [c["id"] for c in doc["campaigns"]]
    assert len(ids) == len(set(ids)) == 10
    for c in doc["campaigns"]:
        for f in FIELDS:
            assert isinstance(c.get(f), str) and c[f].strip(), (c["id"], f)
        assert len(c["crosses"]) >= 3, c["id"]
        assert c["tests"], c["id"]
        for node in c["tests"]:
            path, _, name = node.partition("::")
            assert name in _test_names(ROOT / path), (c["id"], node)
    held_here = {c["id"] for c in doc["campaigns"]
                 if any(t.startswith("tests/test_composed_campaigns.py::")
                        for t in c["tests"])}
    assert held_here == {"K1", "K2", "K3", "K4"}
    assert all("measured" in _registry()[k] for k in held_here)
