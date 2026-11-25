"""The scheduler's replay holds what its writer holds (AUD-068 to AUD-071).

Each rule here was the writer's alone. ``enqueue`` refused a resource need
that is not a whole, non-negative number and a citation that is not a
digest; ``dispatch`` and ``renew_lease`` bounded how many times a lease is
extended and nothing about how far one reaches; ``set_priority`` refused a
priority that is not an int. A record the writer refuses, appended to the
log directly, became state on replay, and the second reader read it clean.

Each test appends one such record and asks both readers about it, beside a
control showing that the genuine form still replays in both.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import reconstruct as rc  # noqa: E402
from agent_substrate.canonical import digest  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.policy import PolicyStore  # noqa: E402
from agent_substrate.scheduler import (  # noqa: E402
    MAX_DEPENDENCIES, MAX_LEASE_SEQS, MAX_PRIORITY, Job, JobState,
    JobTransitionError, Scheduler, SchedulerError, default_policy,
)


def _fresh(log: EventLog, capacity: int = 2) -> Scheduler:
    return Scheduler(log, policy=PolicyStore(log).load(),
                     policy_id="scheduler.default",
                     capacity={"slots": capacity})


def _world(tmp_path, capacity: int = 2):
    log = EventLog(tmp_path / "log.jsonl")
    PolicyStore(log).load().publish(default_policy(), actor="owner")
    return log, _fresh(log, capacity).load()


def _enqueue_record(log: EventLog, name: str, actor: str = "mallory",
                    **fields) -> None:
    """An enqueue appended directly, as no writer here would write it.
    ``fields`` overrides the record, its job_id included."""
    rec = Job(job_id=name, work_digest=digest({"w": name}),
              submitter=actor).to_record()
    rec.update(fields)
    log.append(actor=actor, action="scheduler.enqueue", target=name,
               payload={"job": rec})


def _second(log: EventLog):
    return rc.reconstruct_subsystems(EventLog(log.path))


def _ready(s: Scheduler, job_id: str) -> None:
    s.enqueue(job_id=job_id, work_digest=digest({"w": job_id}),
              submitter="owner")
    s.reconcile()
    assert s.get(job_id).state is JobState.READY


# ---- AUD-068: what enqueue() refuses -------------------------------------
@pytest.mark.parametrize("fields, words", [
    ({"resources": {"slots": "many"}}, "needs 'many' of 'slots'"),
    ({"resources": {"slots": -5}}, "needs -5 of 'slots'"),
    ({"resources": {"slots": True}}, "needs True of 'slots'"),
    ({"resources": {"": 1}}, "needs resource ''"),
    ({"requires_evidence": [7]}, "cites 7"),
    ({"requires_evidence": ["not-a-digest"]}, "cites 'not-a-digest'"),
])
def test_an_enqueue_its_writer_refuses_is_refused_on_replay(tmp_path, fields,
                                                            words):
    log, s = _world(tmp_path)
    with pytest.raises(SchedulerError):
        s.enqueue(job_id="w", work_digest=digest({"w": "w"}),
                  submitter="mallory",
                  resources=fields.get("resources"),
                  requires_evidence=tuple(fields.get("requires_evidence",
                                                     ())))
    _enqueue_record(log, "forged", **fields)
    with pytest.raises(SchedulerError, match=re.escape(words)):
        _fresh(log).load()
    sub = _second(log)
    assert "forged" not in sub.jobs
    assert [a for a in sub.anomalies if "'forged'" in a], sub.anomalies


def test_a_negative_need_no_longer_hands_out_more_than_the_capacity(tmp_path):
    """The defect's consequence, measured before the fix: capacity 2, and a
    forged job needing -5 slots let five jobs be dispatched at once."""
    log, s = _world(tmp_path, capacity=2)
    _enqueue_record(log, "neg", resources={"slots": -5})
    with pytest.raises(SchedulerError, match="needs -5 of 'slots'"):
        _fresh(log).load().dispatch(job_id="neg", worker="w0",
                                    lease_id="L0", lease_seqs=50)


def test_a_genuine_need_and_citation_replay_on_a_smaller_executor(tmp_path):
    """Control. Capacity is the replaying executor's, not the history's, so
    a job sized for a bigger machine still loads on a smaller one."""
    log, s = _world(tmp_path, capacity=4)
    s.enqueue(job_id="big", work_digest=digest({"w": 1}), submitter="owner",
              resources={"slots": 3}, requires_evidence=(digest({"e": 1}),))
    small = _fresh(log, capacity=1).load()
    assert small.get("big").resources == {"slots": 3}
    sub = _second(log)
    assert sub.jobs["big"]["state"] == "WAITING" and not sub.anomalies


# ---- AUD-068: an enqueue's lease and what it waits on, in both readers ----
def _waiting_and_cancelled(s: Scheduler) -> None:
    s.enqueue(job_id="live", work_digest=digest({"w": "live"}),
              submitter="owner")
    s.enqueue(job_id="gone", work_digest=digest({"w": "gone"}),
              submitter="owner")
    s.cancel(job_id="gone", actor="owner", reason="not wanted")


@pytest.mark.parametrize("fields, primary, second", [
    ({"lease_expires_after_seq": 7}, "is enqueued already leased",
     "is enqueued with a lease running to 7"),
    ({"lease_expires_after_seq": "x"}, "is enqueued with lease end 'x'",
     "is enqueued with a lease running to 'x'"),
    ({"depends_on": ["nope"]}, "dependency 'nope' does not exist",
     "depends on 'nope', which no enqueue before it introduced"),
    ({"depends_on": ["gone"]}, "dependency 'gone' is CANCELLED",
     "depends on 'gone', which is CANCELLED and can never succeed"),
    ({"depends_on": {"live": True}}, "depends_on is dict",
     "depends on {'live': True}; dependencies are a list"),
    ({"depends_on": "live"}, "depends_on is str",
     "depends on 'live'; dependencies are a list"),
    ({"resources": [["slots", 1]]}, "resources is list",
     "declares the need [['slots', 1]]"),
    ({"requires_evidence": {digest({"e": 1}): 1}},
     "requires_evidence is dict", "cites {"),
])
def test_an_enqueue_the_primary_refuses_is_refused_by_both_readers(
        tmp_path, fields, primary, second):
    """Each was read clean by one reader and refused by the other, or
    raised out of the primary's fold: a lease end that is not a whole
    number raised TypeError, a dependency list written as a string named
    each of its letters, and a map's keys or a list of pairs were taken
    for the list or the map the writer writes."""
    log, s = _world(tmp_path)
    _waiting_and_cancelled(s)
    _enqueue_record(log, "forged", **fields)
    with pytest.raises(SchedulerError, match=re.escape(primary)):
        _fresh(log).load()
    sub = _second(log)
    assert "forged" not in sub.jobs
    assert [a for a in sub.anomalies if "'forged'" in a and second in a], \
        sub.anomalies


def test_both_readers_bound_how_many_jobs_one_waits_on(tmp_path):
    log, s = _world(tmp_path)
    names = [f"d{i}" for i in range(MAX_DEPENDENCIES + 1)]
    for name in names:
        s.enqueue(job_id=name, work_digest=digest({"w": name}),
                  submitter="owner")
    _enqueue_record(log, "forged", depends_on=names)
    with pytest.raises(SchedulerError,
                       match=f"{MAX_DEPENDENCIES + 1} dependencies exceeds"):
        _fresh(log).load()
    sub = _second(log)
    assert "forged" not in sub.jobs
    assert [a for a in sub.anomalies if "'forged' depends on" in a]


def test_an_enqueue_waiting_on_a_genuine_job_replays_in_both_readers(
        tmp_path):
    """Control: a dependency the writer accepts, on a job still able to
    succeed, written as the writer writes it."""
    log, s = _world(tmp_path)
    _waiting_and_cancelled(s)
    s.enqueue(job_id="after", work_digest=digest({"w": "after"}),
              submitter="owner", depends_on=("live",))
    assert _fresh(log).load().get("after").depends_on == ("live",)
    sub = _second(log)
    assert sub.jobs["after"]["state"] == "WAITING" and not sub.anomalies


# ---- AUD-071: a job's ids are strs ---------------------------------------
@pytest.mark.parametrize("fields, words", [
    ({"job_id": ["q"]}, "names job_id ['q']"),
    ({"depends_on": [[1]]}, "names dependency [1]"),
    ({"depends_on": [""]}, "names dependency ''"),
    ({"idempotency_key": [1]}, "idempotency_key [1]"),
    ({"requires_capability": 5}, "requires_capability 5"),
    ({"task_id": {"t": 1}}, "task_id {'t': 1}"),
])
def test_an_enqueue_naming_something_by_a_non_id_is_refused(tmp_path, fields,
                                                            words):
    log, _ = _world(tmp_path)
    _enqueue_record(log, "forged", **fields)
    with pytest.raises(SchedulerError, match=re.escape(words)):
        _fresh(log).load()


def test_the_writer_refuses_an_idempotency_key_that_is_not_a_str(tmp_path):
    _, s = _world(tmp_path)
    with pytest.raises(SchedulerError, match="idempotency_key must be a str"):
        s.enqueue(job_id="k", work_digest=digest({"w": "k"}),
                  submitter="owner", idempotency_key=["k"])


#: One malformed record each. Every one raised KeyError, ValueError or
#: TypeError out of load() before AUD-071, or was taken by conversion.
MALFORMED = {
    "an enqueue without a job": ("scheduler.enqueue", {}),
    "a transition naming no job": (
        "scheduler.transition", {"src": "WAITING", "dst": "READY"}),
    "a transition for a job never enqueued": (
        "scheduler.transition",
        {"job_id": "nope", "src": "WAITING", "dst": "READY"}),
    "a transition naming its job by a list": (
        "scheduler.transition",
        {"job_id": ["q"], "src": "WAITING", "dst": "READY"}),
    "a transition with no source": (
        "scheduler.transition", {"job_id": "q", "dst": "READY"}),
    "a transition from no state": (
        "scheduler.transition",
        {"job_id": "q", "src": "NOPE", "dst": "READY"}),
    "a transition to a list": (
        "scheduler.transition",
        {"job_id": "q", "src": "WAITING", "dst": ["READY"]}),
    "attempts that are not a number": (
        "scheduler.transition",
        {"job_id": "q", "src": "WAITING", "dst": "READY", "attempts": "x"}),
    "a backoff that is not a number": (
        "scheduler.transition",
        {"job_id": "q", "src": "WAITING", "dst": "READY",
         "backoff_until_seq": "x"}),
    "a lease end written as a string": (
        "scheduler.transition",
        {"job_id": "q", "src": "WAITING", "dst": "READY",
         "lease_expires_after_seq": "7"}),
    "a renewal count that is a bool": (
        "scheduler.transition",
        {"job_id": "q", "src": "WAITING", "dst": "READY",
         "lease_renewals": True}),
    "a priority for a job never enqueued": (
        "scheduler.priority", {"job_id": "nope", "priority": 1,
                               "reason": "r"}),
    "a renewal naming its job by a list": (
        "scheduler.lease_renew",
        {"job_id": ["q"], "lease_id": "L", "lease_seqs": 5}),
}


@pytest.mark.parametrize("label", sorted(MALFORMED))
def test_a_malformed_record_is_refused_in_the_reducers_words(tmp_path,
                                                             label):
    action, payload = MALFORMED[label]
    log, s = _world(tmp_path)
    s.enqueue(job_id="q", work_digest=digest({"w": "q"}), submitter="owner")
    log.append(actor="owner", action=action, target="q", payload=payload)
    with pytest.raises(SchedulerError, match=r"^seq \d+: "):
        _fresh(log).load()


# ---- AUD-069: how far one grant of possession reaches ---------------------
def test_dispatch_hands_out_no_lease_past_the_bound(tmp_path):
    log, s = _world(tmp_path)
    _ready(s, "j")
    head = s.at_seq()
    with pytest.raises(SchedulerError, match="bound on one grant"):
        s.dispatch(job_id="j", worker="w", lease_id="L",
                   lease_seqs=MAX_LEASE_SEQS + 1)
    assert s.at_seq() == head            # nothing was written
    job = s.dispatch(job_id="j", worker="w", lease_id="L",
                     lease_seqs=MAX_LEASE_SEQS)
    assert job.lease_expires_after_seq - job.updated_seq == MAX_LEASE_SEQS
    assert _fresh(log).load().get("j").state is JobState.DISPATCHED
    assert not _second(log).anomalies


def test_a_renewal_reaches_no_farther_than_the_bound(tmp_path):
    log, s = _world(tmp_path)
    _ready(s, "j")
    s.dispatch(job_id="j", worker="w", lease_id="L", lease_seqs=5)
    head = s.at_seq()
    with pytest.raises(SchedulerError, match="bound on one grant"):
        s.renew_lease(job_id="j", worker="w", lease_id="L",
                      lease_seqs=MAX_LEASE_SEQS + 1)
    assert s.at_seq() == head            # refused before it was durable
    end = s.get("j").lease_expires_after_seq
    log.append(actor="w", action="scheduler.lease_renew", target="j",
               payload={"job_id": "j", "lease_id": "L",
                        "lease_seqs": MAX_LEASE_SEQS + 1})
    with pytest.raises(SchedulerError, match="bound on one grant"):
        _fresh(log).load()
    sub = _second(log)
    assert sub.jobs["j"]["lease_expires_after_seq"] == end
    assert [a for a in sub.anomalies if "'j'" in a], sub.anomalies


def test_a_renewal_at_the_bound_replays_in_both_readers(tmp_path):
    log, s = _world(tmp_path)
    _ready(s, "j")
    s.dispatch(job_id="j", worker="w", lease_id="L", lease_seqs=5)
    job = s.renew_lease(job_id="j", worker="w", lease_id="L",
                        lease_seqs=MAX_LEASE_SEQS)
    assert job.lease_expires_after_seq == job.updated_seq + MAX_LEASE_SEQS
    assert _fresh(log).load().get("j").lease_renewals == 1
    sub = _second(log)
    assert sub.jobs["j"]["lease_expires_after_seq"] == \
        job.lease_expires_after_seq and not sub.anomalies


def _dispatch_record(log: EventLog, *, end) -> None:
    log.append(actor="mallory", action="scheduler.transition", target="d",
               payload={"job_id": "d", "src": "READY", "dst": "DISPATCHED",
                        "reason": "mine", "lease_id": "Lm",
                        "lease_holder": "mallory", "task_id": "t",
                        "lease_expires_after_seq": end,
                        "lease_renewals": 0, "attempts": 1})


def test_a_dispatch_record_naming_its_own_end_is_refused(tmp_path):
    """Measured before the fix: the record below held job d to seq 2**62,
    and reconcile never reclaimed it; the second reader noted nothing."""
    log, s = _world(tmp_path)
    _ready(s, "d")
    _dispatch_record(log, end=2 ** 62)
    with pytest.raises(JobTransitionError, match="past the 1000000"):
        _fresh(log).load()
    sub = _second(log)
    assert sub.jobs["d"]["state"] == "READY"
    assert [a for a in sub.anomalies if "'d'" in a], sub.anomalies


def test_a_dispatch_record_with_an_end_the_writer_could_write_replays(
        tmp_path):
    """Control: the same record, with the end its position allows."""
    log, s = _world(tmp_path)
    _ready(s, "d")
    _dispatch_record(log, end=s.at_seq() + 1 + 50)
    assert _fresh(log).load().get("d").lease_holder == "mallory"
    sub = _second(log)
    assert sub.jobs["d"]["state"] == "DISPATCHED" and not sub.anomalies


# ---- AUD-070: a priority as set_priority writes it ------------------------
def _priority_record(log: EventLog, value) -> None:
    log.append(actor="sched", action="scheduler.priority", target="p",
               payload={"job_id": "p", "priority": value,
                        "role": "SCHEDULER", "reason": "urgent"})


@pytest.mark.parametrize("bad", ["0", 0.7, True, None, "abc"])
def test_a_priority_the_writer_refuses_is_refused_on_replay(tmp_path, bad):
    log, s = _world(tmp_path)
    s.enqueue(job_id="p", work_digest=digest({"w": "p"}), submitter="owner")
    with pytest.raises(SchedulerError, match="priority must be an int"):
        s.set_priority(job_id="p", priority=bad, actor="sched",
                       role="SCHEDULER", reason="urgent")
    _priority_record(log, bad)
    with pytest.raises(SchedulerError, match="priority must be an int"):
        _fresh(log).load()


@pytest.mark.parametrize("bad", [99, -1, "0", 0.7, True])
def test_the_second_reader_refuses_a_priority_the_primary_refuses(tmp_path,
                                                                  bad):
    log, s = _world(tmp_path)
    s.enqueue(job_id="p", work_digest=digest({"w": "p"}), submitter="owner")
    _priority_record(log, bad)
    with pytest.raises(SchedulerError):
        _fresh(log).load()
    sub = _second(log)
    assert sub.jobs["p"]["priority"] == MAX_PRIORITY
    assert [a for a in sub.anomalies if "'p'" in a], sub.anomalies


@pytest.mark.parametrize("bad", [99, "0"])
def test_the_second_reader_refuses_an_enqueue_at_a_priority_out_of_range(
        tmp_path, bad):
    log, _ = _world(tmp_path)
    _enqueue_record(log, "forged", priority=bad)
    with pytest.raises(SchedulerError):
        _fresh(log).load()
    sub = _second(log)
    assert "forged" not in sub.jobs and sub.anomalies


def test_a_raise_the_policy_allows_replays_in_both_readers(tmp_path):
    """Control: the scheduler raising a job, with a reason, as it may."""
    log, s = _world(tmp_path)
    s.enqueue(job_id="p", work_digest=digest({"w": "p"}), submitter="owner")
    s.set_priority(job_id="p", priority=0, actor="sched", role="SCHEDULER",
                   reason="urgent")
    assert _fresh(log).load().get("p").priority == 0
    sub = _second(log)
    assert sub.jobs["p"]["priority"] == 0 and not sub.anomalies
