"""Confined reads: the primitive, enforced at the open rather than the caller.

WHY THIS IS A PRIMITIVE AND NOT A HELPER

The write side of this project learned the lesson the expensive way. A guard
that validated an output directory and then let the caller call
``write_bytes`` on whatever it liked was advisory, and a recovered adversarial
test proved it: it overwrote README.md and wrote ``{"status": "PASS"}`` into
the canonical gate table during an ordinary test run. The fix was to move the
allowlist to the point of the write.

Reads had the same shape and had not been fixed. Every governed read in this
package was:

    validate a path string
    ... later ...
    open(that string)

and the gap between those two lines is where the file becomes a symlink to
somewhere else, or a FIFO that never returns, or a different inode entirely.
That is TOCTOU, and no amount of care in the validation closes it, because the
validation and the open are looking at a NAME while an attacker is changing
what the name points to.

WHAT THIS DOES INSTEAD

The authorized root is opened ONCE, as a directory descriptor. Every component
of the relative path is then opened descriptor-relative from its parent, with
``O_NOFOLLOW``, so a symlink anywhere along the way is refused rather than
followed. The final open yields a file descriptor, and from that moment the
kernel has bound the operation to an INODE: renaming, replacing or deleting
the name afterwards cannot redirect the read.

``O_NONBLOCK`` is on the final open for a specific reason: opening a FIFO for
reading blocks until a writer appears, so an attacker who substitutes a named
pipe turns a bounded read into an indefinite hang. With ``O_NONBLOCK`` the
open returns, ``fstat`` says it is a FIFO, and it is refused. The evidence
store learned this on its WRITE path and the read path never got the same
treatment.

WHAT THIS DOES *NOT* GUARANTEE, STATED RATHER THAN IMPLIED

  * **Not a sandbox.** This confines reads made THROUGH it. A process that
    calls ``open()`` itself is not affected. This is mediation.
  * **Hard links are detectable only as a COUNT.** A name inside the root
    may be another name for content outside it; ``O_NOFOLLOW`` says nothing
    about that, because a hard link is not a link, it is the file. What the
    kernel does report is ``st_nlink``, so ``require_unique_link=True``
    refuses any object reachable by more than one name. That is the right
    default for a governed artifact, which was written once by one tool and
    should have exactly one name -- and it is deliberately opt-in, because a
    legitimately hard-linked corpus is not an attack.
  * **Bind mounts and overlays are not detectable** portably. If the root
    itself is a mount somebody else controls, confinement to it means less
    than it appears to.
  * **The window before the open is not closed, only made harmless.** A file
    may be replaced between an authority decision and this call. What is
    guaranteed is that ONE coherent object is read, that it is a regular file
    inside the root, and -- when a digest is supplied -- that its bytes are
    the bytes that were expected. Callers for whom that matters must supply
    the digest; the identity returned lets them notice afterwards if they did
    not.
  * **``openat2(RESOLVE_BENEATH)`` would be stronger** and is not available:
    this Python exposes neither ``os.openat2`` nor ``os.RESOLVE_BENEATH``, so
    the per-component walk below is the strongest portable-on-Linux form
    available here. If a future runtime offers it, this is the module to
    change.

Case-normalization and Unicode-equivalence attacks are out of scope on the
case-sensitive filesystems this project targets; a case-insensitive filesystem
would need the root's own semantics consulted, and that is not attempted here
rather than being claimed and unimplemented.
"""
from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass

from .canonical import digest_bytes

#: Refused above this. A read with no bound is a denial of service wearing a
#: pathname: /dev/zero is 0 bytes to ``stat`` and infinite to ``read``.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024

#: Read granularity. Bounded so a growing file cannot be read unboundedly
#: between the size check and the end of the loop.
CHUNK_BYTES = 1 << 20


class SafeIOError(Exception):
    """Base for every refusal in this module. Always fail closed."""


class PathRefused(SafeIOError):
    """The requested path is not expressible inside the root."""


class SymlinkRefused(SafeIOError):
    """A component of the path is a symbolic link."""


class RootMissing(PathRefused, FileNotFoundError):
    """The read root is not there at all.

    Inherits BOTH, and the reason is a defect this class was written after.
    Wrapping every OSError from opening the root turned "the store directory
    does not exist yet" into a generic refusal, and the evidence store --
    which told UNKNOWN evidence from CORRUPT evidence by catching
    FileNotFoundError -- began reporting a store that had never been written
    to as corrupt. Those are different facts and an operator acts on them
    differently.

    So a caller catching :class:`FileNotFoundError` still sees a missing
    root, a caller catching :class:`SafeIOError` still sees a refusal, and
    neither has to know about the other.
    """


class ReadFailed(SafeIOError):
    """The kernel refused partway through a read that had already begun.

    Distinct from :class:`PathRefused`, which is about reaching the file at
    all. This is about the file being reachable and the read failing anyway
    -- a failing disk, a device that errored, a descriptor limit hit
    mid-stream -- and what matters about it is that no bytes come back.
    """


class NotARegularFile(SafeIOError):
    """The opened object is not a regular file."""


class ReadTooLarge(SafeIOError):
    """The object is, or became, larger than the caller's bound."""


class AliasedFile(SafeIOError):
    """The object has more than one name, so its content is reachable by a
    name the authority never saw."""


class SourceChanged(SafeIOError):
    """The bytes read are not the bytes the caller expected.

    Carries both digests as attributes so a caller wrapping this in its own
    vocabulary does not have to parse the message to say what it means.
    """

    def __init__(self, message: str, *, actual: str = "",
                 expected: str = ""):
        super().__init__(message)
        self.actual = actual
        self.expected = expected


@dataclass(frozen=True)
class ObjectIdentity:
    """What was actually opened, as the kernel saw it.

    Recorded rather than the pathname because the pathname is what an
    attacker controls. ``(device, inode)`` names one object on one filesystem;
    two reads reporting the same pair read the same thing.
    """

    device: int
    inode: int
    size: int
    mtime_ns: int

    def to_record(self) -> dict:
        return {"device": self.device, "inode": self.inode,
                "size": self.size, "mtime_ns": self.mtime_ns}


@dataclass(frozen=True)
class ReadResult:
    """Bytes, and everything needed to argue about where they came from."""

    data: bytes
    digest: str
    identity: ObjectIdentity
    #: The relative path as requested. Diagnostic only: the identity above is
    #: what actually names the object.
    requested: str

    def to_record(self) -> dict:
        return {"requested": self.requested, "digest": self.digest,
                "identity": self.identity.to_record(),
                "bytes": len(self.data)}


def split_relative(rel: str) -> tuple:
    """Path components, or refuse. No absolute paths, no ``..``, no empties.

    Refused BEFORE any filesystem call, so a hostile path never reaches the
    kernel and the error names what was wrong rather than reporting ENOENT.
    """
    if not isinstance(rel, str) or not rel:
        raise PathRefused(
            f"a relative path is required, got {rel!r}; an empty path names "
            "the root itself, which is a directory and not readable content")
    if rel.startswith("/"):
        raise PathRefused(
            f"{rel!r} is absolute; a governed read names a resource INSIDE "
            "its authorized root, and an absolute path is a request to leave "
            "it")
    if "\x00" in rel:
        raise PathRefused("a path may not contain a NUL byte")
    parts = []
    for part in rel.split("/"):
        if part in ("", "."):
            continue                      # "a//b" and "a/./b" mean "a/b"
        if part == "..":
            raise PathRefused(
                f"{rel!r} contains '..'; the root is a boundary, and a path "
                "that climbs out of it is refused by name rather than "
                "resolved and then checked")
        parts.append(part)
    if not parts:
        raise PathRefused(f"{rel!r} names no component inside the root")
    return tuple(parts)


def open_beneath(root_fd: int, rel: str) -> int:
    """Open ``rel`` beneath ``root_fd``, refusing every symlink on the way.

    Returns a file descriptor the caller must close. The descriptor is bound
    to the inode, so what happens to the NAME afterwards cannot change what is
    read through it.
    """
    parts = split_relative(rel)
    cur = root_fd
    opened: list = []
    try:
        for part in parts[:-1]:
            fd = _openat(cur, part,
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                         rel=rel, part=part, expecting="a directory")
            opened.append(fd)
            cur = fd
        # O_NONBLOCK: a substituted FIFO must not turn this into a hang.
        return _openat(cur, parts[-1],
                       os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                       rel=rel, part=parts[-1], expecting="a regular file")
    finally:
        for fd in opened:
            os.close(fd)


def _openat(dir_fd: int, name: str, flags: int, *, rel: str, part: str,
            expecting: str) -> int:
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise SymlinkRefused(
                f"{rel!r}: component {part!r} is a symbolic link. It is "
                "refused rather than followed, because the target is chosen "
                "by whoever can write the directory rather than by the "
                "authority that permitted this read.") from None
        if exc.errno == errno.ENOTDIR:
            # O_DIRECTORY|O_NOFOLLOW on a symlink-TO-a-directory reports
            # ENOTDIR, not ELOOP: O_NOFOLLOW made the kernel open the link
            # itself, and a link is not a directory. The read is already
            # refused either way; this second look decides only WHICH refusal
            # to report, because "a component is a symlink" and "a component
            # is a regular file" send an operator to different places.
            #
            # It is deliberately after the refusal and cannot widen it: if
            # the name changed again in between, the worst outcome is a
            # misleading message about a read that did not happen.
            if _is_symlink_at(dir_fd, name):
                raise SymlinkRefused(
                    f"{rel!r}: component {part!r} is a symbolic link to a "
                    "directory. It is refused rather than followed: a linked "
                    "parent moves the whole subtree somewhere the authority "
                    "never named.") from None
            raise PathRefused(
                f"{rel!r}: component {part!r} is not a directory, so the "
                "path cannot continue through it") from None
        if exc.errno == errno.ENXIO:
            # A UNIX socket, or a device with no driver behind it. open()
            # refuses it outright, so it never becomes a readable object.
            raise NotARegularFile(
                f"{rel!r}: {part!r} cannot be opened as a file (it is a "
                "socket or an unbacked device node), so it is not a "
                "bounded source of bytes") from None
        if exc.errno == errno.ENOENT:
            raise FileNotFoundError(
                f"{rel!r}: {part!r} does not exist beneath the root") from None
        if exc.errno == errno.EACCES:
            raise PathRefused(
                f"{rel!r}: {part!r} is not readable by this process") from None
        raise SafeIOError(
            f"{rel!r}: opening {part!r} (expecting {expecting}) failed: "
            f"{exc.strerror}") from exc


def _is_symlink_at(dir_fd: int, name: str) -> bool:
    """Diagnostic only. Never used to permit anything."""
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode)


def read_beneath(root_fd: int, rel: str, *,
                 max_bytes: int = DEFAULT_MAX_BYTES,
                 expect_digest: str | None = None,
                 # DEFAULT-ON. This was opt-in per call, which meant a
                 # caller that did not think to ask did not get it -- and
                 # the caller most likely not to think of it is a new one.
                 # A second name for an inode is a second way in whatever
                 # the caller intended, so the safe answer is the default
                 # and relaxing it is the deliberate act.
                 require_unique_link: bool = True) -> ReadResult:
    """Read one regular file beneath ``root_fd``. The whole primitive.

    ``expect_digest`` binds the result to CONTENT rather than to a name. It is
    the only thing here that survives an attacker who can replace the file
    before the open: the read succeeds, the digest disagrees, and the caller
    is told the source changed instead of acting on substituted bytes.
    """
    if not isinstance(max_bytes, int) or max_bytes < 1:
        raise SafeIOError(f"max_bytes must be a positive int, got "
                          f"{max_bytes!r}")
    fd = open_beneath(root_fd, rel)
    try:
        st = os.fstat(fd)
        # The OPENED OBJECT is the subject, not the pathname that reached it.
        if not stat.S_ISREG(st.st_mode):
            raise NotARegularFile(
                f"{rel!r} is {_describe(st.st_mode)}, not a regular file. A "
                "bounded read of a pipe, device or socket is not a bounded "
                "read at all.")
        if require_unique_link and st.st_nlink > 1:
            # A second name for the same inode is a second way in, and it
            # can live anywhere on the filesystem -- including outside the
            # root this read is confined to. The count is the only part of
            # that the kernel will tell us, and for an artifact written once
            # by one tool, a count above one is already the answer.
            raise AliasedFile(
                f"{rel!r} has {st.st_nlink} names. A hard link is not a "
                "link, it is the file, so another name for this content may "
                "sit outside the authorized root entirely.")
        if st.st_size > max_bytes:
            raise ReadTooLarge(
                f"{rel!r} is {st.st_size} bytes, over the {max_bytes}-byte "
                "bound")
        chunks: list = []
        total = 0
        while True:
            try:
                chunk = os.read(fd, CHUNK_BYTES)
            except OSError as exc:
                # A read that failed PARTWAY must not return what it got.
                # Bytes that look like the file and are not are the worst
                # outcome available here, and an OSError escaping raw is how
                # a caller ends up handling one: it catches SafeIOError, and
                # gets something else in the middle of a governed read.
                raise ReadFailed(
                    f"{rel!r} failed partway through: "
                    f"{exc.__class__.__name__}: {exc}. {total} byte(s) had "
                    "been read and are discarded; a truncated read that "
                    "returned successfully would be indistinguishable from "
                    "the file") from exc
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ReadTooLarge(
                    f"{rel!r} grew past the {max_bytes}-byte bound while it "
                    "was being read")
            chunks.append(chunk)
        data = b"".join(chunks)
        identity = ObjectIdentity(device=st.st_dev, inode=st.st_ino,
                                  size=st.st_size, mtime_ns=st.st_mtime_ns)
    finally:
        os.close(fd)

    dg = digest_bytes(data)
    if expect_digest is not None and dg != expect_digest:
        raise SourceChanged(
            f"{rel!r} hashes to {dg[:12]}... but {expect_digest[:12]}... was "
            "expected. The name resolved to something other than the content "
            "that was authorized.", actual=dg, expected=expect_digest)
    return ReadResult(data=data, digest=dg, identity=identity, requested=rel)


def _describe(mode: int) -> str:
    for pred, name in ((stat.S_ISDIR, "a directory"),
                       (stat.S_ISFIFO, "a FIFO"),
                       (stat.S_ISSOCK, "a socket"),
                       (stat.S_ISCHR, "a character device"),
                       (stat.S_ISBLK, "a block device"),
                       (stat.S_ISLNK, "a symbolic link")):
        if pred(mode):
            return name
    return f"of type {stat.filemode(mode)}"


class ReadRoot:
    """An authorized root, held open as a descriptor for the whole session.

    Held open on purpose. Re-resolving the root by name on every read would
    reintroduce exactly the window this module exists to close: the root
    itself can be renamed or replaced between two reads.
    """

    def __init__(self, path, *, max_bytes: int = DEFAULT_MAX_BYTES):
        self.path = os.fspath(path)
        self.max_bytes = max_bytes
        self._fd: int | None = None

    def __enter__(self) -> "ReadRoot":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> "ReadRoot":
        if self._fd is None:
            flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
            try:
                self._fd = os.open(self.path, flags)
            except NotADirectoryError:
                raise PathRefused(
                    f"{self.path!r} is not a directory; a read root is a "
                    "subtree, not a file") from None
            except FileNotFoundError as exc:
                # ABSENT is its own fact. A caller that tells "nothing has
                # been stored" from "what is stored is damaged" does it by
                # catching this, and collapsing it into the general refusal
                # below made an empty store look corrupt.
                raise RootMissing(
                    f"{self.path!r} does not exist, so there is no read root "
                    "to confine anything to") from exc
            except OSError as exc:
                # EVERY other refusal too. This caught NotADirectoryError
                # alone, so an unreadable root, or a root that vanished
                # between two sessions, escaped as a raw OSError -- past
                # every caller written to handle this module's errors, at the
                # one moment a read root is being established.
                raise PathRefused(
                    f"{self.path!r} could not be opened as a read root: "
                    f"{exc.__class__.__name__}: {exc}") from exc
        return self

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    @property
    def fd(self) -> int:
        if self._fd is None:
            raise SafeIOError(
                "the read root is not open; a closed root authorizes nothing")
        return self._fd

    def read(self, rel: str, *, max_bytes: int | None = None,
             expect_digest: str | None = None,
             require_unique_link: bool = True) -> ReadResult:
        return read_beneath(self.fd, rel, expect_digest=expect_digest,
                            require_unique_link=require_unique_link,
                            max_bytes=self.max_bytes if max_bytes is None
                            else max_bytes)
