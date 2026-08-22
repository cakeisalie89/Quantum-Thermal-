"""Stage-10 scientific-stack adapters (additive, forecast-only).

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

This subpackage holds the adapters for the declared scientific stack that sit
*outside* the numerical core: visualization interchange (ParaView/VTK,
OpenUSD), global sensitivity and design-space exploration (SALib, OpenMDAO),
a read-only retrieval index over the project's own governed documents, and
the staged interfaces for FEniCSx, a selective Rust kernel, and FMI export.

Governance rules that every module here obeys, without exception:

* **Additive.** Nothing here is imported by the solvers, the gate logic, the
  Monte-Carlo layer, or ``qta_full_sim.py``. Removing this subpackage changes
  no canonical output byte.
* **Workspace-only writes.** Every writer takes an explicit output directory
  and refuses to write into the canonical repository root
  (:func:`qta_multiphysics.stack.workspace.guard_output_dir`). Canonical
  outputs stay byte-gated by ``package_consistency_check.py``.
* **No gate effect.** ``automatic_gate_effect = NONE`` for every module. No
  adapter can promote, demote, or create a gate, and none may introduce a
  ``measured_in_this_system = true`` record.
* **Fail-closed optionality.** Optional third-party packages (SALib, OpenMDAO,
  dolfinx, pxr, a compiled Rust kernel) are never required. When one is
  absent the adapter reports ``availability = UNAVAILABLE`` and falls back to
  the existing in-repo authority; it never silently substitutes a different
  numerical result.
* **Deterministic bytes.** Every artifact written here is reproducible
  byte-for-byte from the same inputs: fixed float formatting, sorted keys,
  no timestamps, no host paths, LF line endings.

``STACK.md`` / ``stack.json`` is the adoption authority for this subpackage
and records the status of each element.
"""
from __future__ import annotations

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"
AUTOMATIC_GATE_EFFECT = "NONE"
STACK_STAGE = "stage10"

__all__ = ["LABEL", "AUTOMATIC_GATE_EFFECT", "STACK_STAGE"]
