"""A second implementation for the subsystems that had none.

R42 and R53 said the same thing from two directions: the independent
reconstruction rebuilt authority records and tasks, and the scheduler,
policy, capability, agent, memory, network, secret and context projections
were "compared only against a fresh replay of themselves, which shares
their reducer and so cannot see a shared misunderstanding".

reconstruct.py sits BELOW every one of those modules in the declared
layering, so the second reader cannot import their enums or call their
helpers even by accident. Everything it does is plain strings and plain
dicts, and every authority rule is restated rather than invoked.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.canonical import digest  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.governed_stage10 import GovernedStage10  # noqa: E402
from qta_agent.reconstruct import (  # noqa: E402
    compare_subsystems, reconstruct_subsystems,
)
from qta_agent.tasks import TaskState  # noqa: E402

WS = "verification/stage10/_pytest_second_reader"


@pytest.fixture()
def gov(request):
    name = request.node.name.replace("/", "_")[:60]
    base = ROOT / WS / name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    g = GovernedStage10(root=ROOT, log=EventLog(base / "log.jsonl"),
                        evidence=EvidenceStore(base / "evidence"))
    g.out_rel = f"{WS}/{name}/out"
    yield g
    if base.exists():
        shutil.rmtree(base)


def _run(g, **over):
    inputs = {"out_dir": g.out_rel, "name": "artifact.json",
              "payload": {"label": "MODEL_ONLY", "value": 42}}
    inputs.update(over.pop("inputs", {}))
    return g.run(tool_id="stage10.emit_artifact", inputs=inputs, **over)


def _primary(g) -> dict:
    """The live projections, flattened to the fields both readers claim.

    Written HERE rather than in reconstruct.py because that module may not
    import these layers -- which is exactly what keeps the two readers
    independent. It also means this extraction could share a mistake with
    the projections, which is why an empty diff is evidence and not proof.
    """
    return {
        "jobs": {j.job_id: {"state": j.state.value, "attempts": j.attempts,
                            "lease_holder": j.lease_holder or ""}
                 for j in g.scheduler.all_jobs().values()},
        "agents": {i.instance_id: {"kind": i.kind.value,
                                   "roles": tuple(sorted(
                                       r.value for r in i.roles))}
                   for i in g.agents.instances()},
        "memory": {e.memory_id: {"author": e.author, "status": e.status.value}
                   for e in g.memory.all_entries()},
        "capabilities": {c: {"revoked_seq": None}
                         for c in g.capabilities.issued_ids()
                         if c not in g.capabilities.revoked_ids()},
    }


# --- an honest history: both readers must agree ----------------------------

def test_both_readers_agree_on_a_governed_run(gov):
    run = _run(gov, idempotency_key="k")
    assert run.state is TaskState.VERIFIED
    recon = reconstruct_subsystems(gov.log)
    assert recon.anomalies == [], recon.anomalies
    assert compare_subsystems(_primary(gov), recon) == ()


def test_the_second_reader_reaches_every_subsystem(gov):
    """A reader that silently covers nothing agrees with everything."""
    _run(gov, idempotency_key="k")
    recon = reconstruct_subsystems(gov.log)
    assert recon.jobs, "no scheduler state"
    assert recon.capabilities, "no capabilities"
    assert recon.agents, "no identities"
    assert recon.memory, "no memory entries"
    assert recon.policies, "no policy versions"
    assert recon.decisions, "no recorded decisions"
    assert recon.contexts, "no context manifests"


def test_the_second_reader_agrees_across_several_runs(gov):
    for i in range(3):
        _run(gov, inputs={"name": f"a{i}.json"})
    recon = reconstruct_subsystems(gov.log)
    assert recon.anomalies == []
    assert len(recon.jobs) == 3
    assert compare_subsystems(_primary(gov), recon) == ()


# --- forged histories: the second reader must NOT agree --------------------

def _forge(gov, action, payload, actor="mallory", target="x"):
    gov.log.append(actor=actor, action=action, target=target, payload=payload)
    return reconstruct_subsystems(gov.log)


def test_a_job_born_succeeded_is_refused_by_the_second_reader(gov):
    """An enqueue introduces WORK, never an outcome."""
    recon = _forge(gov, "scheduler.enqueue", {"job": {
        "job_id": "j-forged", "state": "SUCCEEDED", "submitter": "mallory",
        "work_digest": "a" * 64, "priority": 0, "attempts": 0}})
    assert any("enqueued directly in 'SUCCEEDED'" in a
               for a in recon.anomalies), recon.anomalies
    assert "j-forged" not in recon.jobs


def test_a_job_born_dispatched_with_its_own_lease_is_refused(gov):
    """A guard that can be handed its own precondition is not a guard."""
    recon = _forge(gov, "scheduler.enqueue", {"job": {
        "job_id": "j-lease", "state": "DISPATCHED", "submitter": "mallory",
        "work_digest": "a" * 64, "lease_holder": "mallory",
        "lease_id": "L", "attempts": 0}})
    assert any("enqueued directly in 'DISPATCHED'" in a
               for a in recon.anomalies)
    assert "j-lease" not in recon.jobs


def test_a_lease_at_enqueue_is_refused_even_from_a_valid_initial_state(gov):
    """ISOLATED from the born-in-a-bad-state check, which masked it.

    The test above forges DISPATCHED, and the state check refuses that
    before the lease check is ever reached -- so a mutation deleting the
    lease check survived. Enqueuing in WAITING, which is legitimate, leaves
    the lease as the only thing that can refuse it.
    """
    recon = _forge(gov, "scheduler.enqueue", {"job": {
        "job_id": "j-quiet", "state": "WAITING", "submitter": "mallory",
        "work_digest": "a" * 64, "lease_holder": "mallory",
        "lease_id": "L", "attempts": 0}})
    assert any("already holding a lease" in a for a in recon.anomalies), \
        recon.anomalies
    assert "j-quiet" not in recon.jobs


def test_spent_attempts_at_enqueue_are_refused_from_a_valid_state(gov):
    """The same isolation for the retry budget.

    A job enqueued with attempts already spent arrives closer to
    exhaustion than anything authorised -- or, with a negative count,
    further from it.
    """
    recon = _forge(gov, "scheduler.enqueue", {"job": {
        "job_id": "j-spent", "state": "WAITING", "submitter": "mallory",
        "work_digest": "a" * 64, "attempts": 3}})
    assert any("attempts already spent" in a for a in recon.anomalies)
    assert "j-spent" not in recon.jobs


def test_a_job_naming_a_submitter_it_is_not_is_refused(gov):
    recon = _forge(gov, "scheduler.enqueue", {"job": {
        "job_id": "j-sub", "state": "WAITING", "submitter": "honest-agent",
        "work_digest": "a" * 64, "attempts": 0}})
    assert any("names submitter 'honest-agent'" in a for a in recon.anomalies)


def test_a_capability_that_predates_its_own_record_is_refused(gov):
    recon = _forge(gov, "capability.issue", {
        "capability_id": "cap-back", "subject": "mallory",
        "action": "EXECUTE_TOOL", "task_id": "t", "tool_id": "x",
        "scope": [], "issued_seq": 0, "expires_after_seq": 9999})
    assert any("would predate its own record" in a for a in recon.anomalies)
    assert "cap-back" not in recon.capabilities


def test_a_reissued_capability_id_with_different_terms_is_refused(gov):
    _run(gov)
    live = gov.capabilities.issued_ids()[0]
    recon = _forge(gov, "capability.issue", {
        "capability_id": live, "subject": "mallory", "action": "EXECUTE_TOOL",
        "task_id": "t", "tool_id": "x", "scope": ["/"],
        "issued_seq": gov.log.verify().head_seq + 1,
        "expires_after_seq": 9999})
    assert any("issued twice with different terms" in a
               for a in recon.anomalies)


def test_a_non_human_minting_a_human_is_refused_by_the_second_reader(gov):
    """One step from answering its own escalation, which is both halves of
    the human gate at once."""
    recon = _forge(gov, "agent.register", {"identity": {
        "instance_id": "fake-human", "agent_id": "fake",
        "kind": "HUMAN", "roles": ["REVIEWER"]}},
        actor="stage10-worker")
    assert any("is not HUMAN and registers" in a for a in recon.anomalies)
    assert "fake-human" not in recon.agents


def test_a_retracted_memory_cannot_be_un_retracted(gov):
    run = _run(gov)
    mid = run.memory_id
    gov.memory.retract(mid, actor="stage10-verifier", reason="withdrawn")
    recon = _forge(gov, "memory.status",
                   {"memory_id": mid, "status": "ACTIVE"},
                   actor="stage10-verifier")
    assert any("un-retracted" in a for a in recon.anomalies), recon.anomalies
    assert recon.memory[mid]["status"] == "RETRACTED"


def test_a_memory_entry_naming_an_author_it_is_not_is_refused(gov):
    recon = _forge(gov, "memory.write", {"entry": {
        "memory_id": "m-forged", "author": "stage10-verifier",
        "text": "x", "status": "ACTIVE"}})
    assert any("names author 'stage10-verifier'" in a
               for a in recon.anomalies)


def test_a_malformed_memory_write_is_refused_not_projected(gov):
    """The raw KeyError that once made a whole store unloadable."""
    recon = _forge(gov, "memory.write", {"not_an_entry": True})
    assert any("carries no entry" in a for a in recon.anomalies)


def test_a_rebound_network_grant_is_refused(gov):
    """A live grant replaced by one nobody reviewed."""
    body = {"grant_id": "g1", "host": "api.example.com", "port": 443}
    _forge(gov, "network.grant",
           {"grant": body, "grant_digest": digest(body), "grant_id": "g1"},
           actor="stage10-worker")
    other = {"grant_id": "g1", "host": "collector.evil.test", "port": 443}
    recon = _forge(gov, "network.grant",
                   {"grant": other, "grant_digest": digest(other),
                    "grant_id": "g1"}, actor="stage10-worker")
    assert any("re-issued with different terms" in a
               for a in recon.anomalies)
    assert recon.net_grants["g1"]["body"]["host"] == "api.example.com"


def test_a_forged_job_transition_from_the_wrong_state_is_refused(gov):
    _run(gov)
    (jid,) = list(reconstruct_subsystems(gov.log).jobs)
    recon = _forge(gov, "scheduler.transition",
                   {"job_id": jid, "src": "READY", "dst": "SUCCEEDED"})
    assert any("claims src 'READY'" in a for a in recon.anomalies)


def test_a_terminal_job_cannot_be_revived(gov):
    _run(gov)
    (jid,) = list(reconstruct_subsystems(gov.log).jobs)
    recon = _forge(gov, "scheduler.transition",
                   {"job_id": jid, "src": "SUCCEEDED", "dst": "READY"})
    assert any("leaves terminal state" in a for a in recon.anomalies)


def test_a_policy_downgrade_is_reported(gov):
    _run(gov)
    pid = list(reconstruct_subsystems(gov.log).policies)[0]
    recon = _forge(gov, "policy.publish", {
        "document": {"policy_id": pid, "version": 0, "rules": []},
        "policy_digest": "d" * 64})
    assert any("publishes version 0 after" in a for a in recon.anomalies)


def test_the_second_reader_never_raises_on_a_hostile_history(gov):
    """Findings, not an exception that hides the rest of the log."""
    for payload in ({}, {"job": None}, {"job": {"job_id": None}},
                    {"identity": "not a dict"}, {"entry": []},
                    {"grant": 5}, {"document": None}, {"decision": "no"}):
        for action in ("scheduler.enqueue", "agent.register", "memory.write",
                       "network.grant", "policy.publish", "policy.decision"):
            gov.log.append(actor="mallory", action=action, target="x",
                           payload=payload)
    recon = reconstruct_subsystems(gov.log)
    assert recon.anomalies, "a hostile history produced no findings at all"
    assert recon.events_replayed > 0


def test_a_divergence_between_the_readers_is_reported(gov):
    """The comparison must be able to FAIL, or it proves nothing."""
    _run(gov)
    recon = reconstruct_subsystems(gov.log)
    primary = _primary(gov)
    (jid,) = list(primary["jobs"])
    primary["jobs"][jid]["state"] = "CANCELLED"
    div = compare_subsystems(primary, recon)
    assert div and any(d.field_name == "state" for d in div)


def test_an_object_only_one_reader_holds_is_a_divergence(gov):
    """PRESENCE, not just field values.

    A forged record that reaches one projection and not the other produces
    no field disagreement at all -- there is no shared object to compare.
    Only presence catches it, and a mutation deleting that check survived
    because every other test here mutates a field of something both
    readers hold.
    """
    _run(gov)
    recon = reconstruct_subsystems(gov.log)

    # In the projection, absent from the second reader.
    primary = _primary(gov)
    primary["jobs"]["j-ghost"] = {"state": "SUCCEEDED", "attempts": 0,
                                  "lease_holder": ""}
    div = compare_subsystems(primary, recon)
    assert any(d.record_id == "jobs/j-ghost" and d.field_name == "presence"
               for d in div), div

    # And the other direction: held by the second reader, absent from the
    # projection. Both matter -- one is a record the projection invented,
    # the other is one it dropped.
    primary = _primary(gov)
    (real,) = [k for k in primary["jobs"]]
    del primary["jobs"][real]
    div = compare_subsystems(primary, recon)
    assert any(d.record_id == f"jobs/{real}" and d.field_name == "presence"
               for d in div), div
