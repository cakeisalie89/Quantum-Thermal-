"""The generic HDF5 representation of a ResultBundle: deterministic, exact
on round trip, refusing anything whose parts disagree, and claiming no
authority."""
from __future__ import annotations

import json
import shutil

import h5py
import numpy as np
import pytest

from scientific import hdf5_bundle as H
from scientific.model import run_model_with_artifacts
from scientific.models.slab_transient import SlabTransientModel
from scientific.models.thermal_rc2 import ThermalRC2Model

SLAB = {"L_m": 0.05, "k_W_m_K": 15.0, "rho_c_J_m3_K": 3.6e6, "q_W_m3": 2e5,
        "h_W_m2_K": 50.0, "T_inf_K": 300.0, "T0_K": 300.0, "t_end_s": 600.0,
        "n_cells": 40, "n_steps": 80}
RC2 = {"C1_J_K": 500.0, "C2_J_K": 2000.0, "G12_W_K": 5.0, "G2a_W_K": 2.0,
       "T1_0_K": 300.0, "T2_0_K": 300.0, "Q_W": 10.0, "Tamb_K": 300.0,
       "t_end_s": 3600.0}


@pytest.fixture(scope="module")
def slab():
    m = SlabTransientModel()
    b, pay = run_model_with_artifacts(m, SLAB)
    units = {p.name: p.unit for p in m.parameter_schema.parameters}
    return b, pay, units


@pytest.fixture
def written(slab, tmp_path):
    b, pay, units = slab
    p = tmp_path / "bundle.h5"
    H.write(p, b, artifacts=pay, parameter_units=units,
            verification_links=("a" * 64,))
    return p


def test_the_round_trip_is_exact(slab, written):
    b, pay, _ = slab
    b2, arts, links = H.read(written)
    assert b2.to_record() == b.to_record()
    assert b2.digest() == b.digest()
    assert arts == pay
    assert links == ("a" * 64,)


def test_the_same_bundle_gives_the_same_bytes(slab, tmp_path):
    b, pay, units = slab
    d = [H.write(tmp_path / f"{i}.h5", b, artifacts=pay,
                 parameter_units=units) for i in range(2)]
    assert d[0] == d[1]
    assert (tmp_path / "0.h5").read_bytes() == (tmp_path / "1.h5").read_bytes()


def test_units_resolution_and_arrays_are_preserved(written):
    with h5py.File(written) as f:
        assert f["parameters/L_m"].attrs["unit"] == "m"
        out = f["outputs/T_at_L"]
        assert out.attrs["unit"] == "K"
        assert out.attrs["resolution_class"] == "DISCRETIZATION_ESTIMATE"
        assert out.attrs["resolution"] > 0
        art = f["artifacts/temperature_field"]
        assert art["T"].attrs["unit"] == "K" and art["x"].attrs["unit"] == "m"
        assert art["T"].shape == (40,) and bool(np.all(art["T_valid"][()]))
        assert f.attrs["authority"] == H.AUTHORITY
        assert f.attrs["observation_kind"] == "SIMULATION_RESULT"


def test_a_model_without_artifacts_or_parameter_units(tmp_path):
    b = ThermalRC2Model().run(RC2)
    p = tmp_path / "rc2.h5"
    H.write(p, b)
    b2, arts, links = H.read(p)
    assert b2.to_record() == b.to_record() and arts == {} and links == ()
    with h5py.File(p) as f:
        assert f["parameters/Q_W"].attrs["unit"] == H.UNRESOLVED


def _edit(path, fn):
    with h5py.File(path, "r+") as f:
        fn(f)


@pytest.mark.parametrize("edit, why", [
    (lambda f: f["outputs/T_at_L"].__setitem__((), 999.0), "value differs"),
    (lambda f: f["outputs/T_at_L"].attrs.__setitem__("unit", "degC"),
     "unit differs"),
    (lambda f: f["outputs/T_at_L"].attrs.__setitem__("resolution", 0.0),
     "resolution differs"),
    (lambda f: f["parameters/L_m"].__setitem__((), 0.06),
     "parameter L_m differs"),
    (lambda f: f["invariants/finite"].attrs.__setitem__("holds", False),
     "holds differs"),
    (lambda f: f.attrs.__setitem__("schema", "result-bundle-hdf5/0"),
     "schema"),
    (lambda f: f.attrs.__setitem__("authority", "VERIFIED"),
     "claims authority"),
    (lambda f: f.attrs.__setitem__("bundle_digest", "0" * 64),
     "bundle digest"),
    (lambda f: f.attrs.__setitem__("model_id", "other"), "model_id"),
    (lambda f: f["artifacts/temperature_field/raw"].__setitem__(0, 0),
     "not its digest"),
])
def test_a_tampered_file_is_refused(written, edit, why):
    _edit(written, edit)
    with pytest.raises(H.Hdf5Refused, match=why):
        H.read(written)


def test_a_rewritten_record_is_refused(written):
    def rewrite(f):
        rec = json.loads(bytes(f["bundle_record"][()]))
        rec["warnings"] = []
        del f["bundle_record"]
        f.create_dataset("bundle_record", data=np.frombuffer(
            json.dumps(rec, sort_keys=True).encode(), dtype=np.uint8))
    _edit(written, rewrite)
    with pytest.raises(H.Hdf5Refused, match="bundle digest"):
        H.read(written)


def test_artifact_bytes_must_be_the_cited_ones(slab, tmp_path):
    b, pay, _ = slab
    bad = {k: v[:-1] + b"\x00" for k, v in pay.items()}
    with pytest.raises(H.Hdf5Refused, match="not the ones"):
        H.write(tmp_path / "x.h5", b, artifacts=bad)
    with pytest.raises(H.Hdf5Refused, match="does not cite"):
        H.write(tmp_path / "y.h5", b, artifacts={**pay, "extra": b"x"})
    with pytest.raises(H.Hdf5Refused, match="report digest"):
        H.write(tmp_path / "z.h5", b, artifacts=pay,
                verification_links=("VERIFIED",))


def test_a_failed_output_round_trips_as_a_status(tmp_path):
    b = ThermalRC2Model().run(RC2)
    rec = b.to_record()
    rec["outputs"][0] = {"name": rec["outputs"][0]["name"],
                         "status": "FAILED", "quantity": None,
                         "reason": "diverged"}
    from scientific.result import ResultBundle
    bad = ResultBundle.from_record(rec)
    p = tmp_path / "f.h5"
    H.write(p, bad)
    b2, _, _ = H.read(p)
    assert b2.to_record() == rec
    with h5py.File(p) as f:
        assert f["outputs/T1"].attrs["status"] == "FAILED"


def test_the_manifest_states_representation_not_verification(written,
                                                             tmp_path):
    m = H.manifest(written)
    assert m["authority"] == H.AUTHORITY
    assert "verified" not in json.dumps(m).lower()
    shutil.copy(written, tmp_path / "copy.h5")
    assert H.manifest(tmp_path / "copy.h5")["sha256"] == m["sha256"]


def test_corrupt_metadata_is_a_refusal_not_a_library_error(tmp_path):
    """Found by fuzzing: a flipped byte in the HDF5 metadata made h5py raise
    RuntimeError (incorrect metadata checksum), which escaped the reader.
    Every corruption is now an Hdf5Refused."""
    b = SlabTransientModel().run({"L_m": 0.05, "k_W_m_K": 15.0,
                                  "rho_c_J_m3_K": 3.6e6, "q_W_m3": 2.0e5,
                                  "h_W_m2_K": 50.0, "T_inf_K": 300.0,
                                  "T0_K": 300.0, "t_end_s": 600.0})
    good = tmp_path / "b.h5"
    H.write(good, b)
    raw = bytearray(good.read_bytes())
    refused = 0
    for off in range(0, min(len(raw), 4096), 97):
        bad = tmp_path / f"c{off}.h5"
        mutated = bytearray(raw)
        mutated[off] ^= 0xFF
        bad.write_bytes(bytes(mutated))
        try:
            got, _, _ = H.read(bad)
        except H.Hdf5Refused:
            refused += 1
            continue
        assert got.digest() == b.digest()   # a harmless byte: same bundle
    assert refused > 0
