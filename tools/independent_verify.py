#!/usr/bin/env python3
"""Reconstruct a log's authority state in a process that CANNOT cheat.

WHY A SUBPROCESS AND NOT A FUNCTION CALL

``agent_substrate.reconstruct`` is already a second implementation: it restates
every authority rule in plain dicts rather than importing the reducers it
checks. That is implementation-level separation, and it is real -- but it is
separation the two readers keep by DISCIPLINE. Both live in one interpreter,
share a module cache, and could reach each other's live objects; nothing
except review stops a future edit from importing the primary reducer here to
"remove the duplication", and the day that happens the comparison becomes
circular while continuing to report agreement.

So this runs somewhere the shortcut is not available:

* a separate OS process, with its own interpreter state, so there are no
  shared live objects and no access to the caller's projections;
* reading the log from DISK, from a path on argv, so the input is the durable
  bytes rather than anything the caller is holding;
* under an import guard that REFUSES the primary authority reducers. Not a
  convention -- an ImportError. If a future edit reaches for one, this
  program stops, and the caller sees a crashed verifier rather than a
  confident agreement;
* answering through a narrow interface: one JSON document on stdout, and an
  exit status.

WHAT THE EXIT STATUS MEANS

  0  reconstruction completed and found no anomalies
  1  reconstruction completed and FOUND anomalies (the log is not clean)
  2  the input could not be read or parsed at all
  3  the import guard fired: this program was built wrong
  * anything else, including a signal, is a crashed verifier

A crashed verifier is NOT a pass. The caller treats every non-zero status as
"this did not verify", and distinguishes them only to say why.
"""
from __future__ import annotations

import argparse
import importlib.abc
import json
import sys
from pathlib import Path

#: The only modules of this repository the verifier loads, measured: the
#: package, the log, the canonical digest the log chains with, the action
#: vocabulary, and the reader. The guard refuses every other agent_substrate
#: module. It used to refuse the names in FORBIDDEN and admit the rest, and
#: FORBIDDEN never named authority or tasks -- the two gates the reader was
#: decoupled from -- nor any first reader added after it was written
#: (D-2026-126). A list of what is permitted does not go stale that way.
PERMITTED = frozenset({
    "agent_substrate",
    "agent_substrate.actions",
    "agent_substrate.canonical",
    "agent_substrate.events",
    "agent_substrate.reconstruct",
})

#: The PRIMARY reducers, named: the things being checked. Every one is
#: outside PERMITTED, so the guard refuses it; the list is kept so the tests
#: can show that, one by one, rather than trusting the set arithmetic.
#: Importing one would make the second reading a second call to the first
#: implementation, which agrees with itself for free.
FORBIDDEN = frozenset({
    "agent_substrate.authority",
    "agent_substrate.tasks",
    "agent_substrate.store",
    "agent_substrate.scheduler",
    "agent_substrate.policy",
    "agent_substrate.capability",
    "agent_substrate.agents",
    "agent_substrate.memory",
    "agent_substrate.netauth",
    "agent_substrate.secrets",
    "agent_substrate.context",
    "agent_substrate.governed_tasks",
    "agent_substrate.idempotency",
    "agent_substrate.audit",
    "agent_substrate.checkpoint",
    "agent_substrate.invalidation",
    "agent_substrate.principals",
    "agent_substrate.proposals",
    "agent_substrate.result_rules",
    "agent_substrate.learned_rules",
    "agent_substrate.learned_lifecycle",
})


class _Refuse(importlib.abc.MetaPathFinder):
    """Raise on any import of this repository's code outside PERMITTED.

    Never returns a spec: this finder exists only to refuse.
    """

    def find_spec(self, fullname, path=None, target=None):
        if (fullname.partition(".")[0] == "agent_substrate"
                and fullname not in PERMITTED):
            raise ImportError(
                f"independent_verify refuses to import {fullname!r}. This "
                "process exists to read the log WITHOUT the implementation "
                "it is checking; importing it would make the second reading "
                "a second call to the first one, which agrees with itself "
                "for free.")
        return None


def main() -> int:
    # Installed FIRST, before agent_substrate is touched at all, so the guard
    # is in place for every import this program will ever do.
    sys.meta_path.insert(0, _Refuse())

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("log", help="path to the event log to reconstruct")
    ap.add_argument("--root",
                    default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args()

    if args.root not in sys.path:
        sys.path.insert(0, args.root)

    try:
        from agent_substrate.events import EventLog
        from agent_substrate.reconstruct import (
            reconstruct, reconstruct_subsystems, reconstruct_tasks,
        )
    except ImportError as exc:
        # The guard firing is a defect in THIS program, and it is reported
        # as its own status so the caller does not read it as a log finding.
        print(json.dumps({"ok": False, "reason": f"import guard: {exc}"}))
        return 3

    path = Path(args.log)
    try:
        log = EventLog(path)
        report = log.verify()
    except Exception as exc:                            # noqa: BLE001
        print(json.dumps({
            "ok": False,
            "reason": ("the log could not be read: "
                       f"{type(exc).__name__}: {exc}"),
        }))
        return 2

    findings = list(report.problems)
    result = {
        "log": str(path),
        "head_seq": report.head_seq,
        "chain_ok": not report.problems,
    }

    try:
        subs = reconstruct_subsystems(log)
        recs = reconstruct(log)
        tasks = reconstruct_tasks(log)
    except Exception as exc:                            # noqa: BLE001
        print(json.dumps({
            **result, "ok": False,
            "reason": f"reconstruction raised: {type(exc).__name__}: {exc}",
        }))
        return 2

    findings.extend(subs.anomalies)
    findings.extend(getattr(recs, "anomalies", ()) or ())
    findings.extend(getattr(tasks, "anomalies", ()) or ())

    result.update({
        "events_replayed": subs.events_replayed,
        "jobs": len(subs.jobs),
        "capabilities": len(subs.capabilities),
        "services": len(subs.services),
        "agents": len(subs.agents),
        "memory": len(subs.memory),
        "escalations": len(subs.escalations),
        # Reported so this process says what it actually covered. A verifier
        # that prints counts for the subsystems it happens to have had first
        # is a verifier whose silence about the others reads as agreement.
        "claims": len(subs.claims),
        "checkpoint_anchors": len(subs.checkpoints),
        "compensations": sum(len(v) for v in subs.compensations.values()),
        "root_issuer": subs.root_issuer,
        "records": len(getattr(recs, "records", ()) or ()),
        "tasks": len(getattr(tasks, "tasks", ()) or ()),
        "findings": findings,
        "ok": not findings,
        "reason": ("reconstruction found no anomalies" if not findings
                   else f"{len(findings)} anomaly(ies) in the log"),
    })
    print(json.dumps(result, sort_keys=True))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
