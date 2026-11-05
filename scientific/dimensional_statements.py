"""The dimensional statements each model's governing equations make, checked
against the units the model declares (Assurance H).

Each statement is a product of a model's declared parameters (and named
physical constants) and the dimension that product must have, written from
the equation as the model's own docstring states it. :func:`problems`
checks every statement against the units in the model's ``parameter_schema``
-- so a unit declared wrong (a conductivity written per square metre, a
capacity per kelvin squared) breaks a statement, and says which.

WHAT A PASS MEANS. That the declared units are consistent with these
equations. Not that the equations are the right ones, that the code solves
them, or that the numbers are right: a dimensionally consistent equation
can be wrong, and the code is held to its equations by its invariants and
independent checks, not here. An equation with no statement below is
UNCHECKED, and :func:`coverage` says which models have none.
"""
from __future__ import annotations

from fractions import Fraction as Fr

from .dimensions import parse, residual

#: Constants a statement may name, with their units.
CONSTANTS = {"k_B": "J K^-1"}

#: model_id -> ((statement, product, expected unit), ...)
STATEMENTS: dict = {
    "thermal.slab_transient": (
        # rho c dT/dt = k d2T/dx2 + q: the storage and conduction terms
        # agree (T cancels; the ratio is an inverse Fourier number) ...
        ("storage balances conduction",
         {"rho_c_J_m3_K": 1, "L_m": 2, "k_W_m_K": -1, "t_end_s": -1}, "1"),
        # ... and the source has the dimension of either
        ("the source balances conduction",
         {"q_W_m3": 1, "L_m": 2, "k_W_m_K": -1, "T0_K": -1}, "1"),
        # -k dT/dx = h (T - T_inf): an inverse Biot number
        ("conduction balances convection at x = L",
         {"k_W_m_K": 1, "L_m": -1, "h_W_m2_K": -1}, "1"),
        ("the ambient and the initial state are temperatures alike",
         {"T_inf_K": 1, "T0_K": -1}, "1"),
    ),
    "thermal.rc2_network": (
        # C1 dT1/dt = Q - G12 (T1 - T2)
        ("node 1 stores what is delivered",
         {"C1_J_K": 1, "T1_0_K": 1, "t_end_s": -1, "Q_W": -1}, "1"),
        ("node 1's conductance carries a power",
         {"G12_W_K": 1, "T1_0_K": 1, "Q_W": -1}, "1"),
        # C2 dT2/dt = G12 (T1 - T2) - G2a (T2 - Tamb)
        ("node 2 stores what crosses G12",
         {"C2_J_K": 1, "T2_0_K": 1, "t_end_s": -1, "G12_W_K": -1,
          "T1_0_K": -1}, "1"),
        ("both conductances are conductances",
         {"G2a_W_K": 1, "G12_W_K": -1}, "1"),
        ("ambient and the node temperatures are temperatures alike",
         {"Tamb_K": 1, "T2_0_K": -1}, "1"),
    ),
    "surface.langmuir_capture": (
        # n = p / (k_B T)
        ("ideal-gas number density",
         {"pressure_Pa": 1, "k_B": -1, "T_gas_K": -1}, "m^-3"),
        # F = n vbar / 4, vbar = sqrt(8 k_B T / (pi m))
        ("impingement flux",
         {"pressure_Pa": 1, "k_B": Fr(-1, 2), "T_gas_K": Fr(-1, 2),
          "mass_amu": Fr(-1, 2)}, "m^-2 s^-1"),
        # N(t0 + dt) = N_cap - (N_cap - N(t0)) exp(-s F dt / N_cap)
        ("the capture exponent is a pure number",
         {"sticking": 1, "pressure_Pa": 1, "k_B": Fr(-1, 2),
          "T_gas_K": Fr(-1, 2), "mass_amu": Fr(-1, 2), "window_s": 1,
          "capacity_per_m2": -1}, "1"),
        ("coverage is a fraction", {"initial_coverage": 1}, "1"),
    ),
}


def units_of(model) -> dict:
    """The model's declared parameter units, with the named constants."""
    return {**CONSTANTS,
            **{p.name: p.unit for p in model.parameter_schema.parameters}}


def problems(model) -> list:
    """Every statement of ``model`` whose product does not have the
    dimension it must, and every declared unit that does not parse."""
    out = []
    units = units_of(model)
    for name, unit in sorted(units.items()):
        try:
            parse(unit)
        except ValueError as exc:
            out.append(f"{model.model_id}: {name} is declared in {unit!r}, "
                       f"which does not parse: {exc}")
    for title, product, expect in STATEMENTS.get(model.model_id, ()):
        missing = sorted(set(product) - set(units))
        if missing:
            out.append(f"{model.model_id}: '{title}' names {missing}, which "
                       "the model does not declare")
            continue
        got = residual(product, units)
        want = {b: e for b, e in zip(
            ("kg", "m", "s", "A", "K", "mol", "cd"), parse(expect).dims) if e}
        if got != want:
            out.append(f"{model.model_id}: '{title}' has dimension "
                       f"{_show(got)}, not {expect!r}")
    return out


def coverage(models) -> dict:
    """model_id -> how many statements hold it, 0 meaning UNCHECKED."""
    return {m.model_id: len(STATEMENTS.get(m.model_id, ())) for m in models}


def _show(dims: dict) -> str:
    return " ".join(f"{b}^{e}" for b, e in dims.items()) or "1"
