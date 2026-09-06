"""The read boundary, attacked at the open rather than at the caller.

Every test here answers one question: can a name that was authorized be made
to deliver bytes that were not?

WHY THE RACE TESTS USE BARRIERS AND NOT SLEEP

A race test that hopes the timing works is a test that passes when the race
does not happen, which is most of the time and always on a fast machine. The
races below are DRIVEN: the reader is stopped at the exact point between
authorization and open, the attacker substitutes the target, and only then is
the reader released. The window is exercised deliberately, so a green result
means the window was entered and survived rather than never reached.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
HERE = str(Path(__file__).resolve().parent)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from hangguard import deadline  # noqa: E402
from qta_agent.canonical import digest_bytes  # noqa: E402
from qta_agent.capability import (  # noqa: E402
    Action, CapabilitySet, issue,
)
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.readpath import (  # noqa: E402
    ACT_FILE_READ, GovernedReader, ReadDenied, ReadRequest, read_scope,
)
from qta_agent.safeio import (  # noqa: E402
    NotARegularFile, PathRefused, ReadRoot, ReadTooLarge, SourceChanged,
    SymlinkRefused, split_relative,
)

ACTOR = "agent-reader"
TASK = "task-1"
ROOT_ID = "workspace"


@pytest.fixture()
def tree(tmp_path):
    """An authorized root with a file in it, and a secret outside it."""
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "allowed.txt").write_bytes(b"allowed content")
    (root / "sub" / "nested.txt").write_bytes(b"nested content")
    (tmp_path / "outside.txt").write_bytes(b"SECRET outside the root")
    return {"base": tmp_path, "root": root}


def _caps(scope=(f"{ROOT_ID}/",), cap_id="cap-read", **over):
    base = dict(capability_id=cap_id, subject=ACTOR,
                action=Action.READ_PATHS, task_id=TASK, scope=scope,
                issued_seq=1)
    base.update(over)
    return CapabilitySet(issued={cap_id: issue(**base)}, at_seq=2)


@pytest.fixture()
def reader(tree, tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    r = GovernedReader(log, root_id=ROOT_ID, root_path=tree["root"],
                       capabilities=_caps())
    with r:
        yield r


def _req(resource, **over):
    base = dict(actor=ACTOR, task_id=TASK, root_id=ROOT_ID,
                resource=resource, purpose="test")
    base.update(over)
    return ReadRequest(**base)


# ---------------------------------------------------------------------------
# the primitive: paths that must never reach the filesystem
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "../outside.txt", "sub/../../outside.txt", "a/b/../../../etc/passwd",
    "..", "sub/..",
])
def test_traversal_is_refused_by_name(bad):
    """Refused BEFORE any syscall, so the error names the attempt."""
    with pytest.raises(PathRefused, match=r"\.\."):
        split_relative(bad)


@pytest.mark.parametrize("bad", ["/etc/passwd", "/", "/tmp/x"])
def test_an_absolute_path_is_refused(bad):
    with pytest.raises(PathRefused, match="absolute"):
        split_relative(bad)


@pytest.mark.parametrize("bad", ["", ".", "./", "//", None, 42])
def test_a_path_naming_nothing_inside_the_root_is_refused(bad):
    with pytest.raises(PathRefused):
        split_relative(bad)


def test_a_nul_byte_is_refused():
    with pytest.raises(PathRefused, match="NUL"):
        split_relative("a\x00b")


def test_redundant_separators_are_normalised_not_refused():
    """"a//b" and "a/./b" mean "a/b"; refusing them would be theatre."""
    assert split_relative("a//b") == ("a", "b")
    assert split_relative("a/./b") == ("a", "b")


# ---------------------------------------------------------------------------
# the primitive: symlinks, at every position
# ---------------------------------------------------------------------------

def test_a_symlink_to_a_file_outside_the_root_is_refused(tree):
    link = tree["root"] / "escape.txt"
    link.symlink_to(tree["base"] / "outside.txt")
    with ReadRoot(tree["root"]) as rr:
        with pytest.raises(SymlinkRefused):
            rr.read("escape.txt")


def test_a_symlink_to_an_allowed_file_is_still_refused(tree):
    """Refused for being a link, not for where it points.

    A link whose target is currently innocent is still a name whose meaning
    is chosen by whoever can write the directory, and it can be repointed
    between one read and the next.
    """
    link = tree["root"] / "alias.txt"
    link.symlink_to(tree["root"] / "allowed.txt")
    with ReadRoot(tree["root"]) as rr:
        with pytest.raises(SymlinkRefused):
            rr.read("alias.txt")


def test_a_symlinked_PARENT_directory_is_refused(tree):
    """The classic miss: the leaf is a real file, the directory is the link."""
    outside_dir = tree["base"] / "elsewhere"
    outside_dir.mkdir()
    (outside_dir / "nested.txt").write_bytes(b"SUBSTITUTED")
    (tree["root"] / "linkdir").symlink_to(outside_dir)
    with ReadRoot(tree["root"]) as rr:
        with pytest.raises(SymlinkRefused):
            rr.read("linkdir/nested.txt")


def test_a_symlink_chain_is_refused_at_the_first_link(tree):
    (tree["base"] / "hop.txt").symlink_to(tree["base"] / "outside.txt")
    (tree["root"] / "chain.txt").symlink_to(tree["base"] / "hop.txt")
    with ReadRoot(tree["root"]) as rr:
        with pytest.raises(SymlinkRefused):
            rr.read("chain.txt")


# ---------------------------------------------------------------------------
# the primitive: the opened object is the subject
# ---------------------------------------------------------------------------

def test_a_fifo_is_refused_and_does_not_hang(tree):
    """THE FIFO lesson, applied to reads.

    Opening a FIFO for reading blocks until a writer appears, so an attacker
    who substitutes a named pipe turns a bounded read into an indefinite
    hang. The evidence store learned this on its WRITE path; the read path
    never had the same treatment. O_NONBLOCK plus a post-open fstat is why
    this returns at all.
    """
    os.mkfifo(tree["root"] / "pipe")
    with ReadRoot(tree["root"]) as rr:
        with deadline(5.0):
            with pytest.raises(NotARegularFile, match="FIFO"):
                rr.read("pipe")


def test_a_directory_is_refused(tree):
    with ReadRoot(tree["root"]) as rr:
        with pytest.raises((NotARegularFile, PathRefused)):
            rr.read("sub")


def test_a_socket_is_refused(tree):
    import socket

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(tree["root"] / "sock"))
        with ReadRoot(tree["root"]) as rr:
            with pytest.raises(NotARegularFile, match="socket"):
                rr.read("sock")
    finally:
        sock.close()


@pytest.mark.skipif(not os.path.exists("/dev/zero"),
                    reason="no character device to test against")
def test_a_character_device_is_refused_where_reachable(tmp_path):
    """/dev/zero is 0 bytes to stat and infinite to read.

    A size check that trusted stat would pass it and then read forever.
    """
    with ReadRoot("/dev") as rr:
        with deadline(5.0):
            with pytest.raises(NotARegularFile):
                rr.read("zero")


def test_a_file_over_the_bound_is_refused(tree):
    (tree["root"] / "big.bin").write_bytes(b"x" * 5000)
    with ReadRoot(tree["root"]) as rr:
        with pytest.raises(ReadTooLarge):
            rr.read("big.bin", max_bytes=1000)


def test_an_honest_read_returns_the_bytes_and_their_identity(tree):
    with ReadRoot(tree["root"]) as rr:
        res = rr.read("sub/nested.txt")
    assert res.data == b"nested content"
    assert res.digest == digest_bytes(b"nested content")
    st = (tree["root"] / "sub" / "nested.txt").stat()
    assert res.identity.inode == st.st_ino
    assert res.identity.device == st.st_dev


def test_a_digest_mismatch_is_reported_as_a_changed_source(tree):
    with ReadRoot(tree["root"]) as rr:
        with pytest.raises(SourceChanged, match="expected"):
            rr.read("allowed.txt", expect_digest="a" * 64)


def test_a_closed_root_authorizes_nothing(tree):
    rr = ReadRoot(tree["root"]).open()
    rr.close()
    with pytest.raises(Exception, match="not open"):
        rr.read("allowed.txt")


# ---------------------------------------------------------------------------
# DRIVEN RACES: the window is entered on purpose
# ---------------------------------------------------------------------------

def _swap_with_symlink(target: Path, points_to: Path) -> None:
    tmp = target.with_name(target.name + ".swap")
    tmp.symlink_to(points_to)
    os.replace(tmp, target)


def test_replacing_the_target_with_a_symlink_before_the_open_is_refused(tree):
    """authorize A -> attacker substitutes a link -> open must refuse.

    Not a timing hope: the substitution happens between the two calls, in
    program order, which is the worst case the window allows.
    """
    with ReadRoot(tree["root"]) as rr:
        rr.read("allowed.txt")                      # authorized once
        _swap_with_symlink(tree["root"] / "allowed.txt",
                           tree["base"] / "outside.txt")
        with pytest.raises(SymlinkRefused):
            rr.read("allowed.txt")


def test_replacing_the_target_with_other_content_is_caught_by_the_digest(
        tree):
    """When bytes matter, bind to bytes. The name cannot be trusted."""
    original = digest_bytes(b"allowed content")
    with ReadRoot(tree["root"]) as rr:
        rr.read("allowed.txt", expect_digest=original)
        (tree["root"] / "allowed.txt").write_bytes(b"SUBSTITUTED")
        with pytest.raises(SourceChanged):
            rr.read("allowed.txt", expect_digest=original)


def test_a_rename_after_the_open_cannot_redirect_the_read(tree):
    """The descriptor is bound to the inode, not to the name.

    The reader is held open at the point after the open and before the read
    completes; the attacker renames the file away and puts a different one in
    its place. The bytes that come back must be the ones the open bound to.
    """
    path = tree["root"] / "allowed.txt"
    from qta_agent.safeio import open_beneath

    with ReadRoot(tree["root"]) as rr:
        fd = open_beneath(rr.fd, "allowed.txt")
        try:
            # Attacker replaces the NAME entirely, after the open.
            os.replace(tree["base"] / "outside.txt", path)
            assert path.read_bytes() == b"SECRET outside the root"
            # The descriptor still refers to what was authorized.
            data = b""
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                data += chunk
            assert data == b"allowed content", (
                "the open descriptor followed the name instead of the inode")
        finally:
            os.close(fd)


def test_a_driven_race_between_authorization_and_open_never_reads_the_target(
        tree):
    """A real thread race, driven by barriers rather than by luck.

    The attacker thread swaps the file for a symlink to the secret while the
    reader is parked between deciding and opening. Repeated so the scheduler
    is given many chances; every outcome must be safe.
    """
    secret = tree["base"] / "outside.txt"
    outcomes: list = []
    for _ in range(40):
        target = tree["root"] / "raced.txt"
        if target.is_symlink() or target.exists():
            target.unlink()
        target.write_bytes(b"honest bytes")

        ready = threading.Event()
        go = threading.Event()

        def attacker():
            ready.wait(5.0)
            try:
                _swap_with_symlink(target, secret)
            finally:
                go.set()

        t = threading.Thread(target=attacker)
        t.start()
        try:
            with ReadRoot(tree["root"]) as rr:
                ready.set()
                go.wait(5.0)          # the window, entered deliberately
                try:
                    res = rr.read("raced.txt")
                    outcomes.append(res.data)
                except SymlinkRefused:
                    outcomes.append("REFUSED")
        finally:
            t.join(timeout=5.0)
            assert not t.is_alive()

    leaked = [o for o in outcomes if isinstance(o, bytes)
              and b"SECRET" in o]
    assert not leaked, "the race delivered content from outside the root"
    assert "REFUSED" in outcomes, (
        "no iteration actually hit the substituted state, so this test "
        "exercised nothing")


# ---------------------------------------------------------------------------
# the authority layer
# ---------------------------------------------------------------------------

def test_a_reader_with_no_capabilities_reads_nothing(tree, tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    with GovernedReader(log, root_id=ROOT_ID, root_path=tree["root"],
                        capabilities=None) as r:
        with pytest.raises(ReadDenied, match="default-deny"):
            r.read(_req("allowed.txt"), capability_id="cap-read")


def test_an_authorized_read_returns_the_bytes(reader):
    res = reader.read(_req("allowed.txt"), capability_id="cap-read")
    assert res.data == b"allowed content"


@pytest.mark.parametrize("field,value", [
    ("actor", "agent-other"),
    ("task_id", "task-other"),
])
def test_a_capability_from_the_wrong_actor_or_task_authorizes_nothing(
        reader, field, value):
    with pytest.raises(ReadDenied):
        reader.read(_req("allowed.txt", **{field: value}),
                    capability_id="cap-read")


def test_an_expired_capability_authorizes_nothing(tree, tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    caps = CapabilitySet(
        issued={"cap-read": issue(
            capability_id="cap-read", subject=ACTOR,
            action=Action.READ_PATHS, task_id=TASK,
            scope=(f"{ROOT_ID}/",), issued_seq=1, expires_after_seq=5)},
        at_seq=99)
    with GovernedReader(log, root_id=ROOT_ID, root_path=tree["root"],
                        capabilities=caps) as r:
        with pytest.raises(ReadDenied):
            r.read(_req("allowed.txt"), capability_id="cap-read")


def test_a_revoked_capability_authorizes_nothing(tree, tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    caps = CapabilitySet(
        issued={"cap-read": issue(
            capability_id="cap-read", subject=ACTOR,
            action=Action.READ_PATHS, task_id=TASK,
            scope=(f"{ROOT_ID}/",), issued_seq=1)},
        revoked=frozenset({"cap-read"}), at_seq=2)
    with GovernedReader(log, root_id=ROOT_ID, root_path=tree["root"],
                        capabilities=caps) as r:
        with pytest.raises(ReadDenied):
            r.read(_req("allowed.txt"), capability_id="cap-read")


def test_a_write_capability_does_not_authorize_a_read(tree, tmp_path):
    """Reading and writing are separate authorities, deliberately."""
    log = EventLog(tmp_path / "log.jsonl")
    caps = CapabilitySet(
        issued={"cap-w": issue(
            capability_id="cap-w", subject=ACTOR,
            action=Action.WRITE_PATHS, task_id=TASK,
            scope=(f"{ROOT_ID}/",), issued_seq=1)}, at_seq=2)
    with GovernedReader(log, root_id=ROOT_ID, root_path=tree["root"],
                        capabilities=caps) as r:
        with pytest.raises(ReadDenied):
            r.read(_req("allowed.txt"), capability_id="cap-w")


def test_path_scope_is_not_a_string_prefix(tree, tmp_path):
    """A grant for ".../a" must not cover ".../ab".

    The capability layer already scopes by path COMPONENT; this asserts the
    read boundary inherits that rather than reintroducing a prefix compare.
    """
    (tree["root"] / "a").mkdir()
    (tree["root"] / "a" / "f.txt").write_bytes(b"inside a")
    (tree["root"] / "ab").mkdir()
    (tree["root"] / "ab" / "f.txt").write_bytes(b"inside ab")

    log = EventLog(tmp_path / "log.jsonl")
    caps = _caps(scope=read_scope(ROOT_ID, "a"))
    with GovernedReader(log, root_id=ROOT_ID, root_path=tree["root"],
                        capabilities=caps) as r:
        assert r.read(_req("a/f.txt"),
                      capability_id="cap-read").data == b"inside a"
        with pytest.raises(ReadDenied):
            r.read(_req("ab/f.txt"), capability_id="cap-read")


def test_a_scope_for_one_root_is_not_spendable_against_another(tree,
                                                               tmp_path):
    """Roots are namespaced, so identical relative paths do not collide."""
    log = EventLog(tmp_path / "log.jsonl")
    caps = _caps(scope=read_scope("evidence", "allowed.txt"))
    with GovernedReader(log, root_id=ROOT_ID, root_path=tree["root"],
                        capabilities=caps) as r:
        with pytest.raises(ReadDenied):
            r.read(_req("allowed.txt"), capability_id="cap-read")


# ---------------------------------------------------------------------------
# auditability
# ---------------------------------------------------------------------------

def test_every_permitted_read_is_recorded_with_what_was_opened(reader):
    res = reader.read(_req("allowed.txt", purpose="verification"),
                      capability_id="cap-read")
    rec = [ev for ev in reader.log.read() if ev.action == ACT_FILE_READ]
    assert len(rec) == 1
    p = rec[0].payload
    assert p["allowed"] is True
    assert p["request"]["purpose"] == "verification"
    assert p["capability_id"] == "cap-read"
    assert p["result"]["digest"] == res.digest
    assert p["result"]["identity"]["inode"] == res.identity.inode


def test_a_refused_read_is_recorded_too(reader):
    """"What did this agent try" is the question an incident starts with."""
    with pytest.raises(ReadDenied):
        reader.read(_req("allowed.txt", actor="agent-other"),
                    capability_id="cap-read")
    rec = [ev for ev in reader.log.read() if ev.action == ACT_FILE_READ]
    assert len(rec) == 1
    assert rec[0].payload["allowed"] is False
    assert "ReadDenied" in rec[0].payload["reason"]


def test_a_refused_traversal_is_recorded_as_a_traversal(reader):
    with pytest.raises(PathRefused):
        reader.read(_req("../outside.txt"), capability_id="cap-read")
    rec = [ev for ev in reader.log.read() if ev.action == ACT_FILE_READ]
    assert "PathRefused" in rec[0].payload["reason"]


def test_the_bytes_are_never_written_to_the_log(tree, tmp_path):
    """A read is exactly how a secret would reach an audit trail."""
    import json

    (tree["root"] / "secret.txt").write_bytes(b"hunter2-do-not-log")
    log = EventLog(tmp_path / "log.jsonl")
    with GovernedReader(log, root_id=ROOT_ID, root_path=tree["root"],
                        capabilities=_caps()) as r:
        r.read(_req("secret.txt"), capability_id="cap-read")
    blob = json.dumps([ev.to_record() for ev in log.read()])
    assert "hunter2" not in blob


def test_the_reader_refuses_an_unknown_action_nowhere(reader):
    """The action this module writes is registered, so shared-log readers
    classify it as FOREIGN rather than refusing the whole history."""
    from qta_agent import actions

    reader.read(_req("allowed.txt"), capability_id="cap-read")
    assert actions.owner(ACT_FILE_READ) == "readpath"


# ---------------------------------------------------------------------------
# Mutation-isolating tests.
#
# Three mutations survived the first run: the pre-read size check and the
# streaming growth check each masked the other, so deleting either left the
# other to fail the test. Neither is redundant -- they defend different
# things, and the tests below provoke each with the other unable to fire.
# ---------------------------------------------------------------------------

def test_an_oversized_file_is_diagnosed_as_oversized_not_as_growing(tree):
    """R5, isolated by DIAGNOSIS rather than by outcome.

    Both checks raise ``ReadTooLarge``, which is why the mutation survived.
    They are not interchangeable: the streaming check says the file *grew
    while being read*, which for a file that was always too large is false
    and sends an operator after a race that never happened. The pre-read
    check also reports the size, so the operator learns how far over they
    are -- and it refuses BEFORE reading, which is the whole point of having
    it as well as the streaming one.
    """
    (tree["root"] / "big.bin").write_bytes(b"z" * 4000)
    with ReadRoot(tree["root"]) as rr:
        with pytest.raises(ReadTooLarge) as exc:
            rr.read("big.bin", max_bytes=100)
    assert "is 4000 bytes, over the 100-byte bound" in str(exc.value)
    assert "grew" not in str(exc.value), (
        "a file that was always too large did not grow; that diagnosis "
        "would send an operator hunting a race that never happened")


def test_a_file_that_grows_after_the_size_check_is_still_bounded(tree,
                                                                monkeypatch):
    """R6, isolated by making the pre-read check unable to fire.

    ``fstat`` is patched to under-report the size, which is exactly the
    condition the streaming check defends against: a file that was small when
    it was measured and larger by the time it is read. Without the streaming
    check the read returns the whole oversized file, and the bound the caller
    asked for was never enforced at all.
    """
    import os as _os

    (tree["root"] / "grower.bin").write_bytes(b"z" * 4000)
    real_fstat = _os.fstat

    class _Stale:
        """A stat result reporting a stale, smaller size."""

        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            return getattr(self._st, name)

        @property
        def st_size(self):
            return 10

    monkeypatch.setattr(_os, "fstat", lambda fd: _Stale(real_fstat(fd)))
    with ReadRoot(tree["root"]) as rr:
        with pytest.raises(ReadTooLarge) as exc:
            rr.read("grower.bin", max_bytes=100)
    assert "grew past" in str(exc.value), (
        "the streaming bound did not fire, so a file that was small when "
        "measured and large when read was never bounded")


def test_a_hard_linked_file_is_refused_when_identity_matters(tree):
    """The alias a symlink check cannot see.

    A hard link is not a link, it is the file: ``O_NOFOLLOW`` says nothing
    about it, and the second name can sit anywhere on the filesystem --
    including outside the root this read is confined to. ``st_nlink`` is the
    only part of that the kernel reports, and for an artifact written once by
    one tool a count above one is already the answer.
    """
    from qta_agent.safeio import AliasedFile

    target = tree["root"] / "aliased.txt"
    target.write_bytes(b"content with two names")
    os.link(target, tree["base"] / "second-name.txt")   # OUTSIDE the root
    with ReadRoot(tree["root"]) as rr:
        rr.read("aliased.txt")                          # permitted by default
        with pytest.raises(AliasedFile, match="two names|2 names"):
            rr.read("aliased.txt", require_unique_link=True)


def test_an_ordinary_file_passes_the_unique_link_check(tree):
    """The check must name a real condition, not refuse everything."""
    with ReadRoot(tree["root"]) as rr:
        res = rr.read("allowed.txt", require_unique_link=True)
    assert res.data == b"allowed content"


# --- the alias check must stay opted INTO by the path that needs it --------

def _non_test_references(symbol: str) -> set:
    """Delegates to the ONE module that answers this question.

    It was a local rglob here first. Sharing it matters because the
    question -- "does this defence have a caller that is not its own test"
    -- has been asked wrongly three times in three different files.
    """
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "tools"))
    from repo_scope import non_test_references

    return set(non_test_references(symbol))


def test_the_hard_link_check_keeps_a_production_caller():
    """``require_unique_link`` is opt-IN, so something must opt in.

    A per-call flag nobody passes is the same defect as a function nobody
    invokes -- the check is written, tested, correct and never reached. The
    verification step is the caller that needs it: it re-derives an
    artifact's digest from disk, and a second name for that inode is a
    second way to change what the digest describes.
    """
    refs = _non_test_references("require_unique_link=True")
    assert refs - {"qta_agent/safeio.py", "qta_agent/readpath.py"}, (
        "nothing outside tests passes require_unique_link=True, so the "
        f"alias check never runs in production: {sorted(refs)}")


def test_a_hard_linked_artifact_is_refused_by_the_verification_path(tmp_path):
    """Measured, not asserted from the flag's presence.

    st_nlink is what the kernel will tell us: that the content has another
    name, not where that name is. For an artifact written once by one tool
    a count above one is already the answer, and this proves the count is
    actually consulted on the path that matters.
    """
    import os

    from qta_agent.safeio import AliasedFile, ReadRoot

    root = tmp_path / "root"
    root.mkdir()
    (root / "a.json").write_text("payload")
    elsewhere = tmp_path / "outside"
    elsewhere.mkdir()
    os.link(root / "a.json", elsewhere / "second-name.json")
    assert os.stat(root / "a.json").st_nlink == 2

    with ReadRoot(root) as r:
        with pytest.raises(AliasedFile, match="another name"):
            r.read("a.json", require_unique_link=True)
        # And the opt-out still reads it: the flag is a choice the caller
        # makes, which is exactly why the guard above exists.
        assert r.read("a.json", require_unique_link=False).data == b"payload"
