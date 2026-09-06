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

That classification is not decorative here. ``EXTERNAL`` cannot be registered
without a written ``compensation``, and an ``EXTERNAL`` tool is never
automatically retryable whatever the compensation says -- a sentence telling
an operator how to undo an effect is not the same as that effect having been
undone, and a machine that retries on the strength of an unperformed
compensation performs the external action twice.

OUTPUTS ARE DECLARED BEFORE THE RUN, NOT DISCOVERED AFTER IT

``output_files`` names the files the tool is contracted to produce, as
templates over its own declared inputs. Collecting them is then a check
against the contract rather than a sweep of a directory: a tool that exits 0
having written nothing is caught, and a path that resolves outside the
workspace is refused instead of hashed.

A directory sweep cannot do either. It reports whatever is there, which means
the tool decides after the fact what its outputs were, and an empty directory
and a complete result are the same observation.
"""
from __future__ import annotations

import string
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath
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
class OutputFile:
    """One file the tool is contracted to produce.

    ``path`` is a template over the tool's own declared inputs, so the
    contract can say "the artifact lands at ``{out_dir}/{name}``" without
    knowing either value. It is validated at REGISTRATION -- every
    placeholder must name a declared, required, string input -- so a template
    that could never resolve is refused before any run rather than at the
    moment a result was expected.

    ``required`` is the difference between "produces this" and "may produce
    this". A missing required output turns a zero exit into a FAILED
    execution, because a tool that exited 0 without producing what it is
    contracted to produce has not done the thing that was asked, whatever its
    exit status says.
    """

    name: str
    path: str
    required: bool = True

    def to_record(self) -> dict:
        return {"name": self.name, "path": self.path,
                "required": self.required}


#: Placeholder syntax a path template may use: a bare ``{field}`` and nothing
#: else. No conversions, no format specs, no indexing or attribute access --
#: a filename is not a formatted number, and the richer forms are how a
#: template stops being reviewable by reading it.
_FORMATTER = string.Formatter()


def _template_fields(spec_id: str, tmpl: str) -> tuple:
    """Placeholder names in ``tmpl``, or raise if it is not plain."""
    if not isinstance(tmpl, str) or not tmpl:
        raise ToolError(f"{spec_id}: an output path must be a non-empty str")
    if tmpl.startswith("/"):
        raise ToolError(
            f"{spec_id}: output path {tmpl!r} is absolute; outputs are named "
            "relative to the run's working directory, and an absolute path in "
            "a contract is a path the workspace does not bound")
    names = []
    try:
        parsed = list(_FORMATTER.parse(tmpl))
    except ValueError as exc:
        raise ToolError(f"{spec_id}: output path {tmpl!r} is not a valid "
                        f"template: {exc}") from exc
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if format_spec or conversion:
            raise ToolError(
                f"{spec_id}: output path {tmpl!r} uses a format spec or "
                "conversion; a path template substitutes a value and does "
                "nothing else to it")
        if not field_name.isidentifier():
            raise ToolError(
                f"{spec_id}: output path {tmpl!r} refers to {field_name!r}; "
                "only a bare {field} naming a declared input is permitted, "
                "because indexing and attribute access make a contract that "
                "has to be executed to be understood")
        names.append(field_name)
    return tuple(names)


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
    #: Files the tool is contracted to produce, as templates over its inputs.
    #: Empty means the contract makes no claim about files, and nothing is
    #: collected -- silence, not a claim that there are none.
    output_files: tuple = ()
    #: What undoes this tool's EXTERNAL effect, in words an operator can act
    #: on. Required for -- and permitted only on -- SideEffect.EXTERNAL. It
    #: is a declaration, not an automation: nothing here performs it, and
    #: having one never makes an automatic retry safe.
    compensation: str = ""
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
            "output_files": [f.to_record() for f in self.output_files],
            "compensation": self.compensation,
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

    def resolve_outputs(self, inputs: Mapping) -> tuple:
        """Declared outputs as ``(name, relative path, required)`` triples.

        Call AFTER :meth:`validate_inputs`. Every placeholder was checked at
        registration to name a required string input, so the only thing that
        can go wrong here is being handed inputs that were never validated --
        which is reported rather than papered over with a default.

        The resolved path is refused if it walks upward. That is a contract
        question, answered on the string: whether the path ALSO escapes via a
        symlink is a filesystem question, answered against the real
        filesystem where the file is collected. Both are needed, and neither
        substitutes for the other.
        """
        if not isinstance(inputs, Mapping):
            raise ToolContractViolation(
                f"{self.tool_id}: outputs cannot be resolved against "
                f"{type(inputs).__name__}; validated inputs are required")
        resolved = []
        for out in self.output_files:
            values = {}
            for name in _template_fields(self.tool_id, out.path):
                if name not in inputs:
                    raise ToolContractViolation(
                        f"{self.tool_id}: output {out.name!r} needs input "
                        f"{name!r} to know where it lands, and the inputs do "
                        "not carry it")
                value = inputs[name]
                if not isinstance(value, str):
                    raise ToolContractViolation(
                        f"{self.tool_id}: output {out.name!r} needs input "
                        f"{name!r} as a str, got {type(value).__name__}")
                values[name] = value
            rel = out.path.format(**values)
            parts = PurePosixPath(rel).parts
            if ".." in parts or rel.startswith("/"):
                raise ToolContractViolation(
                    f"{self.tool_id}: output {out.name!r} resolves to "
                    f"{rel!r}, which leaves the working directory. An "
                    "input does not "
                    "get to relocate a declared output.")
            resolved.append((out.name, rel, out.required))
        return tuple(resolved)


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
    _validate_compensation(spec)
    _validate_output_files(spec)


def _validate_compensation(spec: ToolSpec) -> None:
    """EXTERNAL must say how it is undone; nothing else may claim to be.

    The registry is where this is enforced because a tool whose external
    effect has no stated compensation is not reviewable, and discovering that
    after the effect has happened is discovering it too late -- which is the
    thing this module's docstring says it exists to prevent.
    """
    if not isinstance(spec.compensation, str):
        raise ToolError(f"{spec.tool_id}: compensation must be a str")
    external = spec.side_effect is SideEffect.EXTERNAL
    if external and not spec.compensation.strip():
        raise ToolError(
            f"{spec.tool_id}: declares EXTERNAL side effects and no "
            "compensation. External state cannot be rolled back by deleting "
            "what was written, so the compensating action has to be written "
            "down by someone who knows it -- refusing here is the last point "
            "at which that is cheap.")
    if not external and spec.compensation.strip():
        raise ToolError(
            f"{spec.tool_id}: declares a compensation but side_effect is "
            f"{spec.side_effect.value}. A compensation for an effect the "
            "contract says does not happen means one of the two is wrong, "
            "and guessing which would be worse than refusing.")


def _validate_output_files(spec: ToolSpec) -> None:
    """Every declared output must be able to resolve, before anything runs."""
    if not spec.output_files:
        return
    if spec.side_effect is SideEffect.NONE:
        raise ToolError(
            f"{spec.tool_id}: declares output files and no side effects; a "
            "tool that writes a file has an effect, and the contract has to "
            "agree with itself")
    declared = {f.name: f for f in spec.inputs}
    seen = set()
    for out in spec.output_files:
        if not isinstance(out, OutputFile):
            raise ToolError(
                f"{spec.tool_id}: output_files must hold OutputFile entries, "
                f"got {type(out).__name__}")
        if not out.name or not isinstance(out.name, str):
            raise ToolError(
                f"{spec.tool_id}: every output file needs a non-empty name; "
                "an unnamed output cannot be cited by a later reader")
        if out.name in seen:
            raise ToolError(
                f"{spec.tool_id}: duplicate output file name {out.name!r}")
        seen.add(out.name)
        for field_name in _template_fields(spec.tool_id, out.path):
            f = declared.get(field_name)
            if f is None:
                raise ToolError(
                    f"{spec.tool_id}: output {out.name!r} is placed by "
                    f"{{{field_name}}}, which is not a declared input. A "
                    "template over something the contract does not describe "
                    "cannot be checked before the run.")
            if f.type_ != "str":
                raise ToolError(
                    f"{spec.tool_id}: output {out.name!r} is placed by "
                    f"{{{field_name}}}, declared as {f.type_}; a path "
                    "component has to be a str")
            if not f.required:
                raise ToolError(
                    f"{spec.tool_id}: output {out.name!r} is placed by "
                    f"{{{field_name}}}, which is optional. An output whose "
                    "location depends on a field that may be absent has no "
                    "location.")


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
