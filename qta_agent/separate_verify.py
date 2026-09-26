"""Run the independent reconstruction in another PROCESS, and believe it.

WHAT THIS ADDS OVER THE SECOND READER

:mod:`qta_agent.reconstruct` is already a second implementation of every
authority rule. What it is not is a second *process*: it runs in this
interpreter, shares this module cache, and keeps its independence by
discipline. Discipline is a real defence and it is the kind that decays
silently -- one future edit importing the primary reducer "to remove the
duplication" turns the comparison circular while it goes on reporting
agreement.

So the same reader is also run somewhere that shortcut is unavailable:
``tools/independent_verify.py``, in a subprocess, reading the log from disk,
under an import guard that raises on every primary reducer. This module is
the caller.

THE RULES THIS MODULE KEEPS

* **A crashed verifier is not a pass.** Every non-zero exit, every timeout,
  every unparseable answer is a refusal. The status is reported so an
  operator can tell "the log has findings" from "the verifier died", and
  neither is success.
* **The verdict comes from the child, not from here.** This module does not
  re-derive anything; if it did, the separation would be decorative.
* **Bounded.** A verifier that hangs is a verifier that never says no, so it
  runs under a wall clock and a timeout is a failure like any other.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Wall-clock bound for one verification. Generous, because a long log is
#: legitimately slow, and finite, because a hang must not read as success.
DEFAULT_TIMEOUT_S = 300.0

#: Exit statuses the child defines. Anything else is a crash.
EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_UNREADABLE = 2
EXIT_IMPORT_GUARD = 3


class SeparateVerificationFailed(Exception):
    """The independent process did not confirm the log."""


@dataclass(frozen=True)
class SeparateVerification:
    """What another process concluded, and how it exited saying so."""

    ok: bool
    exit_status: int | None
    reason: str
    #: Anomalies the child reported. Empty on a clean log AND on a crash --
    #: which is why ``ok`` is the field to branch on, never this one.
    findings: tuple = ()
    head_seq: int = -1
    events_replayed: int = 0
    counts: dict = field(default_factory=dict)
    stderr_excerpt: str = ""

    def to_record(self) -> dict:
        return {"ok": self.ok, "exit_status": self.exit_status,
                "reason": self.reason, "findings": list(self.findings),
                "head_seq": self.head_seq,
                "events_replayed": self.events_replayed,
                "counts": dict(sorted(self.counts.items()))}

    def raise_if_bad(self) -> "SeparateVerification":
        if not self.ok:
            raise SeparateVerificationFailed(self.reason)
        return self


def verify_in_separate_process(
        log_path, *, root: Path | None = None,
        python: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S) -> SeparateVerification:
    """Reconstruct ``log_path`` in a child process. Never raises on a bad log.

    Returns a verdict rather than raising, because "the log has findings" is
    an ANSWER and turning it into an exception would discard the findings
    that were the reason for asking. :meth:`SeparateVerification.raise_if_bad`
    is there for callers that want the other shape.

    ``python`` selects the interpreter, so a caller can point this at a
    DIFFERENT one -- which is what makes "separate dependency set" testable
    rather than aspirational.
    """
    root = (Path(root) if root is not None
            else Path(__file__).resolve().parent.parent)
    script = root / "tools" / "independent_verify.py"
    if not script.is_file():
        return SeparateVerification(
            ok=False, exit_status=None,
            reason=f"the independent verifier is not at {script}; a verifier "
                   "that is missing has not agreed with anything")
    argv = [python or sys.executable, str(script), str(log_path),
            "--root", str(root)]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s,
            # No stdin: the child reads the log from disk and nothing else.
            stdin=subprocess.DEVNULL, cwd=str(root))
    except subprocess.TimeoutExpired:
        return SeparateVerification(
            ok=False, exit_status=None,
            reason=f"the independent verifier did not finish within "
                   f"{timeout_s}s. A verifier that hangs is one that never "
                   "says no, which is not the same as saying yes")
    except OSError as exc:
        return SeparateVerification(
            ok=False, exit_status=None,
            reason=f"the independent verifier could not be started: {exc}")

    err = (proc.stderr or "")[-2000:]
    try:
        doc = json.loads(proc.stdout or "")
    except ValueError:
        return SeparateVerification(
            ok=False, exit_status=proc.returncode,
            reason=(f"the independent verifier exited {proc.returncode} and "
                    "produced no readable answer; an unparseable verdict is "
                    "not a verdict"),
            stderr_excerpt=err)

    findings = tuple(doc.get("findings") or ())
    counts = {k: v for k, v in doc.items()
              if k in ("jobs", "capabilities", "services", "agents",
                       "memory", "records", "tasks")}

    if proc.returncode == EXIT_CLEAN and doc.get("ok"):
        return SeparateVerification(
            ok=True, exit_status=0,
            reason=str(doc.get("reason", "no anomalies")),
            head_seq=int(doc.get("head_seq", -1)),
            events_replayed=int(doc.get("events_replayed", 0)),
            counts=counts, stderr_excerpt=err)

    # EVERY other shape is a refusal. Spelled out one by one so the message
    # says which, because "the log is bad" and "the verifier is bad" send an
    # operator to different places.
    if proc.returncode == EXIT_FINDINGS:
        why = (f"the independent process found {len(findings)} anomaly(ies) "
               f"in the log: {list(findings[:3])}")
    elif proc.returncode == EXIT_UNREADABLE:
        why = f"the independent process could not read the log: " \
              f"{doc.get('reason')}"
    elif proc.returncode == EXIT_IMPORT_GUARD:
        why = (f"the independent verifier's import guard fired: "
               f"{doc.get('reason')}. It reached for the implementation it "
               "exists to check, so its agreement would have meant nothing")
    elif proc.returncode == EXIT_CLEAN:
        why = ("the independent process exited 0 while reporting ok=False; "
               "a status and a verdict that disagree are not evidence")
    else:
        why = (f"the independent verifier exited {proc.returncode}. A "
               "crashed verifier is not a pass")
    return SeparateVerification(
        ok=False, exit_status=proc.returncode, reason=why, findings=findings,
        head_seq=int(doc.get("head_seq", -1)),
        events_replayed=int(doc.get("events_replayed", 0)),
        counts=counts, stderr_excerpt=err)
