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
__all__: list = []

# ``run_all`` used to live here as a lazy convenience entry point to the
# orchestrator. It was the one edge through which EVERY module of this
# package -- a grid, a unit constant, thermal_1d -- statically reached the
# legacy QTA orchestration (``runner`` -> ``future_3d``): an import of any
# submodule initialises this package, and the framework boundary
# (``tools/framework_boundary.py``) reads the package's lazy imports as its
# dependencies. Its only caller, ``qta_full_sim.py``, now imports
# ``qta_multiphysics.runner.run_all`` itself -- legacy reaching legacy.
