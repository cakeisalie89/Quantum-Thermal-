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
"""
from __future__ import annotations

import os
import resource
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .canonical import digest_bytes
from .capability import Action, CapabilityDenied, CapabilitySet, Request
from .tools import Registry, ToolSpec

#: How much of a failing tool's output to carry back for a human to read. A
#: failure whose cause is only a digest is a failure nobody can diagnose
#: without re-running it -- and a hosted run that cannot be re-run locally is
#: exactly the case where that matters.
EXCERPT_BYTES = 2000

#: Grace between asking a process group to stop and insisting. Long enough for
#: a well-behaved tool to flush and exit, short enough that a hung one does not
#: hold the executor open.
TERMINATE_GRACE_S = 5.0

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
RETRYABLE: frozenset = frozenset({Outcome.TIMED_OUT, Outcome.CANCELLED})


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

    def to_record(self) -> dict:
        return {"wall_seconds": self.wall_seconds,
                "cpu_seconds": self.cpu_seconds,
                "address_space_bytes": self.address_space_bytes,
                "output_bytes": self.output_bytes,
                "additional_tasks": self.additional_tasks,
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
    #: Digests of files the tool declared as outputs, if collected.
    output_digests: dict = field(default_factory=dict)
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
        return self.outcome in RETRYABLE

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
            "output_digests": dict(sorted(self.output_digests.items())),
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
        # No core dumps: a crash inside a sandboxed tool must not write a
        # multi-gigabyte image into a workspace that is size-governed.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return _preexec


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


def run_bounded(argv, *, spec: ToolSpec, cwd: Path, limits: Limits,
                env: dict | None = None,
                cancel: CancellationToken | None = None) -> ExecutionResult:
    """Run ``argv`` under kernel-enforced limits and classify how it ended.

    ``env`` REPLACES the environment rather than extending it. Inheriting the
    caller's environment is how a tool acquires credentials, proxies and paths
    nobody granted it -- the child gets exactly what is passed and nothing
    else.
    """
    started = time.time()
    base = dict(tool_id=spec.tool_id, tool_version=spec.version,
                tool_digest=spec.digest(), limits=limits.to_record(),
                determinism=spec.determinism.value,
                side_effect=spec.side_effect.value, started_wall=started)

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
    try:
        with out_path.open("wb") as fo, err_path.open("wb") as fe:
            proc = subprocess.Popen(
                list(argv), cwd=str(cwd), stdout=fo, stderr=fe,
                stdin=subprocess.DEVNULL, env=dict(env or {}),
                preexec_fn=_apply_limits(limits), close_fds=True)

            deadline = started + limits.wall_seconds
            while True:
                try:
                    proc.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    pass
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

        rc = proc.returncode
        if rc is not None and rc < 0:
            signal_number = -rc
        else:
            exit_status = rc

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

        out, out_size, out_cut = _read_capped(out_path, limits.output_bytes)
        err, err_size, err_cut = _read_capped(err_path, limits.output_bytes)
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
            reason=reason, **base)
    finally:
        if proc is not None and proc.poll() is None:  # pragma: no cover
            _kill_group(proc, signal.SIGKILL)
            proc.wait()
        for p in (out_path, err_path):
            p.unlink(missing_ok=True)
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
            write_paths: tuple = ()) -> ExecutionResult:
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
                reason=f"denied before execution: {exc}")

        spec.validate_inputs(inputs)
        eff = limits or Limits(wall_seconds=spec.timeout_s)
        return run_bounded(argv, spec=spec, cwd=Path(cwd or self.workspace),
                           limits=eff, env=env, cancel=cancel)
