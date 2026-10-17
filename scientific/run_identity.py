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
#: ``GLIBC_TUNABLES`` belongs here too: glibc's math library selects its
#: FMA and AVX2 variants of exp, log, pow and the rest by IFUNC from the same
#: CPU features, and a tunable can mask them.
BACKEND_VARIABLES = (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "GOTO_NUM_THREADS",
    "MKL_NUM_THREADS", "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "NPY_DISABLE_CPU_FEATURES", "NPY_ENABLE_CPU_FEATURES",
    "OPENBLAS_CORETYPE", "MKL_CBWR", "MKL_ENABLE_INSTRUCTIONS",
    "GLIBC_TUNABLES")
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


#: Installed-byte digests, cached per (path, size, mtime, inode) within a
#: process: hashing a numeric stack's native payload is tens of megabytes,
#: and a process computes its identity more than once.
_BYTES: dict = {}


def _installed_sha256(path) -> str | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (str(path), st.st_size, st.st_mtime_ns, st.st_ino)
    if key not in _BYTES:
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            return None
        _BYTES[key] = h.hexdigest()
    return _BYTES[key]


def native_record(distribution: str) -> dict:
    """Which compiled code a distribution installed: every extension module
    and bundled library, by the digest its wheel RECORDED and by the digest
    of the bytes INSTALLED now. The two are different claims. RECORD says
    what the wheel shipped; a library replaced after installation leaves
    RECORD unchanged, so a digest of RECORD alone would call the replaced
    build the same computation. A file that cannot be read is named and the
    record is ``UNRESOLVED``; one whose bytes no longer match RECORD makes
    it ``MODIFIED`` -- both stated, neither guessed."""
    try:
        files = importlib.metadata.files(distribution) or []
    except importlib.metadata.PackageNotFoundError:
        return {"status": "ABSENT"}
    declared = hashlib.sha256()
    installed = hashlib.sha256()
    n = 0
    unreadable, modified = [], []
    for f in sorted(files, key=str):
        if not _NATIVE_FILE.search(str(f)) or f.hash is None:
            continue
        declared.update(f"{f}\0{f.hash.mode}:{f.hash.value}\n".encode())
        n += 1
        now = _installed_sha256(f.locate())
        if now is None:
            unreadable.append(str(f))
            continue
        installed.update(f"{f}\0sha256:{now}\n".encode())
        if f.hash.mode == "sha256" and _b64_sha(now) != f.hash.value:
            modified.append(str(f))
    status = ("UNRESOLVED" if unreadable
              else "MODIFIED" if modified else "RESOLVED")
    return {"status": status, "native_files": n,
            "native_sha256": declared.hexdigest(),
            "installed_sha256": installed.hexdigest(),
            "unreadable": unreadable, "modified": modified}


def _b64_sha(hexdigest: str) -> str:
    """A hex sha256 in the urlsafe, unpadded base64 a wheel RECORD uses."""
    import base64
    return base64.urlsafe_b64encode(bytes.fromhex(hexdigest)).decode(
        ).rstrip("=")


def _read_cpuinfo():
    try:
        return _CPUINFO.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def environment_record(distributions=ENVIRONMENT_DISTRIBUTIONS, *,
                       environ=None, cpuinfo=_READ, runtime=None) -> dict:
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
    natives = {d: native_record(d) for d in distributions}
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
            "native": natives,
            "runtime": runtime,
            "backend_status": backend_status(natives=natives,
                                             runtime=runtime)}


def backend_status(*, natives=None, runtime=None,
                   distributions=ENVIRONMENT_DISTRIBUTIONS) -> str:
    """``RESOLVED`` only when the native builds are read, unmodified, and
    the RUNTIME backend was probed and resolved -- what the numeric stack
    dispatched to, not what metadata says it could. Anything less is
    ``UNRESOLVED``: an identity that cannot say which backend it ran on
    cannot be reused (:func:`may_reuse`)."""
    natives = natives or {d: native_record(d) for d in distributions}
    if any(n.get("status") != "RESOLVED" for n in natives.values()
           if n.get("status") != "ABSENT"):
        return "UNRESOLVED"
    if not isinstance(runtime, dict) or runtime.get("status") != "RESOLVED":
        return "UNRESOLVED"
    return "RESOLVED"


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
    #: Whether the environment digest names a backend that was actually
    #: determined -- probed at runtime, native builds read and unmodified.
    #: Digested with the rest, and consulted by :func:`may_reuse`.
    backend_status: str = "UNRESOLVED"

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
                "upstream_evidence": list(self.upstream_evidence),
                "backend_status": self.backend_status}

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
    # AN UNRESOLVED BACKEND IS RECOMPUTED, never reused -- even against
    # an identical identity: two runs that could not say which backend they
    # ran on agree only in not knowing, and "same unknown" is not "same".
    for which, ident in (("prior", prior), ("current", current)):
        if ident.backend_status != "RESOLVED":
            return False, (f"the {which} run's numeric backend is "
                           f"{ident.backend_status}: recompute")
    diff = prior.differences(current)
    if diff:
        return False, f"identity differs in {diff}: recompute"
    if prior_evidence_intact is not True:
        return False, ("the prior run's evidence was not re-derived intact; "
                       "a run is reused by its evidence, never by its name")
    return True, ""
