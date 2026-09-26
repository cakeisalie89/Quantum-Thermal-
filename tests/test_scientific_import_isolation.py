"""Importing a scientific module loads no hardware ontology (cut C1).

Before C1, ``qta_multiphysics/__init__.py`` imported the orchestrator, so
importing ``thermal_1d`` -- or a grid, or a unit constant -- loaded the
gate-spec assembly and the apparatus modules with it. Measured, not assumed:
the tranche-1 dependency analysis found every module in the package did.

This holds the cut in a FRESH interpreter per module, because a module
already loaded by an earlier test would make any in-process check vacuous.
The two sets are derived from FILE_DISPOSITION.csv, not listed here, so a
module that is re-dispositioned moves between them without an edit:

* CLEAN     -- every ``qta_multiphysics`` module dispositioned
               EXTRACT_GENERIC or KEEP_AS_MODEL_PLUGIN, and every module of
               the ``scientific`` package;
* FORBIDDEN -- every ``qta_multiphysics`` module dispositioned
               RETIRE_TO_HISTORY (the machine FSM, hardware governance, the
               design / BOM registry and validation-experiment registry, the
               mode and state machines, ...), plus the gate-spec assembly
               (``metrics``) and the orchestrator that imports it
               (``runner``).

The anti-vacuity control is the orchestrator itself: importing ``runner``
in the same probe must show ``metrics`` loaded, so a probe that could not
see a forbidden module would fail here rather than pass everywhere.
"""
from __future__ import annotations

import ast
import csv
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

PROBE = ("import importlib, json, sys; importlib.import_module(sys.argv[1]); "
         "print(json.dumps(sorted(sys.modules)))")

EXTRA_FORBIDDEN = ("qta_multiphysics.metrics", "qta_multiphysics.runner")

#: Clean-set modules that still reach the ontology by a path of their own,
#: beyond the package ``__init__`` C1 cut. Pinned EXACTLY: a new offender
#: fails, and so does fixing one of these without removing its entry.
KNOWN_RESIDUAL = {
    "qta_multiphysics.cryopanel_dynamics_3d": (
        "imports three operating-point constants (P_HE_DOSE_PA, "
        "P_C13_WORK_PA, DOSE_WINDOW_S) from species_accounting_3d "
        "(REWRITE_GENERIC), which imports mode_sequence_3d; recorded in "
        "DEPENDENCY_CUTOVER.md section 4 and cut with the cryopanel family "
        "in Phase 4 (C5), where the constants become declared inputs"),
}


def _module_name(path: str) -> str:
    mod = path[:-3].replace("/", ".")
    return mod[:-len(".__init__")] if mod.endswith(".__init__") else mod


def _by_disposition() -> dict:
    out: dict = {}
    with open(ROOT / "FILE_DISPOSITION.csv", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            p = row["path"]
            if p.startswith("qta_multiphysics/") and p.endswith(".py"):
                out.setdefault(row["disposition"], []).append(
                    _module_name(p))
    return out


def clean_modules() -> list:
    d = _by_disposition()
    sci = sorted(_module_name(p.relative_to(ROOT).as_posix())
                 for p in (ROOT / "scientific").rglob("*.py"))
    return sorted(d.get("EXTRACT_GENERIC", [])
                  + d.get("KEEP_AS_MODEL_PLUGIN", [])) + sci


def forbidden_modules() -> set:
    return set(_by_disposition().get("RETIRE_TO_HISTORY", [])) | \
        set(EXTRA_FORBIDDEN)


def loaded_by(module: str) -> set:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    r = subprocess.run([sys.executable, "-c", PROBE, module], cwd=ROOT,
                       capture_output=True, text=True, env=env, timeout=300)
    if r.returncode != 0:
        raise AssertionError(f"import {module} failed:\n{r.stderr[-2000:]}")
    return set(json.loads(r.stdout.strip().splitlines()[-1]))


@pytest.fixture(scope="module")
def probes() -> dict:
    mods = clean_modules()
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(zip(mods, pool.map(loaded_by, mods)))


def test_the_sets_are_what_the_disposition_table_says():
    clean, forbidden = clean_modules(), forbidden_modules()
    assert len(clean) > 50, clean
    assert "qta_multiphysics.thermal_1d" in clean
    assert "qta_multiphysics.machine_fsm" in forbidden
    assert "qta_multiphysics.hardware_governance_3d" in forbidden
    assert "qta_multiphysics.design.registry" in forbidden
    assert not set(clean) & forbidden


def test_no_scientific_module_loads_the_hardware_ontology(probes):
    offenders = {m: sorted(loaded & forbidden_modules())
                 for m, loaded in probes.items()
                 if loaded & forbidden_modules()}
    assert set(offenders) == set(KNOWN_RESIDUAL), json.dumps(offenders,
                                                             indent=1)


def test_the_named_families_are_clean(probes):
    """The directive's list, by name, with no residual allowed: thermal,
    fields, grids, numerics, materials, transport, spin, stack adapters."""
    named = [m for m in probes if any(k in m for k in (
        "thermal", "fields", "grids", "numerics", "material", "transport",
        "nv_spin", ".stack."))]
    assert len(named) >= 15, named
    dirty = {m: sorted(probes[m] & forbidden_modules()) for m in named
             if probes[m] & forbidden_modules()}
    assert not dirty, dirty


def test_the_residual_list_is_reasoned():
    for mod, why in KNOWN_RESIDUAL.items():
        assert len(why) >= 60, mod


def test_the_probe_sees_a_forbidden_module_when_one_is_loaded():
    """ANTI-VACUITY: the same probe, pointed at the orchestrator, must see
    the gate-spec assembly it imports."""
    loaded = loaded_by("qta_multiphysics.runner")
    assert "qta_multiphysics.metrics" in loaded


def test_the_package_init_imports_nothing_at_import_time():
    """The structural half: C1 is a property of this one file, so say it
    about the file and not only about its consequences."""
    tree = ast.parse((ROOT / "qta_multiphysics" / "__init__.py").read_text())
    eager = [n for n in tree.body if isinstance(n, (ast.Import,
                                                    ast.ImportFrom))
             and not (isinstance(n, ast.ImportFrom)
                      and n.module == "__future__")]
    assert not eager, [ast.dump(n) for n in eager]


def test_the_transitional_run_all_still_reaches_the_orchestrator():
    """``qta_full_sim.py`` calls ``qta_multiphysics.run_all``; the lazy
    entry must still be that function, loaded on call."""
    code = ("import sys, qta_multiphysics as q; "
            "assert 'qta_multiphysics.runner' not in sys.modules; "
            "from qta_multiphysics.runner import run_all; "
            "import inspect; src = inspect.getsource(q.run_all); "
            "assert 'from .runner import run_all' in src; print('ok')")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert r.stdout.strip() == "ok", r.stderr


STDLIB_ONLY = ("scientific", "scientific.identity", "scientific.quantity",
               "scientific.observation", "scientific.result",
               "scientific.verification", "scientific.model",
               "scientific.registry", "scientific.run_identity")


@pytest.fixture(scope="module")
def startup() -> set:
    """What a fresh interpreter holds before importing anything of ours --
    the probe's own imports plus whatever the environment loads at startup
    (a virtualenv's ``sitecustomize``)."""
    return loaded_by("json")


@pytest.mark.parametrize("module", STDLIB_ONLY)
def test_the_interfaces_load_only_the_standard_library(probes, startup,
                                                       module):
    """The contract must not drag a solver, the agent substrate or the
    numeric stack into whoever imports it."""
    third = {m.split(".")[0] for m in probes[module] - startup} - \
        set(sys.stdlib_module_names) - {"scientific"}
    assert not third, sorted(third)


def test_the_stdlib_check_can_see_a_third_party_import(startup):
    """Control: the same subtraction, on a module that does import numpy."""
    loaded = loaded_by("qta_multiphysics.grids") - startup
    assert "numpy" in {m.split(".")[0] for m in loaded}
