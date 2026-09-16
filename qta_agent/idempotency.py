"""Durable, authority-scoped idempotency for governed submissions.

WHAT THIS IS, AND WHAT IT IS EMPHATICALLY NOT

This is DURABLE REQUEST IDENTITY plus DUPLICATE SUPPRESSION. A submitter
that resends the same request under the same key gets the first
submission's task back, and the work is not started a second time.

It is NOT end-to-end exactly-once execution, and nothing here should be
read as claiming it. Exactly-once against something outside this system
requires that outside thing to participate -- a transaction it shares, or
an idempotency protocol it honours. A tool declaring
``SideEffect.EXTERNAL`` may have changed state nobody here owns, and if
the supervisor died between that happening and the durable record of it
happening, no amount of local bookkeeping recovers the fact. That case
is represented as UNCERTAIN and refused, not resolved by guessing.

THE KEY IS NOT A GLOBAL STRING

The obvious implementation -- one dictionary from key to task -- has an
authority hole in it that is easy to miss: whoever guesses the string
reads the result. Here the binding is scoped to

    (owner, tool_id, key)

so two submitters using the key "nightly" have two unrelated bindings,
one submitter using "nightly" against two tools has two unrelated
bindings, and there is no lookup an actor can perform that reaches
another actor's task. The cross-actor case is not "refused with a
message"; it does not collide at all, which is stronger, because a
refusal that says "that key belongs to someone else" has already told
the caller something they had no right to learn.

``owner`` is taken from the EVENT'S ACTOR, never from the payload. That
is the same rule the create paths and the register path had to learn: a
record that names its own authority is a record that authorized itself.

REQUEST IDENTITY IS CANONICAL, NOT INCIDENTAL

``request_digest`` is a digest of the canonical serialization of the tool
id and the validated inputs. Not ``repr``, not ``str``, not iteration
order: two structurally identical requests must produce the same digest
in any process, or "the same request" means nothing across a restart --
which is the only time this subsystem is load-bearing.

WHAT A BINDING PROMISES

  same owner, key, request   -> the first task, whatever state it is in
  same owner, key, DIFFERENT -> refused. The key names one request, and
     request                    silently rebinding it would let a second
                                request inherit the first one's identity
  different owner            -> a different binding entirely
  different tool             -> a different binding entirely

A binding is durable BEFORE the work is dispatched, so a crash between
binding and execution is recoverable as "already submitted" rather than
re-submitted. The cost is that a rejected request keeps its key: the key
names a request, that request was rejected, and a corrected request is a
different request that needs a different key. Accepting the corrected one
under the old key would mean the key no longer identifies anything.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet

from .actions import require_known
from .canonical import digest, is_digest

#: The one durable action this module owns.
ACT_BIND = "idempotency.bind"

MINE: FrozenSet[str] = frozenset({ACT_BIND})


class IdempotencyError(Exception):
    """Base class. Every failure here is fail-closed."""


class IdempotencyConflict(IdempotencyError):
    """The key is bound to a different request."""


def request_identity(*, tool_id: str, inputs) -> str:
    """Canonical digest of what is being asked for.

    Deliberately includes the tool. The scope already separates tools, so
    this is redundant for lookup -- and it is not redundant for the
    conflict check, which is the one that decides whether two submissions
    are the same request.
    """
    if not isinstance(tool_id, str) or not tool_id:
        raise IdempotencyError("tool_id must be a non-empty str")
    return digest({"tool_id": tool_id, "inputs": inputs})


def scope_identity(*, owner: str, tool_id: str, key: str) -> str:
    """Digest of the (owner, tool, key) triple this binding lives under.

    A digest rather than a joined string because a separator can be
    smuggled: an owner literally named ``a:b`` and a key ``c`` must not
    collide with owner ``a`` and key ``b:c``. Canonical serialization has
    no such ambiguity, and the resulting id is fixed-width and loggable.
    """
    for name, value in (("owner", owner), ("tool_id", tool_id),
                        ("key", key)):
        if not isinstance(value, str) or not value:
            raise IdempotencyError(f"{name} must be a non-empty str")
    return digest({"owner": owner, "tool_id": tool_id, "key": key})


@dataclass(frozen=True)
class Binding:
    """One durable key -> submission binding."""

    key: str
    owner: str
    tool_id: str
    request_digest: str
    task_id: str
    #: The queue record, when the submission got that far. A binding
    #: written before enqueue legitimately has none yet.
    job_id: str = ""
    #: Where the binding appeared in the log. The log's to say, not the
    #: record's -- see the replay check in :meth:`IdempotencyLedger.apply`.
    bound_seq: int = -1

    @property
    def scope(self) -> str:
        return scope_identity(owner=self.owner, tool_id=self.tool_id,
                              key=self.key)

    def to_record(self) -> dict:
        return {"key": self.key, "owner": self.owner,
                "tool_id": self.tool_id,
                "request_digest": self.request_digest,
                "task_id": self.task_id, "job_id": self.job_id,
                "bound_seq": self.bound_seq,
                "scope": self.scope}

    def digest(self) -> str:
        return digest(self.to_record())


def binding_from_record(payload: object, *, actor: str, seq: int) -> Binding:
    """Build a binding from a log payload, taking authority from the event.

    ``actor`` and ``seq`` come from the EVENT. Anything the payload says
    about either is checked against them rather than believed.
    """
    if not isinstance(payload, dict):
        raise IdempotencyError(
            f"seq {seq}: an idempotency binding must be an object, got "
            f"{type(payload).__name__}")
    for field in ("key", "tool_id", "request_digest", "task_id"):
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise IdempotencyError(
                f"seq {seq}: idempotency binding is missing {field!r}, or it "
                "is not a non-empty string; a binding this store cannot read "
                "is refused rather than projected")
    if not is_digest(payload["request_digest"]):
        raise IdempotencyError(
            f"seq {seq}: request_digest is not a sha-256 hex digest. Request "
            "identity has to be derived from canonical bytes, because a "
            "binding whose identity depends on how a value happened to be "
            "formatted stops matching across a restart -- which is the only "
            "time this subsystem does anything.")
    claimed_owner = payload.get("owner")
    if claimed_owner is not None and claimed_owner != actor:
        raise IdempotencyError(
            f"seq {seq}: the binding names owner {claimed_owner!r} while the "
            f"event was appended by {actor!r}. The owner is who a later "
            "lookup is answered for, so a payload that names its own owner "
            "is a payload that chose whose key namespace to write into.")
    job_id = payload.get("job_id", "")
    if not isinstance(job_id, str):
        raise IdempotencyError(f"seq {seq}: job_id must be a str")
    # Read rather than imposed, because a RETRIED append of an existing
    # binding legitimately carries the original's position and must compare
    # byte-identical to it. Whether a claimed position is allowed is the
    # ledger's question, asked only of bindings that are actually new -- see
    # IdempotencyLedger.apply.
    bound_seq = payload.get("bound_seq", seq)
    if not isinstance(bound_seq, int) or isinstance(bound_seq, bool):
        raise IdempotencyError(f"seq {seq}: bound_seq must be an int")
    return Binding(key=payload["key"], owner=actor,
                   tool_id=payload["tool_id"],
                   request_digest=payload["request_digest"],
                   task_id=payload["task_id"], job_id=job_id,
                   bound_seq=bound_seq)


class IdempotencyLedger:
    """Bindings in force, PROJECTED from the log rather than asserted.

    Same shape and the same reasoning as
    :class:`~qta_agent.capability.CapabilityLedger`: a caller that
    assembles its own map can put anything in it, so the map is rebuilt
    from events and a binding nobody recorded does not exist.
    """

    def __init__(self, log):
        self.log = log
        self._bindings: dict = {}
        self._at_seq = -1

    # ---- projection ----------------------------------------------------
    def load(self) -> "IdempotencyLedger":
        self.log.verify().raise_if_bad()
        self._bindings = {}
        self._at_seq = -1
        for ev in self.log.read():
            self.apply(ev)
        return self

    def apply(self, ev) -> bool:
        """Fold one event in. True when it was an idempotency event."""
        require_known(ev.action, mine=MINE, where=f"seq {ev.seq}")
        if ev.action != ACT_BIND:
            return False
        binding = binding_from_record(ev.payload, actor=ev.actor, seq=ev.seq)

        # IDENTITY FIRST, then position. The other order refuses a retried
        # append of a binding already in force, because that record
        # correctly carries the ORIGINAL's position and would look
        # backdated. Same ordering, and the same reason, as
        # CapabilityLedger.apply.
        scope = binding.scope
        existing = self._bindings.get(scope)
        if existing is not None:
            if existing.digest() == binding.digest():
                # Byte-identical: a retried append of a binding already in
                # force. Its position is the FIRST record's, which was
                # checked when THAT one was read.
                self._at_seq = ev.seq
                return True
            raise IdempotencyError(
                f"seq {ev.seq}: idempotency key {binding.key!r} for tool "
                f"{binding.tool_id!r} is already bound to task "
                f"{existing.task_id!r}, and this record rebinds it to "
                f"{binding.task_id!r}. Rebinding is how a second request "
                "inherits the first one's identity: every later resubmission "
                "of the ORIGINAL request would be answered with this task "
                "instead. The write path refuses it, so a history containing "
                "it was not produced by the write path.")

        # A NEW binding starts where the log says it starts. One appended at
        # seq 900 claiming seq 5 answers "had this been submitted by seq 5?"
        # with a yes it did not earn -- and that question is the whole
        # mechanism: it decides whether work runs again.
        if binding.bound_seq != ev.seq:
            raise IdempotencyError(
                f"seq {ev.seq}: the binding claims bound_seq "
                f"{binding.bound_seq!r}. A binding that names its own "
                "position backdates the moment a duplicate would first have "
                "been caught.")

        self._bindings[scope] = binding
        self._at_seq = ev.seq
        return True

    # ---- reads ---------------------------------------------------------
    def lookup(self, *, owner: str, tool_id: str, key: str):
        """The binding for this exact scope, or None.

        There is no lookup that crosses an owner. That is the privacy
        property, and it is structural rather than a check that could be
        forgotten: a different owner hashes to a different scope and finds
        nothing, so no message about another actor's task can be emitted
        because no such binding was ever reached.
        """
        return self._bindings.get(
            scope_identity(owner=owner, tool_id=tool_id, key=key))

    def bindings(self) -> dict:
        """Every binding in force, keyed by scope digest. For audit."""
        return dict(self._bindings)

    def __len__(self) -> int:
        return len(self._bindings)

    # ---- write ---------------------------------------------------------
    def bind(self, *, owner: str, tool_id: str, key: str,
             request_digest: str, task_id: str, job_id: str = "") -> Binding:
        """Record a binding, or answer with the one already there.

        Returns the EXISTING binding when this is the same request under
        the same key -- that is the whole point -- and raises when the key
        is bound to a different request.
        """
        if not is_digest(request_digest):
            raise IdempotencyError(
                "request_digest must be a sha-256 hex digest; derive it with "
                "request_identity() so it comes from canonical bytes")
        if not isinstance(task_id, str) or not task_id:
            raise IdempotencyError("task_id must be a non-empty str")
        existing = self.lookup(owner=owner, tool_id=tool_id, key=key)
        if existing is not None:
            if existing.request_digest != request_digest:
                raise IdempotencyConflict(
                    f"idempotency key {key!r} is already bound to task "
                    f"{existing.task_id!r} for a DIFFERENT request. The key "
                    "names one request; a corrected or altered request is a "
                    "different request and needs a different key, because "
                    "reusing this one would make every later resubmission of "
                    "the original resolve to the new work.")
            return existing
        payload = {"key": key, "owner": owner, "tool_id": tool_id,
                   "request_digest": request_digest, "task_id": task_id,
                   "job_id": job_id}
        ev = self.log.append(actor=owner, action=ACT_BIND, target=task_id,
                             payload=payload)
        self.apply(ev)
        return self._bindings[scope_identity(owner=owner, tool_id=tool_id,
                                             key=key)]
