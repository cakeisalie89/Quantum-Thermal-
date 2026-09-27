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

import hashlib
import importlib.metadata
import os
import platform
import re
import sys
from dataclasses import dataclass, fields
from pathlib import Path

from .identity import IdentityError, digest, require_digest

#: Distributions whose versions enter the environment digest. The numeric
#: stack is where "same code, different answer" lives.
ENVIRONMENT_DISTRIBUTIONS = ("numpy", "scipy")


#: The CPU flags that choose a SIMD kernel -- what numpy's runtime dispatch
#: and a BLAS's kernel selection read. Microcode, virtualisation and
#: mitigation flags are left out on purpose: they change nothing a kernel
#: computes, and would make every patched host a different machine.
_SIMD_FLAG = re.compile(
    r"^(sse\d*(_\d)?|ssse3|pni|popcnt|avx\w*|fma\w*|f16c|xop|bmi\d|"
    r"amx\w*|asimd\w*|sve\w*|neon|vsx|altivec|vx\w*)$")
#: The identity of the core a BLAS picks its kernels for (x86, then Arm).
_CORE_KEYS = ("vendor_id", "cpu family", "model", "CPU implementer",
              "CPU architecture", "CPU part")
#: Variables that change a numeric result's bits: thread counts change the
#: order of a reduction, and the dispatch and core-type overrides change
#: which kernel runs.
BACKEND_VARIABLES = (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS",
    "NPY_DISABLE_CPU_FEATURES", "NPY_ENABLE_CPU_FEATURES",
    "OPENBLAS_CORETYPE", "MKL_CBWR", "MKL_ENABLE_INSTRUCTIONS")
#: Compiled code in a distribution: extension modules and bundled libraries
#: (numpy ships its BLAS and LAPACK this way).
_NATIVE_FILE = re.compile(r"\.(so(\.\d+)*|pyd|dylib)$")
_CPUINFO = Path("/proc/cpuinfo")
_READ = object()


def cpu_record(cpuinfo: str | None) -> dict:
    """The CPU as a numeric kernel sees it: its SIMD features and its core.
    ``None`` -- nothing to read here -- names the host instead, so a run on
    a machine whose CPU cannot be read is reused only on that machine."""
    if cpuinfo is None:
        return {"source": "UNAVAILABLE", "host": platform.node()}
    flags, core = set(), {}
    for line in cpuinfo.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key in ("flags", "Features"):
            flags |= {f for f in value.split() if _SIMD_FLAG.match(f)}
        elif key in _CORE_KEYS and key not in core:
            core[key] = value
    return {"source": "cpuinfo", "simd": sorted(flags), "core": core}


def native_record(distribution: str) -> dict:
    """Which compiled code a distribution installed, from its own RECORD:
    every extension module and bundled library, by the digest its wheel
    recorded. The native build, read without loading it."""
    try:
        files = importlib.metadata.files(distribution) or []
    except importlib.metadata.PackageNotFoundError:
        return {"status": "ABSENT"}
    h = hashlib.sha256()
    n = 0
    for f in sorted(files, key=str):
        if _NATIVE_FILE.search(str(f)) and f.hash is not None:
            h.update(f"{f}\0{f.hash.mode}:{f.hash.value}\n".encode())
            n += 1
    return {"native_files": n, "native_sha256": h.hexdigest()}


def _read_cpuinfo():
    try:
        return _CPUINFO.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def environment_record(distributions=ENVIRONMENT_DISTRIBUTIONS, *,
                       environ=None, cpuinfo=_READ) -> dict:
    """What the interpreter, the numeric libraries and the numeric BACKEND
    are, read from metadata, ``/proc/cpuinfo`` and the environment --
    nothing is imported to find out.

    The backend is the part version numbers cannot see: the same numpy and
    scipy on a CPU with a different SIMD set dispatch a different kernel
    and change digits (R59), and so do a different thread count, a
    dispatch override, and a different native build of the same version.
    Each is here, so a run on a different backend is a different run.
    ``environ`` defaults to this process's; pass the environment a governed
    tool actually got to describe that tool's run."""
    environ = os.environ if environ is None else environ
    versions = {}
    for d in distributions:
        try:
            versions[d] = importlib.metadata.version(d)
        except importlib.metadata.PackageNotFoundError:
            versions[d] = "ABSENT"
    return {"python": platform.python_version(),
            "implementation": sys.implementation.name,
            "machine": platform.machine(),
            "distributions": versions,
            "cpu": cpu_record(_read_cpuinfo() if cpuinfo is _READ
                              else cpuinfo),
            "backend": {"cpu_count": os.cpu_count(),
                        "variables": {v: environ.get(v)
                                      for v in BACKEND_VARIABLES}},
            "native": {d: native_record(d) for d in distributions}}


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
