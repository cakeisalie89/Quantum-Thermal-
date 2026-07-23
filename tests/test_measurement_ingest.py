"""Measurement-ingestion tests (Stage 4). MODEL-ONLY / FORECAST-ONLY.

Required: malformed records, unit mismatch, unknown quantities, hardware
refusal, active-species violations vs valid residual-gas diagnostics,
alignment, residual mathematics, band coverage, determinism, synthetic
labeling, gate immutability, zero PASS.
"""
import copy
import hashlib
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qta_multiphysics.measurement_ingest_3d import (        # noqa: E402
    validate_record, compare_record, ingest_and_compare, comparison_rows,
    QUANTITY_REGISTRY, HARDWARE_REFUSAL)

GOOD = {"measurement_id": "T-1", "quantity": "P_H2_Pa", "value": 1.0e-12,
        "units": "Pa", "uncertainty": {"type": "none"},
        "timestamp": "2026-07-18T00:00:00Z",
        "alignment": {"mode": "MODE_A"}, "data_class": "SYNTHETIC",
        "instrument": {"id": "i", "type": "t",
                       "calibration": {"status": "UNKNOWN",
                                        "reference": "n/a", "date": "n/a"}},
        "provenance": {"origin": "test", "generator": "test", "note": ""}}


def _mut(**kw):
    r = copy.deepcopy(GOOD)
    r.update(kw)
    return r


def test_malformed_records_fail_closed():
    ok, why = validate_record({"quantity": "P_H2_Pa"})
    assert not ok and any("missing required field" in w for w in why)
    ok, why = validate_record(_mut(value="not-a-number"))
    assert not ok and any("not numeric" in w for w in why)
    ok, why = validate_record(_mut(timestamp="yesterday"))
    assert not ok and any("ISO-8601" in w for w in why)
    ok, why = validate_record("just a string")
    assert not ok


def test_unit_mismatch_rejected_no_conversion():
    ok, why = validate_record(_mut(units="mK",
                                   quantity="T_probe_recovery_end_K",
                                   alignment={"mode": "MODE_C"}))
    assert not ok and any("unit mismatch" in w and "fail-closed" in w
                          for w in why)


def test_unknown_quantity_rejected():
    ok, why = validate_record(_mut(quantity="warp_field_strength"))
    assert not ok and any("unknown quantity" in w for w in why)


def test_hardware_class_refused_with_governance_reason():
    ok, why = validate_record(_mut(data_class="HARDWARE"))
    assert not ok and any(HARDWARE_REFUSAL in w for w in why)
    ok, why = validate_record(_mut(data_class="REAL"))
    assert not ok and any("SYNTHETIC only" in w for w in why)


def test_active_species_policy_vs_diagnostics():
    # PROCESS quantity off-policy: Mode-D SNR aligned to MODE_B -> rejected
    ok, why = validate_record(_mut(quantity="mode_D_SNR", value=5.0,
                                   units="dimensionless",
                                   alignment={"mode": "MODE_B"}))
    assert not ok and any("not permitted" in w for w in why)
    # DIAGNOSTIC residual-gas measurements accepted in ANY mode
    for mode in ("MODE_A", "MODE_B", "MODE_C", "MODE_D"):
        for q, u in (("P_CH4_partial_Pa", "Pa"), ("P_H2_Pa", "Pa"),
                     ("P_He_partial_Pa", "Pa")):
            ok, why = validate_record(_mut(quantity=q, units=u,
                                           alignment={"mode": mode}))
            assert ok, (q, mode, why)
    # diagnostics never touch state: comparator emits report rows only
    row = compare_record(_mut(quantity="P_CH4_partial_Pa",
                              alignment={"mode": "MODE_A"}))
    assert row["kind"] == "diagnostic" and "NONE" in row["predicted"]


def test_alignment_validation():
    ok, why = validate_record(_mut(alignment={"mode": "MODE_X"}))
    assert not ok
    ok, why = validate_record(_mut(quantity="panel_CH4_ML",
                                   units="monolayer_fraction",
                                   alignment={"mode": "MODE_C"}))
    assert not ok and any("cycle" in w for w in why)
    ok, why = validate_record(_mut(quantity="panel_CH4_ML",
                                   units="monolayer_fraction",
                                   alignment={"mode": "MODE_C", "cycle": 9}))
    assert not ok


def test_residual_mathematics():
    r = compare_record(_mut(quantity="T_probe_recovery_end_K", units="K",
                            value=1.02e-2,
                            uncertainty={"type": "stddev", "value": 5e-4},
                            alignment={"mode": "MODE_C"}))
    assert abs(float(r["residual"]) - 2.0e-4) < 1e-18
    assert abs(float(r["normalized_residual"]) - 0.4) < 1e-12
    assert r["normalization"] == "stddev"
    r2 = compare_record(_mut(quantity="T_probe_recovery_end_K", units="K",
                             value=1.02e-2, uncertainty={"type": "none"},
                             alignment={"mode": "MODE_C"}))
    assert r2["normalized_residual"] == "null"
    assert r2["normalization"] == "NONE_AVAILABLE"


def test_band_semantics_not_sigma():
    r = compare_record(_mut(quantity="panel_CH4_ML",
                            units="monolayer_fraction", value=2.1e-6,
                            alignment={"mode": "MODE_C", "cycle": 2}))
    p05, p50, p95 = (float(r["band_p05"]), float(r["band_p50"]),
                     float(r["band_p95"]))
    pos = (2.1e-6 - p05) / (p95 - p05)
    assert abs(float(r["band_position"]) - pos) < 2e-6  # %.6e emit
    assert r["inside_band"] == ("true" if 0 <= pos <= 1 else "false")
    assert abs(float(r["band_scaled_residual"])
               - (2.1e-6 - p50) / (p95 - p05)) < 2e-6  # %.6e emit
    assert "NOT a sigma" in r["band_note"]
    assert "sigma" not in r.get("normalization", "")


def test_band_coverage_and_example_roundtrip():
    rep = ingest_and_compare()
    assert rep["ingestion_status"] == "OK"
    assert rep["n_accepted"] == 8 and rep["n_rejected"] == 2
    cov = rep["coverage"]
    assert cov["n_banded"] == 2 and cov["n_inside"] == 1
    assert cov["fraction"] == "0.500000"
    ids = {x["measurement_id"] for x in rep["rejected"]}
    assert ids == {"SYN-BAD-001", "SYN-BAD-002"}


def test_determinism_two_run_bytes():
    a = json.dumps(ingest_and_compare(), sort_keys=True).encode()
    b = json.dumps(ingest_and_compare(), sort_keys=True).encode()
    assert hashlib.sha256(a).digest() == hashlib.sha256(b).digest()


def test_synthetic_labeling_everywhere():
    rep = ingest_and_compare()
    assert rep["synthetic_only"] is True
    assert rep["measured_in_this_system"] is False
    assert rep["can_PASS_now"] == "NO"
    for a in rep["accepted"]:
        assert a["data_class"] == "SYNTHETIC"
        assert a["synthetic_only"] == "true"
        assert a["measured_in_this_system"] == "false"
        assert a["can_PASS_now"] == "NO"
        assert "not validation" in a["interpretation"]
        assert "never a gate input" in a["interpretation"]


def test_gate_table_immutability_and_missing_file():
    before = hashlib.sha256(open("results_gate_table.csv", "rb")
                            .read()).digest()
    ingest_and_compare()
    after = hashlib.sha256(open("results_gate_table.csv", "rb")
                           .read()).digest()
    assert before == after
    rep = ingest_and_compare("no_such_measurements.json")
    assert rep["ingestion_status"] == "NO_MEASUREMENTS"
    assert rep["n_accepted"] == 0


def test_zero_pass_scan():
    rep = ingest_and_compare()
    blob = json.dumps([rep, comparison_rows(rep)])
    assert '"PASS"' not in blob
    assert "hardware_refused_by_design" in rep
    assert len(QUANTITY_REGISTRY) == 13


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    passed = failed = 0
    for t in TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:            # noqa: BLE001
            print(f"  FAIL  {t.__name__}: {e!r}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    sys.exit(1 if failed else 0)
