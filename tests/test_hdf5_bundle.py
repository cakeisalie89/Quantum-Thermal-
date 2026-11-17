"""The generic HDF5 representation of a ResultBundle: deterministic, exact
on round trip, refusing anything whose parts disagree, and claiming no
authority."""
from __future__ import annotations

import base64
import json
import shutil
import sys
import time
from pathlib import Path

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


# ------------------------------------------------ the library, in a child
# AUD-053. The HDF5 library is native code. A file found by fuzzing makes it
# fault, and a fault kills the process it happens in: no handler in the
# reader runs. So the library reads in a child, and the tests below stand a
# program of their own in the child's place to say what the parent makes of
# each way a child can end. None of them depends on a file that happens to
# break one build of the library.
def _stand_in(monkeypatch, program: str) -> None:
    monkeypatch.setattr(H, "_CHILD", program)


#: The input the fuzzer found, kept in its regression corpus. It made h5py
#: 3.16.0 with HDF5 2.0.0 fault in its attribute reader and killed the
#: process; under another build it may be refused for another reason.
CRASHED = (Path(__file__).resolve().parent / "fuzz_corpus"
           / "hdf5_bundle-crashed-43230537.json")


def test_the_file_that_killed_the_reader_is_refused(tmp_path):
    """AUD-053 as it was found. The reader's process survives the file
    (this test goes on running), and the file is refused."""
    case = json.loads(CRASHED.read_text(encoding="utf-8"))
    bad = tmp_path / "crashed.h5"
    bad.write_bytes(base64.b64decode(case["input_b64"]))
    with pytest.raises(H.Hdf5Refused, match="not a readable representation"):
        H.read(bad)


_NO_CORE = "import resource; resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); "


@pytest.mark.parametrize("program, why", [
    (_NO_CORE + "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)",
     r"the HDF5 library died reading it \(SIGSEGV\)"),
    ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
     r"died reading it \(SIGKILL\)"),
    # a signal the interpreter has no name for is still named
    ("import os, signal; os.kill(os.getpid(), signal.SIGRTMIN + 3)",
     r"died reading it \(signal \d+\)"),
])
def test_a_child_that_dies_by_a_signal_is_a_refusal_of_the_file(
        written, monkeypatch, program, why):
    _stand_in(monkeypatch, program)
    with pytest.raises(H.Hdf5Refused, match=why):
        H.read(written)
    with pytest.raises(H.Hdf5Refused, match="died reading it"):
        H.manifest(written)


def test_a_child_that_does_not_answer_in_time_is_a_refusal(written,
                                                           monkeypatch):
    """A file that makes the library loop held its reader for good."""
    _stand_in(monkeypatch, "import time; time.sleep(15)")
    monkeypatch.setattr(H, "READ_TIMEOUT_S", 0.5)
    started = time.monotonic()
    with pytest.raises(H.Hdf5Refused, match=r"did not end in 0\.5 s"):
        H.read(written)
    assert time.monotonic() - started < 10


@pytest.mark.parametrize("program, why", [
    ("raise SystemExit(3)", r"child failed \(exit 3\)"),
    ("raise ValueError('boom')", r"child failed \(exit 1\)[\s\S]*boom"),
    ("import sys; sys.stdout.write('not json')", "did not answer in JSON"),
    ("import sys; sys.stdout.write('[' * 100000)",
     "did not answer in JSON: RecursionError"),
    ("import sys; sys.stdout.write('[1, 2]')", "not an object"),
    ("import sys; sys.stdout.write('{}')", "not one: KeyError"),
])
def test_a_child_that_ends_any_other_way_is_the_readers_fault(
        written, monkeypatch, program, why):
    """Not a refusal: nothing here is a statement about the file, and a
    caller that handles refusals must not be handed one."""
    _stand_in(monkeypatch, program)
    with pytest.raises(H.Hdf5ReaderFault, match=why):
        H.read(written)
    assert not issubclass(H.Hdf5ReaderFault, H.Hdf5Refused)
    assert not issubclass(H.Hdf5Refused, H.Hdf5ReaderFault)


def test_a_refusal_reaches_the_caller_in_the_readers_own_words(written):
    """The child's refusal is the reader's, word for word: what a caller
    reads is what it read when the library ran in its own process."""
    _edit(written, lambda f: f.attrs.__setitem__("schema",
                                                 "result-bundle-hdf5/0"))
    with pytest.raises(H.Hdf5Refused) as refusal:
        H.read(written)
    assert str(refusal.value) == ("schema 'result-bundle-hdf5/0' is not "
                                  "result-bundle-hdf5/1")


def _answering(slab, **changed) -> str:
    """A program that writes what an honest child answers for the slab
    bundle, with ``changed`` in its place."""
    b, pay, _ = slab
    answer = {"record": b.to_record(), "digest": b.digest(),
              "artifacts": {k: base64.b64encode(v).decode("ascii")
                            for k, v in pay.items()},
              "links": ["a" * 64]}
    answer.update(changed)
    return f"import sys; sys.stdout.write({json.dumps(answer)!r})"


def test_an_honest_answer_is_accepted(slab, written, monkeypatch):
    """The control for the forged answers below: the same answer, unchanged,
    is the bundle."""
    b, pay, _ = slab
    _stand_in(monkeypatch, _answering(slab))
    b2, arts, links = H.read(written)
    assert b2.digest() == b.digest() and arts == pay
    assert links == ("a" * 64,)


def _other_bytes(slab) -> dict:
    _, pay, _ = slab
    return {k: base64.b64encode(v[:-1] + b"\x00").decode("ascii")
            for k, v in pay.items()}


@pytest.mark.parametrize("change, why", [
    (lambda slab: {"digest": "f" * 64},
     "named a bundle its own record is not"),
    (lambda slab: {"artifacts": _other_bytes(slab)},
     "bytes for temperature_field that the bundle does not cite"),
    (lambda slab: {"artifacts": {"uncited": base64.b64encode(b"x").decode()}},
     "bytes for uncited that the bundle does not cite"),
    (lambda slab: {"artifacts": {"temperature_field": "not base64 !"}},
     "not one: Error"),
    (lambda slab: {"links": ["a" * 64, 7]}, "a link that is not text"),
    # a string is a sequence of strings: one link a character
    (lambda slab: {"links": "a" * 64}, "links that are not a list"),
    (lambda slab: {"record": {"not": "a bundle"}}, "not one: "),
])
def test_an_answer_is_held_to_itself_not_believed(slab, written, monkeypatch,
                                                  change, why):
    """The parent rebuilds the bundle and re-hashes the bytes. A child that
    a file had made to lie could hand back nothing the bundle does not
    itself cite."""
    _stand_in(monkeypatch, _answering(slab, **change(slab)))
    with pytest.raises(H.Hdf5ReaderFault, match=why):
        H.read(written)


def test_the_child_imports_nothing_from_where_its_caller_stands(
        written, tmp_path, monkeypatch):
    """Isolated mode. A module planted in the caller's directory, or named
    by its environment, is not the child's."""
    planted = tmp_path / "planted"
    (planted / "scientific").mkdir(parents=True)
    (planted / "json.py").write_text("raise SystemExit('planted json')\n")
    (planted / "scientific" / "__init__.py").write_text(
        "raise SystemExit('planted package')\n")
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PYTHONPATH", str(planted))
    b2, _, _ = H.read(written)
    assert b2.observation_kind


def test_the_child_leaves_nothing_where_its_caller_stands(slab, written,
                                                        tmp_path, monkeypatch):
    """A child stands in a directory of its own, and it is gone when the
    child is: a core file of a fault would otherwise land in the caller's
    directory, which in CI is the repository."""
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    _stand_in(monkeypatch, "import os; open('left', 'w').write(os.getcwd()); "
              + _answering(slab))
    H.read(written)
    assert list(caller.iterdir()) == []


def test_a_path_relative_to_the_caller_is_read(slab, written, monkeypatch):
    """The child stands elsewhere, so it is handed the path the caller
    meant."""
    monkeypatch.chdir(written.parent)
    b2, _, _ = H.read(written.name)
    assert b2.digest() == slab[0].digest()


def test_the_child_compiles_where_its_parent_was_told_to(written, tmp_path,
                                                         monkeypatch):
    """Isolated mode ignores the environment variable that moves the
    bytecode cache, so the parent passes on where its own was moved to. The
    mutation harness moves it for every run; a child that compiled beside
    the source would run one mutation's bytecode for the next."""
    prefix = tmp_path / "pyc"
    monkeypatch.setattr(sys, "pycache_prefix", str(prefix))
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    argv = H._child_argv(written)
    assert argv[:4] == [sys.executable, "-I", "-X",
                        f"pycache_prefix={prefix}"]
    assert "-B" not in argv and argv[-1] == str(written)
    H.read(written)
    assert list(prefix.rglob("hdf5_bundle*.pyc")), "the child compiled there"
    monkeypatch.setattr(sys, "pycache_prefix", None)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    argv = H._child_argv(written)
    assert argv[:3] == [sys.executable, "-I", "-B"] and "-X" not in argv

