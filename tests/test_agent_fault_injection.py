"""Two kinds of failure the rest of the suite could not reach.

A REAL PROCESS, REALLY KILLED, AT EVERY BOUNDARY

tests/test_agent_crash_recovery.py models a crash by discarding every
projection and rebuilding from the log. That is the right property and it is
tested at every boundary -- but it runs in one healthy interpreter, so what
it proves is that the REBUILD is correct, not that the bytes on disk at the
moment of death are the ones the rebuild reads. Only the log-append boundary
had a real SIGKILL behind it. Here every boundary does: a subprocess drives
the real substrate, announces each durable step, is killed with SIGKILL
where it stands, and the parent then rebuilds from what actually survived.

FAULTS IN THE FILESYSTEM ITSELF

A disk that fills, a device that returns EIO, a write that stores fewer
bytes than it was given. These are not crashes -- the process keeps running,
and it is the STORAGE that failed. Nothing in the suite injected one, so
every durability claim rested on the happy path of the write.

The faults are injected at the boundary the code actually crosses: the file
object it writes through and the ``os.fsync`` it calls. Not by patching a
method of this package -- a fault injected into our own code proves our own
code was called -- and not by filling a real filesystem, which is neither
portable nor reproducible.

WHAT IS ASSERTED, IN BOTH HALVES

The same direction as the crash suite: what comes back may be less than what
was attempted, and may never be more. A write that failed must not leave a
record that claims it succeeded, and damage must be REFUSED rather than
grown onto.
"""
from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.canonical import digest  # noqa: E402
from qta_agent.checkpoint import CheckpointError, CheckpointStore  # noqa: E402
from qta_agent.events import ChainBroken, EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.policy import PolicyStore  # noqa: E402
from qta_agent.scheduler import JobState, Scheduler  # noqa: E402
from qta_agent.store import AuthorityStore  # noqa: E402

WORK = digest({"work": "stage10"})

ENOSPC = OSError(errno.ENOSPC, "No space left on device")
EIO = OSError(errno.EIO, "Input/output error")


# ==========================================================================
# Part 1: a real process, killed at every durable boundary
# ==========================================================================
#
# The worker announces a marker AFTER each durable step and blocks forever at
# the one it was told to stop at, so the kill lands at a known boundary
# rather than wherever the scheduler happened to be. "Blocked immediately
# after the commit point" is exactly the state a machine that lost power
# there would leave behind, and unlike a timing race it is the same state
# every run.

WORKER = '''
import sys, time
sys.path.insert(0, {root!r})
from qta_agent.authority import Role, State
from qta_agent.canonical import digest
from qta_agent.checkpoint import CheckpointStore
from qta_agent.events import EventLog
from qta_agent.evidence import EvidenceStore
from qta_agent.policy import PolicyStore
from qta_agent.scheduler import Scheduler, default_policy
from qta_agent.store import AuthorityStore

home = {home!r}
stop = sys.argv[1]
log = EventLog(home + "/log.jsonl")
blobs = EvidenceStore(home + "/evidence")
cps = CheckpointStore(home + "/checkpoints")
policy = PolicyStore(log).load()
policy.publish(default_policy(), actor="owner")
sched = Scheduler(log, policy=policy, policy_id="scheduler.default",
                  capacity={{"slots": 4}})
sched.load()
store = AuthorityStore(log, evidence=blobs).load()
WORK = digest({{"work": "stage10"}})

def mark(name):
    sys.stdout.write(name + "\\n")
    sys.stdout.flush()
    if name == stop:
        while True:
            time.sleep(3600)

store.create(record_id="r1", kind="claim", proposer="p1")
mark("proposal")
sched.enqueue(job_id="j1", work_digest=WORK, submitter="p1")
sched.reconcile()
mark("queued")
sched.dispatch(job_id="j1", worker="w1", lease_id="L1", lease_seqs=50)
mark("leased")
dg = blobs.put(b"the artifact the tool wrote")
mark("evidence_stored")
sched.report(job_id="j1", worker="w1")
mark("completed")
store.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="v1",
                 role=Role.VERIFIER)
store.transition(record_id="r1", dst=State.VERIFIED, actor="v1",
                 role=Role.VERIFIER, evidence={{"verification_report": dg}})
mark("verified")
store.checkpoint(cps)
mark("checkpointed")
'''

BOUNDARIES = ["proposal", "queued", "leased", "evidence_stored",
              "completed", "verified", "checkpointed"]


def _run_until_killed(tmp_path, boundary):
    """Drive the real substrate to ``boundary``, then SIGKILL it there.

    Returns the markers that were actually printed, so a test cannot assert
    about a boundary the worker never reached -- a kill that landed early
    would otherwise look like a passing recovery test.
    """
    script = tmp_path / "worker.py"
    script.write_text(WORKER.format(root=str(ROOT), home=str(tmp_path)),
                      encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script), boundary],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, text=True)
    seen = []
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            seen.append(line.strip())
            if seen[-1] == boundary:
                break
    finally:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=30)
        err = proc.stderr.read()
    assert seen and seen[-1] == boundary, (
        f"the worker never reached {boundary!r}; it printed {seen} and "
        f"said: {err[-2000:]}")
    return seen


class Recovered:
    """Everything rebuilt from what survived on disk. No shared memory."""

    def __init__(self, home: Path):
        self.log = EventLog(home / "log.jsonl")
        self.report = self.log.verify()
        self.blobs = EvidenceStore(home / "evidence")
        self.checkpoints = CheckpointStore(home / "checkpoints")
        policy = PolicyStore(self.log).load()
        self.sched = Scheduler(self.log, policy=policy,
                               policy_id="scheduler.default",
                               capacity={"slots": 4})
        self.sched.load()
        self.store = AuthorityStore(self.log, evidence=self.blobs).load()


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_a_real_sigkill_at_every_boundary_leaves_a_readable_log(
        tmp_path, boundary):
    """The chain must verify after a kill at any commit point.

    An append is one write of one line followed by an fsync. If the kill
    could land inside it, what survives is a prefix, and a reader that
    accepted a prefix would be accepting a record nobody finished writing.
    """
    _run_until_killed(tmp_path, boundary)
    rec = Recovered(tmp_path)
    assert rec.report.ok, rec.report.problems[:5]
    assert rec.report.count >= 1


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_a_real_sigkill_never_leaves_the_state_more_advanced(
        tmp_path, boundary):
    """The direction that matters. Losing progress is a cost; gaining it is
    a failure, and it is the only one of the two that can make a false
    claim durable."""
    _run_until_killed(tmp_path, boundary)
    rec = Recovered(tmp_path)
    reached = BOUNDARIES.index(boundary)

    from qta_agent.authority import State

    if reached >= BOUNDARIES.index("proposal"):
        assert rec.store.get("r1").state is (
            State.VERIFIED if reached >= BOUNDARIES.index("verified")
            else State.PROPOSED)
    if reached < BOUNDARIES.index("queued"):
        assert not rec.sched.all_jobs()
    else:
        job = rec.sched.get("j1")
        if reached >= BOUNDARIES.index("completed"):
            assert job.state is JobState.SUCCEEDED
        elif reached >= BOUNDARIES.index("leased"):
            assert job.state is JobState.DISPATCHED
            assert job.lease_holder == "w1", (
                "the lease is durable: a dead holder keeps it until the "
                "lease lapses, or two workers could hold one job")
        else:
            assert job.state is JobState.READY
    from qta_agent.canonical import digest_bytes

    artifact = digest_bytes(b"the artifact the tool wrote")
    stored = set(rec.blobs.list_digests())
    if reached >= BOUNDARIES.index("evidence_stored"):
        assert artifact in stored
        assert rec.blobs.get(artifact) == b"the artifact the tool wrote", (
            "a blob that survived a kill must still hash to its own name")
    else:
        assert artifact not in stored, (
            "evidence that was never written must not be recoverable into "
            "existence")


def test_a_kill_before_the_checkpoint_costs_time_and_never_authority(
        tmp_path):
    _run_until_killed(tmp_path, "verified")
    rec = Recovered(tmp_path)
    assert rec.checkpoints.latest() is None
    assert rec.report.ok, (
        "a missing checkpoint is a slower replay, never a broken one")


def test_a_kill_after_the_checkpoint_leaves_it_usable(tmp_path):
    _run_until_killed(tmp_path, "checkpointed")
    rec = Recovered(tmp_path)
    cp = rec.checkpoints.latest_usable(rec.log)
    assert cp is not None
    assert cp.seq <= rec.report.head_seq, (
        "a checkpoint ahead of the log would be a projection claiming "
        "events the log does not have")


def test_the_log_written_by_a_killed_process_refuses_to_grow_onto_damage(
        tmp_path):
    """The recovered log is a real log, not a museum piece.

    Damage it after the kill and the next append must refuse -- otherwise
    the crash would have been the moment a tampered prefix became
    unappendable-to in theory and appendable-to in practice.
    """
    _run_until_killed(tmp_path, "completed")
    path = tmp_path / "log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[-1])
    rec["actor"] = "mallory"
    lines[-1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ChainBroken, match="refusing to append"):
        EventLog(path).append(actor="w", action="probe", target="t",
                              payload={})


# ==========================================================================
# Part 2: the filesystem fails while the process keeps running
# ==========================================================================

class ShortWriter:
    """A file that stores ``limit`` bytes of what it is given, then fails.

    The shape of a full disk: the first part of the record reaches the
    device and the rest does not. A writer that raised BEFORE writing
    anything would be a much easier world to be correct in, and is not the
    world this is testing.
    """

    def __init__(self, fh, *, limit, error):
        self._fh = fh
        self._limit = limit
        self._error = error

    def write(self, data):
        if self._limit <= 0:
            raise self._error
        part = data[:self._limit]
        self._limit -= len(part)
        self._fh.write(part)
        if len(part) < len(data):
            self._fh.flush()
            raise self._error
        return len(part)

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self._fh.close()
        except OSError:
            pass
        return False


class FaultyPath:
    """A Path that hands out a failing file for one open mode.

    Everything else is delegated, so the module under test does its ordinary
    stat, exists and read-mode work against the real file. Only the write it
    is being tested about is broken.
    """

    def __init__(self, real, *, mode, limit, error):
        self._real = Path(real)
        self._mode = mode
        self._limit = limit
        self._error = error
        self.opened = 0

    def open(self, mode="r", *a, **kw):
        fh = self._real.open(mode, *a, **kw)
        if mode != self._mode:
            return fh
        self.opened += 1
        return ShortWriter(fh, limit=self._limit, error=self._error)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __fspath__(self):
        return str(self._real)


@pytest.fixture()
def log_path(tmp_path):
    return tmp_path / "log.jsonl"


def _fail_writes_under(monkeypatch, directory, *, limit, error):
    """Break every write-mode fdopen whose file lives under ``directory``.

    Scoped by the fd's real path rather than by call order, so a test can
    say "the checkpoint file fails and the blob store does not" -- which is
    the only way to test the two write paths separately when one calls the
    other. Returns the list of files actually broken, so a test that
    injected a fault nowhere fails instead of passing.
    """
    real = os.fdopen
    hits = []
    where = str(directory)

    def faulty(fd, mode="r", *a, **kw):
        try:
            name = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:                       # pragma: no cover
            name = ""
        fh = real(fd, mode, *a, **kw)
        if "w" in mode and name.startswith(where):
            hits.append(name)
            return ShortWriter(fh, limit=limit, error=error)
        return fh

    monkeypatch.setattr(os, "fdopen", faulty)
    return hits


def test_a_disk_that_fills_mid_append_leaves_a_partial_line(log_path,
                                                            monkeypatch):
    log = EventLog(log_path)
    log.append(actor="w", action="probe", target="t", payload={"i": 0})
    intact = [e.seq for e in log.read()]
    witness = log.head()

    faulty = FaultyPath(log_path, mode="ab", limit=40, error=ENOSPC)
    monkeypatch.setattr(log, "path", faulty)
    with pytest.raises(OSError) as caught:
        log.append(actor="w", action="probe", target="t", payload={"i": 1})
    assert caught.value.errno == errno.ENOSPC
    assert faulty.opened == 1, "the write path was not the one under test"
    monkeypatch.undo()

    raw = log_path.read_bytes()
    assert not raw.endswith(b"\n"), (
        "the point of the test: a truncated record is on disk")

    fresh = EventLog(log_path)
    report = fresh.verify()
    assert not report.ok, (
        "damage must be reported, not smoothed over: a reader that returned "
        "ok here would be answering about a log it could not fully read")
    assert any("truncated mid-append" in p for p in report.problems), \
        report.problems
    assert [e.seq for e in fresh.read(strict=False)] == intact, (
        "a crash mid-append must not cost the records before it")
    assert fresh.head() == witness, (
        "the witness must still name the last record that was durable")


def test_the_log_refuses_to_grow_onto_a_partial_line(log_path, monkeypatch):
    """The dangerous follow-on. The next append is where a partial line
    either gets noticed or gets buried under a valid record that makes the
    file parse again."""
    log = EventLog(log_path)
    log.append(actor="w", action="probe", target="t", payload={"i": 0})
    faulty = FaultyPath(log_path, mode="ab", limit=40, error=ENOSPC)
    monkeypatch.setattr(log, "path", faulty)
    with pytest.raises(OSError):
        log.append(actor="w", action="probe", target="t", payload={"i": 1})
    monkeypatch.undo()

    with pytest.raises(Exception) as caught:
        EventLog(log_path).append(actor="w", action="probe", target="t",
                                  payload={"i": 2})
    said = str(caught.value).lower()
    assert "truncat" in said or "refus" in said or "unparseable" in said, \
        caught.value


def test_a_write_that_stores_fewer_bytes_and_reports_no_error(log_path,
                                                              monkeypatch):
    """The nastiest of the three, because nothing raises.

    write() may store fewer bytes than it was given and report that in its
    RETURN VALUE. A caller that ignores it writes half a record, advances
    the witness, and leaves a log whose independent witness names a record
    that is not there -- the exact shape of damage the witness exists to
    detect, manufactured by the writer.

    events.py ignored the return value until this test was written.
    """
    log = EventLog(log_path)
    log.append(actor="w", action="probe", target="t", payload={"i": 0})
    witness = log.head()

    class Silent:
        """Stores a prefix and says how much. No exception anywhere."""

        def __init__(self, fh, limit):
            self._fh = fh
            self._limit = limit

        def write(self, data):
            part = data[:self._limit]
            self._limit -= len(part)
            self._fh.write(part)
            return len(part)

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

    real_open = log_path.open

    class SilentPath(FaultyPath):
        def open(self, mode="r", *a, **kw):
            fh = real_open(mode, *a, **kw)
            if mode != "ab":
                return fh
            self.opened += 1
            return Silent(fh, 40)

    proxy = SilentPath(log_path, mode="ab", limit=40, error=ENOSPC)
    monkeypatch.setattr(log, "path", proxy)
    with pytest.raises(OSError, match="short write"):
        log.append(actor="w", action="probe", target="t", payload={"i": 1})
    assert proxy.opened == 1
    monkeypatch.undo()

    assert EventLog(log_path).head() == witness, (
        "the witness names a record that is only half on disk")


def test_an_fsync_that_fails_does_not_leave_the_witness_claiming_the_record(
        log_path, monkeypatch):
    """EIO on fsync means the bytes may never reach the device.

    The witness is written after the fsync for exactly this reason: if the
    record is not durable, nothing independent may claim it is. What a
    reader is then allowed to find is a log AHEAD of its witness, which is
    reported as the expected shape of a crash rather than as damage.
    """
    log = EventLog(log_path)
    log.append(actor="w", action="probe", target="t", payload={"i": 0})
    witness_before = log.head()

    real_fsync = os.fsync
    hit = []

    def failing(fd):
        try:
            name = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:                       # pragma: no cover
            name = ""
        if name == str(log_path):
            hit.append(fd)
            raise EIO
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing)
    with pytest.raises(OSError) as caught:
        log.append(actor="w", action="probe", target="t", payload={"i": 1})
    assert caught.value.errno == errno.EIO
    assert hit, "the fault was injected somewhere other than the log"
    monkeypatch.undo()

    assert EventLog(log_path).head() == witness_before, (
        "the witness advanced past a record whose fsync failed")
    report = EventLog(log_path).verify()
    assert report.ok, report.problems[:3]
    assert any("witness is behind" in n for n in report.notes), report.notes


def test_a_full_disk_during_a_blob_write_stores_no_blob(tmp_path,
                                                        monkeypatch):
    """Content-addressed storage has one rule: what is there hashes to its
    name. A partial blob published under a full name would break it
    permanently, so the write is temp-then-rename and a failure must leave
    nothing at all."""
    root = tmp_path / "evidence"
    store = EvidenceStore(root)
    hits = _fail_writes_under(monkeypatch, root, limit=5, error=ENOSPC)
    with pytest.raises(OSError) as caught:
        store.put(b"an artifact that will not fit on the disk")
    assert caught.value.errno == errno.ENOSPC
    assert hits, "no write under the evidence store was actually broken"
    monkeypatch.undo()

    assert list(store.list_digests()) == []
    leftovers = [p.name for p in root.rglob(".tmp-*")]
    assert leftovers == [], (
        f"a failed write left temporary files behind: {leftovers}")
    assert store.verify_store().ok


def test_a_short_blob_write_is_refused_before_it_is_published(tmp_path,
                                                              monkeypatch):
    """The silent version, at the store whose whole contract is the digest."""
    root = tmp_path / "evidence"
    store = EvidenceStore(root)
    real = os.fdopen

    class Silent:
        def __init__(self, fh):
            self._fh = fh

        def write(self, data):
            self._fh.write(data[:5])
            return min(5, len(data))

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

    def faulty(fd, mode="r", *a, **kw):
        try:
            name = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:                       # pragma: no cover
            name = ""
        fh = real(fd, mode, *a, **kw)
        return Silent(fh) if "w" in mode and name.startswith(
            str(root)) else fh

    monkeypatch.setattr(os, "fdopen", faulty)
    with pytest.raises(OSError, match="do not hash to their own name"):
        store.put(b"an artifact that will not fit on the disk")
    monkeypatch.undo()
    assert list(store.list_digests()) == []


def test_the_same_blob_stores_cleanly_once_the_disk_comes_back(tmp_path,
                                                               monkeypatch):
    """A fault must not poison the store against a later, healthy write."""
    root = tmp_path / "evidence"
    store = EvidenceStore(root)
    _fail_writes_under(monkeypatch, root, limit=5, error=ENOSPC)
    with pytest.raises(OSError):
        store.put(b"an artifact that will not fit on the disk")
    monkeypatch.undo()

    dg = store.put(b"an artifact that will not fit on the disk")
    assert store.get(dg) == b"an artifact that will not fit on the disk"
    assert store.verify_store().ok


def test_a_failed_checkpoint_write_leaves_no_checkpoint_to_restore(
        tmp_path, monkeypatch):
    """The fault is scoped to the checkpoint directory alone.

    A checkpoint puts its snapshot in the blob store first, so breaking
    every write would break the blob and never reach the file this test is
    about.
    """
    log = EventLog(tmp_path / "log.jsonl")
    store = AuthorityStore(log, evidence=EvidenceStore(tmp_path / "evidence"))
    store.load()
    store.create(record_id="r1", kind="claim", proposer="p1")
    cp_root = tmp_path / "checkpoints"
    cps = CheckpointStore(cp_root)
    cp_root.mkdir(parents=True, exist_ok=True)

    hits = _fail_writes_under(monkeypatch, cp_root, limit=10, error=EIO)
    with pytest.raises(OSError) as caught:
        store.checkpoint(cps)
    assert caught.value.errno == errno.EIO
    assert hits, "the checkpoint file was not the file that failed"
    monkeypatch.undo()

    assert cps.seqs() == []
    assert cps.latest() is None, (
        "a half-written checkpoint that could be read back would be a "
        "projection nobody verified, restored as though it had been")
    assert cps.latest_usable(log) is None
    leftovers = [p.name for p in cp_root.glob(".tmp-*")]
    assert leftovers == []


def test_a_checkpoint_truncated_after_the_fact_is_refused_not_restored(
        tmp_path):
    """The other half: the write succeeded and the STORAGE lost bytes later.

    Nothing in the write path can prevent that, so the read path has to be
    the one that refuses -- which is why this is asserted rather than
    inferred from the atomic write above.
    """
    log = EventLog(tmp_path / "log.jsonl")
    store = AuthorityStore(log, evidence=EvidenceStore(tmp_path / "evidence"))
    store.load()
    store.create(record_id="r1", kind="claim", proposer="p1")
    cps = CheckpointStore(tmp_path / "checkpoints")
    store.checkpoint(cps)
    seq = cps.seqs()[-1]
    path = cps._path(seq)
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw[:len(raw) // 2], encoding="utf-8")

    with pytest.raises(CheckpointError):
        cps.read(seq)
    assert cps.audit().problems, (
        "the audit must name the damaged checkpoint rather than skipping it")


def test_a_checkpoint_the_storage_altered_is_refused_by_its_own_hash(
        tmp_path):
    """Bit-rot rather than truncation: the file is complete and wrong.

    A truncated file is caught by the parser; a flipped byte inside a
    complete record is not, and this is the check that has to catch it. It
    detects accidental corruption only -- anybody who can rewrite the file
    can recompute the hash -- and the module says so where it is defined.
    """
    log = EventLog(tmp_path / "log.jsonl")
    store = AuthorityStore(log, evidence=EvidenceStore(tmp_path / "evidence"))
    store.load()
    store.create(record_id="r1", kind="claim", proposer="p1")
    cps = CheckpointStore(tmp_path / "checkpoints")
    store.checkpoint(cps)
    seq = cps.seqs()[-1]
    path = cps._path(seq)
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["seq"] = rec["seq"] + 1
    path.write_text(json.dumps(rec, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8")

    with pytest.raises(CheckpointError, match="recomputed"):
        cps.read(seq)


def test_a_corrupt_checkpoint_stays_inside_this_module_s_error_type(
        tmp_path):
    """audit() catches CheckpointError and nothing wider, on purpose.

    A parse failure that escaped as a bare ValueError would go past every
    caller written to handle this module's errors -- and audit(), whose
    entire contract is "never stops", would stop.
    """
    cps = CheckpointStore(tmp_path / "checkpoints")
    (tmp_path / "checkpoints").mkdir(parents=True)
    cps._path(7).write_text("{not json at all", encoding="utf-8")
    audit = cps.audit()
    assert audit.problems and "seq 7" in audit.problems[0], audit.problems
    assert not audit.ok
