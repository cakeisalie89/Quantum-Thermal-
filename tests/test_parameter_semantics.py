"""§13/§32 regression: mode assignments and dimensional/sign meaning.

§13 -- PARAM_REGISTRY carried stale mode letters from before material
processing moved out of the baseline mode: P_LCVD, P_CH4_work and t_growth
were tagged A although LCVD/methane processing is Mode B, and P_CH4_purge_tgt
and t_purge_min were tagged B although purge/pumpout is Mode C.

§32 -- measured_parameters.json stored dZFS_dT_rad_s_K as +464955.7 rad/s/K
while its own source string cites "dD/dT ~ -74 kHz/K (Acosta 2010)". The
magnitude was right and the sign was dropped, so D(T) = D0 + (dD/dT)(T - T_ref)
predicted the NV zero-field splitting RISING with temperature.

MODEL-ONLY / FORECAST-ONLY. Software verification; not a hardware statement.
"""
import csv
import json
import math
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REGISTRY = list(csv.DictReader(open(ROOT / "parameter_registry.csv", newline="")))


def _modes(name):
    for r in REGISTRY:
        if r["name"] == name:
            return r["modes"]
    raise AssertionError(f"{name} absent from parameter_registry.csv")


# ------------------------------------------------- §13 canonical semantics --
#
# authorities.json :: modes_and_species --
#   A = Baseline, B = C-13 Methane Processing, C = Isolation/Recovery,
#   D = He-3/He-4 NV Sensing.

PROCESSING = ("P_LCVD", "P_CH4_work", "t_growth")
RECOVERY = ("P_CH4_purge_tgt", "t_purge_min")
SENSING = ("gamma_NV", "gamma_He3", "tau_c", "C_contr_10mK", "mp_Edes_He4")


def test_material_processing_parameters_are_mode_b():
    for name in PROCESSING:
        assert "B" in _modes(name), \
            f"{name} is LCVD/C-13 methane processing but modes={_modes(name)!r}"


def test_processing_parameters_are_not_baseline():
    """The specific stale assignment: processing tagged A."""
    for name in PROCESSING:
        assert _modes(name) != "A", f"{name} still tagged Mode A only"


def test_purge_and_recovery_parameters_are_mode_c():
    for name in RECOVERY:
        assert "C" in _modes(name), \
            f"{name} is isolation/purge/recovery but modes={_modes(name)!r}"


def test_sensing_parameters_are_mode_d():
    for name in SENSING:
        assert "D" in _modes(name), \
            f"{name} is He-3/He-4 NV sensing but modes={_modes(name)!r}"


def test_every_mode_letter_is_canonical():
    for r in REGISTRY:
        assert re.fullmatch(r"[ABCD]+", r["modes"] or ""), \
            f"{r['name']} has non-canonical modes={r['modes']!r}"


def test_live_species_never_overlap_between_b_and_d():
    """The real 'B and D never overlap' invariant, at its authority."""
    from qta_multiphysics import mode_sequence_3d as M
    b = set(M.CANONICAL_ACTIVE.get("MODE_B", ()))
    d = set(M.CANONICAL_ACTIVE.get("MODE_D", ()))
    assert b and d, (b, d)
    assert not (b & d), f"Mode B and Mode D share live species {b & d}"


def test_registry_source_matches_qta_full_sim():
    """The CSV is generated; it must not drift from PARAM_REGISTRY."""
    src = (ROOT / "qta_full_sim.py").read_text(encoding="utf-8")
    for name in PROCESSING + RECOVERY:
        m = re.search(rf'\("{name}",[^)]*?,"([ABCD]+)","[^"]*"\)', src)
        assert m, f"{name} not found in PARAM_REGISTRY source"
        assert m.group(1) == _modes(name), \
            f"{name}: source says {m.group(1)!r}, CSV says {_modes(name)!r}"


# ----------------------------------------- §32 dimensional / sign meaning --

MEASURED = json.loads((ROOT / "measured_parameters.json").read_text(encoding="utf-8"))


def test_zfs_temperature_coefficient_matches_its_own_citation():
    e = MEASURED["dZFS_dT_rad_s_K"]
    assert e["unit"] == "rad/s/K"
    cited = re.search(r"(-?\d+(?:\.\d+)?)\s*kHz/K", e["source"])
    assert cited, f"source states no kHz/K value: {e['source']}"
    want_hz_per_k = float(cited.group(1)) * 1e3
    got_hz_per_k = e["value"] / (2 * math.pi)
    assert math.copysign(1, got_hz_per_k) == math.copysign(1, want_hz_per_k), (
        f"stored sign {got_hz_per_k:+.1f} Hz/K contradicts the cited "
        f"{want_hz_per_k:+.1f} Hz/K")
    assert abs(got_hz_per_k - want_hz_per_k) / abs(want_hz_per_k) < 1e-9


def test_zfs_decreases_with_temperature():
    """Physical direction, independent of how the constant is stored."""
    from qta_multiphysics.nv_spin.model import temperature_dependent_ZFS_rad_s
    cold = temperature_dependent_ZFS_rad_s(0.01)
    warm = temperature_dependent_ZFS_rad_s(30.0)
    assert warm < cold, (
        f"D(30 K)={warm:.6e} >= D(10 mK)={cold:.6e}; the NV zero-field "
        "splitting must fall as the crystal warms")


def test_zfs_at_reference_temperature_is_unshifted():
    """Mode-D evaluation sits at T_ref, so the canonical value is untouched."""
    from qta_multiphysics.nv_spin.model import (measured_constants,
                                                temperature_dependent_ZFS_rad_s)
    assert temperature_dependent_ZFS_rad_s(0.01) == measured_constants()["D_ZFS_rad_s"]


def test_angular_and_ordinary_frequency_are_not_conflated():
    """rad/s vs Hz: the 2*pi conversion must be present, not assumed."""
    from qta_multiphysics.nv_spin.model import measured_constants
    c = measured_constants()
    assert abs(c["D_ZFS_rad_s"] - 2 * math.pi * c["D_ZFS_Hz"]) < 1e-6
    assert abs(c["gamma_e_rad_s_T"] - 2 * math.pi * 28.025e9) \
        / c["gamma_e_rad_s_T"] < 1e-12


def test_every_measured_parameter_declares_a_unit():
    for name, e in MEASURED.items():
        if isinstance(e, dict) and "value" in e:
            assert e.get("unit"), f"{name} has a value with no unit"


if __name__ == "__main__":
    ns = dict(globals())
    fails = 0
    for name, fn in sorted(ns.items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:                                # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if fails else 0)
