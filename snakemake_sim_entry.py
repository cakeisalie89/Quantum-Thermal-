#!/usr/bin/env python3
"""Workspace-directed wrapper around the AUTHORITATIVE full pipeline.

Runs `qta_full_sim.py` unchanged (the scientific source of truth), then
copies the freshly generated `outputs/` set into the requested workspace
directory. No solver is rewritten; no canonical root file is written.
MODEL-ONLY / FORECAST-ONLY.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ws = Path(sys.argv[1])
r = subprocess.run([sys.executable, "qta_full_sim.py"])
if r.returncode != 0:
    sys.exit(r.returncode)
ws.mkdir(parents=True, exist_ok=True)
for f in sorted(Path("outputs").iterdir()):
    if f.is_file():
        shutil.copy2(f, ws / f.name)
print(f"[snakemake entry] copied {len(list(ws.iterdir()))} outputs -> {ws}")
