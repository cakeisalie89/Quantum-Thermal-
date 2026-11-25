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
import os
import stat
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


#: The store's own bound on one blob (agent_substrate.evidence), restated.
MAX_BLOB_BYTES = 256 * 1024 * 1024
_HEX = frozenset("0123456789abcdef")


class _FileEvidence:
    """``get(digest)`` over the evidence store's layout, read from disk
    without the store's code; the reader re-hashes whatever it returns.

    The store's three read rules are restated here, because the names come
    from the log being verified (AUD-029): a name that is not 64 lowercase
    hex digits builds no path -- ``..`` and an absolute tail would lead
    anywhere; what it names must be a regular file, opened without
    following a link or waiting on a FIFO; and no more than the store's
    bound is read, so a device that never ends cannot exhaust memory."""

    def __init__(self, root: str):
        self.root = Path(root)

    def get(self, digest: str) -> bytes:
        if not isinstance(digest, str) or len(digest) != 64 or \
                not set(digest) <= _HEX:
            raise ValueError("the evidence named is not a digest")
        fd = os.open(self.root / digest[:2] / digest[2:],
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(f"{digest[:12]} is not a regular file")
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(fd, 1 << 20):
                total += len(chunk)
                if total > MAX_BLOB_BYTES:
                    raise ValueError(f"{digest[:12]} is larger than a blob")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)


def _roots(args):
    """The authenticator and the role registry the arguments name, built
    from the restated authentication alone. (None, None) when none is
    named; an argument that cannot be read raises, and the caller reports
    the log unreadable rather than reading it without its roots."""
    if args.key_registry is None and args.role_registry is None:
        if args.attestations is not None:
            raise ValueError("--attestations without --key-registry")
        return None, None
    import importlib.util
    where = Path(args.root) / "tools" / "verify_assurance.py"
    spec = importlib.util.spec_from_file_location(
        "verify_assurance_restated", where)
    if spec is None or spec.loader is None:
        raise ValueError(f"the restated authentication cannot be loaded "
                         f"from {where}")
    restated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restated)
    authenticator = None
    if args.key_registry is not None:
        if args.attestations is None:
            raise ValueError("--key-registry without --attestations")
        doc = restated.strict_json(Path(args.key_registry).read_bytes(),
                                   "the key registry")
        authenticator = restated.RestatedAuthenticator(
            args.attestations, doc, allow_test_keys=args.allow_test_keys,
            require=None if args.require is None
            else frozenset(p for p in args.require.split(",") if p))
    roles = None
    if args.role_registry is not None:
        roles = restated.held_document(restated.strict_json(
            Path(args.role_registry).read_bytes(), "the role registry"))
    return authenticator, roles


def main() -> int:
    # Installed FIRST, before agent_substrate is touched at all, so the guard
    # is in place for every import this program will ever do.
    sys.meta_path.insert(0, _Refuse())

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("log", help="path to the event log to reconstruct")
    ap.add_argument("--root",
                    default=str(Path(__file__).resolve().parent.parent))
    # Assurance D: verify a decision's proof instead -- every field
    # re-derived here from the log on disk, every condition rechecked, the
    # proof's own claims compared and never used.
    ap.add_argument("--proof", default=None,
                    help="a decision-proof/1 JSON file to verify")
    ap.add_argument("--evidence", default=None,
                    help="the evidence store's directory, for the bytes a "
                         "decision cites")
    # The history's roots, for an authenticated or GOVERNED history (S2-2):
    # public documents, never a secret. They are read with the restated
    # authentication of tools/verify_assurance.py -- this process may not
    # import agent_substrate.principals -- and a GOVERNED history read
    # without them, or with others than it declares, reads nothing.
    ap.add_argument("--attestations", default=None,
                    help="the attestation file beside the log")
    ap.add_argument("--key-registry", default=None,
                    help="the key-registry document the history is read "
                         "under")
    ap.add_argument("--role-registry", default=None,
                    help="the role-registry document the history is read "
                         "under")
    ap.add_argument("--allow-test-keys", action="store_true",
                    help="admit TEST keys in the key registry")
    ap.add_argument("--require", default=None,
                    help="comma-separated principals the authenticator "
                         "asks; every actor when absent")
    args = ap.parse_args()

    if args.root not in sys.path:
        sys.path.insert(0, args.root)

    try:
        from agent_substrate.events import EventLog
        from agent_substrate.reconstruct import (
            reconstruct, reconstruct_subsystems, reconstruct_tasks,
            verify_decision_proof,
        )
    except ImportError as exc:
        # The guard firing is a defect in THIS program, and it is reported
        # as its own status so the caller does not read it as a log finding.
        print(json.dumps({"ok": False, "reason": f"import guard: {exc}"}))
        return 3

    path = Path(args.log)
    try:
        authenticator, roles = _roots(args)
        log = EventLog(path, authenticator=authenticator, roles=roles)
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

    if args.proof is not None:
        try:
            proof = json.loads(Path(args.proof).read_text(encoding="utf-8"))
            findings.extend(verify_decision_proof(
                log, proof, evidence=_FileEvidence(args.evidence)
                if args.evidence else None, authenticator=authenticator,
                roles=roles))
        except Exception as exc:                        # noqa: BLE001
            print(json.dumps({
                **result, "ok": False,
                "reason": f"proof verification raised: "
                          f"{type(exc).__name__}: {exc}"}))
            return 2
        result.update({
            "proof": str(args.proof), "findings": findings,
            "ok": not findings,
            "reason": ("the proof is the history's own account of a "
                       "decision that holds" if not findings else
                       f"{len(findings)} way(s) the proof or the decision "
                       "fails")})
        print(json.dumps(result, sort_keys=True))
        return 1 if findings else 0

    try:
        subs = reconstruct_subsystems(log, roles=roles)
        recs = reconstruct(log, authenticator=authenticator, roles=roles)
        tasks = reconstruct_tasks(log, roles=roles)
    except Exception as exc:                            # noqa: BLE001
        print(json.dumps({
            **result, "ok": False,
            "reason": f"reconstruction raised: {type(exc).__name__}: {exc}",
        }))
        return 2

    findings.extend(subs.anomalies)
    findings.extend(getattr(recs, "anomalies", ()) or ())
    findings.extend(getattr(tasks, "anomalies", ()) or ())
    # What each reader REFUSED is a finding too (S2-2). A transition the
    # machine would not take, or a record by a party that may not make it,
    # is kept out of the state and listed as unauthorized, not as an
    # anomaly; a log carrying one was reported clean here while the same
    # reader, in-process, refused it. ``unverifiable`` is not counted: it
    # says this process was given no evidence to decide an admission with,
    # which is not a fault of the log.
    findings.extend(getattr(recs, "unauthorized", ()) or ())
    findings.extend(getattr(tasks, "unauthorized", ()) or ())

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
