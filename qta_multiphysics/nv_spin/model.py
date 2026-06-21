"""NV ground-state S=1 model: operators and Hamiltonian.

Forecast / model-only. Nothing in this module is a measurement of the QTA
system. Physical constants are loaded from the repository's
``measured_parameters.json`` (LITERATURE / MANUFACTURER_SPEC provenance) or are
explicit, cited literature values; every such value is reported in the spin
provenance output. The ``tau_c`` surface-noise correlation time is an UNKNOWN
forecast knob handled in :mod:`qta_multiphysics.nv_spin.noise`, never here, and
never as measured evidence.

Conventions
-----------
* The NV electronic ground state is an S=1 system (basis ms = +1, 0, -1).
* All Hamiltonian terms are expressed as *angular frequencies* in rad/s.
* ``D`` is the zero-field splitting (rad/s); ``gamma_e`` the electron
  gyromagnetic ratio (rad/s/T); ``E`` the transverse strain/electric-field
  splitting (rad/s); optional hyperfine couplings (rad/s) are only included
  when explicit parameters are supplied.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np
import qutip as qt

# --- repository root + measured-constant access -------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MEASURED_PARAMS_PATH = _REPO_ROOT / "measured_parameters.json"

# NV ground-state zero-field splitting. Literature value (not measured here).
# D/2pi = 2.87 GHz at low temperature (e.g. Doherty et al., Phys. Rep. 2013).
_D_ZFS_HZ_LITERATURE = 2.87e9
_D_ZFS_CITATION = "Doherty2013 (NV ground-state ZFS D/2pi = 2.87 GHz, literature)"


@lru_cache(maxsize=1)
def _measured() -> dict:
    with open(_MEASURED_PARAMS_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def measured_constants() -> dict:
    """Return the literature/manufacturer NV constants actually used, with units.

    Provenance is preserved verbatim from ``measured_parameters.json`` for the
    entries it contains; the ZFS is an explicit cited literature constant.
    """
    mp = _measured()
    gNV = mp["gNV_rads_per_T"]
    dZFS = mp["dZFS_dT_rad_s_K"]
    out = {
        "D_ZFS_rad_s": 2.0 * math.pi * _D_ZFS_HZ_LITERATURE,
        "D_ZFS_Hz": _D_ZFS_HZ_LITERATURE,
        "D_ZFS_provenance": _D_ZFS_CITATION,
        "D_ZFS_status": "LITERATURE",
        "gamma_e_rad_s_T": float(gNV["value"]),
        "gamma_e_provenance": gNV["source"],
        "gamma_e_status": gNV["status"],
        "dD_dT_rad_s_K": float(dZFS["value"]),
        "dD_dT_provenance": dZFS["source"],
        "dD_dT_status": dZFS["status"],
    }
    # gHe3 if present (used downstream as a parameterised forecast input only).
    if "gHe3_rads_per_T" in mp:
        out["gamma_He3_rad_s_T"] = float(mp["gHe3_rads_per_T"]["value"])
        out["gamma_He3_provenance"] = mp["gHe3_rads_per_T"]["source"]
        out["gamma_He3_status"] = mp["gHe3_rads_per_T"]["status"]
    return out


# --- S=1 spin operators -------------------------------------------------------

def spin1_operators() -> dict:
    """Return the S=1 spin operators and identity as QuTiP Qobj.

    Keys: Sx, Sy, Sz, Sp, Sm, I, Sz2 (= Sz@Sz). Dimensionless (units of hbar).
    """
    Sx = qt.jmat(1, "x")
    Sy = qt.jmat(1, "y")
    Sz = qt.jmat(1, "z")
    return {
        "Sx": Sx,
        "Sy": Sy,
        "Sz": Sz,
        "Sp": qt.jmat(1, "+"),
        "Sm": qt.jmat(1, "-"),
        "I": qt.qeye(3),
        "Sz2": Sz * Sz,
    }


def temperature_dependent_ZFS_rad_s(T_K: float, T_ref_K: float = 0.01) -> float:
    """D(T) = D0 + (dD/dT) (T - T_ref), all in rad/s.

    Uses the literature ZFS and its measured temperature slope. ``T_ref`` is the
    reference temperature at which ``D0`` is taken (default 10 mK base).
    """
    c = measured_constants()
    return c["D_ZFS_rad_s"] + c["dD_dT_rad_s_K"] * (float(T_K) - float(T_ref_K))


@dataclass(frozen=True)
class HyperfineCoupling:
    """Optional axial/transverse hyperfine coupling (rad/s) to one nuclear spin.

    Only supplied explicitly (e.g. 14N: I=1; 13C: I=1/2). Never defaulted on.
    """
    nuclear_spin: float          # I (e.g. 1.0 for 14N, 0.5 for 13C)
    A_parallel_rad_s: float      # A_zz
    A_perp_rad_s: float          # A_xx = A_yy
    label: str = "hyperfine"


def nv_ground_state_hamiltonian(
    *,
    B_T: float = 0.0,
    theta_B_rad: float = 0.0,
    T_K: float = 0.01,
    E_strain_rad_s: float = 0.0,
    hyperfine: Optional[HyperfineCoupling] = None,
    T_ref_K: float = 0.01,
) -> qt.Qobj:
    """Construct the NV ground-state Hamiltonian (rad/s).

    H = D(T) [Sz^2 - S(S+1)/3 I]
        + gamma_e (Bx Sx + Bz Sz)            # electron Zeeman (B in the x-z plane)
        + E_strain (Sx^2 - Sy^2)             # transverse strain / E-field
        [ + A_par Sz Iz + A_perp (Sx Ix + Sy Iy) ]   # optional hyperfine

    The magnetic field magnitude ``B_T`` is applied at polar angle ``theta_B``
    from the NV axis (z). All terms are Hermitian by construction; this is
    asserted in the test suite.
    """
    c = measured_constants()
    gamma_e = c["gamma_e_rad_s_T"]
    D = temperature_dependent_ZFS_rad_s(T_K, T_ref_K=T_ref_K)

    ops = spin1_operators()
    Sx, Sy, Sz, I, Sz2 = ops["Sx"], ops["Sy"], ops["Sz"], ops["I"], ops["Sz2"]

    Bz = B_T * math.cos(theta_B_rad)
    Bx = B_T * math.sin(theta_B_rad)

    # S(S+1)/3 = 2/3 for S=1.
    H_e = D * (Sz2 - (2.0 / 3.0) * I) + gamma_e * (Bx * Sx + Bz * Sz)
    H_e = H_e + E_strain_rad_s * (Sx * Sx - Sy * Sy)

    if hyperfine is None:
        return H_e

    # Tensor with a nuclear spin space.
    dim_n = int(round(2 * hyperfine.nuclear_spin + 1))
    j = hyperfine.nuclear_spin
    Ix = qt.jmat(j, "x")
    Iy = qt.jmat(j, "y")
    Iz = qt.jmat(j, "z")
    In = qt.qeye(dim_n)

    H_e_full = qt.tensor(H_e, In)
    H_hf = (
        hyperfine.A_parallel_rad_s * qt.tensor(Sz, Iz)
        + hyperfine.A_perp_rad_s * (qt.tensor(Sx, Ix) + qt.tensor(Sy, Iy))
    )
    return H_e_full + H_hf


def transition_frequencies_Hz(H: qt.Qobj) -> list:
    """Eigenfrequency *differences* (Hz) of H, sorted ascending (ODMR lines).

    Returns the set of positive transition frequencies |E_i - E_j|/2pi between
    eigenstates, deduplicated to ~1 Hz. H is in rad/s.
    """
    ev = np.sort(np.real(H.eigenenergies()))  # rad/s
    freqs = []
    for i in range(len(ev)):
        for k in range(i + 1, len(ev)):
            f = abs(ev[k] - ev[i]) / (2.0 * math.pi)
            freqs.append(f)
    freqs = sorted(freqs)
    dedup = []
    for f in freqs:
        if not dedup or abs(f - dedup[-1]) > 1.0:
            dedup.append(f)
    return dedup
