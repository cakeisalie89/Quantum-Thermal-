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
import pathlib
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
ROOT_PATH = pathlib.Path(ROOT)

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


#: The whole-bundle baseline every fixture starts from. It must be VALID:
#: a test that breaks one field proves nothing about that field if some other
#: field was already malformed and refused first.
def good_index(**over) -> dict:
    idx = {
        "release_artifact": {"name": "QTA_source.zip", "size": 1,
                             "sha256": "a" * 64},
        "files": [], "claims": {"scientific_gate_PASS_count": 0},
        "provenance": {"slsa_level_claimed": "NONE"},
        "signing_status": "PENDING",
    }
    idx.update(over)
    return idx


def good_bundle(tmp_path, **over):
    """A structurally valid candidate bundle."""
    b = tmp_path / "bundle"
    b.mkdir(parents=True, exist_ok=True)
    files = {
        "release_index.json": json.dumps(good_index()),
        "SHA256SUMS": f"{'a' * 64}  QTA_source.zip\n",
        "sbom.cdx.json": json.dumps({"components": []}),
        "provenance.intoto.json": json.dumps({"subject": [],
                                              "predicate": {}}),
        "release_trust_policy.json": (
            ROOT_PATH / RT.CANONICAL_POLICY_PATH).read_text(),
    }
    files.update(over)
    for name, body in files.items():
        if body is None:
            continue
        (b / name).write_text(body)
    return b


def baseline_is_valid(tmp_path) -> None:
    """Guard for every negative test: prove the fixture starts clean."""
    problems = []
    assert _vr().parse_candidate_bundle(problems, good_bundle(tmp_path)) \
        is not None, problems


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
    assert isinstance(out, vr.CandidateBundle)
    # The derived collections exist BEFORE any consumer asks for them.
    assert out.index_files == frozenset()
    assert out.sbom_packages == frozenset()
    assert out.subjects == frozenset()
    assert out.artifact_name == "QTA_source.zip"


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


@pytest.mark.parametrize("drop,over", [
    ("release_artifact", {}),
    (None, {"release_artifact": "str"}),
    (None, {"files": {}}),
    (None, {"claims": []}),
    ("provenance", {}),
    (None, {"provenance": "str"}),
    (None, {"signature_bundles": "not-a-list"}),
])
def test_index_with_wrong_field_types_is_classified(tmp_path, drop, over):
    baseline_is_valid(tmp_path)
    vr = _vr()
    idx = good_index(**over)
    if drop:
        idx.pop(drop)
    problems = []
    assert vr.parse_candidate_bundle(
        problems, good_bundle(tmp_path,
                              **{"release_index.json": json.dumps(idx)})
    ) is None
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
# 1b. Nested records.
#
# Outer-container typing ("files is a list") used to be the whole check, so a
# malformed RECORD was first met by a set comprehension -- an expression with
# no vocabulary for refusal. Each case below was reproduced as an uncaught
# KeyError or TypeError before this validation existed.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", [
    {},                                        # KeyError: 'name'
    None,                                      # TypeError: not subscriptable
    "a-string",                                # TypeError: string indices
    ["a", "b"],                                # TypeError: list indices
    {"name": "x"},                             # KeyError: 'sha256'
    {"sha256": "a" * 64},                      # KeyError: 'name'
    {"name": ["x"], "sha256": "a" * 64},       # TypeError: unhashable
    {"name": "x", "sha256": "not-hex"},
    {"name": "x", "sha256": "A" * 64},         # uppercase is not canonical
    {"name": "x", "sha256": "a" * 63},
    {"name": "", "sha256": "a" * 64},
    {"name": "   ", "sha256": "a" * 64},
    {"name": "x", "sha256": None},
    {"name": 7, "sha256": "a" * 64},
])
def test_malformed_index_file_record_is_classified(tmp_path, entry):
    baseline_is_valid(tmp_path)
    vr = _vr()
    idx = good_index(files=[entry])
    problems = []
    assert vr.parse_candidate_bundle(
        problems, good_bundle(
            tmp_path, **{"release_index.json": json.dumps(idx)})) is None
    assert "INVALID_RELEASE_INDEX" in " ".join(problems), problems


def test_duplicate_index_file_names_are_refused(tmp_path):
    """A set comprehension would silently absorb the contradiction.

    Two records naming the same file with different digests collapse into two
    distinct set members -- or, with equal digests, into one -- and either way
    the list's internal contradiction never surfaces. It must be refused.
    """
    baseline_is_valid(tmp_path)
    vr = _vr()
    idx = good_index(files=[{"name": "d", "sha256": "a" * 64},
                            {"name": "d", "sha256": "b" * 64}])
    problems = []
    assert vr.parse_candidate_bundle(
        problems, good_bundle(
            tmp_path, **{"release_index.json": json.dumps(idx)})) is None
    assert "twice" in " ".join(problems)


@pytest.mark.parametrize("field,bad", [
    ("name", 7), ("name", None), ("name", ""), ("name", {}),
    ("sha256", "zz" * 32), ("sha256", 1), ("sha256", None),
    ("size", "1"), ("size", -1), ("size", None), ("size", 1.5),
    ("size", True),          # bool is an int subclass; still malformed
])
def test_malformed_release_artifact_field_is_classified(tmp_path, field, bad):
    baseline_is_valid(tmp_path)
    vr = _vr()
    art = dict(good_index()["release_artifact"])
    art[field] = bad
    problems = []
    assert vr.parse_candidate_bundle(
        problems, good_bundle(tmp_path, **{
            "release_index.json": json.dumps(
                good_index(release_artifact=art))})) is None
    assert "INVALID_RELEASE_INDEX" in " ".join(problems), problems


@pytest.mark.parametrize("field", ["name", "sha256", "size"])
def test_missing_release_artifact_field_is_classified(tmp_path, field):
    baseline_is_valid(tmp_path)
    vr = _vr()
    art = dict(good_index()["release_artifact"])
    art.pop(field)
    problems = []
    assert vr.parse_candidate_bundle(
        problems, good_bundle(tmp_path, **{
            "release_index.json": json.dumps(
                good_index(release_artifact=art))})) is None
    assert field in " ".join(problems)


@pytest.mark.parametrize("comp", [
    {}, None, "str", ["x"], {"name": "n"}, {"version": "1"},
    {"name": "n", "version": None}, {"name": "n", "version": ""},
    {"name": "", "version": "1"}, {"name": 3, "version": "1"},
])
def test_malformed_sbom_component_is_classified(tmp_path, comp):
    baseline_is_valid(tmp_path)
    vr = _vr()
    problems = []
    assert vr.parse_candidate_bundle(
        problems, good_bundle(tmp_path, **{
            "sbom.cdx.json": json.dumps({"components": [comp]})})) is None
    assert "INVALID_SBOM" in " ".join(problems), problems


@pytest.mark.parametrize("subj", [
    {}, None, "str", ["x"],
    {"name": "n"},                                  # no digest
    {"name": "n", "digest": "sha"},                 # digest not an object
    {"name": "n", "digest": {}},                    # no sha256
    {"name": "n", "digest": {"sha256": "nope"}},
    {"name": "n", "digest": {"sha256": None}},
    {"name": "", "digest": {"sha256": "a" * 64}},
    {"digest": {"sha256": "a" * 64}},               # no name
])
def test_malformed_provenance_subject_is_classified(tmp_path, subj):
    baseline_is_valid(tmp_path)
    vr = _vr()
    problems = []
    assert vr.parse_candidate_bundle(
        problems, good_bundle(tmp_path, **{
            "provenance.intoto.json": json.dumps(
                {"subject": [subj], "predicate": {}})})) is None
    assert "INVALID_PROVENANCE" in " ".join(problems), problems


def test_duplicate_provenance_subject_names_are_refused(tmp_path):
    baseline_is_valid(tmp_path)
    vr = _vr()
    problems = []
    subs = [{"name": "d", "digest": {"sha256": "a" * 64}},
            {"name": "d", "digest": {"sha256": "b" * 64}}]
    assert vr.parse_candidate_bundle(
        problems, good_bundle(tmp_path, **{
            "provenance.intoto.json": json.dumps(
                {"subject": subs, "predicate": {}})})) is None
    assert "twice" in " ".join(problems)


@pytest.mark.parametrize("lvl", [3, ["3"], {"a": 1}])
def test_non_string_slsa_claim_is_classified(tmp_path, lvl):
    """A non-string level must be refused, not compared against 'NONE'.

    The level is compared with `not in (None, "NONE")`, which a list or dict
    passes silently -- so an unauthorized claim in a non-string shape would
    have been read as an absent claim.
    """
    baseline_is_valid(tmp_path)
    vr = _vr()
    problems = []
    assert vr.parse_candidate_bundle(
        problems, good_bundle(tmp_path, **{
            "provenance.intoto.json": json.dumps(
                {"subject": [],
                 "predicate": {"slsa_level_claimed": lvl}})})) is None
    assert "INVALID_PROVENANCE" in " ".join(problems)


def test_missing_bundled_trust_policy_is_classified(tmp_path):
    """Reproduced as FileNotFoundError inside the metadata-scan genexpr."""
    baseline_is_valid(tmp_path)
    vr = _vr()
    problems = []
    b = good_bundle(tmp_path)
    (b / "release_trust_policy.json").unlink()
    assert vr.parse_candidate_bundle(problems, b) is None
    assert "MISSING_TRUST_POLICY" in " ".join(problems)


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
