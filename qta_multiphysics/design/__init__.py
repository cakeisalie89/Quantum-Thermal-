"""QTA computable design-object layer (forecast/pre-hardware).

Typed, serializable components, interfaces, and interlocks derived
deterministically from existing repository records (BOM.csv, interface_map.csv,
interlock_table.csv). Operationally consumed by the validation engine and the
simulation/gate layer - not merely exported documentation.
"""
from __future__ import annotations

from .registry import (
    DesignComponent, DesignInterface, Interlock, DesignRegistry,
    load_registry, build_interface_graph, STAGE_ORDER, MODES,
)
from .validation import (
    validate_design, apply_validation_states, write_design_outputs,
    run_design_layer,
)

__all__ = [
    "DesignComponent", "DesignInterface", "Interlock", "DesignRegistry",
    "load_registry", "build_interface_graph", "STAGE_ORDER", "MODES",
    "validate_design", "apply_validation_states", "write_design_outputs",
    "run_design_layer",
]
