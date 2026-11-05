"""The committed crate, packaged as a recipient receives it, and the
negative controls both validators must refuse. The external validator needs
the network for the RO-Crate JSON-LD context and runs in the hosted
ro-crate job; here its absence must read NOT_MEASURED, never agreement."""
from __future__ import annotations

import copy

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import ro_crate_conformance as RCC  # noqa: E402

import pytest  # noqa: E402

import ro_crate_tools as RC  # noqa: E402

#: Where ``ro_crate_tools.py validate`` writes its verdict: the governed
#: workspace, untracked.
DEFAULT_REPORT = ROOT / RC.DEFAULT_VALIDATION_REPORT


def _snapshot(path: Path):
    return path.read_bytes() if path.is_file() else None


@pytest.fixture(autouse=True)
def _the_default_report_is_left_as_found():
    """Under a mutant that lets judging write its report (RC5), every test
    here that judges from the repository root would overwrite the
    validator's own report. Restored afterwards, not ignored: the test that
    looks for the write still sees it while it runs."""
    before = _snapshot(DEFAULT_REPORT)
    yield
    if _snapshot(DEFAULT_REPORT) != before:
        if before is None:
            DEFAULT_REPORT.unlink()
        else:
            DEFAULT_REPORT.write_bytes(before)


def _meta():
    return json.loads((ROOT / "ro-crate" / "ro-crate-metadata.json")
                      .read_text())


def test_the_committed_crate_packages_completely(tmp_path):
    missing = RCC.package(_meta(), tmp_path / "crate")
    assert missing == []
    assert (tmp_path / "crate" / "ro-crate-metadata.json").is_file()
    assert (tmp_path / "crate" / "HDF5_DATA_MODEL.md").is_file()


def test_the_internal_validator_accepts_the_crate_and_refuses_every_control():
    assert RCC.internal(_meta()) == []
    for name, broken in RCC._broken(_meta()).items():
        assert RCC.internal(broken), name


def test_the_internal_validator_writes_nothing_while_judging(
        tmp_path, monkeypatch):
    """ro_crate_tools.validate writes its report as a side effect, to a
    path relative to the working directory; judging a packaged or broken
    crate must write nothing, wherever the process stands -- and above all
    not over the validator's own report."""
    before = _snapshot(DEFAULT_REPORT)
    monkeypatch.chdir(tmp_path)
    RCC.internal(_meta())
    for broken in RCC._broken(_meta()).values():
        RCC.internal(broken)
    assert list(tmp_path.rglob("*")) == []
    assert _snapshot(DEFAULT_REPORT) == before


def test_without_the_external_validator_nothing_is_called_agreement():
    rep = RCC.run(None)
    assert rep["measured"] is False
    assert {v["agreement"] for v in rep["cases"].values()} == \
        {"NOT_MEASURED"}
    assert rep["cases"]["committed_crate"]["internal"] == "ACCEPTED"
    assert set(rep["cases"]) == {"committed_crate", "no_root_dataset",
                                 "no_conformsTo", "no_datePublished",
                                 "dangling_data_entity",
                                 "data_entity_not_a_file",
                                 "undefined_term"}


def test_a_disagreement_is_reported_not_resolved(monkeypatch):
    monkeypatch.setattr(RCC, "external", lambda v, c: {"status": "REFUSED",
                                                        "issues": []})
    rep = RCC.run("fake")
    assert rep["cases"]["committed_crate"]["agreement"] == "DISAGREE"
    assert rep["accepted"] is False
    monkeypatch.setattr(RCC, "external", lambda v, c: {"status": "ACCEPTED",
                                                        "issues": []})
    rep = RCC.run("fake")
    assert rep["cases"]["no_conformsTo"]["agreement"] == "DISAGREE"
    assert rep["accepted"] is False


def test_internal_stricter_is_measured_one_way_and_named(monkeypatch):
    """The community validator's REQUIRED profile accepts a data entity
    typed SoftwareSourceCode alone; RO-Crate 1.1 does not, and neither does
    the internal validator. Measured on hosted CI, that one control is
    INTERNAL_STRICTER and named in the report -- not agreement, and not a
    licence: the internal validator must still refuse it, every other
    control must still agree, and the committed crate must pass both
    (D-2026-125)."""
    def external(v, crate):
        accepted = crate.name in ("committed_crate", "data_entity_not_a_file")
        return {"status": "ACCEPTED" if accepted else "REFUSED", "issues": []}
    monkeypatch.setattr(RCC, "external", external)
    rep = RCC.run("fake")
    case = rep["cases"]["data_entity_not_a_file"]
    assert case["agreement"] == "INTERNAL_STRICTER"
    assert rep["accepted"] is True
    assert set(rep["internal_stricter"]) == {"data_entity_not_a_file"}
    # not for any other control
    def lenient(v, crate):
        accepted = crate.name in ("committed_crate", "no_conformsTo")
        return {"status": "ACCEPTED" if accepted else "REFUSED", "issues": []}
    monkeypatch.setattr(RCC, "external", lenient)
    rep = RCC.run("fake")
    assert rep["cases"]["no_conformsTo"]["agreement"] == "DISAGREE"
    assert rep["accepted"] is False
    # and never the other way round: the internal validator must refuse it
    monkeypatch.setattr(RCC, "external", external)
    monkeypatch.setattr(RCC, "internal", lambda m: [])
    rep = RCC.run("fake")
    assert rep["cases"]["data_entity_not_a_file"]["agreement"] == "AGREE"
    assert rep["accepted"] is False


def test_every_data_entity_is_a_file_or_a_dataset():
    """RO-Crate 1.1: a data entity is a File or a Dataset, whatever else it
    also is. The scripts were SoftwareSourceCode alone, the internal
    validator passed them, and the community validator on hosted CI did
    not (D-2026-111)."""
    meta = _meta()
    by = {e["@id"]: e for e in meta["@graph"]}
    for part in by["./"]["hasPart"]:
        types = by[part["@id"]]["@type"]
        types = types if isinstance(types, list) else [types]
        assert {"File", "Dataset"} & set(types), part["@id"]
    refused = RCC.internal(RCC._broken(meta)["data_entity_not_a_file"])
    assert any("requires File or Dataset" in p for p in refused), refused


def test_every_key_is_a_term_some_context_defines():
    """RO-Crate 1.1 s.3: the descriptor is compacted JSON-LD, so a key no
    context defines is not allowed. The crate wrote 25 "sha256" keys the
    1.1 context does not define, the internal validator passed them, and
    the community validator on hosted CI did not (D-2026-122). The crate
    now defines the term, mapped to the workflow-run vocabulary's."""
    import ro_crate_tools as RC
    meta = _meta()
    assert meta["@context"][0] == RC.SPEC + "/context"
    assert meta["@context"][1] == {"sha256": RC.WFRUN_SHA256}
    refused = RCC.internal(RCC._broken(meta)["undefined_term"])
    assert any("keys no context defines: ['sha256']" in p
               for p in refused), refused
    m = copy.deepcopy(meta)
    m["@graph"][0]["checksumOfSomething"] = "x"
    assert any("checksumOfSomething" in p for p in RCC.internal(m))


def test_a_data_entity_of_another_type_is_still_packaged(tmp_path):
    """The package is the crate as a recipient receives it: every data
    entity the root has, whatever its type -- a hole the validators are not
    shown is one neither can see."""
    broken = RCC._broken(_meta())["data_entity_not_a_file"]
    assert RCC.package(broken, tmp_path / "crate") == []
    assert (tmp_path / "crate" / "Snakefile").is_file()


def test_a_disagreement_prints_both_validators_reasons(monkeypatch,
                                                       tmp_path, capsys):
    issue = {"severity": "REQUIRED", "message": "the reason", "check": "x"}
    monkeypatch.setattr(RCC, "external", lambda v, c: {
        "status": "REFUSED", "issues": [issue]})
    assert RCC.main(["--validator", "fake",
                     "--out", str(tmp_path / "r.json")]) == 1
    out = capsys.readouterr().out
    assert "committed_crate: internal ACCEPTED, external REFUSED -> " \
        "DISAGREE" in out
    assert "external: " in out and "the reason" in out


# --- the validator's scope: nothing, part, and no root -----------------------
# Carried from the retired programme's provenance tests, which held these
# properties of ro_crate_tools.validate and left with it.

def _committed() -> dict:
    return json.loads((ROOT / "ro-crate" / "ro-crate-metadata.json")
                      .read_text(encoding="utf-8"))


def _validate(doc: dict, tmp_path) -> int:
    p = tmp_path / "ro-crate-metadata.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return RC.validate(p, tmp_path / "report.json")


def test_the_committed_crate_validates(tmp_path):
    """The positive control for the three refusals below."""
    assert _validate(_committed(), tmp_path) == 0


def test_a_crate_validation_over_nothing_is_refused(tmp_path):
    """Checksums are verified in a loop over hasPart; with hasPart empty the
    loop runs zero times, and a VALID over nothing is not a verdict."""
    doc = _committed()
    # Only the descriptor, the root and contextual entities stay, so no
    # other rule has anything to object to: the empty scope is the finding.
    doc["@graph"] = [e for e in doc["@graph"]
                     if e["@id"] in ("ro-crate-metadata.json", "./")
                     or e["@id"].startswith("#")]
    root = next(e for e in doc["@graph"] if e["@id"] == "./")
    root["hasPart"] = []
    assert _validate(doc, tmp_path) != 0


def test_a_checksum_outside_hasPart_is_reported_as_unverified(tmp_path):
    """Scope completeness, not only non-emptiness: an entity carrying a
    checksum the hasPart loop never reaches is a hash nobody checked."""
    doc = _committed()
    doc["@graph"].append({"@id": "unreferenced.txt", "@type": "File",
                          "sha256": "a" * 64})
    assert _validate(doc, tmp_path) != 0


def test_a_crate_missing_its_root_fails_rather_than_crashing(tmp_path):
    """A crash is not a refusal: a crate with no root dataset returns a
    verdict, not a KeyError."""
    doc = _committed()
    doc["@graph"] = [e for e in doc["@graph"] if e["@id"] != "./"]
    assert _validate(doc, tmp_path) != 0
