"""§18 regression: a review must bind to the exact record it reviewed.

validate_review_record() checked record_sha256 only for being non-empty. It
never canonicalized the record, never recomputed the digest and never compared
them, and build_evidence_dossier() copied the claimed value straight into the
dossier. A review could therefore be paired with a MODIFIED record carrying the
same measurement_id and be accepted as evidence for it.

Hardware evidence remains disabled/pre-hardware; these tests exercise the
promotion boundary that would gate it, using fixtures that are explicitly not
data.

MODEL-ONLY / FORECAST-ONLY. Software verification; not a hardware statement.
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qta_multiphysics.hardware_governance_3d import (        # noqa: E402
    build_evidence_dossier, canonical_record_bytes, compute_record_sha256,
    validate_review_record)

# The schema-complete fixture from the hardware-governance suite. Using a
# minimal record would make the dossier reject it on schema grounds before the
# binding check is ever reached, which is correct fail-closed ordering but
# would not exercise §18.
from test_hardware_governance import FIX as _FIX                # noqa: E402

RECORD = dict(copy.deepcopy(_FIX), data_class="HARDWARE_REVIEWED")
REVIEW = {
    "reviewer_id": "TEST_FIXTURE_NOT_DATA-REV-1",
    "review_date": "2026-07-18T02:00:00Z",
    "checklist_version": "1.0",
    "decision": "ACCEPT_AS_EVIDENCE",
}


def _bound(record, **over):
    r = dict(REVIEW, record_sha256=compute_record_sha256(record))
    r.update(over)
    return r


# ------------------------------------------------------- canonicalization --

def test_canonical_form_is_deterministic():
    a = dict(RECORD)
    b = {k: RECORD[k] for k in reversed(list(RECORD))}   # different key order
    assert canonical_record_bytes(a) == canonical_record_bytes(b)
    assert compute_record_sha256(a) == compute_record_sha256(b)


def test_canonical_form_excludes_the_reviews_own_hash():
    """A record's hash cannot depend on the hash written about it."""
    a = dict(RECORD)
    b = dict(RECORD, record_sha256="f" * 64)
    assert compute_record_sha256(a) == compute_record_sha256(b)


def test_any_governed_field_change_changes_the_digest():
    base = compute_record_sha256(RECORD)
    for field, new in (("value", 9.9e-12), ("quantity", "P_CH4_Pa"),
                       ("experiment_id", "EXP-V2")):
        assert compute_record_sha256({**RECORD, field: new}) != base, field


# ------------------------------------- the four adversarial cases from §18 --

def test_review_A_with_record_A_is_allowed():
    ok, why = validate_review_record(_bound(RECORD), record=RECORD)
    assert ok, why


def test_review_A_with_modified_record_B_same_id_is_rejected():
    """The attack: same measurement_id, different content."""
    review = _bound(RECORD)
    tampered = dict(RECORD, value=1.0e-6)          # id unchanged
    ok, why = validate_review_record(review, record=tampered)
    assert not ok
    assert any("does not bind" in w for w in why), why


def test_malformed_hash_is_rejected():
    for bad in ("b" * 63, "z" * 64, "not-a-hash", "B" * 65):
        ok, why = validate_review_record(
            dict(REVIEW, record_sha256=bad), record=RECORD)
        assert not ok, bad
        assert any("malformed" in w or "does not bind" in w for w in why), (bad, why)


def test_missing_hash_is_rejected():
    ok, why = validate_review_record(dict(REVIEW), record=RECORD)
    assert not ok
    assert any("record_sha256" in w for w in why), why


# --------------------------------------------- the dossier boundary itself --

def _dossier(record, review, raw_dir=None):
    return build_evidence_dossier("B3", [record],
                                  {record["measurement_id"]: review}, raw_dir)


def _schema_clean(record, td):
    """Give the record a real raw file so only the binding can exclude it."""
    import hashlib
    import pathlib as _pl
    f = _pl.Path(td) / "fixture.dat"
    f.write_bytes(b"fixture!!!!")
    r = copy.deepcopy(record)
    r["raw_data"] = {**record["raw_data"],
                     "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
    return r


def test_a_correctly_bound_review_admits_the_record():
    """Control: without this, the exclusion tests below prove nothing."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        rec = _schema_clean(RECORD, td)
        d = _dossier(rec, _bound(rec), td)
        assert d["n_entries"] == 1, d["excluded"]


def test_dossier_excludes_a_record_whose_review_does_not_bind():
    """The attack, at the trusted boundary: same id, altered content."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        rec = _schema_clean(RECORD, td)
        review = _bound(rec)                       # signed off on THIS record
        tampered = dict(rec, value=1.0e-6)         # id unchanged
        d = _dossier(tampered, review, td)
        assert d["n_entries"] == 0, "a tampered record entered the dossier"
        why = " ".join(str(x) for x in d["excluded"])
        assert "bind" in why, why


def test_dossier_still_excludes_an_unbound_placeholder_review():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        rec = _schema_clean(RECORD, td)
        d = _dossier(rec, dict(REVIEW, record_sha256="b" * 64), td)
        assert d["n_entries"] == 0


def test_hardware_evidence_remains_disabled():
    """Nothing here turns hardware evidence on."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        rec = _schema_clean(RECORD, td)
        d = _dossier(rec, _bound(rec), td)
        assert d["review_readiness"] == "INCOMPLETE"


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
