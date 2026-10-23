"""Bounded execution: running a tool without inheriting the caller's authority.

WHY A SUBPROCESS AND NOT A FUNCTION CALL

An in-process call cannot be bounded. It shares the caller's memory, file
descriptors, environment, signal handlers and lifetime; a tool that spins
cannot be interrupted, one that allocates cannot be capped, and one that calls
``os._exit`` takes the governor down with it. Every limit this module applies
-- CPU, address space, output size, wall clock, process count -- is enforced by
the kernel against a process the caller does not share.

THE THREE OUTCOMES THAT ARE NOT SUCCESS

A great deal of the value here is in refusing to collapse these into "failed":

``TIMED_OUT``   the tool ran out of wall clock. It may have written a complete,
                correct output one instruction before the deadline, and it is
                still not a success -- nothing observed it finish, so nothing
                may treat its output as a finished thing.
``CANCELLED``   something asked it to stop, and it stopped. The distinction
                from failure matters because a cancelled task is retryable and
                a failed one may not be.
``DENIED``      it never ran. No capability authorized it, or the tool is not
                registered. This must never be reported as a failed run,
                because a failed run implies something was attempted.

A tool that exits 0 is ``COMPLETED``, which is a statement about the process
and not about the result. Whether the result is acceptable is a verification
question, answered elsewhere, deliberately by someone else.

OUTPUT IS CAPPED BY THE KERNEL, NOT BY A COUNTER

stdout and stderr go to files with ``RLIMIT_FSIZE`` set, so a runaway producer
is stopped by ``SIGXFSZ`` rather than by this process reading until it runs out
of memory. Reading a pipe with a counter caps what you KEEP, not what you must
first receive; the difference is the whole point when the producer is hostile.

PARTIAL OUTPUT IS EVIDENCE

When a tool is killed, whatever it had already written is captured and hashed.
A killed process that wrote half a file has told you something true about how
far it got, and discarding that in favour of a clean "failed" throws away the
only record of where it stopped.

DECLARED OUTPUTS ARE CHECKED, NOT SWEPT UP

When the contract names output files, they are resolved from the validated
inputs and hashed HERE, and a missing required one turns a zero exit into a
FAILED execution. Sweeping the working directory instead would let the tool
decide after the fact what its outputs were, and would make "produced
nothing" and "produced everything" the same observation.

Each declared path is resolved against the real filesystem and refused if it
lands outside the working directory -- a symlink is the way a well-formed
relative path reaches somewhere else, and this is the moment the difference
is observable.

RETRYING AN EXTERNAL EFFECT IS NOT THIS MODULE'S CALL TO MAKE, SO IT SAYS NO

``retryable`` is False for any tool declaring ``SideEffect.EXTERNAL``,
whatever the outcome. A timeout means nothing observed the tool finish, which
is precisely the case where it may already have changed state this system does
not own; running it again would repeat that. The contract's ``compensation``
travels in the record so an operator can act, and an operator acting is a
different thing from this module retrying.
"""
from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .canonical import digest_bytes
from .capability import Action, CapabilityDenied, CapabilitySet, Request
from .hostid import identify
from .tools import (Registry, SideEffect, ToolContractViolation,
                    ToolSpec)

#: How much of a failing tool's output to carry back for a human to read. A
#: failure whose cause is only a digest is a failure nobody can diagnose
#: without re-running it -- and a hosted run that cannot be re-run locally is
#: exactly the case where that matters.
EXCERPT_BYTES = 2000

#: Grace between asking a process group to stop and insisting. Long enough for
#: a well-behaved tool to flush and exit, short enough that a hung one does not
#: hold the executor open.
TERMINATE_GRACE_S = 5.0

#: How long the executor waits on a running tool before it looks at the
#: deadline, the idle bound and the cancellation token again. A tool that
#: exits inside one interval is not seen until it does -- which is why the
#: bound is checked again after exit (D-2026-78).
POLL_S = 0.05

#: Default caps. Every one is a refusal-to-guess: a tool that needs more says
#: so in its own limits, in a declaration somebody reviewed.
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_ADDRESS_SPACE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_CPU_SECONDS = 120
#: ADDITIONAL tasks the tool may create, over what the user already has.
#:
#: Not an absolute cap, because RLIMIT_NPROC cannot express one. It is a
#: PER-UID limit and on Linux it counts threads as well as processes, so an
#: absolute value means "this tool may run only if the machine happens to be
#: quiet" -- which is not a limit, it is a coin flip. It cost a hosted failure
#: to learn: at an absolute 64, OpenBLAS could not create its worker threads
#: on a runner whose user already held most of that budget, numpy's import
#: died, and the governed run reported SIGINT with no obvious cause.
#:
#: Bounding a process TREE is what cgroups are for. This is the honest
#: approximation available to a process that does not own a cgroup: measure
#: what the user is using and allow this much more.
DEFAULT_MAX_ADDITIONAL_TASKS = 512
#: Open file descriptors per process. RLIMIT_NOFILE was inherited from the
#: caller -- 20000 where it was measured -- so a tool could hold as many
#: descriptors as its supervisor (finding R-2).
DEFAULT_MAX_OPEN_FILES = 256

#: PID 1 of a tool's namespaces. See that file for why a shim and not the
#: tool itself.
_NS_INIT = Path(__file__).with_name("_ns_init.py")
#: Kernel namespaces a tool runs in when the host allows them unprivileged:
#: its own user namespace (to create the others without privilege), its own
#: PID namespace (so every process it starts dies with it, setsid() or
#: not), and its own network namespace (no interface but a loopback that
#: is down: no connection to anything, the host's loopback included).
_NAMESPACES = ("user", "pid", "net")
_isolation_probe: dict = {}


class ExecutionError(Exception):
    """Base class. Every failure here is fail-closed."""


class Outcome(str, Enum):
    """How an execution ended. Never a bare bool, never a bare exit code."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    DENIED = "DENIED"


#: Outcomes that may be treated as having produced a usable result. Encoded
#: once so that "did it work" cannot be re-decided differently at each call
#: site, and mutation-tested so that widening it is visible.
SUCCESSFUL: frozenset = frozenset({Outcome.COMPLETED})

#: Outcomes it is meaningful to retry. FAILED is absent on purpose: a tool
#: that ran and rejected its input will reject it again, and retrying is how a
#: deterministic failure becomes a load problem.
#:
#: NECESSARY, NOT SUFFICIENT. :attr:`ExecutionResult.retryable` also consults
#: the tool's declared side effect, because whether an attempt MAY be repeated
#: is a question about what it touched and not only about how it ended.
RETRYABLE: frozenset = frozenset({Outcome.TIMED_OUT, Outcome.CANCELLED})


#: Filesystem confinement modes. See :attr:`Limits.confine`.
CONFINEMENT_MODES = ("research", "high_assurance")

#: Outside the interpreter, what any dynamically linked program opens to
#: start. Files, granted read only.
_RUNTIME_FILES = ("/etc/ld.so.cache", "/dev/zero", "/dev/urandom",
                  "/proc/cpuinfo")
#: The system's shared-library directories, the dynamic loader among them.
#: Read and execute, never write. The kernel opens an ELF's loader FOR
#: EXECUTION, so without these not even the interpreter starts: an
#: interpreter installed outside /usr -- a uv-managed or standalone build,
#: as CI's second interpreter is -- was refused at exec ("cannot execute
#: ...: Permission denied", status 127) on every confined run. On a host
#: whose interpreter lives under /usr they were granted only by accident,
#: through ``sys.base_prefix``. A directory that does not exist grants
#: nothing.
_LIBRARY_DIRS = ("/lib", "/lib64", "/usr/lib", "/usr/lib64")
#: Opened for writing by anything that discards output -- a subprocess
#: started with ``stdin=DEVNULL`` opens it read-write -- and a write to it
#: reaches nothing. Granted read-only, ``ctypes.util.find_library`` failed
#: inside the confinement and the runtime identity came out UNRESOLVED.
_DISCARD = "/dev/null"
#: The tool's OWN process directory. Resolved by the confining child, whose
#: process id the tool keeps across exec, so it names that process and no
#: other: a run fingerprints its runtime from its own mappings and reads
#: nothing of any other process the host's /proc shows.
_PROC_SELF = "/proc/self"


def confinement_policy(*, cwd: Path, writable_scope: tuple,
                       read_paths: tuple, probe: Path,
                       scratch: Path | None = None,
                       outputs: tuple = ()) -> dict:
    """What a confined tool may open (D-2026-157).

    READ: the interpreter's trees, the system's shared-library directories
    and the runtime files every dynamically linked program needs (the loader
    and its cache), the Python packages directly under ``cwd`` (the tool's
    own code and what it imports), and whatever the caller grants in
    ``read_paths``. LIST: ``cwd`` itself, which the import system lists to
    find those packages -- names beneath it are visible, contents are not.
    WRITE: the tool's declared writable scope, the directory of each output
    its contract names (``outputs``, already resolved and refused by the
    contract if it would leave ``cwd``), and ``scratch``, a temporary
    directory private to this run (its ``TMPDIR``). Nothing else.

    Everything else is refused at ``open``: the rest of the working tree,
    the host's configuration, other users' files, other mounts. ``probe`` is
    a path none of this grants; the confinement is shown to be in force by
    the shim failing to open it.

    Landlock only grants. A supervisor store that lies beneath the writable
    scope -- an authority log kept inside the workspace -- is reachable by
    the tool; where the log is kept is a deployment decision, and the
    deployment contract says so.
    """
    cwd = Path(cwd)
    interp = sorted({sys.base_prefix, sys.prefix, sys.exec_prefix,
                     sys.base_exec_prefix})
    packages = sorted(str(d) for d in cwd.iterdir()
                      if d.is_dir() and (d / "__init__.py").is_file())
    granted = [str(p if Path(p).is_absolute() else cwd / p)
               for p in read_paths]
    return {"read": [*interp, *_LIBRARY_DIRS, *_RUNTIME_FILES, _PROC_SELF,
                     *packages, *granted],
            "list": [str(cwd)],
            "write": [str(cwd / s) for s in writable_scope]
            + sorted({str((cwd / rel).parent) for rel in outputs})
            + ([str(scratch)] if scratch is not None else []) + [_DISCARD],
            "probe": str(probe)}


@dataclass(frozen=True)
class Limits:
    """Kernel-enforced bounds. Every field has a default; none is unlimited."""

    wall_seconds: float = 60.0
    cpu_seconds: int = DEFAULT_MAX_CPU_SECONDS
    address_space_bytes: int = DEFAULT_MAX_ADDRESS_SPACE_BYTES
    output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    #: Additional tasks (processes AND threads) over current usage. See
    #: DEFAULT_MAX_ADDITIONAL_TASKS for why this cannot be absolute.
    additional_tasks: int = DEFAULT_MAX_ADDITIONAL_TASKS
    #: Seconds of NO OUTPUT before the run is abandoned, or 0 to rely on the
    #: wall bound alone.
    #:
    #: A wall bound alone answers "how long may this take", which is the
    #: wrong question for a tool that has stopped making progress: one that
    #: writes a byte a minute runs to the wall bound and consumes it, and one
    #: that deadlocks on the first second consumes it too. Idleness is the
    #: thing an operator actually wants bounded, and it is the only signal
    #: available here -- this executor sees output, not intent.
    idle_seconds: float = 0.0
    open_files: int = DEFAULT_MAX_OPEN_FILES
    #: Run the tool in its own user, PID and network namespaces when the
    #: host allows it. What was actually achieved is recorded per run in
    #: ``ExecutionResult.isolation``, never assumed from this flag.
    isolate: bool = True
    #: Filesystem confinement (D-2026-157). ``research``: confine where the
    #: host can, and where it cannot, run unconfined and record why.
    #: ``high_assurance``: confine, or do not run the tool. Neither mode
    #: downgrades without saying so in the execution record.
    confine: str = "research"
    #: Further paths the tool may READ, relative to its working directory or
    #: absolute. What a tool reads beyond its interpreter and the packages
    #: beside it is something somebody grants, not something it finds.
    read_paths: tuple = ()

    def __post_init__(self) -> None:
        if self.confine not in CONFINEMENT_MODES:
            raise ValueError(
                f"confine must be one of {CONFINEMENT_MODES}, not "
                f"{self.confine!r}")

    def to_record(self) -> dict:
        return {"wall_seconds": self.wall_seconds,
                "cpu_seconds": self.cpu_seconds,
                "address_space_bytes": self.address_space_bytes,
                "output_bytes": self.output_bytes,
                "additional_tasks": self.additional_tasks,
                "idle_seconds": self.idle_seconds,
                "open_files": self.open_files,
                "isolate": self.isolate,
                "confine": self.confine,
                "read_paths": list(self.read_paths),
                "nproc_baseline": count_user_tasks()}


@dataclass
class CancellationToken:
    """Cooperative cancellation, checked before and during a run.

    Cancelling before the process starts means it never starts, which is why
    this is checked at the top of :func:`run_bounded` as well as in the wait
    loop. A cancellation that only takes effect once the work is underway is a
    cancellation that cannot prevent the work.
    """

    _cancelled: bool = False
    reason: str = ""

    def cancel(self, reason: str = "cancelled by request") -> None:
        self._cancelled = True
        self.reason = reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled


@dataclass(frozen=True)
class ExecutionResult:
    """Everything a later reader needs to judge what happened, and no less."""

    outcome: Outcome
    tool_id: str
    tool_version: str
    tool_digest: str
    #: Real exit status. None when the process never ran or was signalled.
    exit_status: int | None = None
    #: Signal number when the process was killed by one. None otherwise.
    signal_number: int | None = None
    stdout_digest: str = ""
    stderr_digest: str = ""
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    started_wall: float = 0.0
    ended_wall: float = 0.0
    duration_s: float = 0.0
    limits: dict = field(default_factory=dict)
    determinism: str = ""
    side_effect: str = ""
    #: Why it ended this way, in a sentence an operator can act on.
    reason: str = ""
    #: The child's PID and process-group id, recorded because recovery by
    #: lease expiry reclaims the WORK and not the PROCESS. A supervisor that
    #: died mid-run leaves a child nothing else can name; with these an
    #: operator -- or a later supervisor -- can at least look for it and
    #: signal the group. They are DIAGNOSTIC: a pid is only meaningful on the
    #: host that produced it and only until it is reused, which is why the
    #: run's own start time is recorded beside them.
    pid: int | None = None
    pgid: int | None = None
    #: The child's full identity -- pid, pgid, boot id and start ticks --
    #: so a LATER supervisor can ask whether it is still that process
    #: rather than whether something now holds its number.
    child_process: dict = field(default_factory=dict)
    #: Digests of the files the contract declared as outputs, keyed by the
    #: contract's own name for each. Empty when the contract declared none --
    #: which is silence, not a claim that the tool wrote nothing.
    output_digests: dict = field(default_factory=dict)
    #: Where each declared output resolved to, relative to the working
    #: directory. Recorded beside the digests because a digest with no path
    #: says what was hashed and not what was hashed FROM, and the path is
    #: derived from the inputs rather than fixed by the contract.
    output_paths: dict = field(default_factory=dict)
    #: Declared outputs that were not collected, each with why. A missing
    #: REQUIRED output is what turns a zero exit into FAILED; an optional one
    #: is recorded and changes nothing.
    missing_outputs: dict = field(default_factory=dict)
    #: Files inside the working directory that appeared, changed or vanished
    #: and were never named by the contract. Declared-output collection sees
    #: only what the contract named; this is what sees the rest.
    undeclared_writes: tuple = ()
    #: The tool's declared compensating action, carried so that an operator
    #: reading a durable record of an EXTERNAL run that did not finish learns
    #: what to do from the record itself.
    compensation: str = ""
    #: What the KERNEL enforced for this run beyond rlimits and the session:
    #: ``{"namespaces": [...], "detail": ...}``. Empty namespaces means the
    #: host did not allow them, and the detail says why -- a run is never
    #: described as contained by more than it was (D-2026-149).
    isolation: dict = field(default_factory=dict)
    #: Bounded excerpts, for a human reading a failure. DELIBERATELY excluded
    #: from :meth:`to_record`, and therefore from the log and from
    #: ``result_digest``: the digests above are the provenance, and putting a
    #: tool's raw output into a hash-chained record makes anything it happened
    #: to print permanent and unremovable.
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""

    @property
    def succeeded(self) -> bool:
        """The ONE place 'did it work' is decided."""
        return self.outcome in SUCCESSFUL

    @property
    def retryable(self) -> bool:
        """Whether repeating this attempt is permitted, not merely sensible.

        Two questions, and both must pass. How it ended: a deterministic
        rejection will reject again. And what it touched: a tool declaring
        ``EXTERNAL`` may already have changed state this system does not own,
        and a TIMED_OUT run is exactly the case where nothing observed
        whether it did. Repeating that performs the external action a second
        time.

        The declared compensation does not unlock this. A written instruction
        for undoing an effect is not the effect having been undone, and
        treating it as one is how "we documented the rollback" becomes "we
        did it twice".
        """
        if self.outcome not in RETRYABLE:
            return False
        return self.side_effect != SideEffect.EXTERNAL.value

    def to_record(self) -> dict:
        return {
            "outcome": self.outcome.value, "tool_id": self.tool_id,
            "tool_version": self.tool_version, "tool_digest": self.tool_digest,
            "exit_status": self.exit_status,
            "signal_number": self.signal_number,
            "stdout_digest": self.stdout_digest,
            "stderr_digest": self.stderr_digest,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "started_wall": self.started_wall, "ended_wall": self.ended_wall,
            "duration_s": self.duration_s, "limits": dict(self.limits),
            "determinism": self.determinism, "side_effect": self.side_effect,
            "reason": self.reason,
            "pid": self.pid, "pgid": self.pgid,
            "child_process": dict(self.child_process),
            "output_digests": dict(sorted(self.output_digests.items())),
            "output_paths": dict(sorted(self.output_paths.items())),
            "missing_outputs": dict(sorted(self.missing_outputs.items())),
            "undeclared_writes": list(self.undeclared_writes),
            "compensation": self.compensation,
            "retryable": self.retryable,
            "isolation": dict(self.isolation),
        }


def count_user_tasks() -> int:
    """Processes owned by this real UID, as a floor on RLIMIT_NPROC usage.

    A floor rather than a count: RLIMIT_NPROC counts THREADS, and totalling
    every process's thread count would mean reading /proc/<pid>/status for
    each one on every spawn. The headroom above is sized so the difference
    does not matter, and being wrong in this direction only makes the limit
    more generous -- never falsely tight, which is the failure that matters.
    """
    uid = os.getuid()
    count = 0
    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid == uid:
                    count += 1
            except OSError:
                continue                       # exited between scan and stat
    except OSError:                            # pragma: no cover - platform
        return 0
    return count


def _apply_limits(limits: Limits):
    """Return a preexec_fn that puts the child in its own session and caps it.

    Runs between fork and exec in the child, so it must not allocate, log or
    take a lock. ``setsid`` first: without a process group of its own, killing
    the child leaves its children running, and an orphaned grandchild holding
    the output file is exactly the leak that makes timeouts unreliable.
    """
    # Measured in the PARENT: /proc cannot be scanned between fork and exec
    # without allocating, and preexec_fn must not allocate.
    nproc = count_user_tasks() + limits.additional_tasks

    def _preexec() -> None:                       # pragma: no cover - in child
        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU,
                           (limits.cpu_seconds, limits.cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS,
                           (limits.address_space_bytes,
                            limits.address_space_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE,
                           (limits.output_bytes, limits.output_bytes))
        resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
        resource.setrlimit(resource.RLIMIT_NOFILE,
                           (limits.open_files, limits.open_files))
        # No core dumps: a crash inside a sandboxed tool must not write a
        # multi-gigabyte image into a workspace that is size-governed.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return _preexec


def namespace_isolation() -> tuple:
    """``(argv prefix, record)`` for running a tool in its own user, PID and
    network namespaces, or ``((), record)`` saying why this host cannot.

    Probed once per process by running the real wrapper over ``true``: a
    host whose policy forbids unprivileged user namespaces (an AppArmor
    restriction, a container without the capability) answers here, not in
    the middle of a governed run.
    """
    if "result" not in _isolation_probe:
        unshare = shutil.which("unshare")
        prefix: tuple = ()
        if unshare is None:
            detail = "no unshare(1) on this host"
        else:
            cand = (unshare, "--user", "--map-root-user", "--pid", "--fork",
                    "--kill-child", "--net", "--")
            try:
                probe = subprocess.run(
                    [*cand, "true"], capture_output=True, timeout=10,
                    env={"PATH": "/usr/bin:/bin"})
                ok = probe.returncode == 0
                why = probe.stderr.decode(errors="replace").strip()[:200]
                detail = ("user, pid and net namespaces" if ok else
                          f"unshare refused: {why}")
            except (OSError, subprocess.TimeoutExpired) as exc:
                ok, detail = False, f"unshare did not run: {exc}"
            if ok:
                prefix = cand
        _isolation_probe["result"] = (prefix, detail)
    prefix, detail = _isolation_probe["result"]
    return prefix, {"namespaces": list(_NAMESPACES) if prefix else [],
                    "detail": detail}


def _kill_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the child's process group -- never our own.

    The guard is the important part. If ``setsid`` did not take effect, the
    child shares the CALLER's process group, and signalling that group kills
    the caller: the executor terminates the process that asked it to enforce a
    timeout. That is not hypothetical. It happened here, from a leftover
    mutation that replaced ``os.setsid()`` with ``pass``, and it presented as
    the test runner mysteriously dying with SIGTERM -- a symptom that looks
    like a sandbox or kernel problem and is neither.

    An executor that can kill its own caller has no containment property worth
    the name, so when the groups match we signal only the child and accept
    that its descendants may survive. Leaking a grandchild is bad; taking down
    the supervisor is worse, and unlike the leak it destroys the record of
    what happened.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None
    if pgid is not None and pgid != os.getpgrp():
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.send_signal(sig)
    except (ProcessLookupError, OSError):
        pass


def _read_capped(path: Path, cap: int) -> tuple:
    """Read at most ``cap`` bytes; report true size and whether cut."""
    try:
        size = path.stat().st_size
    except OSError:
        return b"", 0, False
    with path.open("rb") as fh:
        data = fh.read(cap)
    return data, size, size > cap


def _output_size(*paths) -> int:
    """Total bytes written so far. Missing files count as zero."""
    total = 0
    for p in paths:
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


#: Read in fixed-size blocks so a declared output that turns out to be large
#: is hashed rather than loaded. The executor may not assume a tool's output
#: fits in the supervisor's memory -- that assumption is the same mistake as
#: reading a hostile producer's pipe with a counter.
_HASH_BLOCK = 1024 * 1024


def _inventory(cwd: Path, scopes) -> dict:
    """``{relative path: (size, mtime_ns, inode)}`` under each writable scope.

    SCOPED, not whole-workspace, and both halves of that matter.

    Cost: the working directory is the repository root on the governed path,
    so walking it before and after every run would make a bounded execution
    proportional to how big the checkout is.

    Correctness: a tool may only write inside its declared scope, so that is
    the only place an undeclared write of ITS can appear. Anything changing
    elsewhere was somebody else -- the executor's own scratch directory, a
    concurrent process, the user's editor -- and reporting those as the
    tool's writes would be an accusation the evidence does not support.

    A tool declaring no writable scope is inventoried nowhere, which is
    correct: it claims to write nothing, and the write allowlist is what
    enforces that.

    Deliberately identity rather than content: hashing a workspace before and
    after every run would make the check cost proportional to what is there
    rather than to what changed, and the question being asked -- "did a file
    appear or change that the contract never named" -- is answerable from
    metadata. A tool that rewrites a file to identical bytes with an
    identical mtime has, for this purpose, not written it.

    Symlinks are recorded WITHOUT following them, so replacing a file with a
    link to somewhere else registers as a change rather than as whatever the
    target happens to look like.
    """
    out: dict = {}
    for scope in scopes:
        root = cwd / scope
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # os.walk follows directory symlinks by default, which would make
            # an inventory of a scope containing a link to / take as long as
            # the filesystem is large.
            dirnames[:] = [d for d in dirnames
                           if not os.path.islink(os.path.join(dirpath, d))]
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full, follow_symlinks=False)
                except OSError:                      # pragma: no cover
                    continue
                # Relative to CWD, not to the scope, so these are the same
                # strings the declared output paths are expressed in.
                rel = os.path.relpath(full, cwd)
                out[rel] = (st.st_size, st.st_mtime_ns, st.st_ino)
    return out


def _undeclared_writes(before: dict, after: dict, declared_paths,
                       ignore=()) -> tuple:
    """Files that appeared or changed and were never named by the contract.

    WHAT THIS IS FOR, AND WHAT IT IS NOT

    Declared-output collection sees only the files the CONTRACT named, so a
    tool that writes an UNDECLARED file inside its own scope was invisible to
    it. That is a different question from the write allowlist, which bounds
    WHERE a tool may write; this bounds whether what it wrote is what it said
    it would write.

    Both matter and neither substitutes for the other: the allowlist stops a
    tool touching somebody else's directory, and this notices a tool
    producing a file nobody reviewed inside its own. A run that quietly
    leaves an extra artifact behind is a run whose provenance record is
    incomplete, and an incomplete provenance record is the failure this whole
    subsystem exists to prevent.

    DELETIONS ARE REPORTED TOO. A tool that removes a file it did not declare
    has changed the workspace just as much as one that adds a file, and an
    inventory that only looked for additions would miss the more destructive
    half.

    ``ignore`` names the SUPERVISOR's own durable stores where they sit
    inside a tool's scope -- the authority log and its sidecars, the
    evidence store (paths relative to the working directory; a directory
    covers what is under it). Supervisors write them, under the writer lock,
    and no tool does; a write to them is judged by the hash chain, the head
    witness and the separate-process verification, which a sweep of sizes
    and mtimes could not improve on. Inventoried, every append a CONCURRENT
    supervisor made while a tool ran was charged to that tool, and the run
    refused for writes it never made (D-2026-118).

    An entry with a ``*`` is a PATTERN over names at its own depth -- the
    temp file the head witness is atomically replaced through, which
    another supervisor's append leaves beside the log for an instant
    (D-2026-123). Same depth, so a pattern never covers a subdirectory.
    """
    named = set(declared_paths)
    roots = tuple(str(x).rstrip("/") for x in ignore if "*" not in str(x))
    patterns = tuple(str(x) for x in ignore if "*" in str(x))

    def unattributable(rel: str) -> bool:
        return rel in named or any(
            rel == r or rel.startswith(r + "/") for r in roots) or any(
            fnmatch.fnmatchcase(rel, p) and rel.count("/") == p.count("/")
            for p in patterns)

    found = []
    for rel, stamp in sorted(after.items()):
        if unattributable(rel):
            continue
        if before.get(rel) != stamp:
            found.append(f"{rel}: {'changed' if rel in before else 'created'}")
    for rel in sorted(before):
        if rel not in after and not unattributable(rel):
            found.append(f"{rel}: deleted")
    return tuple(found)


def _collect_outputs(cwd: Path, declared: tuple) -> tuple:
    """Hash each declared output, or say precisely why it was not collected.

    ``declared`` is ``(name, relative path, required)`` triples already
    resolved from validated inputs by
    :meth:`~agent_substrate.tools.ToolSpec.resolve_outputs`, which refused any
    that walked upward as a string. This is the other half: the path is
    resolved against the REAL filesystem and refused if it lands outside
    ``cwd``, because a relative path with no ``..`` in it reaches anywhere at
    all once a symlink is involved, and a tool that wrote the symlink chose
    where.

    Returns ``(digests, paths, missing)``. Nothing raises: a collection
    problem is evidence about the run, and turning it into an exception would
    discard the outcome, the digests and the excerpts that were the reason for
    running anything.
    """
    digests: dict = {}
    paths: dict = {}
    missing: dict = {}
    try:
        root = Path(os.path.realpath(cwd))
    except OSError as exc:                           # pragma: no cover
        return {}, {}, {name: f"working directory unreadable: {exc}"
                        for name, _rel, _req in declared}
    for name, rel, _required in declared:
        paths[name] = rel
        target = cwd / rel
        try:
            real = Path(os.path.realpath(target))
        except OSError as exc:
            missing[name] = f"{rel}: path could not be resolved: {exc}"
            continue
        if real != root and root not in real.parents:
            # Not "missing" in the ordinary sense: the file may well exist.
            # It is outside the boundary, so it is not this run's output.
            missing[name] = (
                f"{rel}: resolves to {real}, outside the working directory. "
                "A declared output that leaves the workspace is refused "
                "rather than hashed -- the contract named a place, and this "
                "is not it.")
            continue
        try:
            st = os.stat(real, follow_symlinks=False)
        except OSError:
            missing[name] = f"{rel}: not produced"
            continue
        if not stat.S_ISREG(st.st_mode):
            missing[name] = (
                f"{rel}: is not a regular file. A declared output has to be "
                "bytes somebody can re-read; a device or a FIFO in its place "
                "would hang the reader instead of failing it.")
            continue
        try:
            h = hashlib.sha256()
            with open(real, "rb") as fh:
                for block in iter(lambda: fh.read(_HASH_BLOCK), b""):
                    h.update(block)
        except OSError as exc:
            missing[name] = f"{rel}: could not be read: {exc}"
            continue
        digests[name] = h.hexdigest()
    return digests, paths, missing


def run_bounded(argv, *, spec: ToolSpec, cwd: Path, limits: Limits,
                env: dict | None = None,
                cancel: CancellationToken | None = None,
                collect: tuple = (),
                ignore_writes: tuple = ()) -> ExecutionResult:
    """Run ``argv`` under kernel-enforced limits and classify how it ended.

    WHAT THE KERNEL ENFORCES HERE, AND WHAT IT DOES NOT. The limits are
    rlimits set between fork and exec: CPU, address space, output size,
    process count, core dumps. NETWORKING IS NOT AMONG THE RLIMITS. With
    ``Limits.isolate`` (the default) the tool runs in a network namespace
    of its own where the host allows unprivileged user namespaces, and then
    has no route to the host's interfaces, loopback included
    (tools/isolation_contract.py observes it, D-2026-176). Where the host
    refuses, the run's ``isolation`` record says so, and nothing in the
    kernel stands between the tool and the network. Egress is also governed
    by authority -- no grant is issued to a tool -- and by an in-process
    guard that patches ``socket.socket.connect`` in the PARENT interpreter;
    that guard is not containment and does not exist in a child process.
    Describing these bounds as denying network access is accurate only for
    a run whose record names the net namespace.

    ``env`` REPLACES the environment rather than extending it. Inheriting the
    caller's environment is how a tool acquires credentials, proxies and paths
    nobody granted it -- the child gets exactly what is passed and nothing
    else.

    ``collect`` is the contract's declared outputs, already resolved to
    ``(name, relative path, required)`` against validated inputs. Resolution
    happens in the caller rather than here because it is a contract question
    and this function's job is the process; passing triples keeps this
    testable without constructing a whole spec.

    ONE RUN AT A TIME IN A WRITABLE SCOPE (D-2026-185). The inventories
    before and after a run charge every change inside the scope to the tool.
    Two tools running at once in one scope would each be charged with the
    other's declared outputs, and both refused for writes they never made.
    So a run holds an exclusive lock on each of its scope directories from
    the first inventory to the last, and another run in the same scope
    waits. The lock is advisory (``flock`` on the directory, so no file
    appears in the scope), taken in a fixed order, and released when its
    holder exits, however it exits.
    """
    with scopes_held(Path(cwd), spec.writable_scope):
        return _bounded(argv, spec=spec, cwd=cwd, limits=limits, env=env,
                        cancel=cancel, collect=collect,
                        ignore_writes=ignore_writes)


#: this thread's held scopes: resolved directory -> [descriptor, depth]
_HELD = threading.local()


@contextlib.contextmanager
def scopes_held(cwd: Path, scopes):
    """An exclusive ``flock`` on each existing scope directory, in sorted
    order so that two holders naming the same scopes cannot deadlock.

    Reentrant within a thread. A supervisor holding a scope around work of
    its own in it (a re-execution's scratch directory) runs the tool inside
    without waiting on itself. Another thread, or another process, waits:
    each opens its own descriptor, and ``flock`` locks conflict between
    descriptors."""
    mine = getattr(_HELD, "scopes", None)
    if mine is None:
        mine = _HELD.scopes = {}
    taken: list = []
    try:
        for scope in sorted(set(scopes)):
            root = cwd / scope
            if not root.is_dir():
                continue
            key = str(root.resolve())
            if key not in mine:
                mine[key] = [os.open(root, os.O_RDONLY | os.O_DIRECTORY), 0]
                taken.append(key)
                fcntl.flock(mine[key][0], fcntl.LOCK_EX)
                mine[key][1] = 1
            else:
                taken.append(key)
                mine[key][1] += 1
        yield
    finally:
        for key in reversed(taken):
            entry = mine[key]
            entry[1] -= 1
            if entry[1] <= 0:
                os.close(entry[0])           # closing releases the flock
                del mine[key]


def _bounded(argv, *, spec: ToolSpec, cwd: Path, limits: Limits, env,
             cancel, collect, ignore_writes) -> ExecutionResult:
    """:func:`run_bounded`, with its scopes held."""
    started = time.time()
    # BEFORE anything runs, and before the early-return paths below: a
    # cancelled run wrote nothing, and comparing against an inventory taken
    # after the fact would call every pre-existing file an undeclared write.
    before_inventory = _inventory(Path(cwd), spec.writable_scope)
    base: dict[str, Any] = dict(
        tool_id=spec.tool_id, tool_version=spec.version,
        tool_digest=spec.digest(), limits=limits.to_record(),
        determinism=spec.determinism.value,
        side_effect=spec.side_effect.value,
        compensation=spec.compensation, started_wall=started)

    if cancel is not None and cancel.cancelled:
        # Checked before spawning: a cancellation that cannot prevent the work
        # is not a cancellation.
        now = time.time()
        return ExecutionResult(
            outcome=Outcome.CANCELLED, ended_wall=now,
            duration_s=now - started,
            reason=f"cancelled before the process started: {cancel.reason}",
            **base)

    out_dir = Path(tempfile.mkdtemp(prefix=".exec-", dir=str(cwd)))
    out_path, err_path = out_dir / "stdout", out_dir / "stderr"
    proc = None
    outcome, reason = Outcome.FAILED, ""
    exit_status = signal_number = None
    # IN ITS OWN NAMESPACES WHEN THE HOST ALLOWS IT (D-2026-149). The
    # session and rlimits below are cooperative-proof only up to a point: a
    # grandchild that calls setsid() leaves the group the timeout signals,
    # and nothing stopped a connection. In a PID namespace every process
    # the tool starts dies with its init; in a network namespace there is
    # nothing to connect to. What was achieved is recorded, not assumed.
    prefix, isolation = (namespace_isolation() if limits.isolate else
                         ((), {"namespaces": [],
                               "detail": "not requested (Limits.isolate)"}))
    isolation = dict(isolation)
    base["isolation"] = isolation
    # FILESYSTEM CONFINEMENT (D-2026-157, finding R-3). Applied by the
    # namespace's init before the tool is forked, so it binds the tool and
    # everything the tool starts. Where it cannot be applied, the mode
    # decides, and the record says which -- never a silent downgrade.
    probe = out_dir / "probe"
    probe.write_bytes(b"a path no confinement policy grants\n")
    scratch = out_dir / "tmp"
    scratch.mkdir()
    policy = confinement_policy(
        cwd=Path(cwd), writable_scope=tuple(spec.writable_scope),
        read_paths=tuple(limits.read_paths), probe=probe, scratch=scratch,
        outputs=tuple(rel for _name, rel, _required in collect))
    filesystem: dict[str, Any] = {"mode": limits.confine, "applied": None,
                                  "policy": {k: policy[k] for k in
                                             ("read", "list", "write")}}
    isolation["filesystem"] = filesystem
    if not prefix:
        filesystem["detail"] = (
            "no namespaces to confine within: " + isolation["detail"])
        if limits.confine == "high_assurance":
            shutil.rmtree(out_dir, ignore_errors=True)
            now = time.time()
            return ExecutionResult(
                outcome=Outcome.FAILED, ended_wall=now,
                duration_s=now - started,
                reason=("filesystem confinement is required (high_assurance) "
                        "and cannot be put in force: "
                        f"{filesystem['detail']}. The tool did not run."),
                **base)
    status_r = status_w = None
    full_argv = list(argv)
    run_env = dict(env or {})
    if prefix:
        status_r, status_w = os.pipe()
        full_argv = [*prefix, sys.executable, "-I", "-S", str(_NS_INIT),
                     str(status_w), "--confine", limits.confine,
                     json.dumps(policy, sort_keys=True), "--", *argv]
        # The one variable this adds to an environment it otherwise takes
        # as given: a temporary directory nobody else's run can see, since a
        # confined tool may write nowhere else. Recorded beside the policy.
        run_env["TMPDIR"] = str(scratch)
        filesystem["tmpdir"] = str(scratch)
    try:
        with out_path.open("wb") as fo, err_path.open("wb") as fe:
            proc = subprocess.Popen(
                full_argv, cwd=str(cwd), stdout=fo, stderr=fe,
                stdin=subprocess.DEVNULL, env=run_env,
                preexec_fn=_apply_limits(limits), close_fds=True,
                pass_fds=(status_w,) if status_w is not None else ())
            if status_w is not None:
                os.close(status_w)
                status_w = None

            # Recorded the moment it exists, so a supervisor that dies in
            # the next statement still leaves the child's identity behind.
            #
            # The full identity, not a bare pid. A pid alone cannot answer
            # "is that still the process I started" -- pids are reused, and
            # a later supervisor acting on a reused one would signal an
            # innocent program. Boot id and start time are what make the
            # number mean something after the process that recorded it is
            # gone. See agent_substrate.hostid.
            child = identify(proc.pid)
            base["pid"] = child.pid
            base["pgid"] = child.pgid
            base["child_process"] = child.to_record()

            deadline = started + limits.wall_seconds
            last_progress = started
            last_size = 0
            while True:
                try:
                    proc.wait(timeout=POLL_S)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if limits.idle_seconds > 0:
                    # Progress is measured as output, because that is the only
                    # signal this executor has. A tool that is thinking hard
                    # and silently looks identical to one that has deadlocked,
                    # and the wall bound is what covers the first case.
                    size = _output_size(out_path, err_path)
                    now = time.time()
                    if size != last_size:
                        last_size, last_progress = size, now
                    elif now - last_progress >= limits.idle_seconds:
                        _kill_group(proc, signal.SIGTERM)
                        try:
                            proc.wait(timeout=TERMINATE_GRACE_S)
                        except subprocess.TimeoutExpired:
                            _kill_group(proc, signal.SIGKILL)
                            proc.wait()
                        outcome = Outcome.TIMED_OUT
                        reason = (
                            f"no output for {limits.idle_seconds:g}s; "
                            "abandoned as idle rather than held until the "
                            f"{limits.wall_seconds:g}s wall bound")
                        break
                if cancel is not None and cancel.cancelled:
                    _kill_group(proc, signal.SIGTERM)
                    try:
                        proc.wait(timeout=TERMINATE_GRACE_S)
                    except subprocess.TimeoutExpired:
                        _kill_group(proc, signal.SIGKILL)
                        proc.wait()
                    outcome = Outcome.CANCELLED
                    reason = f"cancelled while running: {cancel.reason}"
                    break
                if time.time() >= deadline:
                    # Terminate the GROUP, then insist. A tool that ignores
                    # SIGTERM does not thereby get to run forever.
                    _kill_group(proc, signal.SIGTERM)
                    try:
                        proc.wait(timeout=TERMINATE_GRACE_S)
                    except subprocess.TimeoutExpired:
                        _kill_group(proc, signal.SIGKILL)
                        proc.wait()
                    outcome = Outcome.TIMED_OUT
                    reason = (
                        f"exceeded its {limits.wall_seconds}s wall-clock "
                        "bound. A timeout is not a failure of the tool and is "
                        "not a success either: nothing observed it finish.")
                    break

        exited = time.time()
        rc = proc.returncode
        if rc is not None and rc < 0:
            signal_number = -rc
        else:
            exit_status = rc
        refused = None
        if status_r is not None:
            # What the namespace's init reported, a line at a time: the
            # confinement it put in force (or why not), then the tool's own
            # wait status -- an init cannot re-raise a signal, so a signal
            # death arrives here.
            chunks = []
            while True:
                chunk = os.read(status_r, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
            for line in b"".join(chunks).decode(
                    errors="replace").splitlines():
                word, _, rest = line.partition(" ")
                if word == "confined":
                    filesystem["applied"] = rest
                elif word in ("unconfined", "refused"):
                    filesystem["detail"] = rest
                    if word == "refused":
                        refused = rest
                elif word in ("signal", "exit") and rest.isdigit():
                    if word == "signal":
                        signal_number, exit_status = int(rest), None
                    else:
                        signal_number, exit_status = None, int(rest)
        if filesystem["applied"] is None and "detail" not in filesystem:
            filesystem["detail"] = "the init reported no confinement"

        # THE BOUND IS CHECKED AFTER EXIT TOO (D-2026-78). The loop above
        # looks at the deadline only when a POLL_S wait times out, so a
        # process that exited past its deadline but inside one poll interval
        # came back COMPLETED: a run that outlived its declared bound,
        # accepted. It stayed hidden while the tools run here were slow to
        # start. Narrow on purpose -- only a run that would otherwise be
        # COMPLETED is reclassified; a failure keeps its own reason.
        if (outcome not in (Outcome.TIMED_OUT, Outcome.CANCELLED)
                and signal_number is None and exit_status == 0
                and exited - started > limits.wall_seconds):
            outcome = Outcome.TIMED_OUT
            reason = (f"exited after {exited - started:.3f}s, past its "
                      f"{limits.wall_seconds:g}s wall-clock bound. The bound "
                      "is the contract; a run that outlived it is not a "
                      "completed run, and not a success either.")

        if outcome not in (Outcome.TIMED_OUT, Outcome.CANCELLED):
            if signal_number is not None:
                outcome = Outcome.FAILED
                named = signal.Signals(signal_number).name \
                    if signal_number in {s.value for s in signal.Signals} \
                    else str(signal_number)
                reason = (
                    f"killed by {named}. SIGXFSZ means it hit the output cap; "
                    "SIGKILL with no timeout usually means the address-space "
                    "limit.")
            elif exit_status == 0:
                outcome = Outcome.COMPLETED
                reason = ("exited 0. This describes the PROCESS, not the "
                          "result; whether the output is acceptable is a "
                          "verification question answered elsewhere.")
            else:
                outcome = Outcome.FAILED
                reason = f"exited with status {exit_status}"
        if refused is not None:
            # Whatever the status says: the init refused before forking,
            # and the reason is the confinement, not the tool.
            outcome = Outcome.FAILED
            reason = ("filesystem confinement is required "
                      "(high_assurance) and was not in force: "
                      f"{refused}. The tool did not run.")

        out, out_size, out_cut = _read_capped(out_path, limits.output_bytes)
        err, err_size, err_cut = _read_capped(err_path, limits.output_bytes)

        # Collected whatever the outcome: a killed tool's half-written
        # declared output is evidence of where it got to, on exactly the
        # reasoning that keeps partial stdout. Only the VERDICT below is
        # conditional -- a run that was already not a success is not made
        # worse by an output it never had the chance to write.
        out_digests, out_paths, missing = _collect_outputs(
            Path(cwd), tuple(collect))
        undeclared = _undeclared_writes(
            before_inventory, _inventory(Path(cwd), spec.writable_scope),
            [rel for _n, rel, _r in collect], ignore_writes)
        if outcome is Outcome.COMPLETED:
            unmet = [n for n, _rel, req in collect if req and n in missing]
            if unmet:
                outcome = Outcome.FAILED
                detail = "; ".join(f"{n}: {missing[n]}" for n in unmet)
                reason = (
                    f"exited 0 without producing {len(unmet)} declared "
                    f"output(s) -- {detail}. A zero exit describes the "
                    "process; the contract describes what the process was "
                    "for, and this run did not satisfy it.")
        ended = time.time()

        def _excerpt(raw: bytes) -> str:
            text = raw.decode("utf-8", errors="replace")
            if len(text) <= EXCERPT_BYTES:
                return text
            half = EXCERPT_BYTES // 2
            return f"{text[:half]}\n...[{len(text) - EXCERPT_BYTES} chars " \
                   f"elided]...\n{text[-half:]}"

        return ExecutionResult(
            stdout_excerpt=_excerpt(out), stderr_excerpt=_excerpt(err),
            outcome=outcome, exit_status=exit_status,
            signal_number=signal_number,
            stdout_digest=digest_bytes(out), stderr_digest=digest_bytes(err),
            stdout_bytes=out_size, stderr_bytes=err_size,
            stdout_truncated=out_cut, stderr_truncated=err_cut,
            ended_wall=ended, duration_s=ended - started,
            output_digests=out_digests, output_paths=out_paths,
            missing_outputs=missing, undeclared_writes=undeclared,
            reason=reason, **base)
    finally:
        if proc is not None and proc.poll() is None:  # pragma: no cover
            _kill_group(proc, signal.SIGKILL)
            proc.wait()
        for fd in (status_r, status_w):
            if fd is not None:
                os.close(fd)
        for p in (out_path, err_path, probe):
            p.unlink(missing_ok=True)
        shutil.rmtree(scratch, ignore_errors=True)
        try:
            out_dir.rmdir()
        except OSError:                              # pragma: no cover
            pass


class Executor:
    """Authorizes, then runs. Never one without the other."""

    def __init__(self, registry: Registry, *, workspace: Path):
        self.registry = registry
        self.workspace = Path(workspace)

    def run(self, *, tool_id: str, actor: str, task_id: str,
            capability_id: str, capabilities: CapabilitySet,
            inputs: dict, argv, cwd: Path | None = None,
            limits: Limits | None = None, env: dict | None = None,
            cancel: CancellationToken | None = None,
            write_paths: tuple = (),
            ignore_writes: tuple = ()) -> ExecutionResult:
        """Run a registered tool under an explicit grant.

        The order is the point. Registration is checked before the capability,
        because "that tool does not exist" and "you may not run that tool" are
        different answers and conflating them tells a prober which tools are
        real. The capability is checked before the inputs, because validating
        an unauthorized request wastes work on a request that will not run.
        Nothing is executed until all three have passed.
        """
        spec = self.registry.get(tool_id)           # default deny

        paths = tuple(write_paths) or tuple(spec.writable_scope)
        try:
            capabilities.check(capability_id, Request(
                actor=actor, action=Action.EXECUTE_TOOL, task_id=task_id,
                tool_id=tool_id, paths=paths))
        except CapabilityDenied as exc:
            now = time.time()
            return ExecutionResult(
                outcome=Outcome.DENIED, tool_id=tool_id,
                tool_version=spec.version, tool_digest=spec.digest(),
                started_wall=now, ended_wall=now,
                determinism=spec.determinism.value,
                side_effect=spec.side_effect.value,
                compensation=spec.compensation,
                reason=f"denied before execution: {exc}")

        spec.validate_inputs(inputs)
        # Resolved AFTER validation and BEFORE the run: the templates are
        # over inputs, so resolving unvalidated ones would be formatting
        # whatever arrived, and resolving afterwards would mean a contract
        # that cannot be satisfied is discovered only once the work is done.
        #
        # A refusal here RETURNS rather than raises, unlike validate_inputs
        # above, and the difference is deliberate. Malformed inputs are the
        # caller's own bug and should be loud where they were built. A
        # well-typed input that steers a declared output out of the workspace
        # is not a bug, it is the request an attacker sends -- and if that
        # one escaped as an exception it would be the single most
        # security-relevant refusal that never reached the scheduler, never
        # got classified, and left the job's lease to lapse into a retry of
        # an input that can only ever be refused again.
        #
        # DENIED, because nothing was attempted. A failed run implies
        # something ran.
        try:
            collect = spec.resolve_outputs(inputs)
        except ToolContractViolation as exc:
            now = time.time()
            return ExecutionResult(
                outcome=Outcome.DENIED, tool_id=tool_id,
                tool_version=spec.version, tool_digest=spec.digest(),
                started_wall=now, ended_wall=now,
                determinism=spec.determinism.value,
                side_effect=spec.side_effect.value,
                compensation=spec.compensation,
                reason=f"denied before execution: {exc}")
        eff = limits or Limits(wall_seconds=spec.timeout_s)
        return run_bounded(argv, spec=spec, cwd=Path(cwd or self.workspace),
                           limits=eff, env=env, cancel=cancel,
                           collect=collect, ignore_writes=ignore_writes)
