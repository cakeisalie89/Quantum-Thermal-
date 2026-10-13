"""Whether a process that held something is still there.

WHY THIS EXISTS

A lease expires in SEQUENCE NUMBERS, which is what makes the queue
reproducible: replay the log and every deadline falls in the same place.
It has one consequence that only shows up after a crash. The thing that
advances the sequence is the log, the thing that writes the log is the
supervisor, and the supervisor is the thing that died -- so a lease held
by a dead process never lapses, and recovery can never fire.

Wall-clock expiry would fix that and break replay. The alternative is
EVIDENCE: ask the operating system whether the process that holds the
lease still exists. When the answer is a confident no, a recovering
supervisor may reclaim the work and say why. When the answer is anything
else, it may not.

A PID IS NOT AN IDENTITY

Pids are reused, and a reused pid is a different program wearing a dead
one's name. Three facts together are an identity strong enough to act on,
and each is cheap:

  boot id       from /proc/sys/kernel/random/boot_id. A machine that
                rebooted has a new one, so every pid recorded before the
                reboot is known-stale without inspecting anything.
  start ticks   field 22 of /proc/<pid>/stat, the process's start time in
                clock ticks since boot. Reuse gives a DIFFERENT start
                time, which is what distinguishes "still running" from
                "somebody else is now that number".
  pid           the number itself, which is only meaningful with the two
                above.

THREE ANSWERS, NOT TWO

``liveness`` returns ALIVE, GONE or UNKNOWN, and UNKNOWN is load-bearing:
a record from another host, or from a kernel that does not publish this,
is not evidence that the holder is dead. Treating "I cannot tell" as
"it is gone" is how two workers end up running one task, which is the
exact failure a lease exists to prevent.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

#: The process is running and is the same one that was recorded.
ALIVE = "ALIVE"
#: The process that was recorded no longer exists. Safe to reclaim from.
GONE = "GONE"
#: Not answerable here: another host, another boot, or no /proc.
UNKNOWN = "UNKNOWN"

_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def boot_id() -> str:
    """This boot's identifier, or '' when the platform does not offer one.

    Empty rather than a guess: a synthesised value would compare unequal
    across processes on the SAME boot, which would make every record look
    stale and turn every recovery into a reclaim.
    """
    try:
        with open(_BOOT_ID_PATH, encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:                                  # pragma: no cover
        return ""


def parse_stat_start_ticks(raw: str) -> int | None:
    """Field 22 of a /proc/<pid>/stat line, or None if it cannot be read.

    SEPARATE FROM THE FILE READ SO IT CAN BE TESTED, which matters here
    more than usual: the interesting input is a process whose NAME contains
    a parenthesis, and arranging for one of those to exist is far harder
    than handing this function the line such a process would produce.

    The comm field is field 2 and the kernel wraps it in parentheses
    WITHOUT escaping anything inside it. A process named ``foo) 1 2 3`` is
    legal, and splitting on whitespace -- or anchoring on the FIRST ')' --
    shifts every later field, so the start time is read from the wrong
    column. That does not fail loudly; it returns a plausible integer, and
    a live process then looks like a reused pid. Anchoring on the LAST ')'
    is what makes the field positions true again.
    """
    if not isinstance(raw, str):                     # pragma: no cover
        return None
    close = raw.rfind(")")
    if close == -1:
        return None
    rest = raw[close + 2:].split()
    # After comm, field 3 is state; start time is field 22, so index 19.
    if len(rest) < 20:
        return None
    try:
        return int(rest[19])
    except ValueError:
        return None


def start_ticks(pid: int) -> int | None:
    """Process start time in clock ticks since boot, or None."""
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8",
                  errors="replace") as fh:
            raw = fh.read()
    except (OSError, ValueError):
        return None
    return parse_stat_start_ticks(raw)


@dataclass(frozen=True)
class ProcessIdentity:
    """Enough to say whether a recorded process is still that process."""

    pid: int
    pgid: int | None = None
    #: Boot this pid was recorded on. Empty when unavailable.
    host_boot_id: str = ""
    #: Start time in clock ticks. None when unavailable.
    start_ticks: int | None = None

    def to_record(self) -> dict:
        return {"pid": self.pid, "pgid": self.pgid,
                "host_boot_id": self.host_boot_id,
                "start_ticks": self.start_ticks}

    @classmethod
    def from_record(cls, raw: object) -> "ProcessIdentity | None":
        if not isinstance(raw, dict):
            return None
        pid = raw.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool):
            return None
        pgid = raw.get("pgid")
        ticks = raw.get("start_ticks")
        return cls(pid=pid,
                   pgid=pgid if isinstance(pgid, int) else None,
                   host_boot_id=str(raw.get("host_boot_id") or ""),
                   start_ticks=ticks if isinstance(ticks, int) else None)


def identify(pid: int | None = None) -> ProcessIdentity:
    """Identity of ``pid``, defaulting to this process."""
    real = os.getpid() if pid is None else int(pid)
    try:
        pgid = os.getpgid(real)
    except OSError:                                  # pragma: no cover
        pgid = None
    return ProcessIdentity(pid=real, pgid=pgid, host_boot_id=boot_id(),
                           start_ticks=start_ticks(real))


def liveness(recorded: ProcessIdentity | None) -> str:
    """ALIVE, GONE or UNKNOWN for a previously recorded process.

    The order of the checks is the argument. Boot comes first because a
    reboot invalidates every pid at once and needs no per-process lookup.
    Start time comes last because it is the only thing that separates
    "still running" from "that number belongs to somebody else now".
    """
    if recorded is None:
        return UNKNOWN
    here = boot_id()
    if not here or not recorded.host_boot_id:
        # One side cannot say which boot it is from, so nothing about the
        # pid is safe to conclude.
        return UNKNOWN
    if here != recorded.host_boot_id:
        # A different boot of a machine cannot still be running a process
        # from the previous one. This also covers "another host", because
        # a boot id is per-machine-per-boot.
        return GONE
    now = start_ticks(recorded.pid)
    if now is None:
        # No such pid on a boot we KNOW is the same one: the process ended.
        return GONE
    if recorded.start_ticks is None:
        # The pid exists, but the record cannot say it is the same process.
        # Refusing here costs a stalled recovery; guessing costs two
        # workers on one task.
        return UNKNOWN
    return ALIVE if now == recorded.start_ticks else GONE
