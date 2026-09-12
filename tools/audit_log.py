#!/usr/bin/env python3
"""Ask a governed event log what happened. Read-only, and fail-closed.

WHY THIS IS A COMMAND AND NOT A NOTEBOOK

``qta_agent.audit`` has had a queryable index for a while, and everything it
could answer was reachable only by importing it and writing Python. That is
not an auditor: the person who needs it during an incident is the person least
able to write a correct query against an unfamiliar API at the time they need
it. Worse, an ad-hoc query written under pressure is itself unreviewed code
making claims about a history.

So the queries are named, committed, and reviewable like anything else, and
the answers come out in a fixed shape.

READ-ONLY IS A PROPERTY, NOT A PROMISE IN A DOCSTRING

This process opens the log for reading and nothing else. It builds no
projection it writes back, appends no "audit happened" record, and takes no
lock. That matters for the obvious reason -- an auditor that mutates the thing
it is auditing has destroyed the evidence -- and for a less obvious one: an
audit must be safe to run against a live system, mid-incident, by somebody who
is not certain what they are doing. ``tests/test_agent_audit_cli.py`` asserts
it, by hashing the log before and after every command.

FAIL-CLOSED MEANS THE EXIT STATUS CARRIES THE FINDING

A tool that prints "PROVENANCE GAPS" and exits 0 will be run in a pipeline
that ignores it. The exit status is:

    0   the question was answered and nothing was wrong
    1   the history has a finding: a broken chain, a provenance gap, a
        transition that would be refused today, or two readers disagreeing
    2   the question could not be asked: no such log, no such subject

A finding is not a crash. It is the tool working.

WHAT IT DOES NOT ESTABLISH

That the outcomes are CORRECT. Every answer here is about the history: what
records exist, whether they connect, whether they would be authorized if
replayed now, and whether an independent reader agrees. A complete chain over
a well-formed lie is still complete. ``automatic_gate_effect`` is NONE and
nothing this prints is a scientific claim.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.audit import AuditIndex  # noqa: E402
from qta_agent.events import ChainBroken, EventLog  # noqa: E402
from qta_agent.reconstruct import reconstruct, reconstruct_tasks  # noqa: E402

OK, FINDING, CANNOT_ASK = 0, 1, 2


def _out(payload, *, as_json: bool, text: str) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str)
          if as_json else text)


# --- the commands ----------------------------------------------------------

def cmd_verify(log, args) -> int:
    """The chain itself. Every other question presumes this one."""
    r = log.verify()
    _out({"ok": r.ok, "count": r.count, "head_seq": r.head_seq,
          "head_hash": r.head_hash, "problems": r.problems, "notes": r.notes,
          "prefix_verified": r.prefix_verified},
         as_json=args.json,
         text="\n".join(
             [f"chain: {'ok' if r.ok else 'BROKEN'}  "
              f"{r.count} events, head seq {r.head_seq}, "
              f"hash {r.head_hash[:12]}"]
             + [f"  PROBLEM: {p}" for p in r.problems]
             + [f"  note: {n}" for n in r.notes]))
    return OK if r.ok else FINDING


def cmd_subjects(log, args) -> int:
    ix = AuditIndex.from_log(log)
    subjects = ix.subjects()
    _out(list(subjects), as_json=args.json, text="\n".join(subjects))
    return OK


def cmd_actors(log, args) -> int:
    ix = AuditIndex.from_log(log)
    rows = [(a, len(ix.actions_by(a))) for a in ix.actors()]
    _out({a: n for a, n in rows}, as_json=args.json,
         text="\n".join(f"{n:>5}  {a}" for a, n in rows))
    return OK


def _explain(ix, subject: str, kind: str):
    return (ix.explain_task(subject) if kind == "task"
            else ix.explain_record(subject))


def cmd_explain(log, args) -> int:
    """One subject's chain, and what is missing from it."""
    ix = AuditIndex.from_log(log)
    exp = _explain(ix, args.subject, args.kind)
    if exp.outcome == "UNKNOWN" and not exp.steps:
        print(f"no {args.kind} {args.subject!r} in this log", file=sys.stderr)
        return CANNOT_ASK
    _out(exp.to_record(), as_json=args.json, text=exp.render())
    return OK if exp.complete else FINDING


def cmd_gaps(log, args) -> int:
    """Every subject whose provenance has a hole. The sweep, not one lookup.

    This is the command to run when nobody has told you where to look, and
    it is the reason the index keeps its own list of subjects: an auditor
    who has to know the task id in advance can only confirm suspicions.
    """
    ix = AuditIndex.from_log(log)
    found = []
    for subject in ix.subjects():
        for kind in ("task", "record"):
            exp = _explain(ix, subject, kind)
            if exp.steps and exp.gaps:
                found.append({"subject": subject, "kind": kind,
                              "outcome": exp.outcome, "gaps": list(exp.gaps)})
    if args.json:
        _out(found, as_json=True, text="")
    elif not found:
        print("no provenance gaps")
    else:
        for f in found:
            print(f"{f['kind']} {f['subject']}: {f['outcome']}")
            for g in f["gaps"]:
                print(f"    - {g}")
    return FINDING if found else OK


def cmd_decisions(log, args) -> int:
    ix = AuditIndex.from_log(log)
    allowed = None if args.all else not args.denied
    steps = ix.decisions(allowed=allowed)
    _out([s.to_record() for s in steps], as_json=args.json,
         text="\n".join(f"seq {s.seq:<5} {s.actor:<18} {s.summary}"
                        for s in steps))
    return OK


def cmd_decision(log, args) -> int:
    """One decision, joined to the document version that made it."""
    ix = AuditIndex.from_log(log)
    exp = ix.explain_decision(args.at_seq)
    if exp.outcome == "UNKNOWN":
        print(f"no policy decision at seq {args.at_seq}", file=sys.stderr)
        return CANNOT_ASK
    _out(exp.to_record(), as_json=args.json, text=exp.render())
    return OK if exp.complete else FINDING


def cmd_timeline(log, args) -> int:
    ix = AuditIndex.from_log(log)
    events = (ix.actions_by(args.actor) if args.actor else ix.events)
    rows = [{"seq": e.seq, "actor": e.actor, "action": e.action,
             "target": e.target} for e in events]
    _out(rows, as_json=args.json,
         text="\n".join(f"seq {r['seq']:<5} {r['actor']:<18} "
                        f"{r['action']:<22} {r['target']}" for r in rows))
    return OK


def cmd_replay(log, args) -> int:
    """A second reader over the same bytes, and whether it agrees.

    The two reconstructions share no reducer with the live projections. What
    this reports is what they REFUSE -- a transition that would not be
    authorized today is in the log and is not state -- and any structural
    anomaly they found on the way.
    """
    rec = reconstruct(log)
    tasks = reconstruct_tasks(log)
    payload = {
        "records": {"replayed": rec.events_replayed,
                    "foreign": rec.foreign_events,
                    "states": rec.states(),
                    "canonical": list(rec.canonical_ids()),
                    "unauthorized": rec.unauthorized,
                    "anomalies": rec.anomalies},
        "tasks": {"replayed": tasks.events_replayed,
                  "foreign": tasks.foreign_events,
                  "states": tasks.states(),
                  "verified": list(tasks.verified_ids()),
                  "unauthorized": tasks.unauthorized,
                  "anomalies": tasks.anomalies},
    }
    findings = (rec.unauthorized + rec.anomalies
                + tasks.unauthorized + tasks.anomalies)
    if args.json:
        _out(payload, as_json=True, text="")
    else:
        print(f"authority records: {len(rec.records)} "
              f"({len(rec.canonical_ids())} canonical)")
        print(f"tasks: {len(tasks.tasks)} "
              f"({len(tasks.verified_ids())} verified)")
        for f in findings:
            print(f"  FINDING: {f}")
        if not findings:
            print("the independent replay found nothing to report")
    return FINDING if findings else OK


COMMANDS = {
    "verify": cmd_verify, "subjects": cmd_subjects, "actors": cmd_actors,
    "explain": cmd_explain, "gaps": cmd_gaps, "decisions": cmd_decisions,
    "decision": cmd_decision, "timeline": cmd_timeline, "replay": cmd_replay,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="audit_log.py", description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("log", help="path to the event log (.jsonl)")
    p.add_argument("--json", action="store_true",
                   help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("verify", help="check the hash chain")
    sub.add_parser("subjects", help="every target in the log")
    sub.add_parser("actors", help="every actor, with how much they did")

    e = sub.add_parser("explain", help="one subject's provenance chain")
    e.add_argument("subject")
    e.add_argument("--kind", choices=("task", "record"), default="task")

    sub.add_parser("gaps", help="every subject whose provenance has a hole")

    d = sub.add_parser("decisions", help="recorded policy decisions")
    d.add_argument("--denied", action="store_true",
                   help="only refusals (the query an incident starts with)")
    d.add_argument("--all", action="store_true", help="allowed and denied")

    one = sub.add_parser("decision", help="one decision and its document")
    one.add_argument("at_seq", type=int)

    t = sub.add_parser("timeline", help="events in order")
    t.add_argument("--actor", help="only this actor's events")

    sub.add_parser("replay", help="what an independent second reader finds")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    path = Path(args.log)
    if not path.exists():
        print(f"no event log at {path}", file=sys.stderr)
        return CANNOT_ASK
    log = EventLog(path)
    try:
        return COMMANDS[args.command](log, args)
    except ChainBroken as exc:
        # Every command but `verify` builds its view through a verification
        # that raises. Reported as a FINDING rather than a traceback: a
        # broken chain is the most important thing this tool can say, and it
        # must not look like the tool falling over.
        print(f"the log does not verify, so nothing here can be trusted: "
              f"{exc}", file=sys.stderr)
        return FINDING


if __name__ == "__main__":
    raise SystemExit(main())
