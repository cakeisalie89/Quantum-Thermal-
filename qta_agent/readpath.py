"""Governed reads: who may read what, decided before the open and recorded.

WHY THIS IS SEPARATE FROM :mod:`qta_agent.safeio`

``safeio`` is a MECHANISM: it confines a read to a subtree, refuses symlinks
and special files, and binds the operation to an inode instead of a name. It
answers "is this read safe to perform". It does not, and should not, answer
"is this reader allowed to perform it" -- a primitive that consulted a
capability set would be unusable by the evidence store and the event log,
which read their own storage and have no subject to authorize.

This module is the AUTHORITY. It answers the second question, records the
answer, and then delegates the actual read to the primitive. The split is the
same one the write side already makes: the write allowlist lives in the
writer, and the capability check lives above it.

DEFAULT DENY

A reader with no capability reads nothing. There is no "unauthorized but
harmless" path here, because the caller deciding a read is harmless is the
caller whose judgement is in question.

WHAT IS RECORDED

Every attempt -- permitted or refused -- appends a ``file.read`` event naming
the actor, the task, the capability, the root, the requested resource, and,
when the read happened, the identity of the object that was actually opened
and the digest of its bytes. Denials are recorded for the same reason policy
denials are: "what did this agent try" is the question an incident starts
with, and a control plane that logs only successes cannot answer it.

The bytes themselves are never logged. The digest identifies them; the
content may be a secret, and an audit trail is not a place to put one.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .capability import Action, CapabilityError, Request
from .safeio import ReadResult, ReadRoot, SafeIOError, split_relative

#: The action this module writes. Owned here; see qta_agent.actions.
ACT_FILE_READ = "file.read"


class ReadDenied(Exception):
    """No live capability authorizes this read. Default deny."""


@dataclass(frozen=True)
class ReadRequest:
    """What is being asked, described independently of who is asking.

    Mirrors :class:`~qta_agent.capability.Request` and
    :class:`~qta_agent.policy.PolicyRequest`: the decision compares a
    described request against a described grant and cannot consult the
    caller.
    """

    actor: str
    task_id: str
    #: Logical name of the root, e.g. "workspace" or "evidence". Diagnostic;
    #: the reader holds the descriptor that actually confines the read.
    root_id: str
    #: Path relative to that root.
    resource: str
    #: Why this is being read. Recorded, never consulted by the decision --
    #: a purpose the caller writes cannot widen a grant.
    purpose: str = ""
    tool_id: str = ""

    def to_record(self) -> dict:
        return {"actor": self.actor, "task_id": self.task_id,
                "root_id": self.root_id, "resource": self.resource,
                "purpose": self.purpose, "tool_id": self.tool_id}


class GovernedReader:
    """Capability-checked, audited, confined reads from one authorized root.

    The root descriptor is opened once and held. A reader whose root has been
    closed authorizes nothing, which is the honest behaviour: the subtree it
    was confining may no longer be the subtree of that name.
    """

    def __init__(self, log, *, root_id: str, root_path,
                 capabilities=None, max_bytes: int | None = None):
        self.log = log
        self.root_id = root_id
        self._root = ReadRoot(root_path, **(
            {"max_bytes": max_bytes} if max_bytes else {}))
        #: A CapabilitySet, or None. None means default-deny for every read:
        #: a reader with no grants is not a reader with all of them.
        self.capabilities = capabilities

    # ---- lifecycle -----------------------------------------------------
    def open(self) -> "GovernedReader":
        self._root.open()
        return self

    def close(self) -> None:
        self._root.close()

    def __enter__(self) -> "GovernedReader":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def root_path(self) -> str:
        return self._root.path

    # ---- the decision --------------------------------------------------
    def authorize(self, req: ReadRequest, *, capability_id: str) -> None:
        """Refuse unless a live grant covers this exact read.

        The path is validated here TOO, before the capability is consulted,
        so a traversal attempt is refused as a traversal attempt rather than
        as a scope miss. The two produce different operator actions.
        """
        split_relative(req.resource)          # raises PathRefused
        if self.capabilities is None:
            raise ReadDenied(
                f"{req.actor!r} holds no capabilities, so no read is "
                "authorized. Reading is default-deny: absence of a rule is "
                "not an allow rule.")
        scoped = f"{self.root_id}/{req.resource}"
        try:
            self.capabilities.check(capability_id, Request(
                actor=req.actor, action=Action.READ_PATHS,
                task_id=req.task_id, tool_id=req.tool_id,
                paths=(scoped,)))
        except CapabilityError as exc:
            raise ReadDenied(str(exc)) from exc

    # ---- the read ------------------------------------------------------
    def read(self, req: ReadRequest, *, capability_id: str,
             expect_digest: str | None = None,
             max_bytes: int | None = None,
             require_unique_link: bool = True) -> ReadResult:
        """Authorize, read through the confined primitive, record both."""
        try:
            self.authorize(req, capability_id=capability_id)
        except Exception as exc:              # noqa: BLE001 - recorded
            self._record(req, capability_id, allowed=False,
                         reason=f"{type(exc).__name__}: {exc}")
            raise
        try:
            result = self._root.read(req.resource, max_bytes=max_bytes,
                                     expect_digest=expect_digest,
                                     require_unique_link=require_unique_link)
        except (SafeIOError, OSError) as exc:
            self._record(req, capability_id, allowed=False,
                         reason=f"{type(exc).__name__}: {exc}")
            raise
        self._record(req, capability_id, allowed=True, reason="read",
                     result=result)
        return result

    def _record(self, req: ReadRequest, capability_id: str, *,
                allowed: bool, reason: str,
                result: ReadResult | None = None) -> None:
        payload = {"request": req.to_record(), "capability_id": capability_id,
                   "allowed": allowed, "reason": reason,
                   "root_path": self._root.path}
        if result is not None:
            # The identity of what was OPENED, and the digest of the bytes.
            # Never the bytes: a read is exactly how a secret would get here.
            payload["result"] = result.to_record()
        self.log.append(actor=req.actor, action=ACT_FILE_READ,
                        target=req.task_id or req.resource, payload=payload)


def read_scope(root_id: str, *paths: str) -> tuple:
    """Capability scope entries for reads under ``root_id``.

    Scopes are namespaced by root so a grant to read
    ``workspace/verification`` cannot be spent against an ``evidence`` root
    that happens to have a path of the same name.
    """
    return tuple(f"{root_id}/{p.lstrip('/')}" for p in paths)


def identity_of(path) -> tuple:
    """``(device, inode)`` for a path, or None. Diagnostic, for tests."""
    try:
        st = os.lstat(os.fspath(path))
    except OSError:
        return None
    return (st.st_dev, st.st_ino)
