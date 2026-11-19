#!/usr/bin/env python3
"""Build the thermal_rc2 FMU: compile, package deterministically, record.

The FMU is built from ``integrations/fmi/thermal_rc2/`` -- the C source and
the model description -- against the FMI 3.0 headers vendored unmodified in
``integrations/fmi/third_party/fmi3`` (whose recorded sha256 are checked
first). The archive is written deterministically: members in a fixed order,
a fixed timestamp, fixed permissions, so the same binary always packs to the
same bytes. The BINARY is whatever this host's C compiler makes of the
source: its bytes are recorded, with the compiler's identity and flags, and
are not claimed to be reproducible across compilers.

``--fault`` builds a deliberately WRONG variant (the conductance G12 used in
the dynamics is 10 % off what the description declares): a negative control
the verification must reject. Its archive is never the one the harness
imports as the model.

    python tools/fmi_build.py --out DIR [--fault]   # prints JSON provenance
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "integrations" / "fmi" / "thermal_rc2"
HEADERS = ROOT / "integrations" / "fmi" / "third_party" / "fmi3"
PLATFORM = "x86_64-linux"
CFLAGS = ["-std=c99", "-O2", "-fPIC", "-shared", "-Wall", "-Wextra",
          "-Werror", "-ffp-contract=off"]
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class BuildError(RuntimeError):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def check_headers() -> dict:
    rec = json.loads((HEADERS / "SOURCE.json").read_text(encoding="utf-8"))
    for name, sha in rec["files"].items():
        got = _sha((HEADERS / name).read_bytes())
        if got != sha:
            raise BuildError(f"vendored {name}: sha256 {got} differs from "
                             f"the recorded {sha}")
    return rec


def build(out_dir: Path, *, fault: bool = False) -> dict:
    hdr = check_headers()
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        raise BuildError("no C compiler on PATH")
    version = subprocess.run([cc, "--version"], capture_output=True,
                             text=True, check=True).stdout.splitlines()[0]
    out_dir.mkdir(parents=True, exist_ok=True)
    flags = list(CFLAGS) + (["-DTHERMAL_RC2_FAULT=1"] if fault else [])
    with tempfile.TemporaryDirectory(prefix="fmu-build-") as tmp:
        so = Path(tmp) / "thermal_rc2.so"
        subprocess.run([cc, *flags, f"-I{HEADERS}", "-o", str(so),
                        str(SRC / "thermal_rc2.c"), "-lm"], check=True)
        binary = so.read_bytes()
    md = (SRC / "modelDescription.xml").read_bytes()
    members = [("modelDescription.xml", md),
               (f"binaries/{PLATFORM}/thermal_rc2.so", binary),
               ("sources/thermal_rc2.c", (SRC / "thermal_rc2.c").read_bytes())]
    name = "thermal_rc2_FAULT.fmu" if fault else "thermal_rc2.fmu"
    fmu = out_dir / name
    with zipfile.ZipFile(fmu, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, data in members:
            info = zipfile.ZipInfo(arc, date_time=ZIP_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return {"fmu": str(fmu), "fault_variant": fault,
            "fmu_sha256": _sha(fmu.read_bytes()),
            "binary_sha256": _sha(binary),
            "model_description_sha256": _sha(md),
            "source_sha256": _sha((SRC / "thermal_rc2.c").read_bytes()),
            "headers": {"tag": hdr["tag"], "files": hdr["files"]},
            "compiler": version, "flags": flags, "platform": PLATFORM}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--fault", action="store_true")
    args = ap.parse_args(argv)
    try:
        print(json.dumps(build(args.out, fault=args.fault), sort_keys=True))
    except (BuildError, subprocess.CalledProcessError) as exc:
        print(f"FMU BUILD FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
