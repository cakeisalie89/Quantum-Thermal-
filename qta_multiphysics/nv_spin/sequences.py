"""NV pulse sequences, ODMR, Markovian evolution, and observable extraction.

All quantities are forecast/model-only. The module keeps four *distinct*
quantities separate, by name, in code and in returned schemas:

* ``tau_c``  - OU surface-noise bath correlation time (UNKNOWN input).
* ``T1``     - longitudinal population-relaxation time (input rate model).
* ``T2``     - transverse/echo coherence time (sequence-derived observable).
* ``T2star`` - Ramsey (free-induction) coherence time (sequence-derived).

They are never aliased. The Ramsey/Hahn/XY8 coherence curves come from the
colored-noise treatment in :mod:`.noise`; the Markovian routine uses QuTiP
``mesolve`` and is used for trace/positivity preservation and the analytic
Lindblad/T1 limits in the test suite.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import qutip as qt

from . import noise as _noise
from . import model as _model


# --- coherence curves (colored noise) ----------------------------------------

def coherence_curve(
    *,
    sequence: str,
    tau_c_s: float,
    sigma_rad_s: float,
    times_s: np.ndarray,
    n_time: int,
    n_traj: int,
    seed: int,
) -> dict:
    """Coherence W(T) for a sequence over a list of total evolution times.

    Returns dict with arrays: 'times_s', 'W_mc' (Monte-Carlo trajectories),
    'W_ff' (filter-function cross-check). Both W in [0, 1].
    """
    times = np.asarray(times_s, dtype=float)
    w_mc = np.empty_like(times)
    w_ff = np.empty_like(times)
    for i, T in enumerate(times):
        if T <= 0:
            w_mc[i] = 1.0
            w_ff[i] = 1.0
            continue
        w_mc[i] = _noise.coherence_from_trajectories(
            sequence=sequence, tau_c_s=tau_c_s, sigma_rad_s=sigma_rad_s,
            total_time_s=float(T), n_time=n_time, n_traj=n_traj, seed=seed,
        )
        w_ff[i] = _noise.filter_function_coherence(
            sequence=sequence, tau_c_s=tau_c_s, sigma_rad_s=sigma_rad_s,
            total_time_s=float(T),
        )
    return {"times_s": times, "W_mc": w_mc, "W_ff": w_ff}


def one_over_e_time(times_s: np.ndarray, w: np.ndarray) -> Optional[float]:
    """First time at which coherence W drops to 1/e, by linear interpolation.

    Returns None if W never reaches 1/e on the grid.
    """
    times = np.asarray(times_s, dtype=float)
    w = np.asarray(w, dtype=float)
    thr = 1.0 / math.e
    for i in range(1, len(w)):
        if w[i - 1] >= thr >= w[i]:
            # linear interpolation between (i-1, i)
            t0, t1 = times[i - 1], times[i]
            w0, w1 = w[i - 1], w[i]
            if w0 == w1:
                return float(t0)
            frac = (w0 - thr) / (w0 - w1)
            return float(t0 + frac * (t1 - t0))
    return None


# --- Markovian (Lindblad) evolution: trace / positivity / T1 / dephasing ------

def markovian_evolution(
    *,
    H: qt.Qobj,
    rho0: qt.Qobj,
    times_s: np.ndarray,
    T1_s: Optional[float] = None,
    T2_pure_s: Optional[float] = None,
    coh_levels: tuple = (0, 1),
    pop_level: int = 0,
    options: Optional[dict] = None,
) -> dict:
    """Evolve rho0 under H with optional T1 relaxation and pure dephasing.

    Collapse operators (S=1, jmat-z basis order: index 0 -> ms=+1, 1 -> ms=0,
    2 -> ms=-1):
      * T1: |0><+1| and |0><-1| each at rate 1/T1 (population relaxes to ms=0).
      * pure dephasing: sqrt(1/T2_pure) * Sz.
    Returns dict of arrays over time: 'trace', 'min_eig' (positivity),
    'purity', 'population' (of ``pop_level``), 'coherence' (|rho[i,j]| for
    ``coh_levels``).
    """
    ops = _model.spin1_operators()
    Sz = ops["Sz"]
    c_ops = []
    if T1_s is not None and T1_s > 0:
        g1 = 1.0 / T1_s
        ket0 = qt.basis(3, 1)  # ms = 0
        c_ops.append(math.sqrt(g1) * ket0 * qt.basis(3, 0).dag())  # |0><+1|
        c_ops.append(math.sqrt(g1) * ket0 * qt.basis(3, 2).dag())  # |0><-1|
    if T2_pure_s is not None and T2_pure_s > 0:
        gphi = 1.0 / T2_pure_s
        c_ops.append(math.sqrt(gphi) * Sz)

    tlist = np.asarray(times_s, dtype=float)
    solver_opts = {"nsteps": 100000, "atol": 1e-12, "rtol": 1e-10}
    if options:
        solver_opts.update(options)
    res = qt.mesolve(H, rho0, tlist, c_ops=c_ops, e_ops=[], options=solver_opts)
    i, j = coh_levels
    trace = np.empty(len(tlist))
    min_eig = np.empty(len(tlist))
    purity = np.empty(len(tlist))
    population = np.empty(len(tlist))
    coherence = np.empty(len(tlist))
    for k, rho in enumerate(res.states):
        M = rho.full()
        trace[k] = float(np.real(np.trace(M)))
        min_eig[k] = float(np.min(np.real(np.linalg.eigvalsh((M + M.conj().T) / 2))))
        purity[k] = float(np.real(np.trace(M @ M)))
        population[k] = float(np.real(M[pop_level, pop_level]))
        coherence[k] = float(abs(M[i, j]))
    return {
        "times_s": tlist,
        "trace": trace,
        "min_eig": min_eig,
        "purity": purity,
        "population": population,
        "coherence": coherence,
    }


# --- ODMR transition spectrum -------------------------------------------------

def odmr_spectrum(
    *,
    H: qt.Qobj,
    freq_grid_Hz: np.ndarray,
    linewidth_Hz: float,
    contrast: float = 1.0,
) -> dict:
    """Lorentzian transition spectrum from the eigenfrequencies of H.

    Returns dict: 'freq_Hz', 'signal' (sum of Lorentzians at each transition
    frequency), and 'lines_Hz' (the transition frequencies). Model-only ODMR
    lineshape; ``linewidth_Hz`` and ``contrast`` are forecast inputs.
    """
    lines = _model.transition_frequencies_Hz(H)
    f = np.asarray(freq_grid_Hz, dtype=float)
    signal = np.zeros_like(f)
    hw = max(linewidth_Hz, 1.0) / 2.0
    for f0 in lines:
        signal += contrast * (hw ** 2) / ((f - f0) ** 2 + hw ** 2)
    return {"freq_Hz": f, "signal": signal, "lines_Hz": lines}
