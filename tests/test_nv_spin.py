"""Physics + numerical test suite for qta_multiphysics.nv_spin.

Runnable two ways:
    python3 tests/test_nv_spin.py        # standalone, prints PASS/FAIL summary
    python3 -m pytest tests/test_nv_spin.py
All tests are forecast/model-only sanity checks; none assert a measured result.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

# Make the repository importable when run standalone.
_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import qutip as qt
from qta_multiphysics.nv_spin import (
    spin1_operators, nv_ground_state_hamiltonian, HyperfineCoupling,
    run_nv_spin_layer, ModeReadinessError, ci_config,
)
from qta_multiphysics.nv_spin import sequences as seq
from qta_multiphysics.nv_spin import noise as nz


def test_hamiltonian_hermiticity():
    for B in (0.0, 5e-3, 2e-2):
        for E in (0.0, 2 * math.pi * 1e5):
            H = nv_ground_state_hamiltonian(B_T=B, theta_B_rad=0.3, T_K=0.01,
                                            E_strain_rad_s=E)
            assert H.isherm, "H not Hermitian"
            M = H.full()
            assert np.max(np.abs(M - M.conj().T)) < 1e-6
    # with hyperfine (14N: I=1)
    hf = HyperfineCoupling(nuclear_spin=1.0, A_parallel_rad_s=2 * math.pi * -2.16e6,
                           A_perp_rad_s=2 * math.pi * -2.7e6, label="14N")
    H = nv_ground_state_hamiltonian(B_T=5e-3, T_K=0.01, hyperfine=hf)
    M = H.full()
    assert np.max(np.abs(M - M.conj().T)) < 1e-6


def test_s1_operator_algebra():
    o = spin1_operators()
    Sx, Sy, Sz, I = o["Sx"], o["Sy"], o["Sz"], o["I"]
    comm = (Sx * Sy - Sy * Sx).full()
    assert np.allclose(comm, 1j * Sz.full(), atol=1e-9), "[Sx,Sy] != i Sz"
    s2 = (Sx * Sx + Sy * Sy + Sz * Sz).full()
    assert np.allclose(s2, 2.0 * I.full(), atol=1e-9), "S^2 != S(S+1) I = 2 I"


def test_trace_and_positivity_preserved():
    H = nv_ground_state_hamiltonian(B_T=5e-3, T_K=0.01)
    rho0 = qt.ket2dm((qt.basis(3, 0) + qt.basis(3, 1)).unit())
    t = np.linspace(0.0, 100e-9, 60)
    r = seq.markovian_evolution(H=H, rho0=rho0, times_s=t, T1_s=1e-2, T2_pure_s=5e-4)
    assert np.max(np.abs(r["trace"] - 1.0)) < 1e-9, "trace not preserved"
    assert np.min(r["min_eig"]) > -1e-9, "density matrix lost positivity"


def test_zero_noise_unitary_limit():
    # No colored noise -> coherence is exactly 1 for every sequence.
    for s in ("ramsey", "hahn", "xy8"):
        w = nz.coherence_from_trajectories(
            sequence=s, tau_c_s=4e-3, sigma_rad_s=0.0, total_time_s=1e-3,
            n_time=64, n_traj=32, seed=42)
        assert abs(w - 1.0) < 1e-12, f"{s}: zero-noise coherence != 1"
    # No collapse ops -> pure state stays pure (purity 1), trace 1.
    H = nv_ground_state_hamiltonian(B_T=5e-3, T_K=0.01)
    rho0 = qt.ket2dm((qt.basis(3, 0) + qt.basis(3, 1)).unit())
    t = np.linspace(0.0, 100e-9, 40)
    r = seq.markovian_evolution(H=H, rho0=rho0, times_s=t)
    # No collapse ops -> unitary: purity conserved. The density-matrix (mesolve)
    # integration of the stiff GHz Liouvillian carries ~1e-6 ODE error over this
    # window (trace drift is exactly 0; sesolve preserves purity to ~1e-15).
    assert np.max(np.abs(r["purity"] - 1.0)) < 1e-5, "unitary evolution lost purity"
    assert np.max(np.abs(r["trace"] - 1.0)) < 1e-9


def test_lindblad_dephasing_exponential_limit():
    # Pure dephasing only, zero Hamiltonian -> |rho_{+1,0}| decays exp at rate
    # (1/2)*gphi*(dm)^2 = gphi/2 for dm=1, with gphi = 1/T2_pure.
    o = spin1_operators()
    Hk = 0.0 * o["Sz"]
    rho0 = qt.ket2dm((qt.basis(3, 0) + qt.basis(3, 1)).unit())
    T2p = 4e-4
    t = np.linspace(0.0, 6e-4, 80)
    r = seq.markovian_evolution(H=Hk, rho0=rho0, times_s=t, T2_pure_s=T2p,
                                coh_levels=(0, 1))
    c = r["coherence"]
    c0 = c[0]
    mask = c > c0 * 0.05
    slope = np.polyfit(t[mask], np.log(c[mask]), 1)[0]
    rate_fit = -slope
    rate_analytic = 0.5 * (1.0 / T2p) * (1 - 0) ** 2
    assert abs(rate_fit - rate_analytic) / rate_analytic < 0.02, \
        f"dephasing rate {rate_fit:.4g} != analytic {rate_analytic:.4g}"


def test_T1_population_relaxation():
    # T1 only, zero Hamiltonian, start in ms=+1 -> pop decays exp(-t/T1).
    o = spin1_operators()
    Hk = 0.0 * o["Sz"]
    rho0 = qt.ket2dm(qt.basis(3, 0))  # ms=+1
    T1 = 3e-3
    t = np.linspace(0.0, 6e-3, 80)
    r = seq.markovian_evolution(H=Hk, rho0=rho0, times_s=t, T1_s=T1, pop_level=0)
    p = r["population"]
    mask = p > 0.05
    slope = np.polyfit(t[mask], np.log(p[mask]), 1)[0]
    rate_fit = -slope
    assert abs(rate_fit - 1.0 / T1) / (1.0 / T1) < 0.02, \
        f"T1 rate {rate_fit:.4g} != 1/T1 {1.0/T1:.4g}"


def test_trajectory_convergence_to_filter_function():
    # MC coherence converges to the analytic filter-function value as n_traj grows.
    kw = dict(sequence="hahn", tau_c_s=4e-3, sigma_rad_s=2 * math.pi * 5e3,
              total_time_s=3e-3)
    w_ff = nz.filter_function_coherence(**kw)
    w_small = nz.coherence_from_trajectories(n_time=200, n_traj=100, seed=42, **kw)
    w_large = nz.coherence_from_trajectories(n_time=200, n_traj=6000, seed=42, **kw)
    assert abs(w_large - w_ff) <= abs(w_small - w_ff) + 0.02, \
        "MC did not converge toward filter-function reference"
    assert abs(w_large - w_ff) < 0.05, \
        f"converged MC {w_large:.4g} far from analytic {w_ff:.4g}"


def test_time_step_convergence():
    # Refining the inner time grid changes the (large-N) coherence by < tol.
    kw = dict(sequence="xy8", tau_c_s=4e-3, sigma_rad_s=2 * math.pi * 5e3,
              total_time_s=5e-3, n_traj=3000, seed=42)
    w_coarse = nz.coherence_from_trajectories(n_time=100, **kw)
    w_fine = nz.coherence_from_trajectories(n_time=200, **kw)
    assert abs(w_coarse - w_fine) < 0.03, \
        f"time-step not converged: {w_coarse:.4g} vs {w_fine:.4g}"


def test_fixed_seed_reproducibility():
    kw = dict(sequence="ramsey", tau_c_s=4e-3, sigma_rad_s=2 * math.pi * 5e3,
              total_time_s=1e-3, n_time=128, n_traj=200, seed=7)
    a = nz.coherence_from_trajectories(**kw)
    b = nz.coherence_from_trajectories(**kw)
    assert a == b, "same seed produced different coherence"
    # full layer reproducible
    cfg = ci_config(seed=11)
    r1 = run_nv_spin_layer(mode_c_ready=True, cfg=cfg, write_outputs=False)
    r2 = run_nv_spin_layer(mode_c_ready=True, cfg=cfg, write_outputs=False)
    for s in ("ramsey", "hahn", "xy8"):
        assert np.array_equal(r1["curves"][s]["W_mc"], r2["curves"][s]["W_mc"]), \
            f"{s} coherence not reproducible across runs"


def test_mode_c_readiness_enforced():
    cfg = ci_config()
    raised = False
    try:
        run_nv_spin_layer(mode_c_ready=False, cfg=cfg, write_outputs=False)
    except ModeReadinessError:
        raised = True
    assert raised, "Mode D spin layer ran before Mode C readiness"
    # ready -> runs
    r = run_nv_spin_layer(mode_c_ready=True, cfg=cfg, write_outputs=False)
    assert "summary" in r


def test_tau_c_T1_T2_T2star_distinct():
    cfg = ci_config()
    r = run_nv_spin_layer(mode_c_ready=True, cfg=cfg, write_outputs=False)
    dq = r["summary"]["distinct_quantities"]
    assert dq["tau_c_s"]["value"] == cfg.tau_c_s
    assert dq["T1_s"]["value"] == cfg.T1_s
    assert dq["T2_pure_s"]["value"] == cfg.T2_pure_s
    # the three are not aliased
    assert len({dq["tau_c_s"]["value"], dq["T1_s"]["value"], dq["T2_pure_s"]["value"]}) == 3
    obs = r["summary"]["observables"]
    assert set(obs.keys()) == {"T2star_s", "T2_echo_s", "T2_xy8_s"}
    # tau_c must be tagged UNKNOWN, never measured
    assert dq["tau_c_s"]["status"].upper() == "UNKNOWN"


ALL_TESTS = [
    test_hamiltonian_hermiticity,
    test_s1_operator_algebra,
    test_trace_and_positivity_preserved,
    test_zero_noise_unitary_limit,
    test_lindblad_dephasing_exponential_limit,
    test_T1_population_relaxation,
    test_trajectory_convergence_to_filter_function,
    test_time_step_convergence,
    test_fixed_seed_reproducibility,
    test_mode_c_readiness_enforced,
    test_tau_c_T1_T2_T2star_distinct,
]


def main() -> int:
    passed = failed = 0
    for t in ALL_TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(ALL_TESTS)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
