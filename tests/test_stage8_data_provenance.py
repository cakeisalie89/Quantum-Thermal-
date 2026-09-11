"""Stage-8 data/provenance boundary tests (Pydantic models inline).

MODEL-ONLY / FORECAST-ONLY. Software verification only; scientific gate
PASS remains zero. Models validate mapping/schema/crate records at
trusted boundaries; the JSON registries and the in-repo crate validator
remain the authorities (no competing authority is created).
"""
import hashlib
import json
import re
import subprocess
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest                                                    # noqa: E402
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402
from pydantic import (BaseModel, ConfigDict, Field,               # noqa: E402
                      ValidationError, model_validator)

ROOT = pathlib.Path(__file__).resolve().parents[1]
DET = settings(max_examples=25, deadline=None, derandomize=True,
               suppress_health_check=[HealthCheck.too_slow])

MAPPING = json.loads((ROOT / "hdf5_output_mapping.json").read_text())
SCHEMA = json.loads((ROOT / "hdf5_schema.json").read_text())
CRATE = json.loads(
    (ROOT / "ro-crate" / "ro-crate-metadata.json").read_text())


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetPath(_S):
    path: str = Field(pattern=r"^/(tables|native_json|native_text)/"
                              r"[A-Za-z0-9_.-]+$")


class HashRecord(_S):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class UnitDecl(_S):
    unit: str

    @model_validator(mode="after")
    def _nonempty(self) -> "UnitDecl":
        if not self.unit.strip():
            raise ValueError("unit must be non-empty (use 'unresolved')")
        return self


class MappingRecord(_S):
    path: str
    format: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    hdf5_group: str
    byte_preservation: bool

    @model_validator(mode="after")
    def _rules(self) -> "MappingRecord":
        if not self.byte_preservation:
            raise ValueError("governed outputs require byte preservation")
        DatasetPath(path=self.hdf5_group)
        return self


class CrateFileEntity(_S):
    id: str
    sha256: str

    @model_validator(mode="after")
    def _rel(self) -> "CrateFileEntity":
        if self.id.startswith("/") or ":\\" in self.id:
            raise ValueError("absolute paths forbidden in crate")
        if not (re.fullmatch(r"[0-9a-f]{64}", self.sha256)
                or self.sha256.startswith("not-embedded")):
            raise ValueError("sha256 malformed")
        return self


# ------------------------------ unit tests ------------------------------

def test_mapping_registry_valid_and_complete():
    assert MAPPING["schema_version"] == "1.0.0"
    assert MAPPING["n_governed"] == 88
    groups = set()
    for o in MAPPING["outputs"]:
        m = MappingRecord(path=o["path"], format=o["format"],
                          sha256=o["sha256"],
                          hdf5_group=o["hdf5_group"],
                          byte_preservation=o["byte_preservation"])
        assert m.hdf5_group not in groups      # duplicate-path rejection
        groups.add(m.hdf5_group)
        src = ROOT / o["path"]
        assert src.exists()
        assert hashlib.sha256(src.read_bytes()).hexdigest() == o["sha256"]


def test_schema_units_and_dtypes():
    assert len(SCHEMA["datasets"]) == 88
    for d in SCHEMA["datasets"]:
        DatasetPath(path=d["dataset_group"])
        HashRecord(sha256=d["source_sha256"])
        for c in d.get("columns", []):
            UnitDecl(unit=c["unit"])
            assert c["dtype"] in ("float64", "utf8_string")
            if c["dtype"] == "float64":
                assert "zero" not in c["missing_value_policy"]


def test_invalid_records_fail_closed():
    with pytest.raises(ValidationError):
        DatasetPath(path="/etc/passwd")
    with pytest.raises(ValidationError):
        HashRecord(sha256="xyz")
    with pytest.raises(ValidationError):
        MappingRecord(path="a.csv", format="csv", sha256="0" * 64,
                      hdf5_group="/tables/a", byte_preservation=False)
    with pytest.raises(ValidationError):
        CrateFileEntity(id="/abs/path", sha256="0" * 64)


def test_hdf5_builder_fail_closed_on_stale_mapping(tmp_path):
    scratch = tmp_path / "s"
    scratch.mkdir()
    for f in ("build_hdf5.py", "hdf5_output_mapping.json",
              "hdf5_schema.json", "results_gate_table.csv"):
        (scratch / f).write_bytes((ROOT / f).read_bytes())
    (scratch / "outputs").mkdir()
    r = subprocess.run([sys.executable, "build_hdf5.py"],
                       cwd=scratch, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 1
    assert "complete verified set" in r.stdout       # truncated refusal


def test_crate_graph_governance():
    g = {e["@id"]: e for e in CRATE["@graph"]}
    root = g["./"]
    for must in ("PASS count is zero", "PROPOSED_NOT_PERFORMED",
                 "measured_in_this_system=false", "not new evidence"):
        assert must in root["description"]
    for part in root["hasPart"]:
        e = g[part["@id"]]
        CrateFileEntity(id=e["@id"], sha256=e["sha256"])
    assert g["#simulation-action"]["@type"] == "CreateAction"
    blob = json.dumps(CRATE)
    assert "/home/" not in blob and "/tmp/" not in blob


def test_compatibility_outputs_untouched():
    # spot-verify byte preservation of governed sources vs mapping hashes
    for o in MAPPING["outputs"][:12] + MAPPING["outputs"][-4:]:
        p = ROOT / o["path"]
        assert hashlib.sha256(p.read_bytes()).hexdigest() == o["sha256"]


# --------------------------- Hypothesis layer ---------------------------

@DET
@given(stem=st.from_regex(r"[A-Za-z0-9_]{1,20}", fullmatch=True),
       fam=st.sampled_from(["tables", "native_json", "native_text"]))
def test_prop_dataset_paths_stable(stem, fam):
    DatasetPath(path=f"/{fam}/{stem}")


@DET
@given(bad=st.sampled_from(["/abs", "tables/x", "/tables/", "/other/x",
                            "/tables/a b", "../x"]))
def test_prop_bad_paths_rejected(bad):
    with pytest.raises(ValidationError):
        DatasetPath(path=bad)


@DET
@given(h=st.text(alphabet="0123456789abcdef", min_size=64, max_size=64))
def test_prop_hash_records(h):
    HashRecord(sha256=h)


@DET
@given(h=st.one_of(
    st.text(max_size=10),
    st.text(alphabet="0123456789ABCDEF", min_size=64,
            max_size=64).filter(lambda x: any(c in "ABCDEF"
                                              for c in x))))
def test_prop_bad_hashes_rejected(h):
    with pytest.raises(ValidationError):
        HashRecord(sha256=h)


@DET
@given(u=st.sampled_from(["K", "Pa", "unresolved", "monolayer", "1/s"]))
def test_prop_units_accept_declared(u):
    UnitDecl(unit=u)


@DET
@given(payload=st.dictionaries(st.sampled_from(list("abcd")),
                               st.integers(0, 9), max_size=4))
def test_prop_serialization_key_order(payload):
    assert json.dumps(payload, sort_keys=True) == \
        json.dumps(dict(reversed(list(payload.items()))), sort_keys=True)


# ---------------------------------------------------------------------------
# D-2026-39: a verifier that compared nothing and called it EQUIVALENT.
#
# Every check in validate_hdf5_equivalence.py appends to `problems`, so the
# verdict was "EQUIVALENT" exactly when nothing went wrong -- including when
# nothing happened. Handed a mapping with no outputs and an HDF5 file
# carrying a well-formed /provenance group, it compared 0 sources and 0
# datasets, printed RESULT: EQUIVALENT, exited 0, and wrote
# "result": "EQUIVALENT" into a report the RO-Crate publishes as an entity.
#
# The repository already names this class -- tools/performance_baseline.py
# calls it "the vacuous ... defect" and a test in test_agent_performance.py
# says "this repository already carries that defect once, in a verifier that
# compared zero files and printed IDENTICAL". The class was identified, one
# sibling was fixed, and this instance was never swept.
# ---------------------------------------------------------------------------

def _shell_h5(path):
    """An HDF5 file with nothing in it but a well-formed /provenance group.

    Well-formed on purpose: the provenance attribute checks are what masked
    this for as long as they did, and a test that leans on them would be
    asserting the wrong refusal.
    """
    h5py = pytest.importorskip("h5py")
    with h5py.File(path, "w") as h:
        g = h.create_group("provenance")
        for a in ("mapping_sha256", "schema_sha256", "uv_lock_sha256",
                  "manifest_sha256_at_build", "scientific_gate_PASS_count",
                  "can_PASS_now", "measured_in_this_system"):
            g.attrs[a] = "0"


def _run_validator(tmp_path, monkeypatch, mapping):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hdf5_output_mapping.json").write_text(json.dumps(mapping))
    _shell_h5(tmp_path / "shell.h5")
    sys.path.insert(0, str(ROOT))
    import validate_hdf5_equivalence as V
    rc = V.main(str(tmp_path / "shell.h5"), str(tmp_path / "report.json"))
    return rc, json.loads((tmp_path / "report.json").read_text())


def test_an_equivalence_over_nothing_is_refused(tmp_path, monkeypatch):
    rc, report = _run_validator(
        tmp_path, monkeypatch,
        {"schema_version": "1.0.0", "n_governed": 0, "outputs": []})

    assert rc != 0, "a comparison of nothing exited zero"
    assert report["result"] != "EQUIVALENT", report["result"]
    assert report["sources_checked"] == 0
    assert any("no stated scope" in m for m in report["mismatches"]), \
        report["mismatches"]


def test_an_equivalence_over_PART_of_the_set_is_refused(
        tmp_path, monkeypatch):
    """The stronger half, and the reason the guard is not `== 0`.

    "Did you check anything" is the weak form of the question. A mapping
    truncated to a handful of outputs would satisfy it and still report a
    verdict over a set nobody agreed to.
    """
    rc, report = _run_validator(
        tmp_path, monkeypatch,
        {"schema_version": "1.0.0", "n_governed": 5, "outputs": []})

    assert rc != 0
    assert report["result"] != "EQUIVALENT", report["result"]
    assert any("against a declared 5" in m for m in report["mismatches"]), \
        report["mismatches"]


def test_the_committed_equivalence_report_covered_the_whole_declared_set():
    """ANTI-VACUITY, against the real artefact rather than a fixture.

    The two tests above would both pass against a validator that refused
    every input. This one holds the committed report to the same rule the
    guard applies, and it is the rule rather than a remembered number: the
    mapping says how many governed outputs there are, and the report has to
    have compared that many.
    """
    mapping = json.loads((ROOT / "hdf5_output_mapping.json").read_text())
    report = json.loads(
        (ROOT / "stage8_reports" / "hdf5_equivalence_report.json").read_text())

    assert report["result"] == "EQUIVALENT", report["result"]
    assert mapping["n_governed"] == len(mapping["outputs"])
    assert report["sources_checked"] == mapping["n_governed"], (
        f"the committed report compared {report['sources_checked']} of a "
        f"declared {mapping['n_governed']}")
    assert report["datasets_checked"] > 0, (
        "a report with no datasets compared is an equivalence about nothing, "
        "whatever its source count says")


def _minimal_crate(hasPart, extra=()):
    """A crate that clears every structural check the validator makes.

    Deliberately well-formed: the root description phrases and the
    CreateAction are what a lazier fixture would trip over, and a test that
    leaned on those would be asserting the wrong refusal.
    """
    root_desc = ("PASS count is zero. PROPOSED_NOT_PERFORMED. "
                 "measured_in_this_system=false. not new evidence.")
    return {"@context": "https://w3id.org/ro/crate/1.1/context",
            "@graph": [
                {"@id": "ro-crate-metadata.json", "@type": "CreativeWork"},
                {"@id": "./", "@type": ["Dataset"], "description": root_desc,
                 "hasPart": [{"@id": i} for i in hasPart]},
                {"@id": "#simulation-action", "@type": "CreateAction"},
                *extra,
            ]}


def test_a_crate_validation_over_nothing_is_refused(tmp_path):
    """Every checksum this validator verifies is verified inside a loop over
    hasPart. With hasPart empty the loop ran zero times and it printed
    RESULT: VALID -- into a report the manifest hashes."""
    sys.path.insert(0, str(ROOT))
    import ro_crate_tools as R

    p = tmp_path / "crate.json"
    p.write_text(json.dumps(_minimal_crate([])))
    assert R.validate(p, tmp_path / "report.json") != 0, (
        "a validation of nothing exited zero")


def test_a_checksum_outside_hasPart_is_reported_as_unverified(tmp_path):
    """Scope COMPLETENESS, not merely non-emptiness.

    An entity carrying a checksum that sits outside hasPart is one the loop
    never reaches, so its hash is decoration. Contextual `#`-prefixed
    entities are excluded on purpose -- the real crate has one, and a rule
    that flagged it would be a rule about the wrong thing.
    """
    sys.path.insert(0, str(ROOT))
    import ro_crate_tools as R

    real = ROOT / "README.md"
    crate = _minimal_crate(
        [real.name],
        extra=[{"@id": real.name, "@type": "File",
                "sha256": hashlib.sha256(real.read_bytes()).hexdigest()},
               {"@id": "unreferenced.txt", "@type": "File",
                "sha256": "a" * 64}])
    p = tmp_path / "crate.json"
    p.write_text(json.dumps(crate))

    import os
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        assert R.validate(p, tmp_path / "report.json") != 0
    finally:
        os.chdir(cwd)


def test_a_crate_missing_its_root_fails_rather_than_crashing(tmp_path):
    """A crash is not a refusal.

    The checks below the root test index `by["./"]` directly, so a crate
    without one died with a KeyError traceback instead of returning a
    verdict -- in a repository that keeps a whole suite named for not doing
    that.
    """
    sys.path.insert(0, str(ROOT))
    import ro_crate_tools as R

    p = tmp_path / "crate.json"
    p.write_text(json.dumps({"@graph": [
        {"@id": "ro-crate-metadata.json", "@type": "CreativeWork"}]}))
    assert R.validate(p, tmp_path / "report.json") != 0


def test_the_committed_crate_references_every_file_it_checksums():
    """ANTI-VACUITY against the real artefact, and the rule rather than a
    remembered count."""
    doc = json.loads((ROOT / "ro-crate" / "ro-crate-metadata.json").read_text())
    g = doc["@graph"]
    by = {e["@id"]: e for e in g}
    parts = {p["@id"] for p in by["./"]["hasPart"]}

    assert parts, "the committed crate references no files"
    unchecked = sorted(e["@id"] for e in g
                       if "sha256" in e and not e["@id"].startswith("#")
                       and e["@id"] not in parts)
    assert unchecked == [], unchecked


def test_the_crate_validator_writes_only_where_it_is_told(tmp_path):
    """D-2026-40, and the reason `validate` takes a report path at all.

    It writes a TRACKED artefact by default. The tests above call it against
    throwaway crates, and before this parameter existed each of those calls
    overwrote `stage8_reports/ro_crate_validation_report.json` with a verdict
    about the throwaway -- `"entities": 5, "referenced_files": 1,
    "result": "FAIL"` -- which then got committed, because the full suite
    stayed green: `test_manifest_completeness.py` runs alphabetically BEFORE
    `test_stage8_data_provenance.py`, so the manifest was checked and then
    the damage was done.

    This test is order-independent on purpose. It does not ask whether the
    tree is clean at some moment; it asks whether the function can be made to
    write the default path when it was handed another one.
    """
    sys.path.insert(0, str(ROOT))
    import ro_crate_tools as R

    tracked = ROOT / R.DEFAULT_VALIDATION_REPORT
    before = hashlib.sha256(tracked.read_bytes()).hexdigest()

    p = tmp_path / "crate.json"
    p.write_text(json.dumps(_minimal_crate([])))
    out = tmp_path / "elsewhere.json"
    R.validate(p, out)

    assert out.exists(), "it did not write where it was told"
    assert hashlib.sha256(tracked.read_bytes()).hexdigest() == before, (
        "validate() wrote the tracked report while being handed another "
        "path; a test that calls it is then a test that edits the repository")


TESTS = [v for k, v in sorted(globals().items())
         if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    print("run via pytest: .venv/bin/python -m pytest "
          "tests/test_stage8_data_provenance.py")
    sys.exit(0)
