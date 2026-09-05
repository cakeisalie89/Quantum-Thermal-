"""Tool contracts and a default-deny registry.

WHAT A TOOL IS HERE

Not a callable. A *declaration*: an identity, a version, an input and output
shape, the authority it needs, the paths it may write, and an honest statement
of whether running it twice on the same inputs produces the same bytes. The
callable is one field of that declaration, and the least interesting one.

The reason is that everything the governance layer needs to decide -- may this
actor run this, over these paths, and can its result be reproduced -- has to be
answerable *before* the callable runs. A bare function answers none of it.

DEFAULT DENY

An unregistered tool does not run. Not "runs with reduced privileges", not
"runs and is logged": :meth:`Registry.get` raises. Conforming to a schema is
not permission -- a well-formed request to run something nobody registered is
a well-formed request to do something nobody authorized.

Registration is explicit and the registry is frozen once built, so a tool
cannot appear at runtime because some import had a side effect.

DETERMINISM IS DECLARED, NOT ASSUMED

``determinism`` says what the tool's author claims, and the claim is recorded
alongside every result. A NONDETERMINISTIC tool's output is still evidence --
it is just evidence of one run rather than of a reproducible fact, and a
verifier that re-runs it and gets different bytes has learned nothing about
tampering. Recording the claim is what lets a later reader tell those apart;
assuming determinism is how a re-run difference gets misread as corruption.

SIDE EFFECTS ARE CLASSIFIED BECAUSE ROLLBACK DEPENDS ON IT

A tool that only writes into its scoped workspace can be rolled back by
deleting what it wrote. One that mutates external state cannot, and needs a
compensating action instead. The difference has to be declared, because
discovering it after a failure is discovering it too late.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, FrozenSet, Mapping

from .canonical import digest


class ToolError(Exception):
    """Base class. Every failure here is fail-closed."""


class ToolNotRegistered(ToolError):
    """No such tool. Conforming to a schema is not permission to run."""


class ToolContractViolation(ToolError):
    """Inputs or outputs do not match the declared contract."""


class Determinism(str, Enum):
    """What re-running the tool on identical inputs is claimed to produce."""

    #: Byte-identical outputs. A re-run difference is a finding.
    BYTE_IDENTICAL = "BYTE_IDENTICAL"
    #: Same meaning, possibly different bytes (timestamps, float formatting).
    #: A verifier must compare semantically or not at all.
    SEMANTIC = "SEMANTIC"
    #: No reproducibility claimed. The output is evidence of ONE run.
    NONDETERMINISTIC = "NONDETERMINISTIC"


class SideEffect(str, Enum):
    """What running the tool changes, and therefore how it can be undone."""

    #: Reads only. Undone by doing nothing.
    NONE = "NONE"
    #: Writes only within its declared scope. Undone by deleting that.
    SCOPED_WRITES = "SCOPED_WRITES"
    #: Changes state this system does not own. Cannot be rolled back; a
    #: compensating action must be declared by the caller.
    EXTERNAL = "EXTERNAL"


#: JSON-ish types a contract may declare. Deliberately small: a contract
#: language rich enough to express anything is a contract language nobody
#: checks. Everything here is checkable in a few lines and canonically
#: serializable, which matters because inputs are hashed into provenance.
_TYPES: Mapping[str, tuple] = {
    "str": (str,),
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
    "list": (list,),
    "dict": (dict,),
}


@dataclass(frozen=True)
class Field_:
    """One declared field of a tool's input or output."""

    name: str
    type_: str
    required: bool = True
    #: Optional closed set of permitted values.
    choices: tuple = ()

    def to_record(self) -> dict:
        return {"name": self.name, "type": self.type_,
                "required": self.required, "choices": list(self.choices)}


def _check_fields(fields: tuple, value: object, what: str) -> None:
    if not isinstance(value, dict):
        raise ToolContractViolation(
            f"{what} must be an object, got {type(value).__name__}")
    declared = {f.name: f for f in fields}
    unknown = sorted(set(value) - set(declared))
    if unknown:
        # An undeclared field is not harmless: it is unvalidated content that
        # a tool may read, arriving through a contract that claims to describe
        # the interface completely.
        raise ToolContractViolation(
            f"{what} carries undeclared fields {unknown}; the contract is the "
            "whole interface, so anything outside it is refused")
    for name, f in declared.items():
        if name not in value:
            if f.required:
                raise ToolContractViolation(f"{what} is missing {name!r}")
            continue
        v = value[name]
        expected = _TYPES.get(f.type_)
        if expected is None:
            raise ToolContractViolation(
                f"{what}: field {name!r} declares unknown type {f.type_!r}")
        # bool is an int in Python; a contract asking for int must not take a
        # bool, or a flag silently becomes a count.
        if isinstance(v, bool) and f.type_ != "bool":
            raise ToolContractViolation(
                f"{what}: field {name!r} is a bool, expected {f.type_}")
        if not isinstance(v, expected):
            raise ToolContractViolation(
                f"{what}: field {name!r} is {type(v).__name__}, expected "
                f"{f.type_}")
        if f.choices and v not in f.choices:
            raise ToolContractViolation(
                f"{what}: field {name!r} is {v!r}, not one of "
                f"{list(f.choices)}")


@dataclass(frozen=True)
class ToolSpec:
    """A tool's complete declaration. Hashable, so it can be cited."""

    tool_id: str
    version: str
    summary: str
    inputs: tuple = ()
    outputs: tuple = ()
    determinism: Determinism = Determinism.NONDETERMINISTIC
    side_effect: SideEffect = SideEffect.SCOPED_WRITES
    #: Repo-relative prefixes the tool may write. Checked against the
    #: capability at execution time; declaring a scope grants nothing.
    writable_scope: tuple = ()
    #: Wall-clock bound. A tool with no bound cannot be cancelled by timeout,
    #: which is why there is no "unlimited" option.
    timeout_s: float = 60.0
    #: The callable. Excluded from the digest -- a tool's identity is its
    #: contract, and two builds of the same contract must cite the same tool.
    run: Callable | None = field(default=None, compare=False, repr=False)

    def body(self) -> dict:
        return {
            "tool_id": self.tool_id, "version": self.version,
            "summary": self.summary,
            "inputs": [f.to_record() for f in self.inputs],
            "outputs": [f.to_record() for f in self.outputs],
            "determinism": self.determinism.value,
            "side_effect": self.side_effect.value,
            "writable_scope": list(self.writable_scope),
            "timeout_s": self.timeout_s,
        }

    def digest(self) -> str:
        """Content digest of the contract. Cited by every execution record."""
        return digest(self.body())

    def validate_inputs(self, value: object) -> None:
        _check_fields(self.inputs, value, f"{self.tool_id} inputs")

    def validate_outputs(self, value: object) -> None:
        _check_fields(self.outputs, value, f"{self.tool_id} outputs")


def _validate_spec(spec: ToolSpec) -> None:
    if not spec.tool_id or not isinstance(spec.tool_id, str):
        raise ToolError("tool_id must be a non-empty str")
    if not spec.version or not isinstance(spec.version, str):
        raise ToolError(
            f"{spec.tool_id}: version must be a non-empty str; an unversioned "
            "tool cannot be cited, because the citation would not say WHICH "
            "tool ran")
    if not isinstance(spec.determinism, Determinism):
        raise ToolError(f"{spec.tool_id}: determinism must be a Determinism")
    if not isinstance(spec.side_effect, SideEffect):
        raise ToolError(f"{spec.tool_id}: side_effect must be a SideEffect")
    if not isinstance(spec.timeout_s, (int, float)) or spec.timeout_s <= 0:
        raise ToolError(
            f"{spec.tool_id}: timeout_s must be positive; a tool that cannot "
            "time out cannot be cancelled")
    scoped = spec.side_effect is SideEffect.SCOPED_WRITES
    if scoped and not spec.writable_scope:
        raise ToolError(
            f"{spec.tool_id}: declares SCOPED_WRITES with an empty scope; the "
            "scope is what makes the writes scoped")
    if spec.side_effect is SideEffect.NONE and spec.writable_scope:
        raise ToolError(
            f"{spec.tool_id}: declares no side effects but names a writable "
            "scope; one of the two is wrong and guessing which would be worse "
            "than refusing")
    names = [f.name for f in spec.inputs] + [f.name for f in spec.outputs]
    if len(names) != len(set(names)):
        raise ToolError(f"{spec.tool_id}: duplicate field names in contract")


class Registry:
    """The set of tools that may run. Frozen after construction.

    Frozen because a registry that can grow at runtime is a registry whose
    contents depend on import order and on whatever code has run so far. The
    question "what may run here" should have the same answer at every point in
    a process's life.
    """

    def __init__(self, specs=()):
        self._specs: dict = {}
        for spec in specs:
            self._add(spec)
        self._frozen = True

    def _add(self, spec: ToolSpec) -> None:
        if getattr(self, "_frozen", False):
            raise ToolError(
                "the registry is frozen; a tool that appears after "
                "construction is a tool nobody reviewed")
        _validate_spec(spec)
        if spec.tool_id in self._specs:
            raise ToolError(f"duplicate tool_id {spec.tool_id!r}")
        self._specs[spec.tool_id] = spec

    def get(self, tool_id: object) -> ToolSpec:
        """The declared tool, or raise. THE default-deny point."""
        if not isinstance(tool_id, str) or tool_id not in self._specs:
            raise ToolNotRegistered(
                f"tool {tool_id!r} is not registered and will not run. "
                f"Registered: {sorted(self._specs)}. Conforming to a "
                "schema is "
                "not permission -- absence of a rule is not an allow rule.")
        return self._specs[tool_id]

    def ids(self) -> FrozenSet[str]:
        return frozenset(self._specs)

    def digest(self) -> str:
        """Digest over every contract, so a run can cite the whole registry."""
        return digest({tid: s.body()
                       for tid, s in sorted(self._specs.items())})

    def __contains__(self, tool_id: object) -> bool:
        return isinstance(tool_id, str) and tool_id in self._specs

    def __len__(self) -> int:
        return len(self._specs)
