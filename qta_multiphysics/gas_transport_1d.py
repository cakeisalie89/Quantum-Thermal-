"""Gas / contamination transport (1D, non-lumped) along the path
inlet -> sample -> cryobaffle -> pump/sink.

  dn_i/dt = D_eff_i d^2 n_i/dx^2 - v_eff_i dn_i/dx - S_i(x) n_i + source_i(x,t)

Upwind advection + central diffusion (finite volume), method-of-lines, BDF.
Species: CH4, H2, He3, He4. Mode B = inlet source on; Mode C = source off + purge
(enhanced sink); Mode D = residual check.

MODEL-ONLY / FORECAST-ONLY.
"""
from __future__ import annotations
import numpy as np
from dataclasses import dataclass, field
from scipy.integrate import solve_ivp

from .grids import Grid1D
from .units import require_positive, require_nonnegative


@dataclass
class SpeciesSpec:
    name: str
    D_eff_m2_s: float          # effective axial diffusion [m^2/s]
    v_eff_m_s: float           # effective drift toward pump [m/s]
    wall_sink_1_s: float       # wall sticking loss rate [1/s]
    cryobaffle_capture: float  # capture probability at baffle region [0..1]
    pump_sink_1_s: float       # pump removal rate near sink [1/s]
    inlet_source_m3_s: float   # Mode B inlet volumetric source [1/m^3/s scaled]

    def validate(self):
        require_positive(f"{self.name}.D_eff_m2_s", self.D_eff_m2_s)
        require_nonnegative(f"{self.name}.v_eff_m_s", self.v_eff_m_s)
        require_nonnegative(f"{self.name}.wall_sink_1_s", self.wall_sink_1_s)
        require_nonnegative(f"{self.name}.cryobaffle_capture", self.cryobaffle_capture)
        require_nonnegative(f"{self.name}.pump_sink_1_s", self.pump_sink_1_s)
        require_nonnegative(f"{self.name}.inlet_source_m3_s", self.inlet_source_m3_s)
        return self


def default_species():
    # Forecast/ASSUMED transport coefficients (molecular-flow surrogates).
    return [
        SpeciesSpec("CH4", D_eff_m2_s=2.0e-2, v_eff_m_s=5.0e-2, wall_sink_1_s=2.0,
                    cryobaffle_capture=0.9, pump_sink_1_s=50.0, inlet_source_m3_s=1.0e18),
        SpeciesSpec("H2",  D_eff_m2_s=8.0e-2, v_eff_m_s=8.0e-2, wall_sink_1_s=0.2,
                    cryobaffle_capture=0.3, pump_sink_1_s=80.0, inlet_source_m3_s=2.0e17),
        SpeciesSpec("He3", D_eff_m2_s=1.0e-1, v_eff_m_s=1.0e-1, wall_sink_1_s=0.02,
                    cryobaffle_capture=0.05, pump_sink_1_s=60.0, inlet_source_m3_s=0.0),
        SpeciesSpec("He4", D_eff_m2_s=1.0e-1, v_eff_m_s=1.0e-1, wall_sink_1_s=0.02,
                    cryobaffle_capture=0.05, pump_sink_1_s=60.0, inlet_source_m3_s=0.0),
    ]


class GasTransport1DResult:
    def __init__(self, grid, t, sols, specs, regions):
        self.grid = grid
        self.t = t
        self.sols = sols      # dict species -> (n_cells, Nt)
        self.specs = specs
        self.regions = regions  # dict with index ranges

    def profile_final(self, name):
        return self.sols[name][:, -1]

    def sample_region_density(self, name):
        lo, hi = self.regions["sample"]
        return float(np.mean(self.sols[name][lo:hi, -1]))

    def max_density(self, name):
        return float(np.max(self.sols[name]))


def solve_gas_transport_1d(specs=None, n=120, line_length_m=0.5, t_end=2.0,
                           mode="B", rtol=1e-6, atol=1e3, n_eval=60,
                           purge_gain=20.0, n_init=None):
    """Solve 1D transport for all species. mode in {'B','C'}.

    Regions along x in [0,L]: inlet [0,0.15L], sample [0.35L,0.5L],
    cryobaffle [0.6L,0.8L], pump/sink [0.85L,L]."""
    specs = specs or default_species()
    for sp in specs:
        sp.validate()
    grid = Grid1D(line_length_m, n, axis="x")
    x = grid.centers
    dx = grid.dx
    L = line_length_m

    def region_idx(a, b):
        return (int(np.searchsorted(x, a * L)), int(np.searchsorted(x, b * L)))
    regions = {"inlet": region_idx(0.0, 0.15), "sample": region_idx(0.35, 0.50),
               "cryobaffle": region_idx(0.60, 0.80), "pump": region_idx(0.85, 1.0)}

    sols = {}
    for sp in specs:
        S = np.zeros(n)
        ilo, ihi = regions["pump"]
        S[ilo:ihi] += sp.pump_sink_1_s
        clo, chi = regions["cryobaffle"]
        # cryobaffle capture as a local sink rate proportional to capture prob
        S[clo:chi] += sp.cryobaffle_capture * 100.0
        S += sp.wall_sink_1_s
        if mode == "C":
            S = S * purge_gain  # purge/recovery strongly enhances removal

        src = np.zeros(n)
        if mode == "B" and sp.inlet_source_m3_s > 0:
            slo, shi = regions["inlet"]
            src[slo:shi] = sp.inlet_source_m3_s

        D = sp.D_eff_m2_s
        v = sp.v_eff_m_s

        def rhs(t, nvec, D=D, v=v, S=S, src=src):
            nvec = np.clip(nvec, 0.0, None)
            # central diffusion (interior)
            diff = np.zeros_like(nvec)
            diff[1:-1] = D * (nvec[2:] - 2 * nvec[1:-1] + nvec[:-2]) / dx**2
            # upwind advection toward +x (v>0)
            adv = np.zeros_like(nvec)
            adv[1:-1] = -v * (nvec[1:-1] - nvec[:-2]) / dx
            # boundaries: inlet reflective-ish, pump outflow
            diff[0] = D * (nvec[1] - nvec[0]) / dx**2
            adv[0] = 0.0
            diff[-1] = D * (nvec[-2] - nvec[-1]) / dx**2
            adv[-1] = -v * (nvec[-1] - nvec[-2]) / dx
            return diff + adv - S * nvec + src

        if n_init is not None and sp.name in n_init:
            n0 = np.asarray(n_init[sp.name], dtype=float).copy()
        else:
            n0 = np.zeros(n)
        t_eval = np.linspace(0.0, t_end, n_eval)
        so = solve_ivp(rhs, (0.0, t_end), n0, method="BDF", t_eval=t_eval,
                       rtol=rtol, atol=atol, max_step=t_end / 20.0)
        sols[sp.name] = np.clip(so.y, 0.0, None)
    return GasTransport1DResult(grid, t_eval, sols, specs, regions)
