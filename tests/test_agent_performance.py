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
asymptotically worse. A machine ten times UNIFORMLY slower than this one
passes them all, which is the point.

AND WHERE THAT STOPS BEING TRUE, WHICH IS NOT A DETAIL

Uniformly is the load-bearing word, and it was missing here until a hosted
runner removed it. A shared machine is not uniformly slow: it is slow in
bursts, and the two measurements a ratio is built from are taken at
different moments. A guard comparing two SIZES survives that, because an 8x
spread puts healthy (about 8) and quadratic (about 64) an order of magnitude
apart and no amount of CPU steal moves a measurement across that gap. A
guard comparing the SAME size at two different TIMES has no such separation,
and `test_per_append_cost_does_not_grow_with_history` failed a hosted run at
4.74 against a 4.0 ceiling while the property was intact -- measured 0.92 to
1.05 here immediately afterwards.

The answer was not a looser bound. That guard now COUNTS the records the
append path re-hashes instead of timing it, which is strictly stricter (it
asserts equality where the old one allowed 4x) and cannot be moved by a busy
machine at all. D-2026-37, and the same move D-2026-32 made one file over.
"""
from __future__ import annotations

import gc
import json
import os
import shutil
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
from qta_agent.events import Event as _Event  # noqa: E402
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

#: THREE sizes, for the guards that can afford them.
#:
#: Two points fit any line. A ratio under a ceiling says "not catastrophic"
#: and cannot tell linear-with-a-large-constant from mildly superlinear,
#: because a single ratio has no shape. Three points have a slope, and the
#: slope is the thing being claimed: 1 is linear, 2 is quadratic, and an
#: operation drifting towards 1.5 is a regression a ceiling would pass for
#: years.
CURVE_SIZES = (100, 400, 1600)

#: Least-squares slope of log(time) against log(size) that a linear
#: operation may reach. Linear is 1.0; fixed per-call overhead pulls the
#: measured slope BELOW 1 at these sizes, and noise pushes it around. 1.45
#: sits well above anything linear has produced here and far below the 2.0
#: of a quadratic path -- the same discriminator-width argument as the size
#: spread above.
EXPONENT_CEILING = 1.45





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
        if dt >= floor:
            return dt / reps
        if reps >= max_reps:
            # THE ONE HONEST SKIP. Not "this call was fast" -- fast is what
            # repetition is for -- but "even max_reps of it did not add up
            # to something this clock can resolve". A number derived from
            # that is noise wearing a unit.
            pytest.skip(
                f"{max_reps} repetitions totalled {dt * 1000:.1f} ms, below "
                f"the {floor * 1000:.0f} ms this machine can resolve")
        grow = max(2, int(floor / max(dt, 1e-9)) + 1)
        reps = min(max_reps, reps * grow)


#: Where a run writes the numbers it measured, when asked to.
#:
#: A guard that only ever says pass or fail cannot show a DRIFT: an
#: operation creeping from n^1.0 to n^1.3 over twenty commits passes every
#: run and is a different system by the end. tools/performance_baseline.py
#: sets this, collects what the guards measured, and appends the values to
#: docs/performance_baseline.json against the commit they came from.
_PERF_OUT = os.environ.get("QTA_PERF_OUT")


def _record(name: str, value: float, ceiling: float) -> None:
    """Publish one measurement, if this run was asked to collect them."""
    if not _PERF_OUT:
        return
    with open(_PERF_OUT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"guard": name, "value": round(value, 4),
                             "ceiling": ceiling}) + "\n")


def _exponent(sizes, times) -> float:
    """The slope of log(time) vs log(size). 1.0 linear, 2.0 quadratic.

    Ordinary least squares over three points. Not a statistical claim --
    three noisy samples support none -- but a shape claim, and the shape is
    what separates "this got slower" from "this got slower in a way that
    will not stop".

    The measurements come from :func:`_per_call`, which repeats until the
    aggregate clears the clock's resolution and skips when even the
    repetition cap cannot -- so there is no second noise check here. Adding
    one on the PER-CALL figure was wrong and skipped all four of these
    guards on the first run: a 1.7 ms operation measured over 512 calls is
    a good estimate, not an unmeasurable one.
    """
    import math

    xs = [math.log(n) for n in sizes]
    ys = [math.log(t) for t in times]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den


def _curve(build, sizes=CURVE_SIZES) -> tuple:
    """Measure ``build(n)`` at each size and return (sizes, times)."""
    return tuple(sizes), [_per_call(build(n)) for n in sizes]


def _fill(path: Path, n: int) -> EventLog:
    log = EventLog(path)
    for i in range(n):
        log.append(actor="p", action="record.create", target=f"r{i}",
                   payload={"record_id": f"r{i}", "kind": "k",
                            "proposer": "p"})
    return log










def assert_linear_in_records(name, small_count, large_count):
    """Linearity as an EQUALITY, not a ratio under a ceiling.

    If the work is linear in the number of records, then going from SMALL to
    LARGE records costs exactly LARGE - SMALL more units of work. That is a
    sharper statement than "the ratio is under 20", it needs no tolerance,
    and no busy machine can move it. Quadratic growth misses it by orders of
    magnitude rather than by a factor a scheduler could supply.

    Measured for every guard converted in D-2026-46, and identical in all
    five: 700 = 800 - 100.

        append                  99 -> 799
        verify()               100 -> 800
        AuthorityStore.load()  101 -> 801
        reconstruct()          100 -> 800
        AuditIndex.from_log()  100 -> 800
    """
    grew = large_count - small_count
    assert grew == LARGE - SMALL, (
        f"{name}: going from {SMALL} to {LARGE} records cost "
        f"{grew} extra re-hashes, not {LARGE - SMALL}. Linear means the "
        f"extra work IS the extra records; quadratic here would be about "
        f"{(LARGE ** 2 - SMALL ** 2) // 2}. Counts: {small_count} -> "
        f"{large_count}")


# ---- the append path -----------------------------------------------------
def test_appending_a_history_is_not_quadratic_in_its_length(tmp_path):
    """The measured regression, guarded.

    Before the fix this ratio was about 13 for a 4x size increase. Linear is
    about 4.

    WHY THIS COUNTS AND NO LONGER TIMES (D-2026-46)

    It asserted `ratio < 20.0` on wall time, and a hosted runner failed it at
    **37.1** while the property was intact -- measured here immediately
    afterwards at 8.07, which is linear to three significant figures.

    D-2026-37 converted the sibling guard below to counting and left this one
    timed, with a stated reason:

        "Every other ratio guard compares two SIZES with an 8x spread, so
         healthy reads about 8 and quadratic about 64 and a busy runner
         cannot move a measurement across that gap."

    A busy runner moved it to 37.1. The argument was reasonable and it was
    wrong, and it was wrong in the direction that costs a red run on a
    correct tree -- which is how a guard gets widened, and then gets widened
    again, until it cannot see the regression it exists for.
    """
    small = _count_rehashes(lambda: _fill(tmp_path / "small.jsonl", SMALL))
    large = _count_rehashes(lambda: _fill(tmp_path / "large.jsonl", LARGE))
    assert_linear_in_records("appending", small, large)


def _count_rehashes(fn) -> int:
    """Run ``fn`` and return how many log records it re-hashed.

    Every record a verification checks is re-hashed exactly once, and every
    append hashes the one record it writes. So this counts the work the
    append path actually does, in units that do not move when the runner is
    busy -- the same move D-2026-32 made for the governed-run guard, applied
    to the guard that kept failing on noise.
    """
    seen = {"n": 0}
    real = _Event.recompute_hash

    def counting(self):
        seen["n"] += 1
        return real(self)

    _Event.recompute_hash = counting
    try:
        fn()
    finally:
        _Event.recompute_hash = real
    return seen["n"]


def test_per_append_cost_does_not_grow_with_history(tmp_path):
    """The same property stated the way it is actually felt.

    A user does not notice 'the total is quadratic'; they notice that the
    thousandth append is slower than the first.

    WHY THIS ONE COUNTS AND DOES NOT TIME (D-2026-37)

    It used to assert ``ratio < 4.0`` on wall time, and it failed a hosted
    run at 4.74 while the property it guards was intact: measured seven times
    here afterwards the ratio was 0.92-1.05. It is the most noise-exposed
    shape in this file, and for a structural reason. Every other ratio guard
    compares two SIZES with an 8x spread, so healthy reads about 8 and
    quadratic about 64 and a busy runner cannot move a measurement across
    that gap. This one compares the same size at two different TIMES, so it
    has no size signal at all: the only thing separating pass from fail is
    how the machine felt during each burst.

    Counting re-hashes is not a looser bound -- it is a stricter one. The
    assertion below is EQUALITY, where the old one allowed a 4x growth, and
    it cannot be moved by a busy machine in either direction. The partner
    test shows the count still sees the regression the guard exists for.
    """
    log = EventLog(tmp_path / "log.jsonl")

    def burst(n):
        for i in range(n):
            log.append(actor="p", action="record.create", target=f"x{i}",
                       payload={"record_id": f"x{i}", "kind": "k",
                                "proposer": "p"})

    burst(20)                                   # warm-up, not measured
    first = _count_rehashes(lambda: burst(SMALL))
    for _ in range(3):
        burst(SMALL)
    last = _count_rehashes(lambda: burst(SMALL))

    assert first == SMALL, (
        f"{SMALL} appends onto an empty log re-hashed {first} records; one "
        "per append is what an incremental writer does, and a different "
        "number here means this probe is not measuring the append path")
    assert last == first, (
        f"a burst of {SMALL} appends onto a history of {SMALL * 4} re-hashed "
        f"{last} records against {first} onto an empty log. Per-append work "
        "that grows with history is the regression this guard exists for: a "
        "verification whose cost grows without bound is one that gets "
        "switched off.")


def test_the_per_append_probe_can_actually_see_growth(tmp_path):
    """ANTI-VACUITY for the guard above, and it is load-bearing.

    A counter that always returned the same number would satisfy an equality
    assertion perfectly. So reintroduce the growth deliberately -- periodic
    whole-chain verification, which is exactly the shape the incremental
    writer replaced -- and require the count to rise with the history.
    """
    log = EventLog(tmp_path / "log.jsonl")
    log.full_verify_every = SMALL // 2          # the regression, on purpose

    def burst(n):
        for i in range(n):
            log.append(actor="p", action="record.create", target=f"x{i}",
                       payload={"record_id": f"x{i}", "kind": "k",
                                "proposer": "p"})

    burst(20)
    first = _count_rehashes(lambda: burst(SMALL))
    for _ in range(3):
        burst(SMALL)
    last = _count_rehashes(lambda: burst(SMALL))

    assert last > first * 2, (
        f"with whole-chain verification every {log.full_verify_every} "
        f"appends, a burst onto a history of {SMALL * 4} re-hashed {last} "
        f"records against {first} onto an empty log. If that does not grow, "
        "the probe cannot see the regression the guard above rules out")


# ---- reading and verification -------------------------------------------
def test_full_verification_is_linear(tmp_path):
    """Counted, not timed -- see assert_linear_in_records (D-2026-46)."""
    _fill(tmp_path / "small.jsonl", SMALL)
    _fill(tmp_path / "large.jsonl", LARGE)
    small = _count_rehashes(lambda: EventLog(tmp_path / "small.jsonl").verify())
    large = _count_rehashes(lambda: EventLog(tmp_path / "large.jsonl").verify())
    assert_linear_in_records("full verification", small, large)


def test_incremental_verification_does_not_grow_with_the_prefix(tmp_path):
    """The whole reason anchors exist.

    Verifying a fixed tail must cost the same whether the prefix is short or
    long; if it does not, the anchor is being ignored.
    """
    # COUNTED, NOT TIMED (D-2026-46). A fixed tail is a fixed amount of
    # work, so the honest assertion is EQUALITY -- where the timed form
    # allowed a 3.0x window, inside which a prefix-dependent cost could sit
    # unnoticed on any machine quiet enough. Measured: 10 re-hashes for a
    # 10-record tail, at both prefix lengths.
    results = {}
    for name, n in (("small", SMALL), ("large", LARGE)):
        log = _fill(tmp_path / f"{name}.jsonl", n)
        anchor = log.anchor_at(n - 10)
        results[name] = _count_rehashes(
            lambda log=log, a=anchor: log.verify_from(a))
    assert results["large"] == results["small"] == 10, (
        f"verifying a fixed 10-record tail cost {results['small']} re-hashes "
        f"behind a {SMALL}-record prefix and {results['large']} behind "
        f"{LARGE}. Equal is the property; anything else means the anchor is "
        "not being used and the prefix is being re-read")


def test_projection_load_is_linear(tmp_path):
    _fill(tmp_path / "small.jsonl", SMALL)
    _fill(tmp_path / "large.jsonl", LARGE)
    small = _count_rehashes(
        lambda: AuthorityStore(EventLog(tmp_path / "small.jsonl")).load())
    large = _count_rehashes(
        lambda: AuthorityStore(EventLog(tmp_path / "large.jsonl")).load())
    assert_linear_in_records("authority-store load", small, large)


def test_independent_reconstruction_is_linear(tmp_path):
    _fill(tmp_path / "small.jsonl", SMALL)
    _fill(tmp_path / "large.jsonl", LARGE)
    small = _count_rehashes(
        lambda: reconstruct(EventLog(tmp_path / "small.jsonl")))
    large = _count_rehashes(
        lambda: reconstruct(EventLog(tmp_path / "large.jsonl")))
    assert_linear_in_records("independent reconstruction", small, large)


def test_audit_index_construction_is_linear(tmp_path):
    _fill(tmp_path / "small.jsonl", SMALL)
    _fill(tmp_path / "large.jsonl", LARGE)
    small = _count_rehashes(
        lambda: AuditIndex.from_log(EventLog(tmp_path / "small.jsonl")))
    large = _count_rehashes(
        lambda: AuditIndex.from_log(EventLog(tmp_path / "large.jsonl")))
    assert_linear_in_records("audit-index construction", small, large)


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

    # COUNTED, NOT TIMED (D-2026-46), and that also removes a SKIP.
    #
    # The timed form called pytest.skip when the full load fell below the
    # measurement floor -- on a fast machine this guard simply did not run,
    # which this file's own _per_call docstring calls "a hole with a green
    # tick over it". Counting has no floor, so the guard always runs.
    #
    # Measured at LARGE=800: full replay 802 re-hashes, checkpoint load 3.
    full = _count_rehashes(lambda: AuthorityStore(
        EventLog(tmp_path / "log.jsonl"), evidence=evidence).load())
    cached = _count_rehashes(lambda: AuthorityStore.load_from(
        EventLog(tmp_path / "log.jsonl"), checkpoints, blobs=evidence,
        evidence=evidence, require_checkpoint=True))
    assert cached < full, (
        f"loading from a checkpoint re-hashed {cached} records and a full "
        f"replay {full}; a checkpoint that saves nothing is a second source "
        "of truth with no benefit")
    assert cached < full / 10, (
        f"the checkpoint saved only {full - cached} of {full} re-hashes. It "
        "is meant to make the prefix free, not slightly cheaper")


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
    # THE ONE GUARD IN THIS FILE STILL ON WALL TIME, AND WHY.
    #
    # D-2026-46 converted the other five to counting re-hashes. ready_queue
    # hashes nothing -- it is a projection query over state already folded --
    # so there is no work unit to count and no honest conversion. It keeps
    # the timed form and the doubled ceiling, and it is named here as the
    # residue rather than left to look like the others.
    #
    # Its exposure is the same one that failed the append guard at 37.1x
    # against a 20.0 ceiling. It has not failed yet; that is an observation,
    # not a guarantee, and if it does the answer is a countable unit rather
    # than a wider bound.
    assert large / small < LINEAR_CEILING * 2, (
        "computing the ready queue got disproportionately slower as the "
        "queue grew")


# ---- the evidence store --------------------------------------------------
# THE SECOND AND LAST TIMED GUARD. EvidenceStore.get resolves a digest to
# bytes; it verifies nothing and hashes nothing, so there is no work unit to
# count and no honest conversion (D-2026-46). Timed, and named as residue.
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
    log.append(actor="p", action="record.create", target="warm",
               payload={"record_id": "warm", "kind": "k", "proposer": "p"})
    before = _open_fds()
    for i in range(200):
        log.append(actor="p", action="record.create", target=f"r{i}",
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


# ---- shape, not just a ceiling -------------------------------------------
#
# R49 said these guards were "ratios only, at two sizes", and that a defect
# that is linear-with-a-large-constant would be invisible to them. Two points
# fit any line; three have a slope. Each guard below measures the same
# operation at 100, 400 and 1600 records and asserts on the fitted exponent.

def test_full_verification_has_a_linear_shape(tmp_path):
    sizes, times = _curve(
        lambda n: (lambda log=_fill(tmp_path / f"v{n}.jsonl", n):
                   (lambda: EventLog(log.path).verify()))())
    exp = _exponent(sizes, times)
    _record("full_verification", exp, EXPONENT_CEILING)
    assert exp < EXPONENT_CEILING, (
        f"verification scales as n^{exp:.2f} over {sizes}: "
        f"{[round(t * 1000, 2) for t in times]} ms")


def test_projection_load_has_a_linear_shape(tmp_path):
    sizes, times = _curve(
        lambda n: (lambda log=_fill(tmp_path / f"p{n}.jsonl", n):
                   (lambda: AuthorityStore(EventLog(log.path)).load()))())
    exp = _exponent(sizes, times)
    _record("projection_load", exp, EXPONENT_CEILING)
    assert exp < EXPONENT_CEILING, (
        f"loading the projection scales as n^{exp:.2f} over {sizes}: "
        f"{[round(t * 1000, 2) for t in times]} ms")


def test_independent_reconstruction_has_a_linear_shape(tmp_path):
    sizes, times = _curve(
        lambda n: (lambda log=_fill(tmp_path / f"r{n}.jsonl", n):
                   (lambda: reconstruct(EventLog(log.path))))())
    exp = _exponent(sizes, times)
    _record("independent_reconstruction", exp, EXPONENT_CEILING)
    assert exp < EXPONENT_CEILING, (
        f"independent reconstruction scales as n^{exp:.2f} over {sizes}: "
        f"{[round(t * 1000, 2) for t in times]} ms")


def test_the_exponent_fit_can_actually_see_a_quadratic(tmp_path):
    """The discriminator, checked rather than assumed.

    A shape guard whose fit cannot distinguish n from n^2 would pass
    everything, and nothing else in this file would notice. So the fit is
    handed a deliberately quadratic operation and has to say so.
    """
    def quadratic(n):
        data = list(range(n))

        def op():
            total = 0
            for i in data:
                for j in data:
                    total += i ^ j
            return total
        return op

    # Smaller sizes than the real guards use: n^2 at 1600 is 2.5 million
    # inner steps per call, and the point here is the SHAPE of the fit, not
    # the magnitude.
    sizes, times = _curve(quadratic, sizes=(60, 120, 240))
    exp = _exponent(sizes, times)
    assert exp > EXPONENT_CEILING, (
        f"a genuinely quadratic operation fitted n^{exp:.2f}; the fit "
        "cannot see the shapes it is used to refuse")

    def linear(n):
        data = list(range(n))
        return lambda: sum(data)

    lin_exp = _exponent(*_curve(linear, sizes=(30000, 120000, 480000)))
    assert lin_exp < EXPONENT_CEILING, (
        f"a genuinely linear operation fitted n^{lin_exp:.2f}; the fit "
        "refuses shapes it is supposed to accept, so every guard using it "
        "would fail on healthy code")


# ---- one governed operation, against a growing history -------------------
def test_one_governed_operation_does_not_get_slower_as_the_history_grows(
        tmp_path):
    """The cost that WAS quadratic and that no guard here covered.

    Every scaling guard above measures a whole-history operation -- verify,
    load, reconstruct -- and each of those is linear and always was. What
    nothing measured is the cost of ONE governed operation as the history
    behind it grows, and that was O(history): every reducer re-reads the log
    before it decides, and it did so with a full read.

    A profile of 120 campaign cycles spent 10 of its 13 seconds inside
    read(), and doubling the campaign quadrupled its wall time. Nothing was
    wrong with any single operation, which is exactly why it survived: this
    is the third time this repository has recorded a quadratic path, and the
    first two were found the same way.
    """
    def cycle_cost(prefix: int) -> float:
        # The prefix is HISTORY, not queue. Pre-enqueued jobs would grow the
        # live queue too, and reconcile visits every pending job by design --
        # measuring that would conflate "the log is long" with "there is
        # more work", and only the first is the property under test.
        _fill(tmp_path / f"h{prefix}.jsonl", prefix)
        log = EventLog(tmp_path / f"h{prefix}.jsonl")
        pol = PolicyStore(log).load()
        pol.publish(default_policy(), actor="owner")
        sched = Scheduler(log, policy=pol, policy_id="scheduler.default",
                          capacity={"slots": 64}).load()
        t0 = time.perf_counter()
        for i in range(20):
            jid = f"m{i}"
            sched.enqueue(job_id=jid, work_digest=digest({"m": i}),
                          submitter="p1")
            sched.reconcile()
            sched.dispatch(job_id=jid, worker="w1", lease_id=f"L{i}",
                           lease_seqs=200)
            sched.report(job_id=jid, worker="w1")
        return time.perf_counter() - t0

    short = cycle_cost(50)
    long = cycle_cost(1200)
    _record("governed_operation_vs_history", long / short, 4.0)
    assert long < short * 4.0, (
        f"twenty governed operations cost {long * 1000:.0f} ms behind 1200 "
        f"records and {short * 1000:.0f} ms behind 50 -- the per-operation "
        "cost is growing with the history, which is the quadratic campaign")


# ---- the tracked history -------------------------------------------------
#
# R49 said there was "no tracked history of measurements, so a slow drift
# across many commits would not be noticed". docs/performance_baseline.json
# is that history, tools/performance_baseline.py writes it, and the tests
# below stop the file and the code drifting apart from each other -- which is
# the failure mode of every baseline file anybody has ever kept.

BASELINE = ROOT / "docs" / "performance_baseline.json"


def _baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_the_baseline_exists_and_holds_observations():
    doc = _baseline()
    assert doc["schema_version"] == 1
    guards = doc["guards"]
    assert guards, "an empty baseline records no history and detects no drift"
    for name, g in guards.items():
        assert g["observations"], f"{name} has a ceiling and no measurements"
        for o in g["observations"]:
            assert isinstance(o["value"], (int, float))
            assert o["commit"] and o["recorded"], (
                f"{name}: an observation with no commit and no time cannot "
                "be placed in a history")


def test_every_recorded_ceiling_is_the_one_the_suite_enforces():
    """The file and the code must not drift apart.

    A baseline that remembers a ceiling the suite has since loosened would
    report a healthy history while the guard passes everything -- which is
    the way a baseline file usually fails: quietly, and in the reassuring
    direction.
    """
    enforced = {
        "full_verification": EXPONENT_CEILING,
        "projection_load": EXPONENT_CEILING,
        "independent_reconstruction": EXPONENT_CEILING,
        "governed_operation_vs_history": 4.0,
    }
    doc = _baseline()
    assert set(doc["guards"]) == set(enforced), (
        f"the baseline names {sorted(doc['guards'])} and the suite records "
        f"{sorted(enforced)}; a guard in one and not the other is a "
        "measurement nobody is keeping or a history of nothing")
    for name, ceiling in enforced.items():
        assert doc["guards"][name]["ceiling"] == ceiling, (
            f"{name}: the baseline remembers a ceiling of "
            f"{doc['guards'][name]['ceiling']} and the suite enforces "
            f"{ceiling}")


def test_the_latest_recorded_observation_is_under_its_ceiling():
    for name, g in _baseline()["guards"].items():
        latest = g["observations"][-1]
        assert latest["value"] < g["ceiling"], (
            f"{name}: the last recorded measurement {latest['value']} is at "
            f"or above the ceiling {g['ceiling']} -- a history recorded from "
            "a failing run is a history of the wrong system")


def test_the_recorder_refuses_to_record_nothing():
    """The anti-vacuity check on the recorder itself.

    A tool that ran the suite, collected no measurements and wrote an empty
    history would report success and leave a file that proves nothing. This
    repository already carries that defect once, in a verifier that
    compared zero files and printed IDENTICAL.
    """
    src = (ROOT / "tools" / "performance_baseline.py").read_text(
        encoding="utf-8")
    assert "recorded nothing" in src and "vacuous" in src


# ---------------------------------------------------------------------------
# D-2026-32 (P1): the time guard could not see the work.
#
# test_one_governed_operation_does_not_get_slower_as_the_history_grows above
# measures WALL TIME and passed at a ratio of ~1.1 against a ceiling of 4.0,
# while one governed Stage-10 run was performing TWENTY-SIX full chain
# verifications -- reading 391 records on the first run of a fresh log and
# 3,544 on the sixth. A governed run is dominated by a subprocess, so hashing
# three thousand records is microseconds against 1.2 seconds. The guard was
# real, it was measuring the wrong quantity, and it would have stayed green
# for a very long time.
#
# EventLog.advance exists for precisely this and says so in its own
# docstring: "That is the quadratic defect this repository has already
# recorded twice, in a third place." This was the third place. The WRITE path
# was never the problem -- append has always carried an anchor -- it was the
# READ of the head that went back to the beginning, eighteen times per run.
#
# What this test locks is the thing the time guard cannot see: the number of
# FULL chain verifications one governed operation performs must not grow with
# the history.
# ---------------------------------------------------------------------------

def _count_full_passes(fn):
    """Run ``fn`` and return (passes, records read) for EventLog.read.

    WHY ``read`` AND NOT ``verify`` (D-2026-41)

    This counted calls to ``EventLog.verify`` and called them "full chain
    verifications". They were not the same thing, and the gap was not small:
    a warm governed run made **11 verify() calls and 19 passes over the
    log**, because ``projection()`` verified and then read the file AGAIN.
    The ceiling was 13, the real number was 19, and the guard passed --
    because it counted the wrong unit.

    Counting passes also survives the repair that closed the window. With
    ``read_verified`` there are two entry points to a whole-chain check, so
    a probe watching one of them would have reported 11 -> 3 and read like
    an improvement it was not measuring. A pass over the file is a pass over
    the file whichever function asked for it.
    """
    from qta_agent.events import EventLog as _EL

    seen = {"passes": 0, "records": 0}
    real = _EL.read

    def counting(self, *, strict: bool = True):
        events = real(self, strict=strict)
        seen["passes"] += 1
        seen["records"] += len(events)
        return events

    _EL.read = counting
    try:
        fn()
    finally:
        _EL.read = real
    return seen["passes"], seen["records"]


#: Whole-log PASSES one governed Stage-10 run may make. Every number here was
#: measured, none chosen:
#:
#:     first run, brand-new caller and EMPTY log : 14   <-- the maximum
#:     second run, same caller (now warm)        : 11
#:     third run, same caller                    : 11
#:     fresh caller over an EXISTING log         : 13
#:     same caller again                         : 11
#:
#: Records read grow (167, 398, 653, 1067, 1196) and passes do not. That
#: flatness is the property this guards; the ceiling is just where it sits.
#:
#: Eleven on a warm caller: eight from ``projection()`` -- six inside
#: ``_move``, one in ``run``, one in ``recover`` -- and three from
#: ``capability.issue``, which verifies to establish the seq a grant is in
#: force from. The extra two or three on a cold caller are ``_head_seq``
#: verifying the whole chain once, fail-closed, before it has an anchor to
#: advance from: once per caller, not once per operation.
#:
#: READ THIS BEFORE CONCLUDING THE BOUND WAS LOOSENED. It was 13, and it is
#: now 14, and that is a TIGHTENING. Until D-2026-41 this counted ``verify()``
#: CALLS while calling them full chain verifications, and a warm run made 11
#: of those while making 19 passes over the log -- ``projection()`` verified
#: and then read the file again. Against the unit named, the true figures
#: were 19 warm and 22 on a fresh log, both far above the ceiling of 13 that
#: was never compared with them. Closing that window removed eight passes per
#: run; 14 is the measured maximum of what remains.
#:
#: It is deliberately not 1 -- the residual eight and three are a measured,
#: recorded gap (D-2026-32), not a closed one, and writing 1 here would
#: assert something this code does not do.
MAX_FULL_LOG_PASSES_PER_GOVERNED_RUN = 14


def _governed(tmp_path, name: str):
    from qta_agent.evidence import EvidenceStore as _ES
    from qta_agent.governed_stage10 import GovernedStage10

    base = ROOT / "verification" / "stage10" / f"_pytest_perf_{name}"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    g = GovernedStage10(root=ROOT, log=EventLog(base / "log.jsonl"),
                        evidence=_ES(base / "evidence"))
    g.out_rel = f"verification/stage10/_pytest_perf_{name}/out"
    return g, base


def test_a_governed_run_verifies_the_whole_chain_a_bounded_number_of_times(
        tmp_path):
    """The count, and that it does not grow with the history.

    Two runs, the second behind a longer log. If the number of full
    verifications were a function of the history this would show it, and if
    somebody puts ``self.log.verify().head_seq`` back into the production
    caller the count goes straight past the ceiling.
    """
    g, base = _governed(tmp_path, "verifycount")
    try:
        def one(i):
            return lambda: g.run(
                tool_id="stage10.emit_artifact",
                inputs={"out_dir": g.out_rel, "name": f"a{i}.json",
                        "payload": {"label": "MODEL_ONLY", "value": i}})

        first_calls, first_records = _count_full_passes(one(0))
        for i in range(1, 4):
            one(i)()
        later_calls, later_records = _count_full_passes(one(4))

        assert first_calls <= MAX_FULL_LOG_PASSES_PER_GOVERNED_RUN, (
            f"a governed run made {first_calls} whole-log passes on "
            f"a fresh log; the ceiling is "
            f"{MAX_FULL_LOG_PASSES_PER_GOVERNED_RUN}")
        assert later_calls <= MAX_FULL_LOG_PASSES_PER_GOVERNED_RUN, (
            f"a governed run made {later_calls} whole-log passes "
            f"behind a longer history, against a ceiling of "
            f"{MAX_FULL_LOG_PASSES_PER_GOVERNED_RUN}; the COUNT is "
            "growing with the log, which is the shape that made this "
            "quadratic")
        assert later_calls <= first_calls, (
            f"{first_calls} passes on a fresh log and {later_calls} "
            "behind a longer one: the count is a function of the history")
        # Anti-vacuity: the measurement has to be seeing something. A run
        # that verified nothing would satisfy every assertion above.
        assert first_calls > 0 and later_records > first_records, (
            "the probe recorded no log reading at all, so the "
            "assertions above are about nothing")
    finally:
        if base.exists():
            shutil.rmtree(base)


def test_the_pass_probe_can_actually_see_a_regression(tmp_path):
    """Anti-vacuity for the ceiling.

    A counter that always reported zero would pass the test above no matter
    what the production caller did. This drives the count past the ceiling
    deliberately and requires the probe to notice.
    """
    from qta_agent.events import EventLog as _EL

    log = EventLog(tmp_path / "probe.jsonl")
    log.append(actor="a", action="record.create", target="r",
               payload={"record_id": "r", "kind": "k", "proposer": "a"})

    def over_the_ceiling():
        for _ in range(MAX_FULL_LOG_PASSES_PER_GOVERNED_RUN + 1):
            _EL.verify(log)

    calls, records = _count_full_passes(over_the_ceiling)
    assert calls == MAX_FULL_LOG_PASSES_PER_GOVERNED_RUN + 1
    assert calls > MAX_FULL_LOG_PASSES_PER_GOVERNED_RUN
    assert records == calls, "each pass read the one record there is"


def test_the_probe_counts_BOTH_ways_into_a_whole_chain_check(tmp_path):
    """D-2026-41: two entry points, and a probe watching one of them lies.

    `verify()` and `read_verified()` both walk the whole log. A probe that
    counted `verify` calls would have reported this loop as zero work, and
    would have reported the D-2026-41 repair as 11 passes becoming 3 -- an
    improvement it was not measuring, in the direction that makes a guard
    look better while the system does the same thing.
    """
    from qta_agent.events import EventLog as _EL

    log = EventLog(tmp_path / "both.jsonl")
    log.append(actor="a", action="record.create", target="r",
               payload={"record_id": "r", "kind": "k", "proposer": "a"})

    def three_each():
        for _ in range(3):
            _EL.verify(log)
        for _ in range(3):
            _EL.read_verified(log)

    passes, _ = _count_full_passes(three_each)
    assert passes == 6, (
        f"the probe saw {passes} passes where six whole-log reads happened; "
        "a check reached through the other entry point is still a check")


def test_counting_re_hashes_would_SEE_a_quadratic_append_path(tmp_path):
    """ANTI-VACUITY for every guard D-2026-46 converted.

    Five assertions now read `large - small == LARGE - SMALL`. A counter
    wired to something that does not grow would satisfy all five and measure
    nothing, which is exactly the failure mode this file keeps finding
    elsewhere.

    So: a deliberately quadratic append path -- one that re-verifies the
    whole chain on every append, which is what the original defect did --
    must be caught, and caught by a mile rather than by a tolerance.
    """
    log = EventLog(tmp_path / "q.jsonl")

    def quadratic_fill(n):
        for i in range(n):
            log.append(actor="p", action="record.create", target=f"q{i}",
                       payload={"record_id": f"q{i}", "kind": "k",
                                "proposer": "p"})
            log.verify()                # the shape of the original defect

    small = _count_rehashes(lambda: quadratic_fill(SMALL // 10))
    linear_would_be = SMALL // 10
    assert small > linear_would_be * 3, (
        f"a quadratic append path re-hashed {small} times for "
        f"{linear_would_be} appends; the counter cannot see the regression "
        "the five converted guards exist to rule out")

    with pytest.raises(AssertionError, match="extra re-hashes"):
        assert_linear_in_records("a deliberately quadratic path",
                                 small, small * 40)
