"""Mode A/B/C/D sequencing for the additive 3D transient layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Runs the canonical mode order on the 3D thermal core with real state hand-off,
exactly mirroring the 1D/2D ``coupled_mode_solver``:

  Mode B (C-13 methane LCVD processing, source ON)
    -> Mode C (isolation / purge / thermal recovery: source OFF, field
       initialised from the Mode B final state; readiness = NV probe recools
       below ``SolverConfig.mode_d_temp_threshold_K``)
    -> Mode D (He-3 / He-4 sensing) -- constructed ONLY after Mode C readiness.

Canonical species rules (hardware-interlocked in the design registry; enforced
here in software and covered by tests):

* Mode B active species: the C-13 methane precursor only (``C13_CH4``).
* Mode D active species: ``He3`` and ``He4`` only.
* ``H2`` is residual / background / purge species ONLY -- never active.
* Mode B and Mode D can never be active simultaneously.
* Mode D can never begin before Mode C readiness.
* The internal gas-transport id ``CH4`` is legacy shorthand for the C-13
  methane precursor; new 3D-facing text uses ``C13_CH4``. Mode maps that
  declare generic ``CH4`` as a canonical species are rejected as obsolete.

Deterministic: no randomness anywhere in this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .config import MultiphysicsConfig
from .mesh_3d import Grid3DConfig
from .thermal_3d_transient import solve_thermal_3d, Thermal3DResult

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

MODES = ("MODE_A", "MODE_B", "MODE_C", "MODE_D")

#: canonical ACTIVE species per mode (frozen; the single source of truth here)
CANONICAL_ACTIVE = {
    "MODE_A": frozenset(),
    "MODE_B": frozenset({"C13_CH4"}),
    "MODE_C": frozenset(),                    # purge: nothing actively admitted
    "MODE_D": frozenset({"He3", "He4"}),
}

#: species allowed ONLY as residual / background / purge inventory
RESIDUAL_ONLY = frozenset({"H2"})

#: legacy internal ids (gas_transport_1d) -> canonical 3D-facing labels.
#: Accepted at runtime for backward compatibility in Mode B only.
LEGACY_ALIASES = {"CH4": "C13_CH4"}


# ---------------------------------------------------------------------------
# species / mode-map validation
# ---------------------------------------------------------------------------
def normalize_species(name: str) -> str:
    return LEGACY_ALIASES.get(name, name)


def assert_no_overlap(active_modes) -> None:
    """Mode B (LCVD growth) and Mode D (NV sensing) can never overlap."""
    s = set(active_modes)
    if {"MODE_B", "MODE_D"} <= s:
        raise ValueError(
            "Mode B (LCVD growth) and Mode D (NV sensing) are mutually "
            "exclusive, hardware-interlocked modes and can never be active "
            "simultaneously (IL-01/IL-02/IL-14).")


def validate_live_species(mode: str, live_species) -> frozenset:
    """Validate the ACTIVE species set for a mode; returns the normalized set.

    Raises ValueError on any canonical-rule violation.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; canonical modes are {MODES}")
    live = frozenset(normalize_species(s) for s in live_species)
    bad_residual = live & RESIDUAL_ONLY
    if bad_residual:
        raise ValueError(
            f"{sorted(bad_residual)} are residual/background species only and "
            f"can never be ACTIVE process species (mode {mode}).")
    allowed = CANONICAL_ACTIVE[mode]
    extra = live - allowed
    if extra:
        if mode == "MODE_D" and "C13_CH4" in extra:
            raise ValueError(
                "the C-13 methane precursor is a Mode B species only and can "
                "never be live during Mode D sensing (must be off/below "
                "threshold before Mode D).")
        if mode == "MODE_B" and ({"He3", "He4"} & extra):
            raise ValueError(
                "He-3 / He-4 are Mode D sensing species only and can never be "
                "dosed or live during Mode B processing (gate A4).")
        raise ValueError(
            f"species {sorted(extra)} are not canonical ACTIVE species for "
            f"{mode}; allowed: {sorted(allowed) or '(none)'}.")
    return live


def validate_mode_map(mode_map: dict) -> dict:
    """Validate a declared mode map; obsolete/violating maps are rejected.

    A canonical mode map is ``{mode: iterable_of_active_species}`` over exactly
    the four canonical modes. Rejected as obsolete/invalid:
    unknown or missing modes; any concurrency marker (e.g. SIMULTANEOUS_B_D);
    generic ``CH4`` declared as a canonical species; He-3/He-4 in Mode B;
    C-13 methane in Mode D; H2 declared active anywhere.
    """
    if not isinstance(mode_map, dict):
        raise ValueError("mode map must be a dict of {mode: active species}")
    keys = set(mode_map)
    concurrency = [k for k in keys if "SIMULTANEOUS" in str(k).upper()]
    if concurrency:
        raise ValueError(
            f"obsolete mode map: concurrency markers {concurrency} are not "
            "part of the canonical A->B->C->D architecture (B and D never overlap).")
    unknown = keys - set(MODES)
    if unknown:
        raise ValueError(f"obsolete/unknown modes in map: {sorted(unknown)}")
    missing = set(MODES) - keys
    if missing:
        raise ValueError(f"mode map missing canonical modes: {sorted(missing)}")
    out = {}
    for mode in MODES:
        declared = set(mode_map[mode])
        if "CH4" in declared:
            raise ValueError(
                "generic 'CH4' is legacy internal shorthand and cannot be "
                "declared as a canonical species; the Mode B precursor is "
                "'C13_CH4' (C-13 methane).")
        out[mode] = validate_live_species(mode, declared)
    assert_no_overlap([m for m in ("MODE_B", "MODE_D") if out[m]] if
                      (out["MODE_B"] & out["MODE_D"]) else [])
    return out


def canonical_mode_map() -> dict:
    return {m: set(s) for m, s in CANONICAL_ACTIVE.items()}


# ---------------------------------------------------------------------------
# B -> C -> D sequence on the 3D thermal core
# ---------------------------------------------------------------------------
@dataclass
class ModeSequence3DResult:
    tB: Thermal3DResult
    tC: Thermal3DResult
    threshold_K: float
    recovery_time_s: float | None
    mode_c_ready: bool
    mode_d_started: bool = False
    mode_d_live_species: frozenset = frozenset()
    tD_hold: Thermal3DResult | None = None
    channel_meta: tuple = ()
    _active: set = field(default_factory=set)

    @property
    def status(self) -> str:
        if self.mode_d_started:
            return "MODE_D_STARTED_AFTER_RECOVERY"
        return ("MODE_C_READY_MODE_D_NOT_STARTED" if self.mode_c_ready
                else "BLOCKED_RECOVERY_INCOMPLETE")

    def start_mode_d(self, live_species=("He3", "He4")) -> frozenset:
        """Begin Mode D. Raises unless Mode C readiness has been reached."""
        if not self.mode_c_ready:
            raise ValueError(
                "Mode D cannot begin before Mode C recovery/readiness: the NV "
                f"probe has not recooled below {self.threshold_K} K within the "
                "recovery window (BLOCKED_RECOVERY_INCOMPLETE).")
        self._active.add("MODE_D")
        assert_no_overlap(self._active)
        self.mode_d_live_species = validate_live_species("MODE_D", live_species)
        self.mode_d_started = True
        return self.mode_d_live_species

    def timeline_rows(self):
        rows = []
        thr = self.threshold_K
        for k, t in enumerate(self.tB.t):
            rows.append({"phase": "MODE_B", "t_s": f"{t:.9e}",
                         "T_nv_probe_K": f"{self.tB.probe_timeseries_K()[k]:.9e}",
                         "threshold_K": f"{thr:.6e}", "ready": "false",
                         "live_species": "C13_CH4", "label": LABEL})
        tsC = self.tC.probe_timeseries_K()
        for k, t in enumerate(self.tC.t):
            ready = bool(tsC[k] <= thr)
            rows.append({"phase": "MODE_C", "t_s": f"{t:.9e}",
                         "T_nv_probe_K": f"{tsC[k]:.9e}",
                         "threshold_K": f"{thr:.6e}",
                         "ready": "true" if ready else "false",
                         "live_species": "", "label": LABEL})
        rows.append({"phase": "MODE_D",
                     "t_s": f"{self.tC.t[-1]:.9e}",
                     "T_nv_probe_K": f"{tsC[-1]:.9e}",
                     "threshold_K": f"{thr:.6e}",
                     "ready": "true" if self.mode_c_ready else "false",
                     "live_species": ";".join(sorted(self.mode_d_live_species)),
                     "label": f"{self.status} {LABEL}"})
        if self.tD_hold is not None:
            tsD = self.tD_hold.probe_timeseries_K()
            for k, t in enumerate(self.tD_hold.t):
                rows.append({"phase": "MODE_D_HOLD", "t_s": f"{t:.9e}",
                             "T_nv_probe_K": f"{tsD[k]:.9e}",
                             "threshold_K": f"{thr:.6e}",
                             "ready": "true" if tsD[k] <= thr else "false",
                             "live_species": ";".join(sorted(self.mode_d_live_species)),
                             "label": "MODE_D_HOLD_UNDER_SENSING_LOADS " + LABEL})
        return rows


def run_mode_sequence_3d(cfg: MultiphysicsConfig, g3: Grid3DConfig | None = None,
                         n_eval_b: int = 9, n_eval_c: int = 25,
                         threshold_K: float | None = None,
                         start_d_if_ready: bool = True,
                         coupled_channels: bool = True) -> ModeSequence3DResult:
    """Run Mode B -> Mode C (-> Mode D if ready) on the 3D core.

    Deterministic; forecast-only. Mode D is entered only after Mode C
    readiness; the C-13 methane source is structurally OFF (source_scale=0)
    for the whole of Mode C and Mode D.

    ``coupled_channels=True`` drives the bounded auxiliary channels from the
    mode state machine: the shutter/baffle-dependent radiative front-face
    load (REDUCED_ORDER) in every phase, and -- after a successful Mode-D
    start -- a short Mode-D HOLD solve under the sensing loads (microwave
    NV-region map, REDUCED_ORDER; closed-shutter radiative load), giving the
    forecast NV-probe temperature during sensing. All channel magnitudes are
    existing canonical values; nothing is invented.
    """
    from .state_machine_3d import device_state
    from .sources_3d import microwave_nv_region_map_W, radiative_front_flux_W_m2
    from .mesh_3d import StructuredGrid3D

    cfg.validate()
    thr = float(threshold_K if threshold_K is not None
                else cfg.solver.mode_d_temp_threshold_K)
    active: set = set()
    metas = []

    def _front(mode):
        if not coupled_channels:
            return 0.0
        grid = StructuredGrid3D(cfg, g3)
        fx, meta = radiative_front_flux_W_m2(grid, device_state(mode))
        metas.append(meta)
        return fx

    active.add("MODE_B")
    assert_no_overlap(active)
    validate_live_species("MODE_B", {"C13_CH4"})
    tB = solve_thermal_3d(cfg, g3, source_mode="averaged",
                          t_end=cfg.solver.pulse_window_s, n_eval=n_eval_b,
                          extra_front_flux_W_m2=_front("MODE_B"))
    active.discard("MODE_B")           # growth inputs stopped before Mode C

    active.add("MODE_C")
    assert_no_overlap(active)
    validate_live_species("MODE_C", set())
    tC = solve_thermal_3d(cfg, g3, source_mode="averaged", source_scale=0.0,
                          T_init=tB.T[:, -1], t_end=cfg.solver.recovery_window_s,
                          n_eval=n_eval_c,
                          extra_front_flux_W_m2=_front("MODE_C"))
    active.discard("MODE_C")

    tsC = tC.probe_timeseries_K()
    below = np.nonzero(tsC <= thr)[0]
    recovery_time = float(tC.t[below[0]]) if below.size else None
    ready = below.size > 0

    res = ModeSequence3DResult(tB=tB, tC=tC, threshold_K=thr,
                               recovery_time_s=recovery_time,
                               mode_c_ready=ready, channel_meta=tuple(metas),
                               _active=active)
    if start_d_if_ready and ready:
        res.start_mode_d()
        if coupled_channels:
            sD = device_state("MODE_D")
            grid = StructuredGrid3D(cfg, g3)
            Qmw, mmw = microwave_nv_region_map_W(cfg, grid, sD)
            fxD, mfr = radiative_front_flux_W_m2(grid, sD)
            metas.extend([mmw, mfr])
            res.channel_meta = tuple(metas)
            res.tD_hold = solve_thermal_3d(
                cfg, g3, source_mode="averaged", source_scale=0.0,
                T_init=tC.T[:, -1], t_end=cfg.solver.pulse_window_s,
                n_eval=9, extra_volumetric_W=Qmw, extra_front_flux_W_m2=fxD)
    return res
