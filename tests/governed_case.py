"""The generic case the governed-path tests run: the transient slab, checked
by a method-of-lines solve. FIXTURES, NOT DATA.

One definition, so every suite that drives a scientific result from proposal
to authority drives the same model the same way, and a change of case is one
edit. The check is an INDEPENDENT_IMPLEMENTATION -- a different grid,
boundary closure, time integrator and linear solver -- the check type the
admission policy ``scientific_result.admission/1`` admits, and it needs no
runtime beyond this repository's. The run writes a temperature-field
artefact, which the artefact-resolution rules need something to resolve.
The physical parameters are declared (the slab has no defaults for them);
the resolution is left to the model's defaults (80 cells, 240 steps), so a
validated parameter set differs from the one proposed -- which the
run-identity tests rely on.
"""
from __future__ import annotations

MODEL = {"model_id": "thermal.slab_transient", "model_version": "1.0.0"}
CHECK = "thermal.slab_method_of_lines"
#: A 5 cm steel-like slab heated for ten minutes.
PARAMS = {"L_m": 0.05, "k_W_m_K": 15.0, "rho_c_J_m3_K": 3.6e6,
          "q_W_m3": 2e5, "h_W_m2_K": 50.0, "T_inf_K": 300.0,
          "T0_K": 300.0, "t_end_s": 600.0}
#: A second, different run of the same model: a coarser grid.
OTHER = {**PARAMS, "n_cells": 40, "n_steps": 120}
#: Parameters the model refuses: a grid on which its estimate does not hold
#: (D-2026-131).
INVALID = {**PARAMS, "n_cells": 12}
RUN_TOOL = "model.thermal.slab_transient.run"
