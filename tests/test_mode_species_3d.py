"""Mode/species safety invariants for the additive 3D layer.

All checks are software enforcement of the canonical QTA mode/species rules
(MODEL-ONLY; the physical enforcement is the hardware interlock table). Zero
PASS; no measured data.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qta_multiphysics.config import default_config
from qta_multiphysics.mesh_3d import Grid3DConfig
from qta_multiphysics.mode_sequence_3d import (
    assert_no_overlap, validate_live_species, validate_mode_map,
    canonical_mode_map, run_mode_sequence_3d, CANONICAL_ACTIVE, RESIDUAL_ONLY,
)

G3 = Grid3DConfig(nx=8, ny=8, nz=10)


def _expect_raises(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ValueError as e:
        return str(e)
    raise AssertionError(f"{fn.__name__}{args} did not raise ValueError")


# 1. Mode B and Mode D cannot overlap
def test_mode_b_and_mode_d_cannot_overlap():
    msg = _expect_raises(assert_no_overlap, {"MODE_B", "MODE_D"})
    assert "mutually" in msg
    assert_no_overlap({"MODE_B"})          # each alone is fine
    assert_no_overlap({"MODE_D"})
    assert_no_overlap({"MODE_A", "MODE_C"})


# 2. C-13 methane precursor cannot appear as a live Mode D species
def test_c13_methane_never_live_in_mode_d():
    msg = _expect_raises(validate_live_species, "MODE_D", {"He3", "C13_CH4"})
    assert "Mode B species only" in msg
    # the legacy internal alias is equally rejected in Mode D
    _expect_raises(validate_live_species, "MODE_D", {"CH4"})


# 3. He-3 / He-4 cannot appear as live Mode B species
def test_helium_never_live_in_mode_b():
    msg = _expect_raises(validate_live_species, "MODE_B", {"C13_CH4", "He3"})
    assert "Mode D sensing species only" in msg
    _expect_raises(validate_live_species, "MODE_B", {"He4"})


# 4. Mode D cannot begin before Mode C recovery/readiness
def test_mode_d_blocked_before_recovery():
    cfg = default_config()
    cfg.solver.recovery_window_s = 2.0e-6     # far too short to recool
    seq = run_mode_sequence_3d(cfg, G3, n_eval_b=7, n_eval_c=7)
    assert not seq.mode_c_ready
    assert not seq.mode_d_started
    assert seq.status == "BLOCKED_RECOVERY_INCOMPLETE"
    msg = _expect_raises(seq.start_mode_d)
    assert "cannot begin before Mode C" in msg


def test_mode_d_starts_after_recovery():
    cfg = default_config()
    seq = run_mode_sequence_3d(cfg, G3, n_eval_b=7, n_eval_c=25)
    assert seq.mode_c_ready, "default recovery window should reach readiness"
    assert seq.recovery_time_s is not None and seq.recovery_time_s > 0.0
    assert seq.mode_d_started
    assert seq.mode_d_live_species == frozenset({"He3", "He4"})
    assert seq.status == "MODE_D_STARTED_AFTER_RECOVERY"
    rows = seq.timeline_rows()
    assert rows[0]["phase"] == "MODE_B"
    assert any(r["phase"] == "MODE_D" for r in rows)
    assert rows[-1]["phase"] in ("MODE_D", "MODE_D_HOLD")  # hold rows follow D start
    joined = " ".join(str(v) for r in rows for v in r.values())
    assert "PASS" not in joined
    assert "NOT_MEASURED_IN_THIS_SYSTEM" in joined


# 5. obsolete mode maps remain rejected
def test_obsolete_mode_maps_rejected():
    good = canonical_mode_map()
    validate_mode_map(good)                    # canonical map is accepted
    bad_concurrent = dict(good); bad_concurrent["SIMULTANEOUS_B_D"] = True
    assert "concurrency" in _expect_raises(validate_mode_map, bad_concurrent)
    bad_generic = canonical_mode_map(); bad_generic["MODE_B"] = {"CH4"}
    assert "legacy internal shorthand" in _expect_raises(validate_mode_map, bad_generic)
    bad_he_in_b = canonical_mode_map(); bad_he_in_b["MODE_B"] = {"C13_CH4", "He3"}
    _expect_raises(validate_mode_map, bad_he_in_b)
    bad_c13_in_d = canonical_mode_map(); bad_c13_in_d["MODE_D"] = {"He3", "He4", "C13_CH4"}
    _expect_raises(validate_mode_map, bad_c13_in_d)
    bad_unknown = canonical_mode_map(); bad_unknown["MODE_E_LIVE_GROWTH_SENSE"] = {"He3"}
    _expect_raises(validate_mode_map, bad_unknown)
    missing = canonical_mode_map(); missing.pop("MODE_C")
    _expect_raises(validate_mode_map, missing)


# 6. H2 remains residual/background only, never an active canonical species
def test_h2_residual_only():
    assert "H2" in RESIDUAL_ONLY
    for mode in CANONICAL_ACTIVE:
        assert "H2" not in CANONICAL_ACTIVE[mode]
        msg = _expect_raises(validate_live_species, mode, {"H2"})
        assert "residual/background" in msg


ALL_TESTS = [
    test_mode_b_and_mode_d_cannot_overlap,
    test_c13_methane_never_live_in_mode_d,
    test_helium_never_live_in_mode_b,
    test_mode_d_blocked_before_recovery,
    test_mode_d_starts_after_recovery,
    test_obsolete_mode_maps_rejected,
    test_h2_residual_only,
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
