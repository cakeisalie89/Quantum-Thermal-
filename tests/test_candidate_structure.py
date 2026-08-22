"""Phase 2 processes hostile input; it must refuse, not crash.

The verifier reads the bundle and archive BEFORE anything authenticates them,
so every malformed or hostile shape is an expected input, not an exceptional
one. Direct reads failed closed only accidentally -- through a Python
traceback, which exits non-zero but supplies no classification, loses the
remaining diagnostics, and cannot be told apart from a broken tool.

ZIP structural ambiguity gets particular attention because payload-digest
recomputation maps members into a dict keyed by relative path. Two members
that normalize to one key would collapse into a single entry and silently
change what the digest covers.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import release_trust as RT   # noqa: E402


def _vr():
    spec = importlib.util.spec_from_file_location(
        "_vr_cand", os.path.join(ROOT, "verify_release.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["_vr_cand"] = m
    spec.loader.exec_module(m)
    return m


ROOTDIR = "QTA_source"
REQUIRED = [f"{ROOTDIR}/uv.lock",
            f"{ROOTDIR}/results_gate_table.csv",
            f"{ROOTDIR}/{RT.CANONICAL_POLICY_PATH}"]


def good_bundle(tmp_path, **over):
    """A structurally valid candidate bundle."""
    b = tmp_path / "bundle"
    b.mkdir(parents=True, exist_ok=True)
    files = {
        "release_index.json": json.dumps({
            "release_artifact": {"name": "QTA_source.zip", "size": 1,
                                 "sha256": "a" * 64},
            "files": [], "claims": {"scientific_gate_PASS_count": 0}}),
        "SHA256SUMS": f"{'a' * 64}  QTA_source.zip\n",
        "sbom.cdx.json": json.dumps({"components": []}),
        "provenance.intoto.json": json.dumps({"subject": [],
                                              "predicate": {}}),
    }
    files.update(over)
    for name, body in files.items():
        if body is None:
            continue
        (b / name).write_text(body)
    return b


def classify(problems):
    return [c for c in _vr().CANDIDATE_FAILURES
            if any(c in p for p in problems)]


# ---------------------------------------------------------------------------
# 1. Bundle metadata
# ---------------------------------------------------------------------------

def test_a_good_bundle_parses(tmp_path):
    vr = _vr()
    problems = []
    out = vr.parse_candidate_bundle(problems, good_bundle(tmp_path))
    assert out is not None and problems == []
    assert set(out) == {"index", "sums", "sbom", "provenance"}


@pytest.mark.parametrize("name,expect", [
    ("release_index.json", "MISSING_RELEASE_INDEX"),
    ("SHA256SUMS", "MISSING_SHA256SUMS"),
    ("sbom.cdx.json", "MISSING_SBOM"),
    ("provenance.intoto.json", "MISSING_PROVENANCE"),
])
def test_missing_metadata_is_classified(tmp_path, name, expect):
    vr = _vr()
    b = good_bundle(tmp_path, **{name: None})
    problems = []
    assert vr.parse_candidate_bundle(problems, b) is None
    assert classify(problems) == [expect], problems


@pytest.mark.parametrize("name,expect", [
    ("release_index.json", "INVALID_RELEASE_INDEX"),
    ("sbom.cdx.json", "INVALID_SBOM"),
    ("provenance.intoto.json", "INVALID_PROVENANCE"),
])
def test_unparseable_metadata_is_classified(tmp_path, name, expect):
    vr = _vr()
    problems = []
    assert vr.parse_candidate_bundle(
        problems, good_bundle(tmp_path, **{name: "{not json"})) is None
    assert expect in " ".join(problems)


@pytest.mark.parametrize("name,expect", [
    ("release_index.json", "INVALID_RELEASE_INDEX"),
    ("sbom.cdx.json", "INVALID_SBOM"),
    ("provenance.intoto.json", "INVALID_PROVENANCE"),
])
def test_empty_metadata_is_classified(tmp_path, name, expect):
    vr = _vr()
    problems = []
    assert vr.parse_candidate_bundle(
        problems, good_bundle(tmp_path, **{name: ""})) is None
    assert expect in " ".join(problems)


def test_index_of_the_wrong_json_type_is_classified(tmp_path):
    vr = _vr()
    problems = []
    assert vr.parse_candidate_bundle(
        problems, good_bundle(tmp_path,
                              **{"release_index.json": "[1,2,3]"})) is None
    assert "INVALID_RELEASE_INDEX" in " ".join(problems)


@pytest.mark.parametrize("body", [
    '{"files": [], "claims": {}}',                       # no release_artifact
    '{"release_artifact": "str", "files": [], "claims": {}}',
    '{"release_artifact": {}, "files": {}, "claims": {}}',
    '{"release_artifact": {}, "files": [], "claims": []}',
])
def test_index_with_wrong_field_types_is_classified(tmp_path, body):
    vr = _vr()
    problems = []
    assert vr.parse_candidate_bundle(
        problems, good_bundle(tmp_path,
                              **{"release_index.json": body})) is None
    assert "INVALID_RELEASE_INDEX" in " ".join(problems)


def test_sbom_without_components_is_classified(tmp_path):
    vr = _vr()
    problems = []
    assert vr.parse_candidate_bundle(
        problems, good_bundle(tmp_path,
                              **{"sbom.cdx.json": '{"x": 1}'})) is None
    assert "INVALID_SBOM" in " ".join(problems)


def test_provenance_with_wrong_shape_is_classified(tmp_path):
    vr = _vr()
    problems = []
    assert vr.parse_candidate_bundle(
        problems,
        good_bundle(tmp_path,
                    **{"provenance.intoto.json": '{"subject": {}}'})) is None
    assert "INVALID_PROVENANCE" in " ".join(problems)


# ---------------------------------------------------------------------------
# 2. SHA256SUMS
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "no-digest-here\n",
    "zz" * 32 + "  name\n",
    "a" * 63 + "  name\n",
    f"{'a' * 64}  \n",
    "",
    "   \n",
])
def test_malformed_sha256sums_is_classified_not_crashed(tmp_path, body):
    vr = _vr()
    problems = []
    assert vr.parse_candidate_bundle(
        problems, good_bundle(tmp_path, SHA256SUMS=body)) is None
    assert "INVALID_SHA256SUMS" in " ".join(problems)


def test_duplicate_sha256sums_entry_is_refused(tmp_path):
    vr = _vr()
    body = f"{'a' * 64}  dup\n{'b' * 64}  dup\n"
    problems = []
    assert vr.parse_candidate_bundle(
        problems, good_bundle(tmp_path, SHA256SUMS=body)) is None
    assert "duplicate entry" in " ".join(problems)


# ---------------------------------------------------------------------------
# 3. ZIP structure
# ---------------------------------------------------------------------------

def test_a_well_formed_archive_yields_its_root():
    vr = _vr()
    problems = []
    assert vr.validate_zip_structure(problems, REQUIRED) == ROOTDIR
    assert problems == []


def test_empty_zip_is_classified():
    vr = _vr()
    problems = []
    assert vr.validate_zip_structure(problems, []) is None
    assert "EMPTY_ZIP" in " ".join(problems)


@pytest.mark.parametrize("member", [
    "/etc/passwd",
    f"{ROOTDIR}/../escape.txt",
    "../outside.txt",
    f"{ROOTDIR}\\windows\\path.txt",
    "C:/abs.txt",
])
def test_hostile_member_names_are_refused(member):
    vr = _vr()
    problems = []
    assert vr.validate_zip_structure(problems, REQUIRED + [member]) is None
    assert "INVALID_ZIP_STRUCTURE" in " ".join(problems)


def test_duplicate_member_names_are_refused():
    vr = _vr()
    problems = []
    assert vr.validate_zip_structure(
        problems, REQUIRED + [REQUIRED[0]]) is None
    assert "duplicate member" in " ".join(problems)


def test_file_and_directory_of_the_same_name_are_refused():
    vr = _vr()
    problems = []
    assert vr.validate_zip_structure(
        problems, REQUIRED + [f"{ROOTDIR}/thing", f"{ROOTDIR}/thing/"]
    ) is None
    assert "both a file and a directory" in " ".join(problems)


def test_more_than_one_top_level_root_is_refused():
    vr = _vr()
    problems = []
    assert vr.validate_zip_structure(
        problems, REQUIRED + ["OTHER_ROOT/file.txt"]) is None
    assert "exactly one top-level" in " ".join(problems)


@pytest.mark.parametrize("missing", list(range(3)))
def test_missing_required_member_is_classified(missing):
    vr = _vr()
    members = [m for i, m in enumerate(REQUIRED) if i != missing]
    problems = []
    assert vr.validate_zip_structure(problems, members) is None
    assert "MISSING_REQUIRED_ZIP_MEMBER" in " ".join(problems)


def test_directory_entries_do_not_satisfy_required_members():
    """A directory named like a required file must not count as that file."""
    vr = _vr()
    members = REQUIRED[:-1] + [f"{ROOTDIR}/{RT.CANONICAL_POLICY_PATH}/"]
    problems = []
    assert vr.validate_zip_structure(problems, members) is None
    assert "MISSING_REQUIRED_ZIP_MEMBER" in " ".join(problems)


def test_payload_digest_cannot_be_collapsed_by_ambiguous_members():
    """Why the structural check matters for the digest specifically.

    Payload recomputation keys members by relative path. Two members that
    normalize to one key would collapse into a single dict entry and change
    what the digest covers; the structural validator refuses that shape before
    any digest is computed.
    """
    vr = _vr()
    problems = []
    assert vr.validate_zip_structure(
        problems, REQUIRED + [f"{ROOTDIR}/a.py", f"{ROOTDIR}/a.py"]) is None
    assert problems


# ---------------------------------------------------------------------------
# 4. End to end: a hostile bundle produces a refusal, never a traceback.
# ---------------------------------------------------------------------------

def _run(zp, bundle, cwd):
    import subprocess
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "verify_release.py"),
         "--zip", str(zp), "--bundle", str(bundle)],
        cwd=cwd, capture_output=True, text=True, timeout=600)


@pytest.mark.parametrize("break_it", [
    {"release_index.json": "{oops"},
    {"SHA256SUMS": "garbage line\n"},
    {"sbom.cdx.json": ""},
    {"provenance.intoto.json": "[]"},
    {"release_index.json": None},
])
def test_hostile_bundle_never_produces_a_traceback(tmp_path, break_it):
    zp = tmp_path / "QTA_source.zip"
    with zipfile.ZipFile(zp, "w") as z:
        for m in REQUIRED:
            z.writestr(m, "x")
    b = good_bundle(tmp_path, **break_it)
    r = _run(zp, b, tmp_path)
    assert r.returncode == 1
    assert "Traceback" not in r.stderr, r.stderr[-800:]
    assert any(c in r.stdout for c in _vr().CANDIDATE_FAILURES), r.stdout


def test_hostile_zip_never_produces_a_traceback(tmp_path):
    zp = tmp_path / "QTA_source.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("rootA/x.txt", "1")
        z.writestr("rootB/y.txt", "2")
    b = good_bundle(tmp_path)
    r = _run(zp, b, tmp_path)
    assert r.returncode == 1
    assert "Traceback" not in r.stderr, r.stderr[-800:]


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")
         and callable(v)]
