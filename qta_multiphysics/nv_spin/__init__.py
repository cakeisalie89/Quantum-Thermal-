"""NV S=1 spin-dynamics / decoherence layer for QTA (forecast / model-only).

QuTiP-based. Models the NV ground-state spin, ZFS/Zeeman/strain Hamiltonian,
Ramsey/Hahn/XY8 sequences, ODMR transition spectrum, T1 relaxation and
Markovian dephasing, plus a fixed-seed Ornstein-Uhlenbeck colored-noise
treatment tied to the UNKNOWN surface-noise correlation time ``tau_c``.

Nothing here constitutes a measurement of the QTA system. ``tau_c``, T1, T2 and
T2* are kept strictly distinct. He-3/He-4 effects are parameterised forecast
hypotheses only - no fabricated isotope contrast.
"""
from __future__ import annotations

from .spin_config import NVSpinConfig, ci_config, standard_config
from .model import (
    spin1_operators,
    nv_ground_state_hamiltonian,
    temperature_dependent_ZFS_rad_s,
    transition_frequencies_Hz,
    measured_constants,
    HyperfineCoupling,
)
from .runner import run_nv_spin_layer, parameter_provenance, ModeReadinessError

__all__ = [
    "NVSpinConfig", "ci_config", "standard_config",
    "spin1_operators", "nv_ground_state_hamiltonian",
    "temperature_dependent_ZFS_rad_s", "transition_frequencies_Hz",
    "measured_constants", "HyperfineCoupling",
    "run_nv_spin_layer", "parameter_provenance", "ModeReadinessError",
]
