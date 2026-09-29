"""Surface coverage (1D along sample, and 2D radial map) coupled to gas flux and
local surface temperature.

  d(theta_i)/dt = stick_i flux_i (1 - theta_total)
                  - nu_i exp(-E_des_i/(k_B T)) theta_i
                  - purge_i theta_i - cryotrap_i theta_i

Flux from gas density via kinetic theory: flux_i = 0.25 n_i vbar_i,
vbar_i = sqrt(8 k_B T_gas / (pi m_i)). Desorption uses local surface T.

MODEL-ONLY / FORECAST-ONLY.
"""
from __future__ import annotations
import math
import numpy as np
from dataclasses import dataclass
from scipy.integrate import solve_ivp

from .numerics import (require_integrated, resolution_class,
                       resolution_classes)
from .units import K_B, AMU, require_positive, require_nonnegative, require_fraction


@dataclass
class CoverageSpec:
    name: str
    mass_amu: float
    sticking: float            # [0..1]
    nu_1_s: float              # attempt frequency [1/s]
    E_des_J: float             # desorption energy [J]
    cryotrap_1_s: float        # cryotrapping removal rate [1/s]

    def validate(self):
        require_positive(f"{self.name}.mass_amu", self.mass_amu)
        require_fraction(f"{self.name}.sticking", self.sticking)
        require_positive(f"{self.name}.nu_1_s", self.nu_1_s)
        require_positive(f"{self.name}.E_des_J", self.E_des_J)
        require_nonnegative(f"{self.name}.cryotrap_1_s", self.cryotrap_1_s)
        return self


def _eV(x):
    return x * 1.602176634e-19


def default_coverage_specs():
    # Forecast/ASSUMED desorption energetics (physisorption-scale).
    return [
        CoverageSpec("CH4", 16.0, sticking=0.5, nu_1_s=1.0e13, E_des_J=_eV(0.16), cryotrap_1_s=5.0),
        CoverageSpec("H2",  2.0,  sticking=0.3, nu_1_s=1.0e13, E_des_J=_eV(0.05), cryotrap_1_s=1.0),
        CoverageSpec("He3", 3.0,  sticking=0.1, nu_1_s=1.0e12, E_des_J=_eV(0.01), cryotrap_1_s=0.2),
        CoverageSpec("He4", 4.0,  sticking=0.1, nu_1_s=1.0e12, E_des_J=_eV(0.012), cryotrap_1_s=0.2),
    ]


#: Absolute tolerance of the coverage integration, and therefore the smallest
#: fractional coverage this model can distinguish from zero. Declared as a
#: constant rather than written inline at the solve so that the number in the
#: file and the number the callers classify against cannot drift apart.
COVERAGE_ATOL = 1e-12


class CoverageSolve(tuple):
    """The coverage solve's numbers, and what it can resolve.

    A tuple subclass so that every ``out, t = evolve_coverage(...)`` call site
    keeps working unchanged, while the resolution rides along as attributes.
    A solve that knows its own floor should not be able to hand over only the
    digits: that is how a coverage of 0.0 meaning "absent by design" and one
    meaning "below 1e-12" end up written identically into the same column.
    """

    def __new__(cls, *items, floor, raw, trivially_zero):
        self = super().__new__(cls, items)
        self.resolution_floor = floor
        self.raw = raw                      # species -> unclipped theta(t)
        self.trivially_zero = trivially_zero  # species -> bool
        return self

    def resolution_profile(self, name):
        return resolution_classes(
            self.raw[name], self.resolution_floor,
            trivially_zero=bool(self.trivially_zero.get(name, False)),
            low=0.0, high=1.0)

    def resolution_final(self, name):
        return resolution_class(
            float(self.raw[name][-1]), self.resolution_floor,
            trivially_zero=bool(self.trivially_zero.get(name, False)),
            low=0.0, high=1.0)


def kinetic_flux(n_density_m3, T_gas_K, mass_amu):
    vbar = math.sqrt(8.0 * K_B * T_gas_K / (math.pi * mass_amu * AMU))
    return 0.25 * n_density_m3 * vbar  # [1/m^2/s]


def evolve_coverage(specs, fluxes, T_surface_K, t_end, theta0=None, purge_1_s=0.0,
                    n_eval=40, T_gas_K=300.0, sites_m2=1.0e19):
    """Evolve fractional coverages for one phase.

    fluxes: dict species -> incident molecular flux [1/m^2/s].
    Returns dict species -> theta(t) (fraction), and time array."""
    for s in specs:
        s.validate()
    names = [s.name for s in specs]
    if theta0 is None:
        theta0 = np.zeros(len(specs))
    else:
        theta0 = np.array([theta0.get(n, 0.0) for n in names], dtype=float)

    def rhs(t, th):
        th = np.clip(th, 0.0, 1.0)
        th_tot = min(np.sum(th), 1.0)
        d = np.zeros_like(th)
        for i, s in enumerate(specs):
            f = fluxes.get(s.name, 0.0)
            ads = s.sticking * (f / sites_m2) * (1.0 - th_tot)
            des = s.nu_1_s * math.exp(-s.E_des_J / (K_B * max(T_surface_K, 1e-6))) * th[i]
            rem = (purge_1_s + s.cryotrap_1_s) * th[i]
            d[i] = ads - des - rem
        return d

    t_eval = np.linspace(0.0, t_end, n_eval)
    so = solve_ivp(rhs, (0.0, t_end), theta0, method="BDF", t_eval=t_eval,
                   rtol=1e-7, atol=COVERAGE_ATOL, max_step=t_end / 20.0)
    require_integrated(so, "surface coverage")
    out = {names[i]: np.clip(so.y[i], 0.0, 1.0) for i in range(len(names))}
    raw = {names[i]: np.array(so.y[i], dtype=float, copy=True)
           for i in range(len(names))}
    # Zero by the model, not by the tolerance: nothing lands on this surface
    # and nothing was on it to begin with, so the coverage is identically zero
    # whatever atol says. He3 and He4 in Mode C are this case, and reading it
    # off the magnitude instead would file a species that is absent by design
    # as one the solve merely could not see.
    trivially_zero = {
        names[i]: bool(not fluxes.get(names[i], 0.0) and theta0[i] == 0.0)
        for i in range(len(names))}
    return CoverageSolve(out, t_eval, floor=COVERAGE_ATOL, raw=raw,
                         trivially_zero=trivially_zero)


def surface_coverage_1d(gas_sample_densities, T_surface_K, t_end, mode="B",
                        specs=None, theta0=None, purge_1_s=0.0):
    """1D-along-surface coverage using sample-region gas densities as flux source.

    gas_sample_densities: dict species -> density [1/m^3] at the sample region."""
    specs = specs or default_coverage_specs()
    fluxes = {s.name: kinetic_flux(gas_sample_densities.get(s.name, 0.0), 300.0, s.mass_amu)
              for s in specs}
    if mode == "C":
        # purge/recovery: zero incoming process flux; strong removal
        fluxes = {k: 0.0 for k in fluxes}
    solve = evolve_coverage(specs, fluxes, T_surface_K, t_end, theta0=theta0,
                            purge_1_s=purge_1_s)
    out, t = solve
    return CoverageSolve(out, t, specs, floor=solve.resolution_floor,
                         raw=solve.raw, trivially_zero=solve.trivially_zero)


def surface_coverage_2d(gas_radial_flux, T_surface_radial_K, t_end, specs=None,
                        n_r=40, theta0=None, purge_1_s=0.0):
    """Radial coverage map theta_i(r) on the sample surface.

    gas_radial_flux: dict species -> array over r [1/m^2/s].
    T_surface_radial_K: array over r [K]. Returns dict species -> theta(r)."""
    specs = specs or default_coverage_specs()
    nr = len(T_surface_radial_K)
    result = {s.name: np.zeros(nr) for s in specs}
    for ir in range(nr):
        fluxes = {s.name: float(gas_radial_flux.get(s.name, np.zeros(nr))[ir]) for s in specs}
        out, _ = evolve_coverage(specs, fluxes, float(T_surface_radial_K[ir]), t_end,
                                 theta0=theta0, purge_1_s=purge_1_s, n_eval=8)
        for s in specs:
            result[s.name][ir] = out[s.name][-1]
    return result, specs
