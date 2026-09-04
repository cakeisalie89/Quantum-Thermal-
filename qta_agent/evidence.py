"""Content-addressed evidence: what a cited digest actually resolves to.

THE HOLE THIS CLOSES

``authority.check`` enforces invariant I6 -- "every transition requires the
evidence its edge declares" -- by requiring each evidence value to be a
SHA-256 digest. That check is syntactic. Until this module existed, an agent
could promote a record by citing ``"a" * 64`` as its verification report: a
perfectly well-formed digest of nothing at all. I6 read like a guarantee that
evidence exists and cannot change after being cited, and delivered only the
first half of the second clause.

This store makes the guarantee real. A digest is a *name*; the store is what
turns a name into bytes, and it refuses to do so unless the bytes hash back to
the name. Wire it in with ``authority.check(req, resolve=store.contains)`` and
a fabricated citation is rejected at the gate rather than discovered later.

WHAT IS AND IS NOT TRUSTED

Trusted: the digest supplied by the caller, and nothing else.

Not trusted: the store's own directory layout. A blob's path is derived from
its digest, but the filesystem is not a party to the integrity claim -- files
can be renamed, replaced, symlinked, or swapped by anything with write access
to the directory. Every read therefore re-hashes the content and compares. If
that comparison is ever removed, this module degrades to a filesystem with
extra steps; ``tests/test_agent_evidence.py`` mutates it out to prove the
tests notice.

Not trusted: the metadata sidecar. It records media type and first-seen time
for auditing. It is deliberately outside the digest, because the digest must
cover the evidence and only the evidence -- two agents that store identical
bytes with different media types must agree on the digest. The consequence is
that sidecar content carries no integrity guarantee, and this module never
makes a decision based on it.

APPEND-ONLY

There is no delete. Evidence cited by a transition must remain resolvable for
as long as that transition is in the log, and the store cannot know which
digests are cited. Lifecycle -- expiry, archival, legal hold -- is a policy
decision made against the log, not a capability handed to every caller.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .canonical import digest_bytes, is_digest

#: Refuse rather than truncate. Evidence is reports and manifests; a blob at
#: this size is a mistake or an attack, and either way the caller should hear
#: about it instead of storing a prefix that hashes to something else.
MAX_BLOB_BYTES = 256 * 1024 * 1024

#: Streaming chunk for hashing. Bounded so a large blob does not have to be
#: resident to be verified.
CHUNK_BYTES = 1024 * 1024

#: Fan-out width. 256 top-level directories keeps any one directory small
#: enough that listing it stays cheap on ordinary filesystems.
_FANOUT = 2


class EvidenceError(Exception):
    """Base class. Every failure here is fail-closed."""


class UnknownEvidence(EvidenceError):
    """The digest names nothing this store holds."""


class CorruptEvidence(EvidenceError):
    """Stored bytes do not hash to the digest that names them."""


class EvidenceTooLarge(EvidenceError):
    """Content exceeds the store's bound."""


class MalformedDigest(EvidenceError):
    """A digest that is not 64 lowercase hex characters cannot name a blob."""


@dataclass(frozen=True)
class BlobInfo:
    """Auditing metadata. Carries no integrity guarantee -- see module docs."""
    digest: str
    size: int
    media_type: str
    first_seen: float

    def to_record(self) -> dict:
        return {"digest": self.digest, "size": self.size,
                "media_type": self.media_type, "first_seen": self.first_seen}


@dataclass(frozen=True)
class StoreReport:
    """Outcome of a whole-store audit. Structured, never a bare bool."""
    ok: bool
    count: int
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def raise_if_bad(self) -> "StoreReport":
        if not self.ok:
            raise CorruptEvidence("; ".join(self.problems) or "store invalid")
        return self


def _require_digest(value: object) -> str:
    """Validate before the value is ever used to build a path.

    Path traversal is impossible downstream *because* of this function: 64
    characters drawn from ``[0-9a-f]`` cannot contain a separator or a dot
    segment. That is a load-bearing property, not a happy accident, so the
    check happens here once and every path-building site calls it first.
    """
    if not is_digest(value):
        shown = value if isinstance(value, str) else type(value).__name__
        raise MalformedDigest(
            f"{shown!r} is not a lowercase sha256 digest; uppercase is "
            "refused rather than normalized, because on a case-insensitive "
            "filesystem two spellings would name one blob while comparing "
            "unequal in Python")
    return str(value)


class EvidenceStore:
    """A content-addressed, append-only, self-verifying blob store."""

    def __init__(self, root: Path | str, *,
                 max_blob_bytes: int = MAX_BLOB_BYTES):
        self.root = Path(root)
        self.max_blob_bytes = int(max_blob_bytes)
        if self.max_blob_bytes <= 0:
            raise ValueError("max_blob_bytes must be positive")

    # ---- layout -------------------------------------------------------
    def _blob_path(self, dg: str) -> Path:
        return self.root / dg[:_FANOUT] / dg[_FANOUT:]

    def _meta_path(self, dg: str) -> Path:
        return self.root / dg[:_FANOUT] / (dg[_FANOUT:] + ".meta.json")

    # ---- writing ------------------------------------------------------
    def put(self, content: bytes, *,
            media_type: str = "application/octet-stream") -> str:
        """Store ``content``; return its digest. Idempotent by construction."""
        if not isinstance(content, (bytes, bytearray)):
            raise EvidenceError(
                f"evidence must be bytes, got {type(content).__name__}; "
                "callers holding an object should canonicalize it first so "
                "the digest is over a defined byte sequence")
        content = bytes(content)
        if len(content) > self.max_blob_bytes:
            raise EvidenceTooLarge(
                f"{len(content)} bytes exceeds the {self.max_blob_bytes}-byte "
                "bound")
        dg = digest_bytes(content)
        self._write_blob(dg, content, media_type)
        return dg

    def put_file(self, path: Path | str, *,
                 media_type: str = "application/octet-stream") -> str:
        """Store a file's bytes, hashing in bounded chunks.

        Refuses anything that is not a regular file *before* opening it. A
        FIFO would otherwise block this call forever waiting for a writer that
        may never come, turning a storage call into a hang -- a denial of
        service that looks like a slow disk.
        """
        p = Path(path)
        st = p.lstat()          # lstat: do not follow a symlink to decide
        if stat.S_ISLNK(st.st_mode):
            raise EvidenceError(
                f"{p} is a symlink; refusing to store the bytes of "
                "whatever it points at under a name the caller chose for "
                "the link")
        if not stat.S_ISREG(st.st_mode):
            raise EvidenceError(
                f"{p} is not a regular file ({stat.filemode(st.st_mode)}); "
                "refusing rather than blocking on a FIFO or reading a device")
        if st.st_size > self.max_blob_bytes:
            raise EvidenceTooLarge(
                f"{p} is {st.st_size} bytes, over the "
                f"{self.max_blob_bytes}-byte bound")

        h = hashlib.sha256()
        total = 0
        with p.open("rb") as fh:
            while True:
                chunk = fh.read(CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_blob_bytes:
                    # The file grew between lstat and read.
                    raise EvidenceTooLarge(
                        f"{p} grew past the {self.max_blob_bytes}-byte bound "
                        "while being read")
                h.update(chunk)
        dg = h.hexdigest()

        # Re-read to write. The file could have changed between the hashing
        # pass and this one, so the written blob is verified against dg before
        # it is published; a mismatch means the source was not stable and the
        # call fails rather than storing bytes under the wrong name.
        content = p.read_bytes()
        if digest_bytes(content) != dg:
            raise CorruptEvidence(
                f"{p} changed while being stored; its digest was {dg[:12]} "
                "during hashing and differs on re-read")
        self._write_blob(dg, content, media_type)
        return dg

    def _write_blob(self, dg: str, content: bytes, media_type: str) -> None:
        """Atomically publish ``content`` at ``dg``, or leave the store alone.

        Written to a temporary name in the destination directory, fsynced,
        then renamed. A crash therefore leaves either nothing or a complete
        blob -- never a prefix that a later reader would have to distinguish
        from real evidence.
        """
        blob = self._blob_path(dg)
        blob.parent.mkdir(parents=True, exist_ok=True)

        if blob.exists():
            # Already held. Do not rewrite: the existing bytes are what any
            # prior citation referred to. If they no longer hash to dg the
            # store is corrupt, and silently overwriting would erase the
            # evidence of that.
            existing = self._read_verified(dg)
            if digest_bytes(existing) != dg:   # pragma: no cover
                raise CorruptEvidence(f"{dg[:12]} is already stored corrupt")
            self._write_meta_if_absent(dg, len(content), media_type)
            return

        fd, tmp_name = tempfile.mkstemp(dir=str(blob.parent), prefix=".tmp-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, blob)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self._fsync_dir(blob.parent)
        self._write_meta_if_absent(dg, len(content), media_type)

    def _write_meta_if_absent(self, dg: str, size: int,
                              media_type: str) -> None:
        meta = self._meta_path(dg)
        if meta.exists():
            # First-seen is first-seen. A second put of identical bytes does
            # not restate when the store learned them.
            return
        import time
        rec = {"digest": dg, "size": size, "media_type": str(media_type),
               "first_seen": time.time()}
        tmp = meta.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, sort_keys=True, separators=(",", ":")),
                       encoding="utf-8")
        os.replace(tmp, meta)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError:                 # pragma: no cover
            return
        try:
            os.fsync(fd)
        except OSError:                 # pragma: no cover
            pass
        finally:
            os.close(fd)

    # ---- reading ------------------------------------------------------
    def get(self, dg: str) -> bytes:
        """Return the bytes named by ``dg``, or raise. Verified on every read.

        There is no unverified read path in this class, deliberately. A
        caller that has already been handed bytes has already acted on them;
        offering a fast lane would mean offering a way to act on content the
        store never checked.
        """
        return self._read_verified(_require_digest(dg))

    def _read_verified(self, dg: str) -> bytes:
        blob = self._blob_path(dg)
        try:
            st = blob.lstat()
        except FileNotFoundError:
            raise UnknownEvidence(
                f"no evidence stored for {dg[:12]}...; a citation is not "
                "self-validating -- the content must have been "
                "stored") from None
        if stat.S_ISLNK(st.st_mode):
            raise CorruptEvidence(
                f"{dg[:12]}... is a symlink in the store; refusing to follow "
                "it, because the link target is chosen by whoever can write "
                "the directory rather than by the digest")
        if not stat.S_ISREG(st.st_mode):
            raise CorruptEvidence(
                f"{dg[:12]}... is not a regular file "
                f"({stat.filemode(st.st_mode)})")
        if st.st_size > self.max_blob_bytes:
            raise EvidenceTooLarge(
                f"{dg[:12]}... is {st.st_size} bytes, over the "
                f"{self.max_blob_bytes}-byte bound")
        content = blob.read_bytes()
        actual = digest_bytes(content)
        if actual != dg:
            raise CorruptEvidence(
                f"stored bytes hash to {actual[:12]}... but are filed under "
                f"{dg[:12]}...; the store's layout is not evidence of "
                "anything, so the content is refused")
        return content

    def contains(self, dg: object, *, verify: bool = True) -> bool:
        """Does this store hold resolvable evidence named ``dg``?

        Verifying by default is the whole point: an unverified containment
        check answers "is there a file at that path", which is the question a
        tamperer wants asked. Pass ``verify=False`` only where the cost
        matters and a later read will verify anyway -- and say so at the call
        site.

        A malformed digest is False rather than an exception, because callers
        use this as a predicate over untrusted input.
        """
        if not is_digest(dg):
            return False
        dgs = str(dg)
        if not verify:
            path = self._blob_path(dgs)
            return path.is_file() and not path.is_symlink()
        try:
            self._read_verified(dgs)
        except EvidenceError:
            return False
        return True

    def info(self, dg: str) -> BlobInfo:
        """Auditing metadata for a held blob.

        The size is taken from the verified content, not from the sidecar, so
        a caller cannot be misled about how much evidence they are citing.
        Media type and first-seen come from the sidecar and are exactly as
        trustworthy as the directory they sit in -- which is to say, not.
        """
        dgs = _require_digest(dg)
        content = self._read_verified(dgs)
        media_type, first_seen = "application/octet-stream", 0.0
        meta = self._meta_path(dgs)
        if meta.is_file():
            try:
                rec = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                rec = {}
            if isinstance(rec, dict):
                mt = rec.get("media_type")
                fs = rec.get("first_seen")
                if isinstance(mt, str) and mt:
                    media_type = mt
                if isinstance(fs, (int, float)) and not isinstance(fs, bool):
                    first_seen = float(fs)
        return BlobInfo(dgs, len(content), media_type, first_seen)

    # ---- auditing -----------------------------------------------------
    def list_digests(self) -> Iterator[str]:
        """Every name the store's layout claims to hold, in sorted order.

        Claims, not facts: a name here has not been verified. ``verify_store``
        is what turns the claim into a finding. Entries whose layout does not
        spell a digest are skipped here and reported there, so that a caller
        iterating this never receives a string it could mistake for one.
        """
        if not self.root.is_dir():
            return
        for prefix in sorted(p for p in self.root.iterdir() if p.is_dir()):
            for entry in sorted(prefix.iterdir()):
                if entry.name.endswith(".meta.json") or entry.name.startswith(
                        ".tmp-"):
                    continue
                candidate = prefix.name + entry.name
                if is_digest(candidate):
                    yield candidate

    def verify_store(self) -> StoreReport:
        """Re-hash everything. Report every problem, never just the first."""
        problems: list = []
        notes: list = []
        count = 0
        if not self.root.exists():
            return StoreReport(True, 0, [], ["store directory does not exist"])
        if not self.root.is_dir():
            return StoreReport(False, 0, [f"{self.root} is not a directory"])

        for prefix in sorted(p for p in self.root.iterdir()):
            if not prefix.is_dir():
                problems.append(
                    f"{prefix.name}: unexpected entry at the store root")
                continue
            if len(prefix.name) != _FANOUT or not is_digest(
                    prefix.name + "0" * (64 - _FANOUT)):
                problems.append(
                    f"{prefix.name}: not a digest-prefix directory")
                continue
            for entry in sorted(prefix.iterdir()):
                if entry.name.startswith(".tmp-"):
                    notes.append(
                        f"{entry.name}: abandoned temporary file; a write "
                        "crashed before publishing, which is the safe outcome")
                    continue
                if entry.name.endswith(".meta.json"):
                    continue
                candidate = prefix.name + entry.name
                if not is_digest(candidate):
                    problems.append(
                        f"{prefix.name}/{entry.name}: filename does not spell "
                        "a digest")
                    continue
                count += 1
                try:
                    self._read_verified(candidate)
                except EvidenceError as exc:
                    problems.append(f"{candidate[:12]}...: {exc}")
        return StoreReport(not problems, count, problems, notes)


def require_resolvable(evidence: dict, resolve, *,
                       skip: frozenset = frozenset({"policy_id"})) -> None:
    """Raise unless every citation in ``evidence`` resolves via ``resolve``.

    This is the *single* implementation of the rule "a cited digest must name
    stored content". :func:`authority.check` calls it at the transition gate
    and :class:`store.AuthorityStore` calls it at record creation; both go
    through here so the two cannot drift into disagreeing about what counts
    as a citation.

    ``resolve`` is a predicate over a digest rather than a store, so the rule
    stays testable without a filesystem and so a caller may supply a resolver
    backed by something other than :class:`EvidenceStore` -- a remote store, a
    cache, a union of both.

    A value is treated as a citation exactly when it is syntactically a
    digest. Non-digest values are annotations and are not resolved: the
    alternative is to demand that free-text notes name blobs. Keys in
    ``skip`` are identities rather than content references -- ``policy_id``
    names a policy, it is not a digest of one.
    """
    unresolved = []
    for key in sorted(evidence):
        if key in skip:
            continue
        val = evidence[key]
        if not is_digest(val):
            continue
        if not resolve(val):
            unresolved.append(key)
    if unresolved:
        raise UnknownEvidence(
            f"evidence {unresolved} is cited but not stored; a digest is a "
            "name, and a name that resolves to nothing is an assertion, not "
            "evidence")
