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


#: What each negative control is refused for. Refused for anything else, a
#: control does not test what it is named for.
CONTROL_REASONS = {
    "no_root_dataset": "root dataset entity missing",
    "no_conformsTo": "no conformsTo an RO-Crate 1.x specification",
    "no_datePublished": "no ISO 8601 datePublished",
    "dangling_data_entity": "referenced file missing: does/not/exist.csv",
    "data_entity_not_a_file": "requires File or Dataset",
    "undefined_term": "keys no context defines",
}


def test_the_internal_validator_accepts_the_crate_and_refuses_every_control():
    assert RCC.internal(_meta()) == []
    controls = RCC._broken(_meta())
    assert set(controls) == set(CONTROL_REASONS)
    for name, broken in controls.items():
        refused = RCC.internal(broken)
        assert any(CONTROL_REASONS[name] in line for line in refused), \
            (name, refused)


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


# --- what the crate states about its files (AUD-076 to AUD-078) -------------

def _entity(doc: dict, rid: str) -> dict:
    return next(e for e in doc["@graph"] if e["@id"] == rid)


def _report(doc, tmp_path, raw: str | None = None) -> dict:
    """The verdict the validator writes on ``doc``, or on the text ``raw``,
    from a judgment that must not raise."""
    p = tmp_path / "ro-crate-metadata.json"
    p.write_text(raw if raw is not None else json.dumps(doc),
                 encoding="utf-8")
    rep = tmp_path / "report.json"
    rep.unlink(missing_ok=True)
    rc = RC.validate(p, rep)
    verdict = json.loads(rep.read_text(encoding="utf-8"))
    assert rc == (0 if verdict["result"] == "VALID" else 1)
    assert (verdict["result"] == "VALID") == (verdict["problems"] == [])
    return verdict


def test_the_committed_crate_is_what_the_builder_writes(tmp_path,
                                                         monkeypatch):
    """The crate states the manifest's size and the manifest hashes the
    crate. Regenerated once, the chain left the committed crate stating the
    size the manifest had before, 370 bytes short, and nothing compared the
    two (AUD-076). Built again from the tree as it stands, the crate is the
    committed one, byte for byte."""
    monkeypatch.chdir(ROOT)
    RC.build(tmp_path)
    assert (tmp_path / "ro-crate-metadata.json").read_bytes() == \
        (ROOT / RC.META).read_bytes()


@pytest.mark.parametrize("rid", ["uv.lock", "final_manifest.json",
                                 "manifest_hash.txt"])
def test_a_size_its_file_does_not_have_is_refused(tmp_path, rid):
    """Every size the crate states is its file's: the manifest's and its
    detached hash's too, which carry no checksum here, so their size is what
    can be checked. Nothing compared them, and the committed crate stated
    the manifest 370 bytes short and validated (AUD-076)."""
    true = (ROOT / rid).stat().st_size
    for stated in (str(true - 370), str(true + 1), None):
        doc = _committed()
        if stated is None:
            _entity(doc, rid).pop("contentSize")
        else:
            _entity(doc, rid)["contentSize"] = stated
        assert _report(doc, tmp_path)["problems"] == [
            f"contentSize mismatch: {rid} is stated as {stated!r}; the file "
            f"has {true} bytes"], stated


def test_only_the_manifest_and_its_hash_go_without_a_checksum(tmp_path):
    """The validator skipped the checksum of any data entity whose sha256
    began "not-embedded", the exemption the manifest pair needs. Marked so,
    uv.lock went unchecked; with every file marked so, nothing was, and the
    crate validated (AUD-077)."""
    doc = _committed()
    _entity(doc, "uv.lock")["sha256"] = RC.NOT_EMBEDDED
    assert _report(doc, tmp_path)["problems"] == [
        "data entity uv.lock claims the manifest's exemption from a "
        "checksum; only ['final_manifest.json', 'manifest_hash.txt'] may"]
    doc = _committed()
    for e in doc["@graph"]:
        if "sha256" in e:
            e["sha256"] = RC.NOT_EMBEDDED
    claimed = [p.split()[2] for p in _report(doc, tmp_path)["problems"]]
    files = {x["@id"] for x in _entity(doc, "./")["hasPart"]}
    assert RC.NO_EMBED <= files
    assert sorted(claimed) == sorted(files - RC.NO_EMBED)
    # the pair keeps the exemption it needs
    assert _report(_committed(), tmp_path)["problems"] == []


def test_a_data_entity_outside_the_tree_is_refused(tmp_path):
    """The validator hashed the file at a data entity's @id wherever it
    pointed: a crate naming /etc/hostname, with its checksum and size,
    validated, describing bytes the repository does not hold (AUD-077).
    The first path climbs out of the tree and back in, so the bytes are the
    tree's own and only the rule that a data entity is a path inside the
    tree refuses it."""
    for rid in (f"../{ROOT.name}/uv.lock", str(ROOT / "uv.lock")):
        doc = _committed()
        doc["@graph"].append(dict(_entity(doc, "uv.lock"), **{"@id": rid}))
        _entity(doc, "./")["hasPart"].append({"@id": rid})
        problems = _report(doc, tmp_path)["problems"]
        assert f"data entity {rid!r} is not a path inside this tree" \
            in problems, problems
        if rid.startswith(".."):
            assert len(problems) == 1, problems


def _set(rid, key, value):
    def f(doc):
        _entity(doc, rid)[key] = value
    return f


def _drop(rid, key):
    def f(doc):
        _entity(doc, rid).pop(key)
    return f


def _part(rid):
    def f(doc):
        doc["@graph"].append(dict(_entity(doc, "uv.lock"), **{"@id": rid}))
        _entity(doc, "./")["hasPart"].append({"@id": rid})
    return f


def _edited(f) -> dict:
    doc = _committed()
    f(doc)
    return doc


def _malformed() -> dict:
    """Crates the checks cannot read. On each the validator raised KeyError,
    TypeError, AttributeError, OSError, IsADirectoryError, RecursionError
    or the parser's error, where it returns a verdict now (AUD-078). Text
    is the metadata as written; a mapping is the committed crate, edited."""
    deep = json.dumps(_edited(_set("uv.lock", "description", "@@nest@@")))
    return {
        "a part with no checksum": _edited(_drop("uv.lock", "sha256")),
        "a part's checksum a number": _edited(_set("uv.lock", "sha256", 7)),
        "a hasPart entry a string": _edited(
            lambda d: _entity(d, "./")["hasPart"].append("uv.lock")),
        "a hasPart entry with no @id": _edited(
            lambda d: _entity(d, "./")["hasPart"].append({"name": "x"})),
        "a root with no hasPart": _edited(_drop("./", "hasPart")),
        "hasPart a mapping": _edited(_set("./", "hasPart",
                                          {"@id": "uv.lock"})),
        "no @graph": _edited(lambda d: d.pop("@graph")),
        "@graph a mapping": _edited(
            lambda d: d.update({"@graph": {"@id": "./"}})),
        "an entity a string": _edited(lambda d: d["@graph"].append("x")),
        "an entity with no @id": _edited(
            lambda d: d["@graph"].append({"name": "x"})),
        "an @id a number": _edited(
            lambda d: d["@graph"].append({"@id": 5, "sha256": "x"})),
        "an @id a list": _edited(lambda d: d["@graph"].append({"@id": ["a"]})),
        "a root with no @type": _edited(_drop("./", "@type")),
        "a root with no description": _edited(_drop("./", "description")),
        "a root description a number": _edited(_set("./", "description", 5)),
        "a descriptor about a string": _edited(
            _set("ro-crate-metadata.json", "about", "./")),
        "an action with no @type": _edited(_drop("#workflow-action",
                                                 "@type")),
        "an @type holding an object": _edited(
            _set("uv.lock", "@type", ["File", {"x": 1}])),
        "a part that is a directory": _edited(_part("docs")),
        "a part named the empty string": _edited(_part("")),
        "a part named 5000 characters": _edited(_part("x" * 5000)),
        "nested 1200 levels": deep.replace('"@@nest@@"',
                                           "[" * 1200 + "]" * 1200),
        "not JSON": "{not json",
        "a JSON list": "[]",
        "a JSON number": "5",
    }


_MALFORMED = _malformed()


@pytest.mark.parametrize("name", sorted(_MALFORMED))
def test_a_crate_the_checks_cannot_read_is_refused_in_their_words(
        tmp_path, name):
    case = _MALFORMED[name]
    raw = case if isinstance(case, str) else None
    verdict = _report(None if raw is not None else case, tmp_path, raw)
    assert verdict["result"] == "FAIL" and verdict["problems"], name


def test_every_refusal_writes_its_verdict_and_says_why(tmp_path):
    """Refused before the checks -- no root, or no CreateAction -- the
    validator wrote no verdict, and the file kept the last one: VALID, over
    a crate just refused. With no CreateAction it named no reason
    (AUD-078)."""
    p = tmp_path / "ro-crate-metadata.json"
    rep = tmp_path / "report.json"
    for gone, why in (("./", "root dataset entity missing"),
                      ("#workflow-action", "workflow CreateAction missing")):
        assert _report(_committed(), tmp_path)["result"] == "VALID"
        doc = _committed()
        doc["@graph"] = [e for e in doc["@graph"] if e["@id"] != gone]
        p.write_text(json.dumps(doc), encoding="utf-8")
        assert RC.validate(p, rep) == 1
        verdict = json.loads(rep.read_text(encoding="utf-8"))
        assert verdict["result"] == "FAIL", gone
        assert why in verdict["problems"], verdict["problems"]


def test_the_documented_order_names_every_generator_and_ends_on_the_pair():
    """Section 4 of MANIFEST_BOUNDARY.md is the order a contributor runs
    after a change. It ran the crate and the manifest once each, which left
    the crate stating the manifest's size from before (AUD-076), and named
    seven of the ten generators that rewrite a tracked file on every change,
    and not the audit's ledger. It names each, and ends on the pair, twice."""
    import audit_ledger
    text = (ROOT / "MANIFEST_BOUNDARY.md").read_text(encoding="utf-8")
    section = text.split("## 4. ", 1)[1].split("\n## ", 1)[0]
    block = section.split("```\n", 1)[1].split("```", 1)[0]
    steps = [line.split("#", 1)[0].split(None, 1)[1].strip()
             for line in block.splitlines() if line.strip()]
    generators = {gen for gen, _check in audit_ledger.DERIVED.values()}
    assert len(generators) == 10
    assert sorted(generators - set(steps)) == []
    assert "tools/audit_ledger.py derive" in steps
    assert steps[-4:] == ["ro_crate_tools.py", "generate_manifest.py"] * 2
