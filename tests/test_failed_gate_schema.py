"""failed_gate_samples.csv must carry one schema, rows or no rows.

The generator used to derive the header from ``rows[0]`` when the Monte-Carlo
run produced failures, and to write a completely different six-column header
("A,B,C,D,SNR,Ts_mK") when it produced none. A governed artifact's schema
therefore depended on the outcome of a stochastic run, and the package checker
did not cover this file at all, so nothing would have noticed.

MODEL-ONLY / FORECAST-ONLY. Nothing here asserts a scientific value.
"""
import csv
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qta_full_sim import (                                       # noqa: E402
    FAILED_GATE_SAMPLE_FIELDS, write_failed_gate_samples)

ARTIFACT = os.path.join(ROOT, "failed_gate_samples.csv")


def _row(**over):
    r = {k: 0.0 for k in FAILED_GATE_SAMPLE_FIELDS}
    r["dominant_failure"] = "tau_c_detection"
    r.update(over)
    return r


def _read(path):
    with open(path, newline="") as f:
        rd = csv.DictReader(f)
        return list(rd.fieldnames or ()), list(rd)


# ------------------------------------------------------------- zero rows ----

def test_zero_rows_still_emit_the_canonical_header(tmp_path):
    p = tmp_path / "f.csv"
    write_failed_gate_samples(p, [])
    header, rows = _read(p)
    assert header == list(FAILED_GATE_SAMPLE_FIELDS)
    assert rows == []


def test_zero_rows_are_deterministic_bytes(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    write_failed_gate_samples(a, [])
    write_failed_gate_samples(b, iter([]))
    assert a.read_bytes() == b.read_bytes()
    assert a.read_bytes() == (",".join(FAILED_GATE_SAMPLE_FIELDS) + "\r\n").encode()


def test_zero_rows_no_longer_produce_the_old_six_column_header(tmp_path):
    p = tmp_path / "f.csv"
    write_failed_gate_samples(p, [])
    assert "A,B,C,D,SNR,Ts_mK" not in p.read_text()


# -------------------------------------------------------------- one row ----

def test_single_row_round_trips(tmp_path):
    p = tmp_path / "f.csv"
    write_failed_gate_samples(p, [_row(tc_us=1.5, SNR=4.25)])
    header, rows = _read(p)
    assert header == list(FAILED_GATE_SAMPLE_FIELDS)
    assert len(rows) == 1
    assert float(rows[0]["tc_us"]) == 1.5
    assert float(rows[0]["SNR"]) == 4.25
    assert rows[0]["dominant_failure"] == "tau_c_detection"


# ------------------------------------------------ heterogeneous row sets ----

def test_a_field_only_present_in_a_later_row_cannot_disappear(tmp_path):
    """The declared header covers every field, so later rows keep their values."""
    first = _row()
    del first["g_d18"]
    p = tmp_path / "f.csv"
    write_failed_gate_samples(p, [first, _row(g_d18=7.0)])
    header, rows = _read(p)
    assert "g_d18" in header
    assert rows[0]["g_d18"] == ""          # genuinely absent, not truncated
    assert float(rows[1]["g_d18"]) == 7.0


def test_a_field_outside_the_contract_fails_closed(tmp_path):
    p = tmp_path / "f.csv"
    try:
        write_failed_gate_samples(p, [_row(unexpected_column=1.0)])
    except ValueError as e:
        assert "unexpected_column" in str(e)
    else:
        raise AssertionError("an undeclared field was accepted silently")


# ---------------------------------------------------------- regeneration ----

def test_same_rows_give_byte_identical_output(tmp_path):
    rows = [_row(tc_us=i) for i in range(5)]
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    write_failed_gate_samples(a, rows)
    write_failed_gate_samples(b, list(rows))
    assert a.read_bytes() == b.read_bytes()


# ----------------------------------------------- the shipped artifact -------

def test_shipped_artifact_matches_the_declared_schema():
    header, rows = _read(ARTIFACT)
    assert header == list(FAILED_GATE_SAMPLE_FIELDS), (
        "the committed artifact's header drifted from the declared contract")
    assert rows, "expected failing MC samples in the committed artifact"


def test_existing_consumers_can_still_read_it():
    """DictReader over the shipped file yields the declared fields per row."""
    _, rows = _read(ARTIFACT)
    for r in rows[:20]:
        assert set(r) == set(FAILED_GATE_SAMPLE_FIELDS)



def test_the_producer_emits_exactly_the_declared_fields():
    """The MC producer's row keys must equal the contract, not merely fit it.

    DictWriter would blank a field the producer stopped emitting; this catches
    that, and catches a field added to the producer but not to the contract.
    """
    import qta_full_sim as Q
    mc = Q.run_mode_D_MC(N=400, seed=7)
    rows = mc["failed_samples"]
    assert rows, "N=400 produced no failing samples; widen N if this ever trips"
    for r in rows[:50]:
        assert tuple(r) == FAILED_GATE_SAMPLE_FIELDS, (
            f"producer keys drifted from the declared contract: {tuple(r)}")


if __name__ == "__main__":
    ns = dict(globals())
    for _n, _f in ns.items():
        if _n.startswith("test_") and callable(_f) and not _f.__code__.co_argcount:
            _f()
    print("RESULT: failed_gate_samples schema checks passed")
