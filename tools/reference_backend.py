#!/usr/bin/env python3
"""The canonical reference backend: its recipe, its conformance, its identity.

``reference_backend/spec.json`` DECLARES a numerical backend whose arithmetic
the physical host cannot choose: OpenBLAS built for one target with no
DYNAMIC_ARCH and one thread, NumPy built with no runtime dispatch, a pinned
interpreter and root filesystem, all RUN on a pinned software CPU
(``reference_backend/run.sh``: qemu, Nehalem-v1). This tool checks that the
recipe says so, records what a build produced, and -- run INSIDE the
reference runtime -- checks that what executes is what was declared, failing
closed on any difference.

    env           the shell assignments build.sh and run.sh read
    recipe        static checks of spec.json, build.sh and run.sh
    build-record  after build.sh: the digests of everything it produced
    verify        inside run.sh: probe, FP checks, conformance, identity
    outputs DIR   sha256 of every file in an output directory
    host-record --verify V --outputs DIR
                  outside run.sh: one host's run -- its PHYSICAL CPU, the
                  emulator's bytes, verify's verdict, the outputs' digests
    two-host A B  the two-physical-host acceptance test (directive 7 s.34)

It is STAGED. It does not define the canonical corpus -- the corpus was
reproduced on a host-dispatched backend (docs/byte_reproduction_profile.json)
-- and nothing here migrates it. It claims reproducibility only: not physical
validity, not experimental verification, not ground truth.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "reference_backend" / "spec.json"
BUILD = ROOT / "reference_backend" / "build.sh"
RUN = ROOT / "reference_backend" / "run.sh"
SCHEMA = "canonical-reference-backend/1"

#: Flags that let the HOST choose, or that change IEEE semantics.
FORBIDDEN_FLAGS = ("-march=native", "-mtune=native", "-mcpu=native",
                   "-ffast-math", "-Ofast", "-funsafe-math-optimizations",
                   "-fassociative-math")
#: A software CPU that is the host's in disguise.
PASSTHROUGH_CPUS = ("host", "max", "native", "")
FP_POLICY_KEYS = ("rounding", "fma_contraction", "extended_precision",
                  "flush_to_zero", "denormals_are_zero", "fast_math", "libm",
                  "compiler_isa_baseline")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def load_spec(path: Path = SPEC) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256(path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True,
                                     separators=(",", ":")).encode()
                          ).hexdigest()


# ---- the recipe -----------------------------------------------------------
def shell_env(spec: dict) -> dict:
    """The variables build.sh and run.sh read -- the spec, flattened."""
    s = spec["sources"]
    env = {"QTA_REF_PREFIX": spec["prefix"],
           "QTA_REF_PYTHON_REQUEST": s["python"]["uv_request"],
           "QTA_REF_PYTHON_SHA256": s["python"]["binary_sha256"],
           "QTA_REF_ROOTFS_URL": s["rootfs"]["url"],
           "QTA_REF_ROOTFS_SHA256": s["rootfs"]["sha256"],
           "QTA_REF_CFLAGS": spec["cflags"],
           "QTA_REF_BUILD_DEPS": " ".join(spec["build_python_deps"]),
           "QTA_REF_WHEEL_PLAT": spec["wheel_platform"],
           "QTA_REF_CPU": spec["software_cpu"]["cpu"],
           "QTA_REF_EMULATOR": spec["software_cpu"]["emulator"]}
    for name in ("openblas", "numpy", "scipy"):
        up = name.upper()
        env[f"QTA_REF_{up}_URL"] = s[name]["url"]
        env[f"QTA_REF_{up}_SHA256"] = s[name]["sha256"]
        env[f"QTA_REF_{up}_VERSION"] = s[name]["version"]
    for pkg, version in spec["toolchain"].items():
        env["QTA_REF_TOOL_" + pkg.upper().replace("-", "_")] = version
    for k, v in spec["openblas"].items():
        env[f"QTA_REF_OB_{k}"] = v
    for k, v in spec["numpy"].items():
        env["QTA_REF_NP_" + k.upper().replace("-", "_")] = v
    for k, v in spec["scipy"].items():
        env["QTA_REF_SP_" + k.upper().replace("-", "_")] = v
    for k, v in spec["threads"].items():
        env[f"QTA_REF_THREADS_{k}"] = v
    patches = spec.get("patches", [])
    env["QTA_REF_PATCH_COUNT"] = str(len(patches))
    for i, pt in enumerate(patches):
        env[f"QTA_REF_PATCH_{i}_PATH"] = pt["path"]
        env[f"QTA_REF_PATCH_{i}_SHA256"] = pt["sha256"]
    return env


def shell_code(text: str) -> str:
    """A shell script without its comments: what it DOES, not what it says
    about what it does."""
    return "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("#"))


def recipe_problems(spec: dict, build: str, run: str) -> list:
    """Does the recipe fix the arithmetic, or leave any of it to the host?"""
    build, run = shell_code(build), shell_code(run)
    out = []
    if spec.get("schema") != SCHEMA:
        return [f"schema is {spec.get('schema')!r}"]
    ob, np_, cpu = spec["openblas"], spec["numpy"], spec["software_cpu"]
    if ob.get("DYNAMIC_ARCH") != "0":
        out.append("OpenBLAS DYNAMIC_ARCH is not 0: the host would pick the "
                   "kernel at load time")
    if not ob.get("TARGET") or ob["TARGET"].upper() in ("HOST", "NATIVE"):
        out.append("OpenBLAS TARGET is not one fixed target")
    if ob.get("USE_THREAD") != "0" or ob.get("NUM_THREADS") != "1" or \
            ob.get("USE_OPENMP") != "0":
        out.append("OpenBLAS is not single-threaded")
    if np_.get("cpu-dispatch") != "none":
        out.append("NumPy keeps runtime dispatch targets")
    if np_.get("cpu-baseline") in ("native", "max", "", None):
        out.append("NumPy's baseline is the host's, not a fixed one")
    flags = " ".join([spec.get("cflags", ""), ob.get("COMMON_OPT", ""),
                      ob.get("FCOMMON_OPT", "")])
    for f in FORBIDDEN_FLAGS:
        if f in flags or f in build or f in run:
            out.append(f"{f} appears in the recipe")
    for name, text in (("cflags", spec.get("cflags", "")),
                       ("COMMON_OPT", ob.get("COMMON_OPT", "")),
                       ("FCOMMON_OPT", ob.get("FCOMMON_OPT", ""))):
        if "-ffp-contract=off" not in text:
            out.append(f"{name} does not turn FMA contraction off")
    for k, v in spec["threads"].items():
        if v != "1":
            out.append(f"{k} is {v!r}, not 1")
    if cpu.get("cpu", "").lower() in PASSTHROUGH_CPUS:
        out.append("the software CPU is the host's (passthrough)")
    if cpu.get("emulator") != "qemu-x86_64":
        out.append("no software CPU emulator is declared")
    for k in FP_POLICY_KEYS:
        v = spec.get("fp_policy", {}).get(k, "")
        if not v or "UNRESOLVED" in v.upper():
            out.append(f"floating-point policy {k!r} is not stated")
    for name, src in spec["sources"].items():
        sha = src.get("sha256") or src.get("binary_sha256") or ""
        if not _HEX64.match(sha):
            out.append(f"source {name} is not pinned by sha256")
    for needle, why in (
            ('DYNAMIC_ARCH="$QTA_REF_OB_DYNAMIC_ARCH"',
             "build.sh does not pass the declared DYNAMIC_ARCH"),
            ('TARGET="$QTA_REF_OB_TARGET"',
             "build.sh does not pass the declared TARGET"),
            ('-Dcpu-dispatch="$QTA_REF_NP_CPU_DISPATCH"',
             "build.sh does not pass NumPy's declared dispatch"),
            ('-Dcpu-baseline="$QTA_REF_NP_CPU_BASELINE"',
             "build.sh does not pass NumPy's declared baseline"),
            ):
        if needle not in build:
            out.append(why)
    # what is fetched is verified: by fetch() itself, each source through it
    # with its own pin, and the interpreter by its binary's pin
    body = re.search(r"^fetch\(\)\s*\{(.*?)^\}", build, re.S | re.M)
    if not body or "sha256sum -c" not in body.group(1):
        out.append("build.sh does not verify what it fetches")
    for pt in spec.get("patches", []):
        if pt.get("applies_to") != "openblas" or not pt.get("reason") or \
                not _HEX64.match(pt.get("sha256") or ""):
            out.append(f"patch {pt.get('path')} is not pinned and explained")
        elif sha256(ROOT / pt["path"]) != pt["sha256"]:
            out.append(f"patch {pt['path']} does not have its pinned sha256")
    if spec.get("patches") and not re.search(
            r'sha256sum -c[^\n]*\n[^\n]*patch -s -p1', build):
        out.append("build.sh does not verify each patch before applying it")
    for name in ("openblas", "numpy", "scipy", "rootfs"):
        up = name.upper()
        if not re.search(rf'^fetch "\$QTA_REF_{up}_URL" "\$QTA_REF_{up}_SHA256"',
                         build, re.M):
            out.append(f"build.sh does not fetch {name} against its pin")
    if not re.search(r'"\$QTA_REF_PYTHON_SHA256 [^\n]*\| sha256sum -c', build):
        out.append("build.sh does not verify the interpreter against its pin")
    for needle, why in (
            ('-cpu "$QTA_REF_CPU"', "run.sh does not run on the declared "
                                    "software CPU"),
            ('-L "$QTA_REF_PREFIX/rootfs"', "run.sh does not use the pinned "
                                            "root filesystem"),
            ("env -i", "run.sh inherits the caller's environment")):
        if needle not in run:
            out.append(why)
    if re.search(r"-cpu\s+(host|max)\b", run):
        out.append("run.sh passes the host CPU through")
    return out


# ---- what a build produced ----------------------------------------------
def build_record(spec: dict) -> dict:
    prefix = Path(spec["prefix"])
    files = {}
    for sub in ("openblas/lib", "venv/lib", "wheels"):
        base = prefix / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and (p.suffix in (".so", ".whl") or ".so." in
                                p.name):
                files[str(p.relative_to(prefix))] = sha256(p)
    rootfs = prefix / "rootfs"
    libs = {n: sha256(rootfs / "usr/lib/x86_64-linux-gnu" / n) for n in (
        "libc.so.6", "libm.so.6", "ld-linux-x86-64.so.2")}
    tools = {}
    for pkg in spec["toolchain"]:
        try:
            tools[pkg] = subprocess.run(
                ["dpkg-query", "-W", "-f=${Version}", pkg],
                capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            tools[pkg] = None
    emulator = subprocess.run(["which", spec["software_cpu"]["emulator"]],
                              capture_output=True, text=True).stdout.strip()
    return {"spec_digest": digest(spec), "native_files": files,
            "rootfs_libraries": libs, "toolchain": tools,
            "emulator_sha256": sha256(emulator) if emulator else None}


# ---- conformance, inside the reference runtime ----------------------------
def fp_environment() -> dict:
    """What the floating-point environment DOES, measured."""
    import numpy as np
    tiny = np.finfo(np.float64).tiny
    return {
        "subnormal_survives": bool(np.float64(tiny) / 2.0 != 0.0),
        "ties_to_even": bool(1.0 + 2.0 ** -53 == 1.0 and
                             1.0 + 3 * 2.0 ** -53 == 1.0 + 2 * 2.0 ** -52),
        "compiled_dispatch": list(
            __import__("numpy._core._multiarray_umath",
                       fromlist=["x"]).__cpu_dispatch__),
    }


def conformance(spec: dict, record: dict, fp: dict,
                rootfs_libraries: dict | None) -> list:
    """Every way the executing backend differs from the declared one."""
    exp = spec["expected_runtime"]
    out = []
    rt = (record or {}).get("runtime")
    if not isinstance(rt, dict) or record.get("backend_status") != "RESOLVED":
        return ["the reference runtime's backend is not RESOLVED: "
                f"{(rt or {}).get('unresolved')}"]
    n = rt["numpy"]
    targets = (n.get("dispatch") or {}).get("targets") or {}
    if not targets or not all(t.startswith("baseline") for t in targets):
        out.append(f"NumPy dispatched outside the baseline: {targets}")
    if fp.get("compiled_dispatch"):
        out.append(f"NumPy was built with dispatch targets "
                   f"{fp['compiled_dispatch']}")
    if (n.get("simd") or {}).get("baseline") != exp["numpy_baseline"]:
        out.append("NumPy's baseline is "
                   f"{(n.get('simd') or {}).get('baseline')}, not "
                   f"{exp['numpy_baseline']}")
    for f in exp["cpu_features_forbidden"]:
        if f in (n.get("cpu_features") or []):
            out.append(f"the CPU offers {f}: this is not the declared "
                       "software CPU")
    bundled = rt["blas"]["bundled"]
    if not bundled:
        out.append("no OpenBLAS was identified")
    for b in bundled:
        if b["kernel"] != exp["openblas_kernel"]:
            out.append(f"{b['distribution']} OpenBLAS runs {b['kernel']}, "
                       f"not {exp['openblas_kernel']}")
        if b["threads"] != exp["openblas_threads"]:
            out.append(f"{b['distribution']} OpenBLAS runs {b['threads']} "
                       "threads")
        if b["parallel"] != exp["openblas_parallel"]:
            out.append(f"{b['distribution']} OpenBLAS threading model is "
                       f"{b['parallel']}")
        for token in exp["openblas_config_forbids"]:
            if token in str(b["config"]):
                out.append(f"{b['distribution']} OpenBLAS was built with "
                           f"{token}")
    variables = (record.get("backend") or {}).get("variables") or {}
    for k, v in spec["threads"].items():
        if variables.get(k) != v:          # unrecorded is not "as declared"
            out.append(f"{k} is {variables.get(k)!r}, not {v!r}")
    if (rt.get("interpreter") or {}).get("sha256") != \
            spec["sources"]["python"]["binary_sha256"]:
        out.append("the interpreter is not the pinned one")
    if not rootfs_libraries:
        out.append("no build record names the root filesystem's libraries: "
                   "the C and math libraries cannot be checked")
    else:
        libs = rt.get("system_libraries") or {}
        for key, name in (("c", "libc.so.6"), ("m", "libm.so.6"),
                          ("loader", "ld-linux-x86-64.so.2")):
            got = (libs.get(key) or {}).get("sha256")
            if got is None or got != rootfs_libraries.get(name):
                out.append(f"lib{key if key != 'loader' else ''} is not the "
                           f"pinned root filesystem's {name}")
    if not fp.get("subnormal_survives"):
        out.append("subnormals are flushed")
    if not fp.get("ties_to_even"):
        out.append("rounding is not to nearest, ties to even")
    return out


def reference_identity(spec: dict, record: dict) -> dict:
    """The identity two hosts must share. /proc/cpuinfo is the HOST's under
    qemu-user and is left out; what the dispatchers saw -- CPUID, the
    software CPU's -- is in, through NumPy's features and dispatch."""
    rt = record["runtime"]
    return {"spec": digest(spec),
            "python": record.get("python"),
            "distributions": record.get("distributions"),
            "native": record.get("native"),
            "numpy": rt.get("numpy"), "blas": rt.get("blas"),
            "system_libraries": rt.get("system_libraries"),
            "interpreter": rt.get("interpreter"),
            "variables": (record.get("backend") or {}).get("variables"),
            "software_cpu": spec["software_cpu"]}


def outputs_manifest(directory: Path) -> dict:
    return {p.name: sha256(p) for p in sorted(Path(directory).iterdir())
            if p.is_file()}


# ---- the two-physical-host acceptance test (directive 7 s.34) ---------------
HOST_SCHEMA = "reference-host-run/1"
TWO_HOST_IDENTICAL = "TWO_HOST_BYTE_IDENTICAL"


def physical_host(cpuinfo: str | None = None) -> dict:
    """The PHYSICAL CPU, read outside the emulator: what the reference is
    meant to make irrelevant, and what two hosts must differ in for their
    agreement to prove anything."""
    if cpuinfo is None:
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8")
        except OSError:
            cpuinfo = ""
    first = {}
    for line in cpuinfo.split("\n\n")[0].splitlines():
        k, _, v = line.partition(":")
        first.setdefault(k.strip(), v.strip())
    flags = sorted(first.get("flags", "").split())
    return {"vendor": first.get("vendor_id"),
            "family": first.get("cpu family"), "model": first.get("model"),
            "model_name": first.get("model name"),
            "flags_sha256": digest(flags) if flags else None,
            "avx512f": "avx512f" in flags, "avx2": "avx2" in flags}


def host_class(host: dict) -> tuple:
    """Two hosts are one CPU class when vendor, family, model and feature
    set all agree; the marketing name is not part of it."""
    return (host.get("vendor"), host.get("family"), host.get("model"),
            host.get("flags_sha256"))


def host_record(spec: dict, verify: dict, outputs: dict, host: dict,
                emulator_sha256: str | None,
                generator_digest: str | None) -> dict:
    return {"schema": HOST_SCHEMA, "spec_digest": digest(spec),
            "generator_digest": generator_digest,
            "physical_host": host, "emulator_sha256": emulator_sha256,
            "conformance": verify.get("conformance"),
            "identity_digest": verify.get("identity_digest"),
            "outputs": outputs}


def generator_digest(root: Path = ROOT) -> str | None:
    """The generator the outputs came from: the measured closure digest the
    byte-reproduction witness profile binds (D-2026-101)."""
    try:
        prof = json.loads((Path(root) / "docs" /
                           "byte_reproduction_profile.json").read_text(
                               encoding="utf-8"))
        return prof["generator"]["digest"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def two_host(a: dict, b: dict, declared, exempt) -> tuple:
    """(verdict, problems) for two hosts' runs of the reference. Only
    TWO_HOST_BYTE_IDENTICAL is a proof; everything that is not verified to
    agree is a difference, never a pass."""
    problems = []
    for name, rec in (("A", a), ("B", b)):
        if not isinstance(rec, dict) or rec.get("schema") != HOST_SCHEMA:
            return "REFUSED", [f"host {name}: not a {HOST_SCHEMA} record"]
        if rec.get("conformance") != [] or not rec.get("identity_digest"):
            problems.append(f"host {name}: the reference runtime did not "
                            f"conform: {rec.get('conformance')}")
        if not rec.get("physical_host") or \
                not rec["physical_host"].get("flags_sha256"):
            problems.append(f"host {name}: its physical CPU is not recorded")
    if problems:
        return "REFUSED", problems
    if a["spec_digest"] != b["spec_digest"]:
        return "DIFFERENT_RECIPES", ["the hosts ran different spec.json"]
    if not a.get("generator_digest") or \
            a.get("generator_digest") != b.get("generator_digest"):
        return "DIFFERENT_GENERATORS", ["the hosts ran different generators, "
                                        "or one is unrecorded"]
    if not a.get("emulator_sha256") or \
            a.get("emulator_sha256") != b.get("emulator_sha256"):
        return "DIFFERENT_EMULATORS", ["the emulator binaries differ or are "
                                       "unrecorded"]
    if host_class(a["physical_host"]) == host_class(b["physical_host"]):
        return "NOT_TWO_HOSTS", ["both runs are on one physical CPU class: "
                                 "their agreement proves nothing about "
                                 "host independence"]
    if a["identity_digest"] != b["identity_digest"]:
        return "REFERENCE_IDENTITY_DIFFERS", [
            "the reference runtime is not the same on both hosts: a hidden "
            "environmental input is outside the boundary"]
    scope = sorted(set(declared) - set(exempt))
    if not scope:
        return "REFUSED", ["no canonical outputs are declared"]
    differ = [n for n in scope
              if a["outputs"].get(n) is None or
              a["outputs"].get(n) != b["outputs"].get(n)]
    if differ:
        return "BYTES_DIFFER", [f"{len(differ)} of {len(scope)} canonical "
                                f"file(s) differ or are missing: {differ}"]
    return TWO_HOST_IDENTICAL, []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=("env", "recipe", "build-record",
                                    "verify", "outputs", "host-record",
                                    "two-host"))
    ap.add_argument("path", nargs="*")
    ap.add_argument("--verify", help="host-record: verify's JSON output")
    ap.add_argument("--outputs", help="host-record: the output directory")
    args = ap.parse_args(argv)
    spec = load_spec()
    if args.cmd == "env":
        for k, v in shell_env(spec).items():
            print(f"export {k}={shlex.quote(v)}")
        return 0
    if args.cmd == "recipe":
        problems = recipe_problems(spec, BUILD.read_text(encoding="utf-8"),
                                   RUN.read_text(encoding="utf-8"))
        for p in problems:
            print(f"  - {p}")
        print("REFERENCE RECIPE: " + ("REFUSED" if problems else
                                      "fixes the arithmetic path"))
        return 1 if problems else 0
    if args.cmd == "build-record":
        print(json.dumps(build_record(spec), indent=1, sort_keys=True))
        return 0
    if args.cmd == "outputs":
        print(json.dumps(outputs_manifest(Path(args.path[0])), indent=1,
                         sort_keys=True))
        return 0
    if args.cmd == "host-record":      # OUTSIDE the emulator
        emulator = shutil.which(spec["software_cpu"]["emulator"])
        print(json.dumps(host_record(
            spec, json.loads(Path(args.verify).read_text(encoding="utf-8")),
            outputs_manifest(Path(args.outputs)), physical_host(),
            sha256(emulator) if emulator else None, generator_digest()),
            indent=1, sort_keys=True))
        return 0
    if args.cmd == "two-host":
        sys.path.insert(0, str(ROOT / "tools"))
        import cross_env_semantics
        declared, exempt = cross_env_semantics.declared_scope(ROOT)
        a, b = (json.loads(Path(p).read_text(encoding="utf-8"))
                for p in args.path)
        verdict, problems = two_host(a, b, declared, exempt)
        for p in problems:
            print(f"  - {p}")
        print(f"TWO-HOST ACCEPTANCE: {verdict}")
        return 0 if verdict == TWO_HOST_IDENTICAL else 1
    # verify: inside the reference runtime
    sys.path.insert(0, str(ROOT))
    from scientific.backend_probe import run_environment
    record = run_environment(distributions=("numpy", "scipy"))
    rec_path = Path(spec["prefix"]) / "BUILD_RECORD.json"
    rootfs = None
    if rec_path.is_file():
        rootfs = json.loads(rec_path.read_text())["rootfs_libraries"]
    problems = conformance(spec, record, fp_environment(), rootfs)
    identity = reference_identity(spec, record) if not problems else None
    print(json.dumps({"conformance": problems,
                      "identity_digest": digest(identity) if identity
                      else None,
                      "identity": identity}, indent=1, sort_keys=True))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
