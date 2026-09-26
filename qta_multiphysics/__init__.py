"""QTA non-lumped 1D/2D reduced-order multiphysics forecast layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.
A 3D transient layer exists as forecast-only / benchmark-numerical
validation, reduction-checked against 1D/2D; it is not hardware-validated
and adds no PASS gate (see future_3d.py, STATUS).

IMPORTING THIS PACKAGE LOADS NOTHING (cut C1). It used to import
``runner.run_all`` eagerly, so importing ANY module here -- thermal_1d, a
grid, a unit constant -- loaded the orchestrator and, through it, the gate-spec
assembly (``metrics``) and the rest of the apparatus ontology. A physics model
must be importable without the machine it was first written for;
``tests/test_scientific_import_isolation.py`` holds that in a fresh
interpreter for every module dispositioned as generic infrastructure or as a
model plugin.
"""
__all__ = ["run_all"]


def run_all(*args, **kwargs):
    """TRANSITIONAL lazy entry point to the QTA orchestrator (C1).

    Kept so ``from qta_multiphysics import run_all`` still works while the
    orchestrator exists; it loads ``runner`` -- and with it the gate-spec
    assembly -- only when called. It retires with the orchestrator (C8).
    """
    from .runner import run_all as _run_all
    return _run_all(*args, **kwargs)
