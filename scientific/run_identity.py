"""When a verified run may be reused instead of recomputed.

A run is identified by everything that could change its answer: the model
and its implementation digest, the parameter digest, the environment digest
(interpreter and the numeric libraries -- the part of the code the
implementation digest cannot see), the seeds, the workflow revision, and the
digests of the upstream evidence it consumed. Two runs with the same
identity digest are the same computation.

Reuse needs that AND the evidence. ``may_reuse`` answers yes only when the
identities are equal field by field and the caller has re-derived the prior
run's evidence digests from the evidence store and found them intact. It
never answers yes on a name, a model id alone, or a matching parameter
digest with different code; and it says which field differs when it says
no, so a refused reuse is diagnosable instead of mysterious.
"""
from __future__ import annotations

import importlib.metadata
import platform
import sys
from dataclasses import dataclass, fields

from .identity import IdentityError, digest, require_digest

#: Distributions whose versions enter the environment digest. The numeric
#: stack is where "same code, different answer" lives.
ENVIRONMENT_DISTRIBUTIONS = ("numpy", "scipy")


def environment_record(distributions=ENVIRONMENT_DISTRIBUTIONS) -> dict:
    """What the interpreter and the numeric libraries are, read from
    metadata -- nothing is imported to find out."""
    versions = {}
    for d in distributions:
        try:
            versions[d] = importlib.metadata.version(d)
        except importlib.metadata.PackageNotFoundError:
            versions[d] = "ABSENT"
    return {"python": platform.python_version(),
            "implementation": sys.implementation.name,
            "machine": platform.machine(),
            "distributions": versions}


def environment_digest(record: dict | None = None) -> str:
    return digest(environment_record() if record is None else record)


class ReuseRefused(ValueError):
    pass


@dataclass(frozen=True)
class RunIdentity:
    model_id: str
    model_version: str
    implementation_digest: str
    parameter_digest: str
    environment_digest: str
    seeds: tuple = ()
    workflow_revision: str = ""
    upstream_evidence: tuple = ()

    def __post_init__(self):
        for f in ("implementation_digest", "parameter_digest",
                  "environment_digest"):
            require_digest(f, getattr(self, f))
        for e in self.upstream_evidence:
            require_digest("upstream evidence", e)
        object.__setattr__(self, "upstream_evidence",
                           tuple(sorted(self.upstream_evidence)))
        object.__setattr__(self, "seeds", tuple(self.seeds))
        if not self.model_id or not self.model_version:
            raise IdentityError("a run identity needs the model id and "
                                "version")

    def to_record(self) -> dict:
        return {"model_id": self.model_id,
                "model_version": self.model_version,
                "implementation_digest": self.implementation_digest,
                "parameter_digest": self.parameter_digest,
                "environment_digest": self.environment_digest,
                "seeds": list(self.seeds),
                "workflow_revision": self.workflow_revision,
                "upstream_evidence": list(self.upstream_evidence)}

    def digest(self) -> str:
        return digest(self.to_record())

    def differences(self, other: "RunIdentity") -> list:
        return [f.name for f in fields(self)
                if getattr(self, f.name) != getattr(other, f.name)]


def may_reuse(prior: RunIdentity, current: RunIdentity, *,
              prior_evidence_intact: bool) -> tuple:
    """``(True, "")`` only for an identical identity with intact evidence;
    otherwise ``(False, why)``."""
    # Field by field rather than digest against digest: the digest is over
    # exactly these fields, and naming the one that differs is the point.
    diff = prior.differences(current)
    if diff:
        return False, f"identity differs in {diff}: recompute"
    if prior_evidence_intact is not True:
        return False, ("the prior run's evidence was not re-derived intact; "
                       "a run is reused by its evidence, never by its name")
    return True, ""
