"""Evidence store: the module that makes invariant I6 mean something.

Every test here is written against a specific way the store could lie. The
store's job is to answer "what do these bytes hash to, really" -- so the tests
are mostly about what happens when the filesystem says one thing and the
content says another.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import stat
import sys
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from qta_agent.authority import (  # noqa: E402
    Role, State, TransitionError, TransitionRequest, check,
)
from qta_agent.canonical import digest_bytes  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import (  # noqa: E402
    CHUNK_BYTES, CorruptEvidence, EvidenceError, EvidenceStore,
    EvidenceTooLarge, MalformedDigest, UnknownEvidence, require_resolvable,
)
from qta_agent.store import AuthorityStore, StoreError  # noqa: E402

REPORT = b'{"result":"verified","gates":83,"pass":0}'

#: Long enough that a loaded machine never trips it, short enough that a
#: genuine hang is a test failure in seconds rather than a stalled CI job.
HANG_DEADLINE_S = 5.0


class Hung(Exception):
    """The call under test did not return within its deadline."""


@contextlib.contextmanager
def deadline(seconds: float = HANG_DEADLINE_S):
    """Fail a test that blocks, instead of letting it stall the whole run.

    The FIFO tests below exist because reading a named pipe with no writer
    blocks forever. Asserting only ``pytest.raises`` would let the mutation
    that removes the file-type check "pass" by hanging the suite -- and a
    mutation matrix would then record it as killed by a 40-minute timeout
    rather than by a test. SIGALRM turns the hang into a failure.

    Interrupting a blocked read works because the handler raises: PEP 475
    retries a syscall interrupted by a signal only when the handler returns
    normally.
    """
    def _fire(signum, frame):
        raise Hung(f"call did not return within {seconds}s")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_the_hang_deadline_actually_observes_a_hang():
    """Prove the probe works before trusting it to prove anything else.

    A guard that silently never fires would make both FIFO tests pass for no
    reason at all -- the same class of error as a mutation surviving because
    an adjacent check masked it.
    """
    r, w = os.pipe()
    try:
        with pytest.raises(Hung):
            with deadline(0.5):
                os.read(r, 1)          # blocks: nothing is ever written
    finally:
        os.close(r)
        os.close(w)


@pytest.fixture()
def store(tmp_path):
    return EvidenceStore(tmp_path / "evidence")


# --- round trip ------------------------------------------------------------

def test_content_round_trips_under_its_own_digest(store):
    dg = store.put(REPORT, media_type="application/json")
    assert dg == digest_bytes(REPORT)
    assert store.get(dg) == REPORT
    assert store.contains(dg)


def test_storing_the_same_bytes_twice_is_idempotent(store):
    a = store.put(REPORT)
    b = store.put(REPORT)
    assert a == b
    assert store.verify_store().count == 1


def test_the_empty_blob_is_storable_and_distinct_from_absent(store):
    """``sha256(b"")`` is a real digest and must not be confused with nothing.

    A store that treated empty content as "not stored" would let an agent cite
    an empty verification report and have the citation quietly vanish.
    """
    dg = store.put(b"")
    assert dg == digest_bytes(b"")
    assert store.get(dg) == b""
    assert store.contains(dg)
    assert not store.contains("f" * 64)


def test_first_seen_is_not_restated_by_a_later_identical_put(store):
    dg = store.put(REPORT, media_type="application/json")
    first = store.info(dg).first_seen
    store.put(REPORT, media_type="text/plain")
    again = store.info(dg)
    assert again.first_seen == first
    assert again.media_type == "application/json", (
        "a second put must not relabel evidence that is already held")


# --- the store does not trust its own layout -------------------------------

def test_tampered_bytes_are_refused_rather_than_returned(store):
    """The load-bearing check. Without it this class is a directory.

    The filename still spells the right digest; only the content changed.
    Anything that answers from the path alone hands back the attacker's bytes.
    """
    dg = store.put(REPORT)
    blob = store._blob_path(dg)
    blob.write_bytes(b'{"result":"verified","gates":83,"pass":83}')

    with pytest.raises(CorruptEvidence, match="hash to"):
        store.get(dg)
    assert not store.contains(dg)
    rep = store.verify_store()
    assert not rep.ok and any("hash to" in p for p in rep.problems)


def test_a_blob_swapped_for_another_real_blob_is_refused(store):
    """Both files are genuine evidence -- filed under each other's names."""
    a = store.put(b"report A")
    b = store.put(b"report B")
    pa, pb = store._blob_path(a), store._blob_path(b)
    pa.write_bytes(b"report B")
    pb.write_bytes(b"report A")

    for dg in (a, b):
        with pytest.raises(CorruptEvidence):
            store.get(dg)
    assert store.verify_store().problems, "both swaps must be reported"


def test_an_unverified_containment_check_is_available_but_not_the_default(store):
    """``verify=False`` answers a weaker question, and says so.

    Kept because a caller about to read the blob anyway should not pay to hash
    it twice. Pinned here so that the default can never be quietly flipped:
    the tampered blob is present-but-invalid, and the two calls must disagree.
    """
    dg = store.put(REPORT)
    store._blob_path(dg).write_bytes(b"not the report")
    assert store.contains(dg, verify=False) is True
    assert store.contains(dg) is False


def test_a_symlinked_blob_is_refused_without_being_followed(store, tmp_path):
    """A link's target is chosen by whoever can write the directory."""
    secret = tmp_path / "elsewhere.txt"
    secret.write_bytes(REPORT)
    dg = digest_bytes(REPORT)
    blob = store._blob_path(dg)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.symlink_to(secret)

    with pytest.raises(CorruptEvidence, match="symlink"):
        store.get(dg)
    assert not store.contains(dg)
    # Even though following it would have produced content hashing to dg.
    assert digest_bytes(secret.read_bytes()) == dg


def test_a_fifo_in_the_store_is_refused_rather_than_read(store):
    """Reading a FIFO blocks until a writer appears, which may be never.

    A test that merely asserted "raises" would pass by timing out the whole
    suite. The refusal must come from the file-type check, before any read.
    """
    dg = "a" * 64
    blob = store._blob_path(dg)
    blob.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(blob)
    assert stat.S_ISFIFO(blob.lstat().st_mode)

    with deadline():
        with pytest.raises(CorruptEvidence, match="not a regular file"):
            store.get(dg)


# --- digests are names, and names are validated ----------------------------

def test_an_absent_digest_raises_unknown_not_corrupt(store):
    """The two failures mean different things to an operator."""
    with pytest.raises(UnknownEvidence, match="no evidence stored"):
        store.get("c" * 64)


def test_uppercase_digests_are_refused_rather_than_normalized(store):
    dg = store.put(REPORT)
    with pytest.raises(MalformedDigest, match="case-insensitive"):
        store.get(dg.upper())
    assert store.contains(dg.upper()) is False


@pytest.mark.parametrize("bad", [
    "../../etc/passwd", "..", "/etc/passwd", "a" * 63, "a" * 65,
    "g" * 64, "", 42, None, b"a" * 64,
])
def test_a_non_digest_can_never_reach_the_filesystem(store, bad):
    """Path traversal is impossible *because* of this validation, not by luck.

    64 characters drawn from ``[0-9a-f]`` cannot contain a separator or a dot
    segment, so the check that enforces that shape is what keeps the store
    inside its root.
    """
    with pytest.raises(MalformedDigest):
        store.get(bad)
    assert store.contains(bad) is False


# --- bounds ----------------------------------------------------------------

def test_oversized_content_is_refused_not_truncated(tmp_path):
    s = EvidenceStore(tmp_path / "e", max_blob_bytes=64)
    with pytest.raises(EvidenceTooLarge):
        s.put(b"x" * 65)
    assert list(s.list_digests()) == [], "nothing may be written on refusal"


def test_an_oversized_blob_already_on_disk_is_refused_on_read(tmp_path):
    """The bound can be lowered after content was stored."""
    big = EvidenceStore(tmp_path / "e", max_blob_bytes=1024)
    dg = big.put(b"y" * 500)
    small = EvidenceStore(tmp_path / "e", max_blob_bytes=100)
    with pytest.raises(EvidenceTooLarge):
        small.get(dg)


def test_a_non_bytes_payload_is_refused_with_an_actionable_message(store):
    with pytest.raises(EvidenceError, match="canonicalize"):
        store.put({"result": "verified"})


# --- put_file --------------------------------------------------------------

def test_put_file_matches_put_for_the_same_bytes(store, tmp_path):
    p = tmp_path / "report.json"
    p.write_bytes(REPORT)
    assert store.put_file(p) == store.put(REPORT)


def test_put_file_streams_content_larger_than_one_chunk(store, tmp_path):
    p = tmp_path / "big.bin"
    content = os.urandom(CHUNK_BYTES + 1024)
    p.write_bytes(content)
    dg = store.put_file(p)
    assert dg == digest_bytes(content)
    assert store.get(dg) == content


def test_put_file_refuses_a_symlink(store, tmp_path):
    target = tmp_path / "real.txt"
    target.write_bytes(REPORT)
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(EvidenceError, match="symlink"):
        store.put_file(link)


def test_put_file_refuses_a_fifo_instead_of_blocking(store, tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with deadline():
        with pytest.raises(EvidenceError, match="not a regular file"):
            store.put_file(fifo)


def test_put_file_refuses_a_file_over_the_bound(tmp_path):
    s = EvidenceStore(tmp_path / "e", max_blob_bytes=16)
    p = tmp_path / "big.txt"
    p.write_bytes(b"z" * 17)
    with pytest.raises(EvidenceTooLarge):
        s.put_file(p)


# --- crash safety ----------------------------------------------------------

def test_an_abandoned_temporary_file_is_a_note_not_a_corruption(store):
    """A crash mid-write leaves a temp file, never a partial blob.

    The publish is a rename, so an interrupted write is invisible to readers.
    The leftover is worth reporting -- it is evidence that a crash happened --
    but it is not a failure of the store.
    """
    dg = store.put(REPORT)
    (store._blob_path(dg).parent / ".tmp-abandoned").write_bytes(b"partial")
    rep = store.verify_store()
    assert rep.ok, rep.problems
    assert any("abandoned" in n for n in rep.notes)
    assert list(store.list_digests()) == [dg]


def test_a_stray_file_that_does_not_spell_a_digest_is_reported(store):
    dg = store.put(REPORT)
    (store._blob_path(dg).parent / "notadigest").write_bytes(b"?")
    rep = store.verify_store()
    assert not rep.ok
    assert any("does not spell a digest" in p for p in rep.problems)


def test_an_empty_store_verifies_and_lists_nothing(tmp_path):
    s = EvidenceStore(tmp_path / "never-created")
    assert s.verify_store().ok
    assert list(s.list_digests()) == []


# --- the sidecar is not trusted --------------------------------------------

def test_a_tampered_sidecar_cannot_change_the_reported_size(store):
    """Size is reported from the verified content, never from metadata.

    Otherwise an auditor could be told a 40-byte report is 4 GB, or the
    reverse -- and the sidecar is exactly as writable as the blob.
    """
    dg = store.put(REPORT, media_type="application/json")
    meta = store._meta_path(dg)
    meta.write_text(json.dumps(
        {"digest": dg, "size": 999999, "media_type": "text/plain",
         "first_seen": 0.0}), encoding="utf-8")
    info = store.info(dg)
    assert info.size == len(REPORT)
    # The advisory fields do follow the sidecar -- and are documented as
    # untrusted precisely because of this.
    assert info.media_type == "text/plain"


def test_an_unreadable_sidecar_does_not_prevent_resolving_the_evidence(store):
    """Losing metadata must not lose the evidence."""
    dg = store.put(REPORT, media_type="application/json")
    store._meta_path(dg).write_text("{not json", encoding="utf-8")
    assert store.get(dg) == REPORT
    assert store.info(dg).size == len(REPORT)
    assert store.info(dg).media_type == "application/octet-stream"


def test_a_missing_sidecar_does_not_prevent_resolving_the_evidence(store):
    dg = store.put(REPORT)
    store._meta_path(dg).unlink()
    assert store.get(dg) == REPORT
    assert store.verify_store().ok


# --- the gate ---------------------------------------------------------------

def _verify_req(evidence):
    return TransitionRequest(
        record_id="r1", src=State.UNDER_REVIEW, dst=State.VERIFIED,
        actor="v", role=Role.VERIFIER, proposer="p", evidence=evidence)


def test_a_fabricated_citation_passes_the_syntactic_check(store):
    """The hole this module exists to close, demonstrated before it is closed.

    ``"a" * 64`` is a perfectly well-formed digest of nothing. With no
    resolver, I6 accepts it -- which is why the resolver is not optional in
    any path that can make a record canonical.
    """
    edge = check(_verify_req({"verification_report": "a" * 64}))
    assert edge.dst is State.VERIFIED


def test_a_fabricated_citation_is_refused_once_a_resolver_is_supplied(store):
    with pytest.raises(TransitionError, match="cited but not stored"):
        check(_verify_req({"verification_report": "a" * 64}),
              resolve=store.contains)


def test_a_real_citation_is_accepted_by_the_same_gate(store):
    dg = store.put(REPORT, media_type="application/json")
    edge = check(_verify_req({"verification_report": dg}),
                 resolve=store.contains)
    assert edge.dst is State.VERIFIED


def test_a_citation_that_was_real_and_then_tampered_stops_resolving(store):
    """Evidence is not a one-time check: it is re-established at every gate."""
    dg = store.put(REPORT)
    req = _verify_req({"verification_report": dg})
    assert check(req, resolve=store.contains).dst is State.VERIFIED
    store._blob_path(dg).write_bytes(b"different conclusion")
    with pytest.raises(TransitionError, match="cited but not stored"):
        check(req, resolve=store.contains)


def test_an_extra_unrequired_key_holding_a_fake_digest_is_still_refused(store):
    """A fabricated citation is a fabrication whether or not the edge asks.

    An auditor reading the record later sees "supporting_data: <digest>" and
    has no way to know the edge never required it.
    """
    dg = store.put(REPORT)
    with pytest.raises(TransitionError, match=r"\['supporting_data'\]"):
        check(_verify_req({"verification_report": dg,
                           "supporting_data": "b" * 64}),
              resolve=store.contains)


def test_a_non_digest_annotation_is_not_treated_as_a_citation(store):
    """Free text is an annotation. Demanding it name a blob would be absurd."""
    dg = store.put(REPORT)
    edge = check(_verify_req({"verification_report": dg,
                              "note": "re-run after the seal was replaced"}),
                 resolve=store.contains)
    assert edge.dst is State.VERIFIED


def test_policy_id_is_an_identity_and_is_never_resolved(store):
    """``policy_id`` names a policy; it is not a digest of one."""
    dg = store.put(REPORT)
    edge = check(TransitionRequest(
        record_id="r1", src=State.VERIFIED, dst=State.PROMOTED,
        actor="promoter", role=Role.PROMOTER, proposer="p",
        evidence={"verification_report": dg, "policy_id": "policy-1"},
        policy_id="policy-1"), resolve=store.contains)
    assert edge.dst is State.PROMOTED


def test_require_resolvable_is_the_single_implementation_of_the_rule():
    """Both call sites go through one function, so they cannot disagree.

    ``authority.check`` and ``AuthorityStore.create`` enforce the same rule at
    different moments. Two implementations would drift; this asserts there is
    one.
    """
    import inspect

    from qta_agent import authority, store as store_mod
    for src in (inspect.getsource(authority.check),
                inspect.getsource(store_mod.AuthorityStore
                                  ._require_evidence_exists)):
        assert "require_resolvable" in src


def test_require_resolvable_takes_a_predicate_not_a_store():
    """Keeps the rule testable without a filesystem, and store-agnostic."""
    calls = []

    def resolver(dg):
        calls.append(dg)
        return dg == "d" * 64

    require_resolvable({"ok": "d" * 64}, resolver)
    assert calls == ["d" * 64]
    with pytest.raises(UnknownEvidence):
        require_resolvable({"bad": "e" * 64}, resolver)


# --- integration with the authority store -----------------------------------

def test_a_record_cannot_be_created_citing_evidence_that_does_not_exist(
        tmp_path):
    """Refused at creation, so the fabrication never enters the log.

    The log is append-only and hash-chained: if this were caught only at
    promotion, the fabricated citation would already be a permanent fact.
    """
    ev = EvidenceStore(tmp_path / "e")
    s = AuthorityStore(EventLog(tmp_path / "log.jsonl"), evidence=ev).load()
    with pytest.raises(StoreError, match="cited but not stored"):
        s.create(record_id="r1", kind="claim", proposer="p",
                 evidence={"basis": "a" * 64})
    assert s.log.verify().count == 0, "nothing may be logged on refusal"


def test_a_full_promotion_cycle_works_with_real_evidence(tmp_path):
    ev = EvidenceStore(tmp_path / "e")
    basis = ev.put(b"the claim's basis")
    report = ev.put(REPORT, media_type="application/json")
    s = AuthorityStore(EventLog(tmp_path / "log.jsonl"), evidence=ev).load()

    s.create(record_id="r1", kind="claim", proposer="p",
             evidence={"basis": basis})
    s.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="v",
                 role=Role.VERIFIER)
    s.transition(record_id="r1", dst=State.VERIFIED, actor="v",
                 role=Role.VERIFIER,
                 evidence={"verification_report": report})
    s.transition(record_id="r1", dst=State.PROMOTED, actor="promoter",
                 role=Role.PROMOTER, policy_id="policy-1",
                 evidence={"verification_report": report,
                           "policy_id": "policy-1"})
    assert s.get("r1").state is State.PROMOTED
    assert s.log.verify().ok


def test_promotion_is_refused_when_the_evidence_was_never_stored(tmp_path):
    ev = EvidenceStore(tmp_path / "e")
    basis = ev.put(b"the claim's basis")
    s = AuthorityStore(EventLog(tmp_path / "log.jsonl"), evidence=ev).load()
    s.create(record_id="r1", kind="claim", proposer="p",
             evidence={"basis": basis})
    s.transition(record_id="r1", dst=State.UNDER_REVIEW, actor="v",
                 role=Role.VERIFIER)
    with pytest.raises(TransitionError, match="cited but not stored"):
        s.transition(record_id="r1", dst=State.VERIFIED, actor="v",
                     role=Role.VERIFIER,
                     evidence={"verification_report": "f" * 64})
    assert s.get("r1").state is State.UNDER_REVIEW
    assert s.log.verify().ok


def test_a_store_with_no_evidence_attached_behaves_exactly_as_before(tmp_path):
    """The resolver is opt-in; omitting it must not change any other rule."""
    s = AuthorityStore(EventLog(tmp_path / "log.jsonl")).load()
    assert s._resolver is None
    s.create(record_id="r1", kind="claim", proposer="p",
             evidence={"basis": "a" * 64})
    assert s.get("r1").state is State.PROPOSED


# ---------------------------------------------------------------------------
# Mutation-isolating tests.
#
# Three mutations survived the first run of tools/mutations/agent_evidence.json.
# None of them was a redundant check; each was a real gap in what the tests
# above provoke. They are recorded here with what each mutation would actually
# cost, because "add a test until the matrix is green" is how a suite ends up
# asserting the wrong thing.
# ---------------------------------------------------------------------------

def test_an_oversized_file_is_diagnosed_as_oversized_not_as_growing(tmp_path):
    """E10: the pre-read size check, isolated from the streaming one.

    Both checks raise ``EvidenceTooLarge``, so the outcome alone cannot tell
    them apart -- which is why the mutation survived. They are not
    interchangeable: the streaming check's message says the file *grew while
    being read*, which for a file that was always too large is simply false
    and sends an operator hunting a race that never happened. The pre-read
    check also reports the actual size, so the operator learns how far over
    the bound they are rather than only that they are over it.
    """
    s = EvidenceStore(tmp_path / "e", max_blob_bytes=16)
    p = tmp_path / "big.txt"
    p.write_bytes(b"z" * 40)
    with pytest.raises(EvidenceTooLarge) as exc:
        s.put_file(p)
    assert "is 40 bytes, over the 16-byte bound" in str(exc.value)
    assert "grew" not in str(exc.value), (
        "a file that was already too large did not grow; that diagnosis "
        "would send an operator after a race that never happened")


def test_storing_again_does_not_silently_repair_a_tampered_blob(tmp_path):
    """E13: re-putting identical bytes must not overwrite what is on disk.

    This is the difference the idempotency test could not see, because with
    an intact store both branches write the same bytes. With a *tampered*
    store they differ completely: the guard reports the corruption, while
    overwriting erases the only evidence that anything was tampered with --
    and does it during a call the caller believes is a no-op.

    Silent repair is worse than the corruption. The corruption is detectable;
    a store that heals itself on the next write is not.
    """
    s = EvidenceStore(tmp_path / "e")
    dg = s.put(REPORT)
    s._blob_path(dg).write_bytes(b"substituted conclusion")

    with pytest.raises(CorruptEvidence):
        s.put(REPORT)

    # The tampering is still there to be found, and still reported.
    assert s._blob_path(dg).read_bytes() == b"substituted conclusion"
    assert not s.verify_store().ok


def test_an_intact_blob_is_not_rewritten_when_stored_again(tmp_path):
    """The other half of E13: the guard must not be paid for with a rewrite.

    Checked by inode rather than by content, since the content is identical
    either way. A rewrite would also reset the file's identity for anything
    tracking it -- backups, dedupe, an auditor's own records.
    """
    s = EvidenceStore(tmp_path / "e")
    dg = s.put(REPORT)
    first = s._blob_path(dg).stat().st_ino
    assert s.put(REPORT) == dg
    assert s._blob_path(dg).stat().st_ino == first, (
        "an idempotent put replaced the blob instead of leaving it alone")


def test_list_digests_never_yields_a_name_that_is_not_a_digest(tmp_path):
    """E17: a caller iterating the store must not receive a plausible lie.

    ``list_digests`` feeds audits and any caller enumerating held evidence.
    A stray filename yielded from here would be passed straight back into
    ``get``, where it would raise -- but the caller would already have
    reported it as evidence the store holds.
    """
    s = EvidenceStore(tmp_path / "e")
    dg = s.put(REPORT)
    prefix = s._blob_path(dg).parent
    (prefix / "notadigest").write_bytes(b"?")
    (prefix / ("z" * 62)).write_bytes(b"?")          # right length, bad chars
    (prefix / "DEADBEEF").write_bytes(b"?")          # uppercase

    listed = list(s.list_digests())
    assert listed == [dg], f"list_digests yielded non-digests: {listed}"
    assert all(len(x) == 64 for x in listed)

    # And the audit still reports them, because skipping them here must not
    # mean nobody ever hears about them.
    problems = s.verify_store().problems
    assert len(problems) == 3, problems


def test_two_writers_storing_identical_bytes_do_not_race_on_a_temp_name(
        tmp_path):
    """Content addressing means this race has no losing side.

    The metadata was written through a FIXED temporary name,
    ``<digest>.meta.tmp``, while the blob used a unique one. Two processes
    storing identical bytes therefore collided: the first renamed the temp
    away and the second's rename failed with FileNotFoundError. The crash was
    an artefact of the name, not of the situation -- both writers had the
    same bytes and the same digest, and neither had anything to lose.

    Reproduced here with threads and a barrier, so the two writes are inside
    the window rather than merely near it. The cross-process form is in
    tests/test_agent_concurrency.py, where it failed 3 runs in 8 before this
    was fixed.
    """
    import threading

    store = EvidenceStore(tmp_path / "evidence")
    payload = b"identical bytes from every writer" * 50
    ready = threading.Barrier(6)
    results: list = []
    errors: list = []

    def put():
        ready.wait(timeout=10)
        try:
            results.append(store.put(payload))
        except Exception as exc:                    # noqa: BLE001 - collected
            errors.append(exc)

    threads = [threading.Thread(target=put) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors[:3]
    assert len(set(results)) == 1
    assert store.get(results[0]) == payload
    assert store.verify_store().ok
    leftovers = [p.name for p in (tmp_path / "evidence").rglob("*")
                 if p.is_file() and p.name.endswith(".tmp")]
    assert not leftovers, f"temporary files were left behind: {leftovers}"
