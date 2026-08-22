"""Colored surface-noise model for NV dephasing, tied to the UNKNOWN ``tau_c``.

Two complementary, documented treatments of the same Ornstein-Uhlenbeck (OU)
surface-noise process are provided:

1. ``ou_trajectories`` - fixed-seed OU noise realisations delta(t) (a stationary
   Gaussian process with Lorentzian spectrum), used to accumulate the
   sequence-dependent phase and average the coherence (Monte Carlo).
2. ``filter_function_coherence`` - the analytic overlap of the OU power spectral
   density with a pulse-sequence filter function, used as an independent
   cross-check of the trajectory result.

Critically, ``tau_c`` is the *bath correlation time* of this process. It is an
UNKNOWN forecast parameter (``measured_in_this_system = false``). A simple
Markovian Lindblad dephasing term alone does NOT determine ``tau_c`` - that is
exactly why a colored-noise treatment is implemented. ``tau_c`` is kept distinct
from T1, T2 and T2* throughout.

The OU process x(t):
    <x> = 0,  <x(t) x(t')> = sigma^2 exp(-|t - t'|/tau_c)
    PSD S(omega) = 2 sigma^2 tau_c / (1 + (omega tau_c)^2)   (one-sided in omega)
Exact update over a step dt:
    x_{n+1} = x_n e^{-dt/tau_c} + sqrt(sigma^2 (1 - e^{-2 dt/tau_c})) * xi_n
with xi_n ~ N(0, 1) i.i.d.
"""
from __future__ import annotations

import math
from typing import Callable

import numpy as np


def ou_trajectories(
    *,
    tau_c_s: float,
    sigma_rad_s: float,
    t_grid_s: np.ndarray,
    n_traj: int,
    seed: int,
) -> np.ndarray:
    """Generate ``n_traj`` fixed-seed OU realisations on ``t_grid_s``.

    Returns array of shape (n_traj, len(t_grid_s)) with units rad/s (a
    fluctuating angular-frequency detuning delta(t) coupling as delta(t) * Sz).
    """
    if tau_c_s <= 0:
        raise ValueError("tau_c_s must be > 0")
    if sigma_rad_s < 0:
        raise ValueError("sigma_rad_s must be >= 0")
    t = np.asarray(t_grid_s, dtype=float)
    if t.ndim != 1 or t.size < 1:
        raise ValueError("t_grid_s must be a 1D non-empty array")
    rng = np.random.default_rng(seed)
    n = t.size
    x = np.empty((n_traj, n), dtype=float)
    # Stationary initial condition: x0 ~ N(0, sigma^2).
    x[:, 0] = rng.standard_normal(n_traj) * sigma_rad_s
    for k in range(1, n):
        dt = t[k] - t[k - 1]
        a = math.exp(-dt / tau_c_s)
        b = sigma_rad_s * math.sqrt(max(0.0, 1.0 - a * a))
        x[:, k] = a * x[:, k - 1] + b * rng.standard_normal(n_traj)
    return x


def sequence_modulation(sequence: str, t_grid_s: np.ndarray, total_time_s: float) -> np.ndarray:
    """Return the +/-1 dephasing modulation s(t) for a pulse sequence.

    The accumulated dephasing phase is phi = integral s(t) delta(t) dt. pi pulses
    flip the sign of s(t). Supported: 'ramsey' (no pi pulse), 'hahn' (one pi at
    T/2), 'xy8' (8 equally spaced pi pulses).
    """
    t = np.asarray(t_grid_s, dtype=float)
    s = np.ones_like(t)
    if sequence == "ramsey":
        flip_times = []
    elif sequence == "hahn":
        flip_times = [0.5 * total_time_s]
    elif sequence == "xy8":
        n_pi = 8
        flip_times = [total_time_s * (i + 0.5) / n_pi for i in range(n_pi)]
    else:
        raise ValueError(f"unknown sequence: {sequence!r}")
    sign = 1.0
    fi = 0
    for k in range(t.size):
        while fi < len(flip_times) and t[k] >= flip_times[fi]:
            sign = -sign
            fi += 1
        s[k] = sign
    return s


def coherence_from_trajectories(
    *,
    sequence: str,
    tau_c_s: float,
    sigma_rad_s: float,
    total_time_s: float,
    n_time: int,
    n_traj: int,
    seed: int,
) -> float:
    """Monte-Carlo coherence |<exp(i phi)>| for a sequence at one total time.

    Pure-dephasing colored-noise model: phi = integral s(t) delta(t) dt, with
    delta(t) an OU trajectory. Coherence W = |mean_traj exp(i phi)|, in [0, 1].
    """
    t = np.linspace(0.0, total_time_s, n_time)
    traj = ou_trajectories(
        tau_c_s=tau_c_s, sigma_rad_s=sigma_rad_s, t_grid_s=t,
        n_traj=n_traj, seed=seed,
    )
    s = sequence_modulation(sequence, t, total_time_s)
    integrand = traj * s[None, :]
    phi = np.trapezoid(integrand, t, axis=1)  # rad, shape (n_traj,)
    w = np.abs(np.mean(np.exp(1j * phi)))
    return float(w)


#: Working-set target for the filter-function omega blocking, in bytes. Sized
#: to stay in cache rather than to bound total memory; it changes runtime only,
#: never a returned value.
_FF_BLOCK_BYTES = 8 << 20


def _filter_function(sequence: str, omega: np.ndarray, total_time_s: float) -> np.ndarray:
    """Pulse-sequence filter function F(omega) for total evolution time T.

    Standard forms (see e.g. Cywinski et al. PRB 2008). Dimensionless.
    Ramsey:  |sin(wT/2)|^2 ... expressed via the standard y(omega) = sinc form.
    Here we use the modulation-function approach: F(omega) = |int_0^T s(t)
    e^{i omega t} dt|^2 * omega^2 (so that chi = (1/pi) int S(omega) F/omega^2).
    Computed numerically from s(t) for exact consistency with the MC modulation.
    """
    T = total_time_s
    n = 4000
    t = np.linspace(0.0, T, n)
    s = sequence_modulation(sequence, t, T)
    # y(omega) = int_0^T s(t) e^{i omega t} dt
    # F(omega)/omega^2 = |y(omega)|^2  (so chi = (1/pi) int S(omega) |y|^2 domega)
    #
    # Evaluated in blocks of omega. The whole-array form materialised
    # exp(1j*outer(omega, t)) -- 4000 x 4000 complex128, 256 MB -- and then a
    # second array of the same size for the product, per call, 69 calls per
    # run. It was the single largest cost in the canonical regeneration.
    #
    # The rows of that array are INDEPENDENT: trapezoid along axis=1 touches
    # one omega at a time, so blocking changes nothing about the arithmetic
    # any row sees, only how much of it is resident at once. The quadrature,
    # the grid, the summation order within a row and therefore every output
    # bit are unchanged -- test_filter_function_blocking asserts that against
    # the whole-array form.
    y = np.empty(omega.shape[0], dtype=complex)
    block = max(1, _FF_BLOCK_BYTES // (n * 16))
    for i in range(0, omega.shape[0], block):
        chunk = omega[i:i + block]
        phase = np.exp(1j * np.outer(chunk, t))
        phase *= s[None, :]              # in place: no second full temporary
        y[i:i + block] = np.trapezoid(phase, t, axis=1)
    return np.abs(y) ** 2  # this is |y(omega)|^2 == F/omega^2


def filter_function_coherence(
    *,
    sequence: str,
    tau_c_s: float,
    sigma_rad_s: float,
    total_time_s: float,
    n_omega: int = 4000,
    omega_max_factor: float = 50.0,
) -> float:
    """Analytic OU coherence via filter-function / PSD overlap (cross-check).

    chi = (1/pi) integral_0^inf S(omega) |y(omega)|^2 domega ,  W = exp(-chi),
    with one-sided OU PSD S(omega) = 2 sigma^2 tau_c / (1 + (omega tau_c)^2)
    and |y(omega)|^2 the (numerically evaluated) modulation overlap.
    """
    if total_time_s <= 0:
        return 1.0
    omega_max = omega_max_factor / tau_c_s
    omega = np.linspace(1e-6 / tau_c_s, omega_max, n_omega)
    S = 2.0 * sigma_rad_s ** 2 * tau_c_s / (1.0 + (omega * tau_c_s) ** 2)
    yf2 = _filter_function(sequence, omega, total_time_s)
    chi = (1.0 / math.pi) * np.trapezoid(S * yf2, omega)
    return float(math.exp(-max(0.0, chi)))
