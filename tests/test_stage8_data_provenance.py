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


TESTS = [v for k, v in sorted(globals().items())
         if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    print("run via pytest: .venv/bin/python -m pytest "
          "tests/test_stage8_data_provenance.py")
    sys.exit(0)
