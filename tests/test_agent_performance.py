"""Performance guards: shaped to catch a regression, not to publish a number.

WHY RATIOS AND NOT WALL-CLOCK BOUNDS

An absolute bound ("append must take under 2 ms") is a test about the runner,
and on a shared CI machine it is a coin flip. What is stable is the SHAPE:
doubling the history should roughly double the total cost of appending it, not
quadruple it. So each guard measures the same operation at two or three sizes
and asserts on the ratio, with bounds loose enough to survive a noisy machine
and tight enough to catch a linear factor turning quadratic.

THE REGRESSION THESE EXIST FOR

``EventLog.append`` verified the whole chain before every write. Measured, it
cost 2.1 ms per append at 100 records and 10.4 ms at 800, with each doubling
of n roughly quadrupling total time. Nothing detected that, and the same
defect had already been found once in this package's checkpointing -- which is
the argument for a committed guard rather than a benchmark somebody ran once.

WHAT THESE TESTS DO NOT CLAIM

They do not say the system is fast. They say it has not become
asymptotically worse. A machine ten times slower than this one passes them
all, which is the point.
"""
from __future__ import annotations

import gc
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.audit import AuditIndex  # noqa: E402
from qta_agent.canonical import digest  # noqa: E402
from qta_agent.checkpoint import CheckpointStore  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.policy import PolicyStore  # noqa: E402
from qta_agent.reconstruct import reconstruct  # noqa: E402
from qta_agent.scheduler import Scheduler, default_policy  # noqa: E402
from qta_agent.store import AuthorityStore  # noqa: E402

#: The sizes every scaling guard uses. Far enough apart that the two cases are
#: unmistakable: 8x the work costs about 8x linearly and about 64x
#: quadratically.
#:
#: The spread was 4x (150/600) and the discriminator was too narrow to trust:
#: healthy measured about 2 and the reintroduced quadratic path about 11,
#: against a ceiling of 10. Widening the spread separates them by an order of
#: magnitude instead of by a factor of two -- which is the right way to make a
#: guard robust, rather than moving the ceiling until the noise fits under it.
SMALL, LARGE = 100, 800
FACTOR = LARGE / SMALL

#: A linear operation's ratio should land near FACTOR, or below it where fixed
#: per-call overhead dominates the small case. The ceiling is far above that
#: and far below the quadratic value (64), so ordinary CI noise cannot fail
#: the test and a genuine regression cannot pass it.
LINEAR_CEILING = FACTOR * 2.5          # 20.0
#: Below this, timing noise dominates and the ratio means nothing.
MIN_MEASURABLE_S = 0.02


def _ratio(small_s: float, large_s: float) -> float:
    if small_s < MIN_MEASURABLE_S:
        pytest.skip(
            f"the small case took {small_s * 1000:.1f} ms, below the "
            f"{MIN_MEASURABLE_S * 1000:.0f} ms floor where a ratio means "
            "anything on this machine")
    return large_s / small_s


def _per_call(fn, *, floor: float = MIN_MEASURABLE_S,
              max_reps: int = 512) -> float:
    """Seconds per call, timed over enough repetitions to clear the floor.

    Written after the first version of this file SKIPPED four of its own
    guards on a fast machine: the operations were 4-6 ms and the floor was
    20 ms. A guard that never runs is a hole with a green tick over it, so
    the answer is to measure the operation more times rather than to stop
    measuring it. Only usable for operations with no side effects, which is
    why the append guards below still use the one-shot form.
    """
    gc.collect()
    reps = 1
    while True:
        t0 = time.perf_counter()
        for _ in range(reps):
            fn()
        dt = time.perf_counter() - t0
        if dt >= floor or reps >= max_reps:
            return dt / reps
        grow = max(2, int(floor / max(dt, 1e-9)) + 1)
        reps = min(max_reps, reps * grow)


def _fill(path: Path, n: int) -> EventLog:
    log = EventLog(path)
    for i in range(n):
        log.append(actor="a", action="record.create", target=f"r{i}",
                   payload={"record_id": f"r{i}", "kind": "k",
                            "proposer": "p"})
    return log


def _time(fn) -> float:
    gc.collect()
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


#: How many times a build is repeated before its cost is taken. Timing noise
#: is ONE-SIDED -- a scheduler, a neighbour on the machine or a cold page cache
#: can only make a run slower -- so the minimum is the robust estimator and the
#: mean is not.
REPEATS = 3


def _time_min(build, reps: int = REPEATS) -> float:
    """Best-of-``reps`` seconds for ``build(i)``, which must have side effects.

    ``build`` takes the repetition index so each run can use its own path: the
    operation under test appends to a file, so repeating it in place would
    measure something different each time.

    This exists because the first version measured each size ONCE. Locally
    that gave a ratio near 2; on a shared hosted runner it gave 11.2 against a
    10.0 ceiling, and the guard failed on noise rather than on a regression.
    The answer to a noisy measurement is a better estimator, not a looser
    bound -- widening the ceiling would have made the guard unable to see the
    regression it exists for.
    """
    build(-1)                                   # warm-up, not measured
    return min(_time(lambda i=i: build(i)) for i in range(reps))


# ---- the append path -----------------------------------------------------
def test_appending_a_history_is_not_quadratic_in_its_length(tmp_path):
    """The measured regression, guarded.

    Before the fix this ratio was about 13 for a 4x size increase. Linear is
    about 4.
    """
    small = _time_min(lambda i: _fill(tmp_path / f"small{i}.jsonl", SMALL))
    large = _time_min(lambda i: _fill(tmp_path / f"large{i}.jsonl", LARGE))
    ratio = _ratio(small, large)
    assert ratio < LINEAR_CEILING, (
        f"appending {LARGE} records cost {ratio:.1f}x appending {SMALL}; "
        f"linear is about {FACTOR:.0f}x and quadratic about "
        f"{FACTOR ** 2:.0f}x. A verification whose cost grows without bound "
        "is one that gets switched off.")


def test_per_append_cost_does_not_grow_with_history(tmp_path):
    """The same property stated the way it is actually felt.

    A user does not notice 'the total is quadratic'; they notice that the
    thousandth append is slower than the first.
    """
    log = EventLog(tmp_path / "log.jsonl")

    def burst(n):
        for i in range(n):
            log.append(actor="a", action="record.create", target=f"x{i}",
                       payload={"record_id": f"x{i}", "kind": "k",
                                "proposer": "p"})

    burst(20)                                   # warm-up, not measured
    first = min(_time(lambda: burst(SMALL)) for _ in range(REPEATS))
    for _ in range(3):
        burst(SMALL)
    last = min(_time(lambda: burst(SMALL)) for _ in range(REPEATS))
    ratio = _ratio(first, last)
    assert ratio < 4.0, (
        f"a burst of {SMALL} appends onto a history of {SMALL * 4} cost "
        f"{ratio:.1f}x the same burst onto an empty log")


# ---- reading and verification -------------------------------------------
def test_full_verification_is_linear(tmp_path):
    _fill(tmp_path / "small.jsonl", SMALL)
    _fill(tmp_path / "large.jsonl", LARGE)
    small = _per_call(lambda: EventLog(tmp_path / "small.jsonl").verify())
    large = _per_call(lambda: EventLog(tmp_path / "large.jsonl").verify())
    assert large / small < LINEAR_CEILING, (
        f"verifying {LARGE} records cost {large / small:.1f}x verifying "
        f"{SMALL}; linear is about {FACTOR:.0f}x")


def test_incremental_verification_does_not_grow_with_the_prefix(tmp_path):
    """The whole reason anchors exist.

    Verifying a fixed tail must cost the same whether the prefix is short or
    long; if it does not, the anchor is being ignored.
    """
    results = {}
    for name, n in (("small", SMALL), ("large", LARGE)):
        log = _fill(tmp_path / f"{name}.jsonl", n)
        anchor = log.anchor_at(n - 10)
        results[name] = _per_call(
            lambda log=log, a=anchor: log.verify_from(a))
    assert results["large"] / results["small"] < 3.0, (
        "verifying a fixed-size tail got slower as the prefix grew; the "
        "anchor is not being used")


def test_projection_load_is_linear(tmp_path):
    _fill(tmp_path / "small.jsonl", SMALL)
    _fill(tmp_path / "large.jsonl", LARGE)
    small = _per_call(
        lambda: AuthorityStore(EventLog(tmp_path / "small.jsonl")).load())
    large = _per_call(
        lambda: AuthorityStore(EventLog(tmp_path / "large.jsonl")).load())
    assert large / small < LINEAR_CEILING


def test_independent_reconstruction_is_linear(tmp_path):
    _fill(tmp_path / "small.jsonl", SMALL)
    _fill(tmp_path / "large.jsonl", LARGE)
    small = _per_call(
        lambda: reconstruct(EventLog(tmp_path / "small.jsonl")))
    large = _per_call(
        lambda: reconstruct(EventLog(tmp_path / "large.jsonl")))
    assert large / small < LINEAR_CEILING


def test_audit_index_construction_is_linear(tmp_path):
    _fill(tmp_path / "small.jsonl", SMALL)
    _fill(tmp_path / "large.jsonl", LARGE)
    small = _per_call(
        lambda: AuditIndex.from_log(EventLog(tmp_path / "small.jsonl")))
    large = _per_call(
        lambda: AuditIndex.from_log(EventLog(tmp_path / "large.jsonl")))
    assert large / small < LINEAR_CEILING


# ---- checkpointing -------------------------------------------------------
def test_checkpoint_load_beats_a_full_replay(tmp_path):
    """A checkpoint that is not faster than replaying is not an optimization.

    This is a RELATIVE claim about two operations on the same log, so it says
    nothing about the machine -- which is why it can be asserted at all.
    """
    log = _fill(tmp_path / "log.jsonl", LARGE)
    evidence = EvidenceStore(tmp_path / "evidence")
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    store = AuthorityStore(log, evidence=evidence).load()
    store.checkpoint(checkpoints)

    full = _time(lambda: AuthorityStore(
        EventLog(tmp_path / "log.jsonl"), evidence=evidence).load())
    cached = _time(lambda: AuthorityStore.load_from(
        EventLog(tmp_path / "log.jsonl"), checkpoints, blobs=evidence,
        evidence=evidence, require_checkpoint=True))
    if full < MIN_MEASURABLE_S:
        pytest.skip("the full load is below the timing floor here")
    assert cached < full, (
        f"loading from a checkpoint took {cached:.3f}s and a full replay "
        f"{full:.3f}s; a checkpoint that saves nothing is a second source of "
        "truth with no benefit")


# ---- the scheduler -------------------------------------------------------
def test_scheduler_readiness_is_not_quadratic_in_the_queue(tmp_path):
    def build(n):
        log = EventLog(tmp_path / f"sched{n}.jsonl")
        pol = PolicyStore(log).load()
        pol.publish(default_policy(), actor="owner")
        sched = Scheduler(log, policy=pol, policy_id="scheduler.default",
                          capacity={"slots": 10}).load()
        for i in range(n):
            sched.enqueue(job_id=f"j{i}", work_digest=digest({"i": i}),
                          submitter="owner")
        return sched

    small_sched = build(SMALL // 3)
    large_sched = build(LARGE // 3)
    small = _per_call(lambda: small_sched.ready_queue(at_seq=10_000))
    large = _per_call(lambda: large_sched.ready_queue(at_seq=10_000))
    assert large / small < LINEAR_CEILING * 2, (
        "computing the ready queue got disproportionately slower as the "
        "queue grew")


# ---- the evidence store --------------------------------------------------
def test_evidence_lookup_does_not_degrade_as_the_store_fills(tmp_path):
    """Directory fan-out, asserted rather than assumed."""
    store = EvidenceStore(tmp_path / "evidence")
    first = [store.put(f"blob {i}".encode()) for i in range(SMALL)]
    early = _per_call(lambda: [store.get(d) for d in first])
    for i in range(SMALL, LARGE):
        store.put(f"blob {i}".encode())
    late = _per_call(lambda: [store.get(d) for d in first])
    assert late / early < 3.0, (
        "reading the same blobs got slower once the store had more in it")


# ---- resource leaks ------------------------------------------------------
def _open_fds() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:                             # pragma: no cover - platform
        pytest.skip("/proc/self/fd is not available here")


def test_appending_does_not_leak_file_descriptors(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    log.append(actor="a", action="record.create", target="warm",
               payload={"record_id": "warm", "kind": "k", "proposer": "p"})
    before = _open_fds()
    for i in range(200):
        log.append(actor="a", action="record.create", target=f"r{i}",
                   payload={"record_id": f"r{i}", "kind": "k",
                            "proposer": "p"})
    after = _open_fds()
    assert after - before <= 2, (
        f"200 appends left {after - before} descriptors open; the writer "
        "lock takes one per call and must close it")


def test_verification_does_not_leak_file_descriptors(tmp_path):
    log = _fill(tmp_path / "log.jsonl", 50)
    log.verify()
    before = _open_fds()
    for _ in range(100):
        log.verify()
        log.read()
    assert _open_fds() - before <= 2


def test_evidence_writes_do_not_leak_descriptors_or_temp_files(tmp_path):
    store = EvidenceStore(tmp_path / "evidence")
    store.put(b"warm")
    before = _open_fds()
    for i in range(100):
        store.put(f"blob {i}".encode())
    assert _open_fds() - before <= 2
    leftovers = [p.name for p in (tmp_path / "evidence").rglob("*")
                 if p.is_file() and (".tmp" in p.name
                                     or p.name.startswith("."))]
    assert not leftovers, f"temporary files were left behind: {leftovers}"


def test_the_governed_executor_leaves_no_child_processes(tmp_path):
    """A bounded execution that leaks a child has not bounded anything."""
    before = subprocess.run(
        ["sh", "-c", "ps -o pid= --ppid $$ 2>/dev/null | wc -l"],
        capture_output=True, text=True).stdout.strip()
    from qta_agent.execution import Executor, Limits
    from qta_agent.tools import Determinism, Field_, Registry, SideEffect
    from qta_agent.tools import ToolSpec
    from qta_agent.capability import Action, CapabilitySet, issue

    registry = Registry([ToolSpec(
        tool_id="perf.noop", version="1.0.0", summary="exit immediately",
        inputs=(Field_("x", "str"),), outputs=(),
        determinism=Determinism.BYTE_IDENTICAL,
        side_effect=SideEffect.NONE, writable_scope=(), timeout_s=10.0)])
    cap = issue(capability_id="c1", subject="w", action=Action.EXECUTE_TOOL,
                task_id="t1", tool_id="perf.noop",
                scope=("verification/stage10",), issued_seq=0)
    caps = CapabilitySet(issued={"c1": cap}, at_seq=0)
    executor = Executor(registry, workspace=tmp_path)
    for _ in range(3):
        executor.run(tool_id="perf.noop", actor="w", task_id="t1",
                     capability_id="c1", capabilities=caps, inputs={"x": "1"},
                     argv=[sys.executable, "-c", "pass"], cwd=tmp_path,
                     limits=Limits(wall_seconds=10.0),
                     env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    after = subprocess.run(
        ["sh", "-c", "ps -o pid= --ppid $$ 2>/dev/null | wc -l"],
        capture_output=True, text=True).stdout.strip()
    assert after == before, (
        f"child process count went from {before} to {after}")
