"""Two runs in one writable scope at once (D-2026-185).

The executor inventories a run's writable scope before and after it, and
charges every change it finds to the tool. Two governed runs of one
deployment share that scope: two agents on two objectives, say. Run at the
same time, each was charged with the other's declared outputs and refused
for writes it never made. In the measurement that found it, that happened
to 6 of 8 runs (tools/integration_demo.py, V7).

A run now holds its scope directories with an advisory lock from the first
inventory to the last, so another run in the scope waits for it. The
control removes the lock and sees the misattribution come back.
"""
from __future__ import annotations

import contextlib
import multiprocessing as mp
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_substrate.execution as X  # noqa: E402
from agent_substrate.execution import Limits, Outcome, run_bounded  # noqa: E402
from agent_substrate.tools import Determinism, Field_, ToolSpec  # noqa: E402

#: each tool writes its declared output, then lives long enough that two
#: started together overlap
TOOL = ("import sys, time\n"
        "open(f'out/{sys.argv[1]}.txt', 'w').write(sys.argv[1])\n"
        "time.sleep(1.0)\n")


def _spec() -> ToolSpec:
    return ToolSpec(tool_id="writer", version="1.0", summary="writes",
                    inputs=(Field_("n", "int"),),
                    determinism=Determinism.BYTE_IDENTICAL,
                    writable_scope=("out",), timeout_s=10.0)


def _racer(cwd: str, name: str, go, out, locked: bool) -> None:
    if not locked:                       # the control: no lock at all
        X.scopes_held = lambda cwd, scopes: contextlib.nullcontext()
    go.wait(30)
    r = run_bounded([sys.executable, "-c", TOOL, name], spec=_spec(),
                    cwd=Path(cwd), limits=Limits(wall_seconds=10.0),
                    env={"PATH": "/usr/bin:/bin"},
                    collect=(("result", f"out/{name}.txt", True),))
    out.put((name, r.outcome.value, list(r.undeclared_writes),
             r.started_wall, r.ended_wall))


def _race(tmp_path: Path, *, locked: bool) -> dict:
    (tmp_path / "out").mkdir()
    ctx = mp.get_context("fork")
    go, out = ctx.Event(), ctx.Queue()
    procs = [ctx.Process(target=_racer,
                         args=(str(tmp_path), n, go, out, locked))
             for n in ("a", "b")]
    for p in procs:
        p.start()
    go.set()
    got = {r[0]: r for r in (out.get(timeout=60) for _ in procs)}
    for p in procs:
        p.join(60)
    return got


def test_two_runs_in_one_scope_are_each_charged_with_their_own_writes(
        tmp_path):
    got = _race(tmp_path, locked=True)
    for name, outcome, undeclared, _s, _e in got.values():
        assert (outcome, undeclared) == (Outcome.COMPLETED.value, []), name
    # one after the other: the second waited for the first to finish
    first, second = sorted(got.values(), key=lambda r: r[3])
    assert second[3] >= first[4]


def test_control_without_the_lock_each_is_charged_with_the_others(tmp_path):
    got = _race(tmp_path, locked=False)
    charged = {n: u for n, _o, u, _s, _e in got.values() if u}
    assert charged, got
    for name, undeclared in charged.items():
        other = "b" if name == "a" else "a"
        assert any(f"out/{other}.txt" in u for u in undeclared), got
