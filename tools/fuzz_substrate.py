#!/usr/bin/env python3
"""Fuzz every parser and trust boundary in the agent substrate.

WHAT THIS IS FOR

Every module in ``qta_agent`` refuses malformed input somewhere, and each of
those refusals was written against the malformed inputs its author thought
of. Fuzzing is the part that supplies the ones nobody thought of: truncated
records, type confusion, absurd sizes, structures that are valid JSON and
invalid everything else.

WHAT COUNTS AS A FINDING

Not "it raised". Refusing is the correct behaviour and every target declares
which exceptions ARE the refusal. A finding is:

  ACCEPTED   malformed input was accepted as valid -- the worst outcome, and
             the one that turns a parser into an authority hole;
  CRASHED    an exception outside the declared set, which means the refusal
             happened by accident rather than by design, in a place nobody
             chose;
  HUNG       the target did not return inside its bound. Counted SEPARATELY
             and never as a pass, for the same reason the mutation harness
             treats a timeout as a defect rather than a kill: a hang is not a
             test result.

DETERMINISM

The campaign is seeded and the seed is printed. A finding is reproducible
from ``--seed``, and every finding is also written out as a standalone corpus
file so the regression test does not depend on the fuzzer running again.

BOUNDS

Each case runs under a wall-clock alarm and a size cap. An unbounded fuzzer
finds a memory exhaustion in itself before it finds one in the target.

USAGE

    python3 tools/fuzz_substrate.py --cases 500
    python3 tools/fuzz_substrate.py --seed 1234 --target events
    python3 tools/fuzz_substrate.py --corpus tests/fuzz_corpus   # replay only
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import random
import signal
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Per-case wall-clock bound. A target that exceeds it is a HUNG finding.
CASE_TIMEOUT_S = 5

#: Inputs above this are not generated. The bound is the point: a fuzzer that
#: allocates a gigabyte has found a defect in itself.
MAX_INPUT_BYTES = 256 * 1024

ACCEPTED = "ACCEPTED"
CRASHED = "CRASHED"
HUNG = "HUNG"


class Hung(Exception):
    """The target did not return inside its bound."""


class _Deadline:
    """SIGALRM bound around one case. Not a substitute for the target's own."""

    def __init__(self, seconds: int):
        self.seconds = seconds

    def __enter__(self):
        def fire(signum, frame):
            raise Hung(f"target did not return within {self.seconds}s")
        self.previous = signal.signal(signal.SIGALRM, fire)
        signal.alarm(self.seconds)
        return self

    def __exit__(self, *exc):
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self.previous)
        return False


# ---- targets -------------------------------------------------------------
def _log_reader(data: bytes):
    from qta_agent.events import EventLog
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "log.jsonl"
        path.write_bytes(data)
        return EventLog(path).read(strict=True)


def _head_reader(data: bytes):
    from qta_agent.events import EventLog
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "log.jsonl"
        path.write_bytes(b"")
        (Path(tmp) / "log.jsonl.head").write_bytes(data)
        return EventLog(path).head()


def _checkpoint_reader(data: bytes):
    from qta_agent.checkpoint import CheckpointStore
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "checkpoints"
        d.mkdir()
        (d / "000000.json").write_bytes(data)
        store = CheckpointStore(d)
        return [store.read(seq) for seq in store.seqs()]


def _evidence_name(data: bytes):
    from qta_agent.evidence import EvidenceStore
    with tempfile.TemporaryDirectory() as tmp:
        return EvidenceStore(Path(tmp)).contains(
            data.decode("utf-8", "surrogateescape"))



def _read_path(data: bytes):
    """Fuzz the governed read boundary's path parser.

    A path is the most attacker-shaped input this package takes: it arrives
    as a string, it is compared against an authority, and it is then handed
    to the kernel. Anything it accepts, it accepts on behalf of a reader.
    """
    from qta_agent.safeio import split_relative

    return split_relative(data.decode("utf-8", "surrogateescape"))


def _read_beneath(data: bytes):
    """Fuzz an actual confined read against a real, tiny root.

    The parser above says which paths are expressible. This says what
    happens when one reaches the filesystem: whatever the bytes are, the
    result must be a refusal or a bounded read, never a hang and never
    content from outside the root.
    """
    import os
    import tempfile

    from qta_agent.safeio import ReadRoot

    rel = data.decode("utf-8", "surrogateescape")
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "f.txt"), "wb") as fh:
            fh.write(b"in-root")
        with ReadRoot(d, max_bytes=4096) as rr:
            return rr.read(rel)


#: How many operations one fuzzed sequence may perform. Bounded so a case
#: stays inside CASE_TIMEOUT_S with room to spare, and so a finding is a
#: sequence a person can read.
MAX_SEQUENCE_OPS = 48

#: Job and record identifiers the sequence may name. Small on purpose:
#: interesting interleavings need operations to COLLIDE, and a fuzzer that
#: invents a fresh id every time never makes two operations meet.
SEQUENCE_ARITY = 4


def replay_invariants(path) -> int:
    """Rebuild everything from the log and check what must be true.

    Extracted from the sequence target so it can be TESTED. An invariant
    inlined in a fuzz target is only ever exercised by inputs the fuzzer
    happens to generate, so nothing establishes that it can fire at all --
    and an invariant that cannot fire is indistinguishable from one that
    holds.
    """
    from qta_agent.events import EventLog
    from qta_agent.policy import PolicyStore
    from qta_agent.reconstruct import compare, reconstruct
    from qta_agent.scheduler import JobState, Scheduler
    from qta_agent.store import AuthorityStore

    report = EventLog(path).verify()
    if not report.ok:
        raise AssertionError(
            "a sequence of legal-and-refused operations left an "
            f"unverifiable log: {report.problems[:3]}")
    fresh_log = EventLog(path)
    rebuilt = Scheduler(fresh_log, policy=PolicyStore(fresh_log).load(),
                        policy_id="scheduler.default",
                        capacity={"slots": 4}).load()
    rebuilt_store = AuthorityStore(fresh_log).load()
    differences = compare(rebuilt_store, reconstruct(fresh_log))
    if differences:
        raise AssertionError(
            "primary and independent reconstruction disagree after a "
            f"fuzzed sequence: {differences[:3]}")
    for job in rebuilt.all_jobs().values():
        if job.state is JobState.DISPATCHED and not job.lease_holder:
            raise AssertionError(
                f"{job.job_id} is DISPATCHED with no lease holder")
        if job.attempts > job.max_attempts:
            raise AssertionError(
                f"{job.job_id} was attempted {job.attempts} times against a "
                f"budget of {job.max_attempts}")
    return len(rebuilt.all_jobs())


def _scheduler_sequence(data: bytes):
    """Drive the real state machines through a fuzzed SEQUENCE of operations.

    WHAT THIS TARGETS THAT THE OTHERS DO NOT

    Every target above fuzzes ONE RECORD: bytes in, a parse or a refusal
    out. That finds a parser that trusts its input, and it cannot find
    anything about ORDER -- and order is where the interesting defects in
    this package have actually been. Dispatch before ready. Report from a
    worker whose lease lapsed. Renew after reconcile requeued the job.
    Cancel between the two halves of a promotion. None of those is a
    malformed record; each is a well-formed record in the wrong place.

    THE INVARIANT, WHICH IS THE WHOLE POINT

    Every operation may legitimately be refused, and refusals are not
    findings -- a fuzzed sequence is mostly illegal moves. What may NEVER
    happen is that the sequence leaves a log the system cannot rebuild
    itself from. So the operations run inside a refusal-tolerant loop and
    the REPLAY runs outside it: a projection is loaded from scratch, an
    independent reconstruction is compared against it, and anything either
    of them raises reaches run_case as an undeclared exception.

    That invariant is not hypothetical. A perfectly valid chain that no
    reducer could replay is the exact defect four concurrent processes
    produced, and the exact one a badly ordered single-process sequence can
    produce too.
    """
    import tempfile

    from qta_agent.authority import Role, State, TransitionError
    from qta_agent.canonical import digest
    from qta_agent.events import EventLog
    from qta_agent.policy import PolicyError, PolicyStore
    from qta_agent.scheduler import (
        FailureClass, Scheduler, SchedulerError, default_policy,
    )
    from qta_agent.store import AuthorityStore, StoreError

    if not data:
        return None
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "log.jsonl"
        log = EventLog(path)
        pol = PolicyStore(log).load()
        pol.publish(default_policy(), actor="owner")
        sched = Scheduler(log, policy=pol, policy_id="scheduler.default",
                          capacity={"slots": 4}).load()
        store = AuthorityStore(log).load()

        # Every refusal the state machines are ENTITLED to make. Written
        # out rather than caught as Exception: a bare except here would
        # swallow the AssertionError the invariant below raises, and the
        # target would report success over a log nobody could replay --
        # which is the vacuous-verifier defect, inside the fuzzer.
        refusals = (SchedulerError, StoreError, PolicyError, TransitionError,
                    ValueError, KeyError, TypeError)
        ops = data[:MAX_SEQUENCE_OPS]
        for i, byte in enumerate(ops):
            which = byte % 10
            n = (byte // 10) % SEQUENCE_ARITY
            jid = f"j{n}"
            rid = f"r{n}"
            before = None
            if jid in sched.all_jobs():
                before = sched.get(jid).state.value
            outcome = "ok"
            try:
                if which == 0:
                    sched.enqueue(job_id=jid, work_digest=digest({"j": n}),
                                  submitter="p1")
                elif which == 1:
                    sched.reconcile()
                elif which == 2:
                    sched.dispatch(job_id=jid, worker=f"w{i % 2}",
                                   lease_id=f"L{i}", lease_seqs=1 + (i % 5))
                elif which == 3:
                    sched.report(job_id=jid, worker=f"w{i % 2}")
                elif which == 4:
                    sched.report(job_id=jid, worker=f"w{i % 2}",
                                 failure=FailureClass.TRANSIENT)
                elif which == 5:
                    sched.renew_lease(job_id=jid, worker=f"w{i % 2}",
                                      lease_id=f"L{i - 1}", lease_seqs=8)
                elif which == 6:
                    sched.cancel(job_id=jid, actor="p1", reason="fuzz")
                elif which == 7:
                    store.create(record_id=rid, kind="claim", proposer="p1")
                elif which == 8:
                    store.transition(record_id=rid, dst=State.UNDER_REVIEW,
                                     actor="v1", role=Role.VERIFIER)
                else:
                    sched.set_priority(job_id=jid, priority=1 + (i % 8),
                                       actor="scheduler", role="SCHEDULER",
                                       reason="fuzz")
            except refusals as exc:
                outcome = type(exc).__name__      # an illegal move, refused
            finally:
                # THE FEEDBACK SIGNAL FOR A STATE MACHINE. Which state the
                # operation was attempted from, which operation it was, and
                # whether the machine allowed it. Two sequences that ran the
                # same lines and attempted different transitions are
                # different cases, and this is what says so.
                feature(("sched", before, which, outcome))

        # OUTSIDE the tolerant loop, on purpose. Everything below is the
        # invariant, and anything it raises is a finding.
        return replay_invariants(path)


def _record_target(builder):
    def run(data: bytes):
        rec = json.loads(data.decode("utf-8", "surrogateescape"))
        return builder(rec)
    return run


def _policy_decision(data: bytes):
    """A recorded VERDICT, not a document.

    document_from_record was already fuzzed. The decision record is the other
    half and the more dangerous one: it is what an auditor reads to learn why
    something was permitted, and ``PolicyStore._recheck_decision`` is the only
    thing standing between a forged ALLOW and every reader downstream
    repeating it.
    """
    from qta_agent.events import EventLog
    from qta_agent.policy import (
        ACT_POLICY_DECISION, ANY, Effect, PolicyStore, document, rule,
    )
    rec = json.loads(data.decode("utf-8", "surrogateescape"))
    with tempfile.TemporaryDirectory() as tmp:
        log = EventLog(Path(tmp) / "log.jsonl")
        store = PolicyStore(log).load()
        store.publish(document(
            policy_id="p", version=1,
            rules=(rule(rule_id="r", effect=Effect.ALLOW, actions=("act",),
                        subjects=(ANY,), roles=(ANY,), resources=(ANY,),
                        obligations=("record_evidence",)),)), actor="owner")
        log.append(actor="attacker", action=ACT_POLICY_DECISION, target="t",
                   payload=rec)
        return PolicyStore(log).load()


def _capability_chain(data: bytes):
    """A grant record folded against a live root issuer.

    capability_from_record covers the SHAPE of a grant. This covers the
    authority question the shape cannot ask: who minted it, what it claims to
    derive from, and whether that derivation widens anything.
    """
    from qta_agent.capability import (
        ACT_ISSUE, Action, CapabilityLedger, issue,
    )
    from qta_agent.events import EventLog
    rec = json.loads(data.decode("utf-8", "surrogateescape"))
    with tempfile.TemporaryDirectory() as tmp:
        log = EventLog(Path(tmp) / "log.jsonl")
        led = CapabilityLedger(log).load()
        led.issue(issue(capability_id="root-cap", subject="holder",
                        action=Action.READ_PATHS, task_id="t1",
                        scope=("verification/stage10",), issued_seq=1),
                  actor="control-plane")
        log.append(actor="attacker", action=ACT_ISSUE, target="t1",
                   payload=rec)
        return CapabilityLedger(log).load()


def _url_target(data: bytes):
    from qta_agent.netauth import parse_target
    return parse_target(data.decode("utf-8", "surrogateescape"))


def _host_match(data: bytes):
    from qta_agent.netauth import host_matches
    text = data.decode("utf-8", "surrogateescape")
    half = len(text) // 2 or 1
    return host_matches(text[:half], text[half:])


def _redactor(data: bytes):
    from qta_agent.secrets import Redactor
    r = Redactor()
    r.add("s1", "a-secret-value-long-enough")
    return r.walk(data.decode("utf-8", "surrogateescape"))


def _canonical(data: bytes):
    from qta_agent.canonical import canonical_bytes
    return canonical_bytes(json.loads(data.decode("utf-8",
                                                  "surrogateescape")))


def _rag_index(data: bytes):
    from qta_multiphysics.stack import rag_index
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "index.json"
        path.write_bytes(data)
        return rag_index.load_index(path)


def _targets() -> dict:
    """name -> (callable, declared refusal exceptions, seed inputs)."""
    from qta_agent.agents import (
        AgentError, escalation_from_record, identity_from_record,
        message_from_record,
    )
    from qta_agent.capability import (
        CapabilityError, capability_from_record,
    )
    from qta_agent.canonical import CanonicalizationError
    from qta_agent.checkpoint import CheckpointError
    from qta_agent.context import ContextError, manifest_from_record
    from qta_agent.events import EventLogError
    from qta_agent.evidence import EvidenceError
    from qta_agent.memory import MemoryError_, entry_from_record
    from qta_agent.safeio import SafeIOError
    from qta_agent.netauth import NetworkError, grant_from_record
    from qta_agent.policy import PolicyError, document_from_record
    from qta_agent.scheduler import SchedulerError, job_from_record

    #: Every target may also raise these: a malformed byte string is not JSON
    #: and is not valid UTF-8, and saying so is a refusal like any other.
    common = (ValueError, TypeError, KeyError, UnicodeDecodeError,
              json.JSONDecodeError)

    return {
        "events": (_log_reader, (EventLogError,) + common,
                   [b'{"seq":0,"event_id":"a","wall_time":1.0,"actor":"a",'
                    b'"action":"record.create","target":"t","payload":{},'
                    b'"prev_hash":"' + b"0" * 64 + b'","hash":"' + b"1" * 64
                    + b'","canonical_form_version":1}\n']),
        "log_head": (_head_reader, (EventLogError,) + common,
                     [b'{"seq": 3, "head_hash": "' + b"a" * 64 + b'"}']),
        "checkpoint": (_checkpoint_reader, (CheckpointError,) + common,
                       [b'{"seq": 1, "head_hash": "' + b"b" * 64
                        + b'", "state_digest": null}']),
        "evidence_name": (_evidence_name, (EvidenceError,) + common,
                          [b"c" * 64, b"../../etc/passwd", b""]),
        "capability": (_record_target(capability_from_record),
                       (CapabilityError,) + common,
                       [json.dumps({
                           "capability_id": "c1", "subject": "w",
                           "action": "WRITE_PATHS", "task_id": "t",
                           "tool_id": "", "scope": ["verification/stage10"],
                           "issued_seq": 0,
                           "expires_after_seq": -1}).encode()]),
        "policy": (_record_target(document_from_record),
                   (PolicyError,) + common,
                   [json.dumps({
                       "policy_id": "p", "version": 1, "description": "",
                       "rules": [{"rule_id": "r", "effect": "ALLOW",
                                  "actions": ["*"], "subjects": ["*"],
                                  "roles": ["*"], "resources": ["*"],
                                  "reason": ""}]}).encode()]),
        "policy_decision": (
            _policy_decision, (PolicyError, EventLogError) + common,
            [json.dumps({
                "decision": {
                    "allowed": True, "policy_id": "p", "version": 1,
                    "policy_digest": "d" * 64, "rule_id": "r",
                    "effect": "ALLOW",
                    "request": {"action": "act", "subject": "s",
                                "role": "WORKER", "resource": "r",
                                "task_id": "", "attributes": {}},
                    "reason": "", "obligations": ["record_evidence"],
                    "at_seq": -1},
                "decision_digest": "e" * 64}).encode()]),
        "capability_chain": (
            _capability_chain, (CapabilityError, EventLogError) + common,
            [json.dumps({
                "task_id": "t1", "capability_id": "c2", "subject": "helper",
                "action": "READ_PATHS", "tool_id": "",
                "scope": ["verification/stage10"], "issued_seq": 2,
                "expires_after_seq": -1,
                "parent_id": "root-cap"}).encode()]),
        "job": (_record_target(job_from_record), (SchedulerError,) + common,
                [json.dumps({"job_id": "j", "work_digest": "d" * 64,
                             "submitter": "s", "priority": 9,
                             "state": "WAITING"}).encode()]),
        "memory": (_record_target(entry_from_record),
                   (MemoryError_,) + common,
                   [json.dumps({"memory_id": "m", "text": "t",
                                "author": "a"}).encode()]),
        "identity": (_record_target(identity_from_record),
                     (AgentError,) + common,
                     [json.dumps({"agent_id": "a", "instance_id": "i",
                                  "kind": "AGENT",
                                  "roles": ["PROPOSER"]}).encode()]),
        "message": (_record_target(message_from_record),
                    (AgentError,) + common,
                    [json.dumps({"message_id": "m", "sender_instance": "i",
                                 "recipient_agent": "a", "task_id": "t",
                                 "subject": "s",
                                 "body_digest": "e" * 64}).encode()]),
        "escalation": (_record_target(escalation_from_record),
                       (AgentError,) + common,
                       [json.dumps({"escalation_id": "e", "task_id": "t",
                                    "question": "q?", "raised_by": "p",
                                    "options": ["y", "n"]}).encode()]),
        "egress_grant": (_record_target(grant_from_record),
                         (NetworkError,) + common,
                         [json.dumps({
                             "grant_id": "g", "subject": "s", "task_id": "t",
                             "tool_id": "x", "schemes": ["https"],
                             "hosts": ["api.example.com"], "ports": [443],
                             "methods": ["GET"]}).encode()]),
        "context_manifest": (_record_target(manifest_from_record),
                             (ContextError,) + common,
                             [json.dumps({
                                 "task_id": "t", "purpose": "p",
                                 "items": [], "omissions": [],
                                 "budget_bytes": 10, "used_bytes": 0,
                                 "policy_identity": "", "policy_digest": "",
                                 "at_seq": 1}).encode()]),
        "read_path": (_read_path, (SafeIOError,) + common,
                      [b"a/b.txt", b"../../etc/passwd", b"/etc/passwd",
                       b"a//b", b"a/./b", b".", b"", b"a\x00b"]),
        "read_beneath": (_read_beneath,
                         (SafeIOError, FileNotFoundError) + common,
                         [b"f.txt", b"../f.txt", b"missing", b"f.txt/x"]),
        "url": (_url_target, (NetworkError,) + common,
                [b"https://api.example.com/v1",
                 b"https://user@evil.test/", b"http://[::1]:80/x"]),
        "host_match": (_host_match, common,
                       [b"*.example.comsub.example.com"]),
        "redaction": (_redactor, common,
                      [b"a-secret-value-long-enough in a line"]),
        "canonical": (_canonical, (CanonicalizationError,) + common,
                      [b'{"a": 1, "b": [1, 2, {"c": null}]}']),
        "scheduler_sequence": (
            _scheduler_sequence, common + (OSError,),
            [bytes([0, 1, 2, 3]), bytes([0, 1, 2, 5, 1, 2, 3]),
             bytes([7, 8, 0, 1, 2, 6]), bytes(range(10))]),
        "rag_index": (_rag_index, common + (OSError,),
                      [b'{"schema_version": 1, "chunks": []}']),
    }


# ---- coverage feedback ---------------------------------------------------
#
# WHY A RANDOM FUZZER PLATEAUS
#
# Random mutation reaches a deep parser state by luck, and the luck runs out
# fast: past the first few branches the probability that an unguided mutation
# lands on the byte pattern which opens the next one is negligible. So a
# campaign of ten thousand random cases explores roughly what a campaign of
# five hundred did, and reports the same "no findings" with the same
# confidence. R50 named this as the gap it was.
#
# Feedback closes it in the standard way: run each case with line coverage
# on, and KEEP the inputs that reached somewhere new. The next mutation
# starts from one of those rather than from a seed, so progress compounds.
#
# WHY THIS IS AFFORDABLE
#
# sys.monitoring (PEP 669) charges only for the code objects that stay
# instrumented, and the callback returns DISABLE for every file outside
# qta_agent -- permanently, per instruction. Measured cost on this suite:
# 1.3x, against roughly 30x for sys.settrace. A tracer nobody can afford to
# leave on is a tracer that gets switched off.

#: Which sys.monitoring tool slot to claim. 0-5 are free for tools; the
#: profiler and debugger ids are avoided so a run under either still works.
_TOOL_ID = 3

#: Inputs kept per target. Bounded because a corpus that grows without limit
#: makes each later case slower to choose from and the campaign's cost
#: depend on its own history.
MAX_CORPUS_PER_TARGET = 64

#: How often a case starts from a DECLARED SEED rather than from something
#: the campaign kept. See the comment at the selection site: without this
#: the corpus dilutes the seeds and guided coverage falls below unguided.
SEED_SHARE = 0.5


#: Features a TARGET declares interesting, unioned with line coverage to
#: form a case's signature.
#:
#: WHY LINES ARE NOT ENOUGH FOR A STATE MACHINE
#:
#: Line coverage over these parsers saturates in a few hundred random cases:
#: measured, the sequence target reaches 1538 lines from its seeds alone and
#: feedback on that signal made coverage very slightly WORSE, because the
#: kept corpus dilutes the seeds while telling the fuzzer nothing new. The
#: interesting thing about a sequence is not which lines ran but which
#: (state, operation, outcome) triples were attempted, and no line coverage
#: can see the difference between dispatching a READY job and dispatching a
#: CANCELLED one -- both run the same lines and one of them is the case that
#: matters.
_FEATURES: set = set()


def feature(item) -> None:
    """Declare that this case reached something worth keeping an input for."""
    _FEATURES.add(item)


def _take_features() -> frozenset:
    global _FEATURES
    out = frozenset(_FEATURES)
    _FEATURES = set()
    return out


class Coverage:
    """Line coverage inside ``qta_agent``, collected per case.

    Falls back to collecting NOTHING when sys.monitoring is unavailable or
    its tool slot is taken -- and says so rather than reporting an empty
    coverage set as though the code executed nothing, which would make the
    guidance silently random again.
    """

    def __init__(self):
        self.available = False
        self.reason = ""
        self._hits: set = set()
        self._mon = getattr(sys, "monitoring", None)
        if self._mon is None:                     # pragma: no cover
            self.reason = "sys.monitoring is not available on this build"
            return
        try:
            self._mon.use_tool_id(_TOOL_ID, "qta-fuzz")
        except ValueError as exc:                 # pragma: no cover
            self.reason = f"tool id {_TOOL_ID} is in use: {exc}"
            return
        self._mon.register_callback(
            _TOOL_ID, self._mon.events.LINE, self._line)
        self.available = True

    def _line(self, code, line):
        if "qta_agent" not in code.co_filename:
            # DISABLE is permanent for this instruction until events are
            # restarted, which is exactly what makes the cost bearable.
            return self._mon.DISABLE
        self._hits.add((code.co_filename, line))
        return None

    def start(self) -> None:
        if self.available:
            self._mon.set_events(_TOOL_ID, self._mon.events.LINE)

    def stop(self) -> None:
        if self.available:
            self._mon.set_events(_TOOL_ID, 0)

    def take(self) -> frozenset:
        """The lines hit since the last call, and reset."""
        hits = frozenset(self._hits)
        self._hits = set()
        return hits

    def close(self) -> None:
        if self.available:
            self.stop()
            self._mon.free_tool_id(_TOOL_ID)
            self.available = False


# ---- mutation ------------------------------------------------------------
def _mutate(rng: random.Random, seed: bytes) -> bytes:
    """One mutation of ``seed``. Bounded by construction."""
    data = bytearray(seed)
    how = rng.randrange(11)
    if not data:
        data = bytearray(b"{}")
    if how == 0:                                    # bit flip
        i = rng.randrange(len(data))
        data[i] ^= 1 << rng.randrange(8)
    elif how == 1:                                  # truncate
        data = data[:rng.randrange(len(data) + 1)]
    elif how == 2:                                  # duplicate a slice
        i = rng.randrange(len(data))
        j = min(len(data), i + rng.randrange(1, 64))
        data[i:i] = data[i:j]
    elif how == 3:                                  # insert control bytes
        i = rng.randrange(len(data) + 1)
        data[i:i] = bytes(rng.randrange(256)
                          for _ in range(rng.randrange(1, 16)))
    elif how == 4:                                  # very long field
        data += b'"' + b"A" * rng.randrange(1, 4096) + b'"'
    elif how == 5:                                  # nesting
        depth = rng.randrange(1, 200)
        data = bytearray(b"[" * depth + b"1" + b"]" * depth)
    elif how == 6:                                  # type confusion
        try:
            obj = json.loads(bytes(data).decode("utf-8", "surrogateescape"))
        except Exception:                           # noqa: BLE001
            obj = {}
        if isinstance(obj, dict) and obj:
            key = rng.choice(sorted(obj))
            obj[key] = rng.choice(
                [None, [], {}, True, -1, 10 ** 20, "", "\x00", 1.5,
                 [[]] * 5])
        data = bytearray(json.dumps(obj).encode())
    elif how == 7:                                  # unicode and nulls
        data += rng.choice([b"\x00", b"\xff\xfe", "‮".encode(),
                            "\ud800".encode("utf-8", "surrogatepass")])
    elif how == 8:                                  # traversal-shaped strings
        data += rng.choice([b"../", b"..\\", b"%2e%2e%2f", b"/etc/passwd",
                            b"\x00/etc/passwd"])
    elif how == 9:                                  # base64-ish noise
        data += base64.b64encode(bytes(rng.randrange(256)
                                       for _ in range(rng.randrange(1, 64))))
    else:                                           # empty
        data = bytearray()
    return bytes(data[:MAX_INPUT_BYTES])


# ---- the campaign --------------------------------------------------------
def run_case(name: str, fn, declared, data: bytes) -> dict | None:
    """Run one case. Returns a finding, or None when the target behaved."""
    try:
        with _Deadline(CASE_TIMEOUT_S):
            fn(data)
    except Hung as exc:
        return {"target": name, "kind": HUNG, "detail": str(exc),
                "input_b64": base64.b64encode(data).decode()}
    except declared:
        return None                                 # a refusal: correct
    except RecursionError:
        # Bounded by the interpreter rather than by the target. Reported so a
        # parser that recurses on attacker-controlled nesting is visible.
        return {"target": name, "kind": CRASHED,
                "detail": "RecursionError: nesting is bounded by the "
                          "interpreter, not by this parser",
                "input_b64": base64.b64encode(data).decode()}
    except MemoryError:                             # pragma: no cover
        return {"target": name, "kind": CRASHED, "detail": "MemoryError",
                "input_b64": base64.b64encode(data).decode()}
    except BaseException as exc:                    # noqa: BLE001 - the point
        return {"target": name, "kind": CRASHED,
                "detail": f"{type(exc).__name__}: {exc}",
                "input_b64": base64.b64encode(data).decode(),
                "traceback": traceback.format_exc(limit=4)}
    return None


def campaign(*, cases: int, seed: int, only: str | None = None,
             guided: bool = True) -> tuple:
    """Run a bounded campaign. Returns (findings, cases_run, stats).

    With ``guided``, each case runs under line coverage and any input that
    reached a line no earlier case reached is kept and mutated from. The
    corpus starts as the declared seeds, so an unguided run is the same
    campaign with the feedback switched off -- which is how the two are
    compared rather than asserted about.
    """
    rng = random.Random(seed)
    targets = _targets()
    if only:
        if only not in targets:
            raise SystemExit(
                f"unknown target {only!r}; known: {sorted(targets)}")
        targets = {only: targets[only]}
    findings: list = []
    run = 0
    names = sorted(targets)
    cov = Coverage()
    corpus = {n: list(targets[n][2]) for n in names}
    reached: dict = {n: set() for n in names}
    feats: dict = {n: set() for n in names}
    kept = 0
    # Captured BEFORE the close in the finally below, which sets available
    # to False. Reading it afterwards reported every guided campaign as
    # unguided -- a status field describing the state of the machinery
    # rather than the state of the run.
    measuring = cov.available
    # Feedback is possible whenever there is ANY signal: line coverage, or a
    # target's own declared features. A target that reports features can be
    # guided on a build where sys.monitoring is unavailable.
    was_guided = bool(guided)
    unavailable = cov.reason if not measuring else ""
    try:
        if measuring:
            cov.start()
        for i in range(cases):
            name = names[i % len(names)]
            fn, declared, seeds = targets[name]
            # HALF THE TIME, START FROM A DECLARED SEED.
            #
            # The first version of this loop chose uniformly from the whole
            # corpus, and measured WORSE than no guidance at all: within a
            # few hundred cases the corpus held ninety mutated inputs and
            # three seeds, so a valid record was the parent two per cent of
            # the time and every case was mutating something already broken.
            # Coverage went DOWN. Keeping the seeds in play is what makes
            # the feedback compound instead of drift.
            if corpus[name] and rng.random() >= SEED_SHARE:
                parent = rng.choice(corpus[name])
            else:
                parent = rng.choice(seeds)
            data = _mutate(rng, parent)
            run += 1
            if measuring:
                cov.take()                        # discard anything pending
            _take_features()
            found = run_case(name, fn, declared, data)
            lines = cov.take() if measuring else frozenset()
            marks = _take_features()
            if lines or marks:
                new_signal = ((lines - reached[name])
                              | (marks - feats[name]))
                reached[name] |= lines
                feats[name] |= marks
                if new_signal:
                    # Both signals are MEASURED either way; only the
                    # feedback is switched off. Measuring one arm and not
                    # the other would make the comparison between them
                    # meaningless, which is the whole reason --no-guidance
                    # exists.
                    if guided and len(corpus[name]) < MAX_CORPUS_PER_TARGET:
                        corpus[name].append(data)
                        kept += 1
            if found is not None:
                found["seed"] = seed
                findings.append(found)
    finally:
        cov.close()
    stats = {
        "guided": bool(was_guided),
        "guidance_unavailable": unavailable,
        "lines_reached": sum(len(v) for v in reached.values()),
        "features_reached": sum(len(v) for v in feats.values()),
        "inputs_kept": kept,
        "targets": len(names),
    }
    return findings, run, stats


def replay_corpus(corpus: Path) -> list:
    """Re-run every committed regression input. Any finding is a failure."""
    targets = _targets()
    findings: list = []
    for path in sorted(corpus.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        name = case["target"]
        if name not in targets:
            findings.append({"target": name, "kind": CRASHED,
                             "detail": f"{path.name}: unknown target"})
            continue
        fn, declared, _ = targets[name]
        data = base64.b64decode(case["input_b64"])
        found = run_case(name, fn, declared, data)
        if found is not None:
            found["corpus_file"] = path.name
            findings.append(found)
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cases", type=int, default=400)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--target", default=None)
    ap.add_argument("--corpus", type=Path,
                    default=ROOT / "tests" / "fuzz_corpus")
    ap.add_argument("--replay-only", action="store_true")
    ap.add_argument("--no-guidance", action="store_true",
                    help="run without coverage feedback, for comparison")
    ap.add_argument("--min-lines", type=int, default=0,
                    help="fail if the campaign reached fewer lines than this")
    ap.add_argument("--min-features", type=int, default=0,
                    help="fail if it reached fewer state transitions")
    ap.add_argument("--save", action="store_true",
                    help="write new findings into the corpus directory")
    args = ap.parse_args()

    args.corpus.mkdir(parents=True, exist_ok=True)
    print(f"replaying corpus: {args.corpus}")
    regressions = replay_corpus(args.corpus)
    for f in regressions:
        print(f"  REGRESSION {f['kind']:8s} {f['target']}: {f['detail']}")
    if not regressions:
        n = len(list(args.corpus.glob("*.json")))
        print(f"  {n} corpus case(s), all still refused correctly")

    findings: list = []
    if not args.replay_only:
        seed = args.seed if args.seed is not None else random.randrange(2**31)
        print(f"\ncampaign: {args.cases} cases, seed {seed}")
        findings, run, stats = campaign(cases=args.cases, seed=seed,
                                        only=args.target,
                                        guided=not args.no_guidance)
        print(f"  {run} case(s) run over "
              f"{len(_targets()) if not args.target else 1} target(s)")
        if stats["guided"]:
            print(f"  coverage-guided: {stats['lines_reached']} line(s) "
                  f"and {stats['features_reached']} state-transition "
                  f"feature(s) reached, {stats['inputs_kept']} input(s) kept")
        elif stats["lines_reached"] or stats["features_reached"]:
            print(f"  UNGUIDED: {stats['lines_reached']} line(s) and "
                  f"{stats['features_reached']} state-transition feature(s) "
                  "reached, feedback off (measured for comparison only)")
        elif stats["guidance_unavailable"]:
            print("  coverage guidance UNAVAILABLE "
                  f"({stats['guidance_unavailable']}); this campaign was "
                  "random, and says so rather than reporting guided cases "
                  "it did not run")
        for f in findings:
            print(f"  {f['kind']:8s} {f['target']}: {f['detail']}")
            if args.save:
                name = (f"{f['target']}-{f['kind'].lower()}-"
                        f"{abs(hash(f['input_b64'])) % 10 ** 8:08d}.json")
                (args.corpus / name).write_text(
                    json.dumps(f, indent=2, sort_keys=True), encoding="utf-8")
                print(f"           saved as {name}")
        if not findings:
            print("  no findings")

        # THE ANTI-VACUITY FLOOR, AND WHY A FUZZER NEEDS ONE.
        #
        # "no findings" is the same sentence whether the campaign exercised
        # the whole package or nothing at all: a target that raises at
        # import, a mutation operator that started returning b"", a refusal
        # tuple widened until every case is "correctly refused" -- each of
        # those turns this tool into a green tick over an empty run. The
        # floors make the campaign state how much it actually reached and
        # fail when that collapses.
        if args.min_lines and stats["lines_reached"] < args.min_lines:
            print(f"  FLOOR: reached {stats['lines_reached']} line(s), "
                  f"below the required {args.min_lines}. A campaign that "
                  "stopped reaching the code is not a campaign that found "
                  "nothing")
            return 1
        if args.min_features and stats["features_reached"] < args.min_features:
            print(f"  FLOOR: reached {stats['features_reached']} state "
                  f"transition(s), below the required {args.min_features}")
            return 1

    return 1 if (regressions or findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
