"""PID 1 of a tool's namespaces: run the tool, say how it ended, and take
every process it left behind down with it (D-2026-149).

Started by :func:`agent_substrate.execution.run_bounded` as

    unshare --user --map-root-user --pid --fork --kill-child --net -- \\
        python -I -S _ns_init.py STATUS_FD -- TOOL ARGV...

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

Standard library only, started with -I -S: nothing on the host's path or
in the tool's environment decides how this runs.
"""
import os
import signal
import sys

_FORWARDED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def main(argv: list) -> int:
    if len(argv) < 4 or argv[2] != "--":
        os.write(2, b"usage: _ns_init.py STATUS_FD -- ARGV...\n")
        return 2
    status_fd = int(argv[1])
    tool = argv[3:]
    os.set_inheritable(status_fd, False)
    pid = os.fork()
    if pid == 0:
        for s in _FORWARDED:
            signal.signal(s, signal.SIG_DFL)
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
