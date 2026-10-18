"""PID 1 of a tool's namespaces: run the tool, say how it ended, and take
every process it left behind down with it (D-2026-149).

Started by :func:`agent_substrate.execution.run_bounded` as

    unshare --user --map-root-user --pid --fork --kill-child --net -- \\
        python -I -S _ns_init.py STATUS_FD [--confine MODE POLICY] -- TOOL...

WHY A SHIM. The first process in a PID namespace is its init. The kernel
does not deliver to an init a signal it has no handler for, so a tool
running as PID 1 would survive its own SIGTERM, and a tool killed by a
signal would be reported as an exit. This process is that init, so the
tool runs as an ordinary process with ordinary signal semantics.

WHAT IT GUARANTEES. When this process exits, the kernel kills every other
process in the namespace. That includes a grandchild that called setsid()
to leave the process group the executor signals; such a grandchild
outlived the timeout before (finding R-1). Orphans in the namespace are
reparented here and reaped.

HOW IT REPORTS. The tool's wait status is written to STATUS_FD as
``exit N`` or ``signal N``, because an init cannot re-raise a signal on
itself. The executor reads it, so a signal death is still a signal death.

WHAT IT CONFINES (D-2026-157). With ``--confine MODE POLICY`` the forked
child restricts itself with Landlock before it executes the tool, so the
tool and everything it starts can open only what POLICY grants, and proves
it by failing to open a probe path the policy does not grant. It reports ``confined ...`` first. If
confinement cannot be put in force, MODE decides: ``research`` reports
``unconfined REASON`` and runs the tool, so the record says so;
``high_assurance`` reports ``refused REASON`` and does not run it.

Standard library only, started with -I -S: nothing on the host's path or
in the tool's environment decides how this runs.
"""
import json
import os
import signal
import sys

_FORWARDED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
_MODES = ("research", "high_assurance")

# ---- filesystem confinement (D-2026-157, finding R-3) ----------------------
#
# Landlock, through the three system calls it is, from the standard library:
# this process runs with -I -S and imports nothing it was not built with. The
# numbers are the generic syscall numbers, shared by every architecture since
# Landlock was added (5.13). The ruleset is applied HERE, after the
# namespaces exist and before the tool is forked, so the tool and everything
# it starts -- a grandchild that calls setsid() included -- inherit it and
# cannot shed it.
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_EXECUTE, _WRITE_FILE, _READ_FILE, _READ_DIR = 1, 2, 4, 8
_TRUNCATE, _IOCTL_DEV = 1 << 14, 1 << 15
#: Landlock ABI version -> how many access bits it handles.
_ABI_ACCESS_BITS = {1: 13, 2: 14, 3: 15, 4: 15, 5: 16}
#: The rights a rule on a FILE may carry; the rest only make sense beneath a
#: directory, and the kernel refuses them on a file.
_FILE_RIGHTS = _EXECUTE | _WRITE_FILE | _READ_FILE | _TRUNCATE | _IOCTL_DEV
_READ_RIGHTS = _EXECUTE | _READ_FILE | _READ_DIR
_LIST_RIGHTS = _READ_DIR


class ConfinementError(Exception):
    """Confinement was asked for and is not in force."""


def _landlock(policy: dict) -> str:
    """Restrict this process and its descendants to ``policy``, then prove
    it: a path the policy does not grant must be refused to open.

    ``policy`` is ``{"read": [...], "list": [...], "write": [...],
    "probe": path}``. Returns what is in force, or raises
    :class:`ConfinementError` -- and the caller then does not run the tool.
    """
    import ctypes
    import stat
    import struct

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    def fail(what: str):
        err = ctypes.get_errno()
        raise ConfinementError(f"{what}: {os.strerror(err) if err else '?'}")

    abi = libc.syscall(_SYS_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0),
                       ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION))
    if abi < 1:
        fail("Landlock is not available")
    handled = (1 << _ABI_ACCESS_BITS.get(abi, 16)) - 1
    attr = ctypes.create_string_buffer(struct.pack("=Q", handled), 8)
    ruleset = libc.syscall(_SYS_LANDLOCK_CREATE_RULESET, attr,
                           ctypes.c_size_t(8), ctypes.c_uint32(0))
    if ruleset < 0:
        fail("cannot create a Landlock ruleset")
    try:
        grants = ([(p, _READ_RIGHTS) for p in policy.get("read", ())]
                  + [(p, _LIST_RIGHTS) for p in policy.get("list", ())]
                  + [(p, handled) for p in policy.get("write", ())])
        for path, rights in grants:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except FileNotFoundError:
                continue                  # a path that is not there grants nothing
            try:
                allowed = rights & handled
                if not stat.S_ISDIR(os.fstat(fd).st_mode):
                    allowed &= _FILE_RIGHTS
                rule = ctypes.create_string_buffer(
                    struct.pack("=Qi", allowed, fd), 12)
                if libc.syscall(_SYS_LANDLOCK_ADD_RULE, ctypes.c_int(ruleset),
                                ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
                                rule, ctypes.c_uint32(0)) != 0:
                    fail(f"cannot grant {path}")
            finally:
                os.close(fd)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            fail("cannot set no_new_privs")
        if libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset),
                        ctypes.c_uint32(0)) != 0:
            fail("cannot restrict this process")
    finally:
        os.close(ruleset)
    # THE PROOF. A restriction that reports success is a claim; an open the
    # kernel refuses is an observation. A probe that opens means the policy
    # granted it, or nothing was restricted -- either way the record would
    # say "confined" of a tool that is not, so the tool does not run.
    probe = policy.get("probe")
    if not probe:
        raise ConfinementError("no probe path: confinement cannot be shown")
    try:
        fd = os.open(probe, os.O_RDONLY | os.O_CLOEXEC)
    except PermissionError:
        return f"landlock abi {abi}"
    except OSError as exc:
        raise ConfinementError(f"the probe could not be tried: {exc}")
    os.close(fd)
    raise ConfinementError(
        f"{probe} was readable after restriction: the confinement this run "
        "would report is not in force")


def _report(fd: int, line: str) -> None:
    os.write(fd, (" ".join(line.split()) + "\n").encode())


def main(argv: list) -> int:
    confine = None
    if len(argv) >= 7 and argv[2] == "--confine" and argv[5] == "--":
        confine, rest = (argv[3], argv[4]), argv[6:]
    elif len(argv) >= 4 and argv[2] == "--":
        rest = argv[3:]
    else:
        rest = []
    if not rest or (confine is not None and confine[0] not in _MODES):
        os.write(2, b"usage: _ns_init.py STATUS_FD "
                    b"[--confine research|high_assurance POLICY] -- ARGV...\n")
        return 2
    status_fd = int(argv[1])
    tool = rest
    os.set_inheritable(status_fd, False)
    pid = os.fork()
    if pid == 0:
        for s in _FORWARDED:
            signal.signal(s, signal.SIG_DFL)
        if confine is not None:
            # IN THE CHILD, before exec: the tool keeps this process id, so
            # the policy's "/proc/self" names the tool's own process and no
            # other. Reported on the status descriptor, which exec closes.
            mode, policy = confine
            try:
                _report(status_fd,
                        f"confined {_landlock(json.loads(policy))}")
            except (ConfinementError, ValueError) as exc:
                if mode == "high_assurance":
                    _report(status_fd, f"refused {exc}")
                    os._exit(126)
                _report(status_fd, f"unconfined {exc}")
        try:
            os.execvp(tool[0], tool)
        except OSError as exc:
            os.write(2, f"cannot execute {tool[0]}: {exc}\n".encode())
        os._exit(127)

    def forward(signum, _frame):
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass

    for s in _FORWARDED:
        signal.signal(s, forward)
    status = 0
    while True:
        try:
            done, st = os.waitpid(-1, 0)    # an init reaps every orphan
        except ChildProcessError:
            break
        if done == pid:
            status = st
            break
    if os.WIFSIGNALED(status):
        num = os.WTERMSIG(status)
        os.write(status_fd, f"signal {num}\n".encode())
        return 128 + num
    code = os.WEXITSTATUS(status)
    os.write(status_fd, f"exit {code}\n".encode())
    return code


if __name__ == "__main__":
    os._exit(main(sys.argv))
