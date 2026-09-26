"""A wall-clock bound for tests whose failure mode is BLOCKING.

WHY THIS IS SHARED RATHER THAN COPIED

A test that hangs reports nothing. Under the mutation harness it is worse
than nothing: the mutation is recorded as "KILLED (TIMEOUT)", which counts as
a kill while saying nothing about which check was lost, and it costs the full
suite timeout of wall clock on every run. Two mutations were sitting in that
state -- an unbounded lock wait and a leaked lock descriptor -- and each was
burning 300 seconds per hosted run to report a result nobody could act on.

The guard turns "blocked" into a named failure in seconds. It lives here
rather than in one suite because the second copy is where the two drift, and
a deadline that quietly stopped firing would make every test that relies on
it pass for no reason at all.

Interrupting a blocked syscall works because the handler RAISES: PEP 475
retries a syscall interrupted by a signal only when the handler returns
normally.

LIMITS, STATED RATHER THAN ASSUMED

SIGALRM is delivered to the main thread of THIS process. It does not
interrupt a child process, and it does not interrupt a worker thread that is
itself blocked -- it makes the main thread's wait for that worker fail, which
is the observable this is for. Callers waiting on children should ALSO bound
their own join or pool call; this is the backstop, not the only bound.
"""
from __future__ import annotations

import contextlib
import signal

#: Long enough that a loaded machine never trips it, short enough that a
#: genuine hang is a test failure in seconds rather than a stalled CI job.
HANG_DEADLINE_S = 5.0

#: For tests that legitimately start several processes. Calibrated, not
#: guessed: the heaviest such test in this repository (four spawned writers,
#: fifteen appends each) takes 0.23s healthy, so this leaves two orders of
#: magnitude of headroom for a cold runner bringing up a spawn-based Pool,
#: while a genuinely blocked worker fails in half a minute instead of parking
#: the suite until an outer timeout notices.
PROCESS_DEADLINE_S = 30.0


class Hung(Exception):
    """The call under test did not return within its deadline."""


@contextlib.contextmanager
def deadline(seconds: float = HANG_DEADLINE_S):
    """Fail a test that blocks, instead of letting it stall the whole run."""
    def _fire(signum, frame):
        raise Hung(f"call did not return within {seconds}s")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
