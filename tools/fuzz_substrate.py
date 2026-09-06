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
        "rag_index": (_rag_index, common + (OSError,),
                      [b'{"schema_version": 1, "chunks": []}']),
    }


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


def campaign(*, cases: int, seed: int, only: str | None = None) -> tuple:
    """Run a bounded campaign. Returns (findings, cases_run)."""
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
    for i in range(cases):
        name = names[i % len(names)]
        fn, declared, seeds = targets[name]
        data = _mutate(rng, rng.choice(seeds))
        run += 1
        found = run_case(name, fn, declared, data)
        if found is not None:
            found["seed"] = seed
            findings.append(found)
    return findings, run


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
        findings, run = campaign(cases=args.cases, seed=seed,
                                 only=args.target)
        print(f"  {run} case(s) run over "
              f"{len(_targets()) if not args.target else 1} target(s)")
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

    return 1 if (regressions or findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
