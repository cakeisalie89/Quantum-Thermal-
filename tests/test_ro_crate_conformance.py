"""The committed crate, packaged as a recipient receives it, and the
negative controls both validators must refuse. The external validator needs
the network for the RO-Crate JSON-LD context and runs in the hosted
ro-crate job; here its absence must read NOT_MEASURED, never agreement."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import ro_crate_conformance as RCC  # noqa: E402


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
    not over the committed report."""
    committed = ROOT / "stage8_reports" / "ro_crate_validation_report.json"
    before = committed.read_bytes()
    monkeypatch.chdir(tmp_path)
    RCC.internal(_meta())
    for broken in RCC._broken(_meta()).values():
        RCC.internal(broken)
    assert list(tmp_path.rglob("*")) == []
    assert committed.read_bytes() == before


def test_without_the_external_validator_nothing_is_called_agreement():
    rep = RCC.run(None)
    assert rep["measured"] is False
    assert {v["agreement"] for v in rep["cases"].values()} == \
        {"NOT_MEASURED"}
    assert rep["cases"]["committed_crate"]["internal"] == "ACCEPTED"
    assert set(rep["cases"]) == {"committed_crate", "no_root_dataset",
                                 "no_conformsTo", "no_datePublished",
                                 "dangling_data_entity"}


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
