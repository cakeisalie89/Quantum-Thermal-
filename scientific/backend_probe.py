"""What numeric backend a governed scientific worker actually runs on.

WHY A PROBE, AND WHY HERE

:func:`scientific.run_identity.environment_record` describes the environment
from metadata, ``/proc/cpuinfo`` and variables, and deliberately imports
nothing. What that cannot see is what the numeric stack DECIDED at runtime:
which SIMD loops NumPy dispatched to on this CPU, which kernel OpenBLAS
selected, and which system math libraries the dynamic loader would resolve.
R59 is exactly those decisions: the same wheels, a different CPU, a different
kernel, different digits. This module asks the running stack itself.

It imports NumPy -- inside :func:`runtime_record`, never at import time -- so
it belongs in the governed WORKER, which is running NumPy anyway, and not in
the top-level scientific interfaces, which must not need it.

ORDER-INDEPENDENT BY DESIGN. The run identity is computed in one process and
the run in another, and the two must agree. So nothing here depends on what
happens to be loaded: no snapshot of the process's mapped libraries (a run
loads more of SciPy than the identity check does). The facts recorded are
the ones the stack reports about itself once NumPy is imported, and the
system libraries are resolved the way the dynamic loader would resolve them.

WHAT IS NOT RESOLVED STAYS UNRESOLVED. Any part that cannot be determined --
a BLAS that does not report its kernel, a system math library the loader
search cannot find, a file that cannot be read -- makes the whole record
``UNRESOLVED`` and says which part. An unresolved backend is never reused
(:func:`scientific.run_identity.may_reuse`): it is recomputed.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import os
from pathlib import Path

RESOLVED = "RESOLVED"
UNRESOLVED = "UNRESOLVED"

#: The system libraries a numeric result can depend on outside the wheels'
#: own payload: the C and math runtimes, and the OpenMP and Fortran runtimes
#: a BLAS or LAPACK may link. Resolved by the loader's search, hashed by
#: their installed bytes.
SYSTEM_LIBRARIES = ("c", "m", "gomp", "gfortran")
#: A library that is absent is not unresolved when nothing needs it: libgomp
#: and libgfortran are optional unless the BLAS in use names them.
OPTIONAL_LIBRARIES = frozenset({"gomp", "gfortran"})

_BYTES_CACHE: dict = {}


def installed_sha256(path) -> str | None:
    """sha256 of a file's CURRENT bytes, cached per (path, size, mtime,
    inode) within a process. None when it cannot be read."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p.resolve()), st.st_size, st.st_mtime_ns, st.st_ino)
    if key not in _BYTES_CACHE:
        h = hashlib.sha256()
        try:
            with p.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            return None
        _BYTES_CACHE[key] = h.hexdigest()
    return _BYTES_CACHE[key]


def _loader_path(name: str) -> str | None:
    """Where the dynamic loader resolves ``lib<name>``: the soname from
    ``ctypes.util.find_library``, then the loaded handle's own path."""
    soname = ctypes.util.find_library(name)
    if soname is None:
        return None
    try:
        handle = ctypes.CDLL(soname)
    except OSError:
        return None
    return _handle_path(handle, soname)


def _handle_path(handle, soname: str) -> str | None:
    """The file a loaded handle came from, via ``dlinfo``-free means: the
    first mapping in this process whose basename is the soname."""
    try:
        maps = Path("/proc/self/maps").read_text(encoding="utf-8",
                                                 errors="replace")
    except OSError:
        return None
    return _path_for_soname(maps, soname)


def _mapped_files(maps: str):
    """The absolute paths ``/proc/self/maps`` text names, in order. Parsed,
    not trusted: a line that does not have the shape of a mapping is
    skipped, and at most 65536 lines are read."""
    for line in maps.splitlines()[:65536]:
        parts = line.split(None, 5)
        if len(parts) == 6 and parts[5].startswith("/"):
            yield parts[5].strip()


def _path_for_soname(maps: str, soname: str) -> str | None:
    """From ``/proc/self/maps`` text, the path of the mapping whose basename
    is ``soname``."""
    for path in _mapped_files(maps):
        if os.path.basename(path) == soname:
            return path
    return None


#: The dynamic loader's basenames: glibc's and musl's.
LOADER_PREFIXES = ("ld-linux", "ld-musl")


def _dynamic_loader() -> str | None:
    """The dynamic loader that started this process. It is mapped in every
    dynamically linked process from the first instruction, whatever was
    imported since, so reading it from the mappings is not order-dependent
    the way reading a library the process happened to load would be."""
    try:
        maps = Path("/proc/self/maps").read_text(encoding="utf-8",
                                                 errors="replace")
    except OSError:
        return None
    for path in _mapped_files(maps):
        if os.path.basename(path).startswith(LOADER_PREFIXES):
            return path
    return None


#: The symbols a bundled OpenBLAS answers "which kernel did you select" by.
#: NumPy and SciPy each bundle their own build under its own prefix --
#: NumPy's ILP64 (``..._64_``), SciPy's LP64 -- and a process that has
#: imported both has both mapped.
_CORENAME = ("scipy_openblas_get_corename64_", "scipy_openblas_get_corename",
             "openblas_get_corename64_", "openblas_get_corename")
_CONFIG = ("scipy_openblas_get_config64_", "scipy_openblas_get_config",
           "openblas_get_config64_", "openblas_get_config")
#: How many threads the library WILL use for a level-3 call, as it answers
#: at runtime -- not what a variable asked for. A thread count changes the
#: order of a blocked reduction, so it is part of which arithmetic runs.
_THREADS = ("scipy_openblas_get_num_threads64_",
            "scipy_openblas_get_num_threads",
            "openblas_get_num_threads64_", "openblas_get_num_threads")
#: Which threading model the build uses (0 sequential, 1 pthreads, 2 OpenMP).
_PARALLEL = ("scipy_openblas_get_parallel64_", "scipy_openblas_get_parallel",
             "openblas_get_parallel64_", "openblas_get_parallel")
#: The distributions whose bundled BLAS a scientific run can execute on.
BLAS_DISTRIBUTIONS = ("numpy", "scipy")


def _ask(handle, names) -> str | None:
    for sym in names:
        fn = getattr(handle, sym, None)
        if fn is not None:
            fn.restype = ctypes.c_char_p
            answer = fn()
            return None if answer is None else answer.decode("ascii",
                                                             "replace")
    return None


def _ask_int(handle, names) -> int | None:
    for sym in names:
        fn = getattr(handle, sym, None)
        if fn is not None:
            fn.restype = ctypes.c_int
            return int(fn())
    return None


def _bundled_blas() -> list:
    """Every OpenBLAS the numeric distributions BUNDLE, found through their
    own RECORD -- in a fixed order, never by what this process happens to
    have mapped, which depends on what it imported first -- each loaded and
    asked which kernel it selected on this CPU. SciPy's matters as much as
    NumPy's: ``scipy.linalg`` runs on it."""
    import importlib.metadata
    out = []
    for dist in BLAS_DISTRIBUTIONS:
        try:
            files = importlib.metadata.files(dist) or []
        except importlib.metadata.PackageNotFoundError:
            continue
        for f in sorted(files, key=str):
            name = os.path.basename(str(f))
            if "openblas" not in name or ".so" not in name:
                continue
            path = str(f.locate())
            entry = {"distribution": dist, "name": name,
                     "sha256": installed_sha256(path) or UNRESOLVED}
            try:
                handle = ctypes.CDLL(path)
            except OSError:
                entry.update(kernel=UNRESOLVED, config=UNRESOLVED,
                             threads=UNRESOLVED, parallel=UNRESOLVED)
            else:
                threads = _ask_int(handle, _THREADS)
                parallel = _ask_int(handle, _PARALLEL)
                entry.update(kernel=_ask(handle, _CORENAME) or UNRESOLVED,
                             config=_ask(handle, _CONFIG) or UNRESOLVED,
                             threads=UNRESOLVED if threads is None
                             else threads,
                             parallel=UNRESOLVED if parallel is None
                             else parallel)
            out.append(entry)
    return out


def _installed(dist: str) -> bool:
    import importlib.metadata
    try:
        importlib.metadata.distribution(dist)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def _build_blas(np) -> dict:
    """What NumPy says it was BUILT against -- which names a library, not the
    kernel that runs; those come from :func:`_bundled_blas`."""
    try:
        deps = np.show_config(mode="dicts").get("Build Dependencies", {})
    except Exception:                               # noqa: BLE001
        deps = {}
    return {k: {f: dict(deps.get(k) or {}).get(f) for f in ("name",
                                                           "version")}
            for k in ("blas", "lapack")}


def _numpy_dispatch() -> dict | None:
    """Which implementation NumPy's runtime dispatch SELECTED, function by
    function and signature by signature, in this process.

    ``__cpu_features__`` says what the CPU offers and a disabled feature
    may still read as present there; this is what the dispatcher chose from
    it -- the thing R59 measured, and the only reading on which a build with
    no runtime dispatch and a host-dispatched build can be told apart. None
    when NumPy cannot say, which is unresolved rather than assumed."""
    try:
        from numpy.lib.introspect import opt_func_info
        info = opt_func_info()
    except Exception:                               # noqa: BLE001
        return None
    rows = sorted((str(func), str(sig), str(chosen.get("current")))
                  for func, sigs in info.items()
                  for sig, chosen in sigs.items())
    if not rows:
        return None
    targets: dict = {}
    for _, _, current in rows:
        targets[current] = targets.get(current, 0) + 1
    return {"functions": len(rows), "targets": dict(sorted(targets.items())),
            "sha256": hashlib.sha256("\n".join(
                "\0".join(r) for r in rows).encode()).hexdigest()}


def runtime_record() -> dict:
    """The numeric backend of THIS process, canonicalisable. Imports NumPy
    when it is installed; a process with no NumPy installed has no NumPy
    backend, and says so -- which is not an unresolved one."""
    import importlib.util
    if importlib.util.find_spec("numpy") is None:
        return _record([], numpy=None, build=None)
    import numpy as np
    unresolved = []
    try:
        from numpy._core._multiarray_umath import __cpu_features__ as feats
    except ImportError:                             # pragma: no cover
        feats = None
    if feats is None:
        unresolved.append("numpy cpu features")
    try:
        simd = np.show_config(mode="dicts").get("SIMD Extensions", {})
    except Exception:                               # noqa: BLE001
        simd = {}
        unresolved.append("numpy simd configuration")
    dispatch = _numpy_dispatch()
    if dispatch is None:
        unresolved.append("numpy runtime dispatch")
    numpy = {"version": np.__version__,
             "cpu_features": sorted(k for k, v in (feats or {}).items() if v),
             "simd": {k: simd.get(k) for k in ("baseline", "found",
                                               "not found")},
             "dispatch": dispatch}
    return _record(unresolved, numpy=numpy, build=_build_blas(np))


def _record(unresolved: list, *, numpy, build) -> dict:
    """The bundled BLAS builds, the system libraries -- the C and math
    runtimes, the optional OpenMP and Fortran ones, the dynamic loader --
    and the verdict, joined to what NumPy reported (``None`` without it)."""
    bundled = _bundled_blas()
    have = {lib["distribution"] for lib in bundled}
    # What the probe can IDENTIFY is a BLAS its distribution bundles and
    # records. Anything else -- MKL, Accelerate, a system library the build
    # linked -- does the linear algebra unnamed, and is UNRESOLVED rather
    # than assumed; a build with no external BLAS at all ("none") has
    # nothing to name.
    for part in ("blas", "lapack") if build is not None else ():
        name = str(build[part]["name"] or "").lower()
        if name in ("", "none"):
            continue
        if "openblas" not in name or "numpy" not in have:
            unresolved.append(f"numpy {part} {name!r} is not a bundled "
                              "build the probe identifies")
    if _installed("scipy") and "scipy" not in have:
        unresolved.append("scipy's BLAS is not a bundled build the probe "
                          "identifies")
    for lib in bundled:
        for part in ("sha256", "kernel", "config", "threads", "parallel"):
            if lib[part] == UNRESOLVED:
                unresolved.append(f"{lib['distribution']} blas {part}")
    libs = {}
    for name in SYSTEM_LIBRARIES:
        path = _loader_path(name)
        if path is None:
            libs[name] = None if name in OPTIONAL_LIBRARIES else UNRESOLVED
            if name not in OPTIONAL_LIBRARIES:
                unresolved.append(f"lib{name}")
            continue
        sha = installed_sha256(path)
        libs[name] = {"name": os.path.basename(path),
                      "sha256": sha or UNRESOLVED}
        if sha is None:
            unresolved.append(f"lib{name} bytes")
    loader = _dynamic_loader()
    sha = None if loader is None else installed_sha256(loader)
    if sha is None:
        unresolved.append("dynamic loader")
    libs["loader"] = (UNRESOLVED if sha is None else
                      {"name": os.path.basename(loader), "sha256": sha})
    interpreter = _interpreter(unresolved)
    return {
        "status": UNRESOLVED if unresolved else RESOLVED,
        "unresolved": sorted(unresolved),
        "numpy": numpy,
        "blas": {"build": build, "bundled": bundled},
        "system_libraries": libs,
        "interpreter": interpreter,
    }


def _interpreter(unresolved: list) -> dict:
    """The interpreter executing this process, by its installed bytes --
    and a shared libpython, when it runs from one. A version string names a
    release; two builds of one release are two binaries, and the one that
    ran is the one that parsed every float literal and called libm."""
    import sys
    exe = os.path.realpath(sys.executable)
    sha = installed_sha256(exe)
    out = {"name": os.path.basename(exe), "sha256": sha or UNRESOLVED}
    try:
        maps = Path("/proc/self/maps").read_text(encoding="utf-8",
                                                 errors="replace")
    except OSError:
        maps = ""
    for path in _mapped_files(maps):
        if os.path.basename(path).startswith("libpython"):
            out["libpython"] = {"name": os.path.basename(path),
                                "sha256": installed_sha256(path)
                                or UNRESOLVED}
            break
    if sha is None or (out.get("libpython") or {}).get("sha256") == \
            UNRESOLVED:
        unresolved.append("interpreter bytes")
    return out


def run_environment(**kw) -> dict:
    """The environment of a run IN THIS WORKER: the metadata record, with
    the runtime backend probed. What a run identity and the bundle it
    describes are both computed from, so they cannot disagree."""
    from .run_identity import environment_record
    return environment_record(runtime=runtime_record(), **kw)
