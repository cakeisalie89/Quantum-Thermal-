"""Phase 2 must answer hostile input with a classified refusal, end to end.

The unit tests next door prove each validator refuses its own malformation.
This suite proves the CLAIM the verifier makes about itself: that driving the
real entry point with a hostile artifact yields a named failure and never a
Python traceback.

The distinction is not cosmetic. A traceback exits non-zero, so a shallow
reading calls it "fail closed" -- but it carries no classification, abandons
the remaining diagnostics, and is indistinguishable from a broken verifier. A
consumer holding a truncated download and a consumer holding a tampered index
would receive the same unhelpful crash.

Every case below was reproduced as an uncaught exception before the
validation existed; the comment on each records which one.

Each fixture starts from a COMPLETE, coherent release and breaks exactly one
thing. `test_the_baseline_fixture_is_otherwise_healthy` is the guard that
keeps that true: without it, a case could "pass" by failing earlier for an
unrelated reason and prove nothing about the field it names.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import release_trust as RT   # noqa: E402

POLICY_REL = str(RT.CANONICAL_POLICY_PATH)
ZIPROOT = "QTA_source"


def _vr():
    spec = importlib.util.spec_from_file_location(
        "_vr_hostile", os.path.join(ROOT, "verify_release.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["_vr_hostile"] = m
    spec.loader.exec_module(m)
    return m


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def build(tmp_path, *, index=None, sbom=None, prov=None, zip_mode="ok",
          drop=None):
    """A coherent release, then exactly one mutation."""
    d = tmp_path / "rel"
    if d.exists():
        shutil.rmtree(d)
    b = d / "bundle"
    b.mkdir(parents=True)
    zp = d / "QTA_source.zip"

    policy_src = os.path.join(ROOT, POLICY_REL)
    members = {
        f"{ZIPROOT}/uv.lock": b'name = "numpy"\nversion = "1.0"\n',
        f"{ZIPROOT}/results_gate_table.csv": b"gate,status\nB4,CONDITIONAL\n",
        f"{ZIPROOT}/{POLICY_REL}": open(policy_src, "rb").read(),
    }
    if zip_mode == "ok":
        with zipfile.ZipFile(zp, "w") as z:
            for n, v in members.items():
                z.writestr(n, v)
    elif zip_mode == "missing":
        pass
    elif zip_mode == "directory":
        zp.mkdir()
    elif zip_mode == "broken_symlink":
        os.symlink(str(d / "nowhere.zip"), str(zp))
    elif zip_mode == "not_an_archive":
        zp.write_bytes(b"#!/bin/sh\necho definitely not a zip\n")
    elif zip_mode == "truncated":
        with zipfile.ZipFile(zp, "w") as z:
            for n, v in members.items():
                z.writestr(n, v)
        raw = zp.read_bytes()
        zp.write_bytes(raw[:len(raw) // 2])
    elif zip_mode == "empty_archive":
        with zipfile.ZipFile(zp, "w"):
            pass
    elif zip_mode == "required_member_is_a_directory":
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr(f"{ZIPROOT}/uv.lock", b"x")
            z.writestr(f"{ZIPROOT}/results_gate_table.csv", b"x")
            z.writestr(f"{ZIPROOT}/{POLICY_REL}/", b"")
    elif zip_mode in ("invalid_utf8_lock", "invalid_utf8_gate"):
        target = (f"{ZIPROOT}/uv.lock" if zip_mode == "invalid_utf8_lock"
                  else f"{ZIPROOT}/results_gate_table.csv")
        with zipfile.ZipFile(zp, "w") as z:
            for n, v in members.items():
                z.writestr(n, b"\xff\xfe not utf-8 \xc3\x28" if n == target
                           else v)
    elif zip_mode == "two_roots":
        with zipfile.ZipFile(zp, "w") as z:
            for n, v in members.items():
                z.writestr(n, v)
            z.writestr("OTHER/x.txt", b"1")
    else:                                        # pragma: no cover
        raise AssertionError(f"unknown zip_mode {zip_mode!r}")

    zb = zp.read_bytes() if (zp.exists() and zp.is_file()) else b""
    zh = _sha(zb)

    idx = {
        "release_artifact": {"name": "QTA_source.zip", "size": len(zb),
                             "sha256": zh},
        "files": [{"name": "QTA_source.zip", "sha256": zh}],
        "claims": {"scientific_gate_PASS_count": 0},
        "provenance": {"slsa_level_claimed": "NONE"},
        "signing_status": "PENDING",
        "signature_bundles": [],
    }
    if index is not None:
        idx = index(idx) or idx
    sb = {"components": [{"name": "numpy", "version": "1.0"}]}
    if sbom is not None:
        sb = sbom(sb) or sb
    pv = {"subject": [{"name": "QTA_source.zip", "digest": {"sha256": zh}}],
          "predicate": {"slsa_level_claimed": "NONE"}}
    if prov is not None:
        pv = prov(pv) or pv

    written = {
        "release_index.json": json.dumps(idx),
        "SHA256SUMS": f"{zh}  QTA_source.zip\n",
        "sbom.cdx.json": json.dumps(sb),
        "provenance.intoto.json": json.dumps(pv),
        "release_trust_policy.json": open(policy_src).read(),
    }
    for name, body in written.items():
        if name == drop:
            continue
        (b / name).write_text(body)
    return zp, b, d


def _forge_declared_size(raw: bytes, suffix: bytes, size: int) -> bytes:
    """Rewrite the uncompressed size in the central directory entry.

    Central-directory file header: signature at 0, uncompressed size at 24,
    filename length at 28, filename at 46.
    """
    buf = bytearray(raw)
    i = 0
    patched = 0
    while True:
        i = buf.find(b"PK\x01\x02", i)
        if i < 0:
            break
        n = struct.unpack_from("<H", buf, i + 28)[0]
        if bytes(buf[i + 46:i + 46 + n]).endswith(suffix):
            struct.pack_into("<I", buf, i + 24, size)
            patched += 1
        i += 4
    assert patched == 1, f"patched {patched} central-directory entries"
    return bytes(buf)


def run(zp, b, cwd, *args):
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "verify_release.py"),
         "--zip", str(zp), "--bundle", str(b), *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=600)


def assert_classified_refusal(r):
    """The whole contract, in one place."""
    assert "Traceback" not in r.stderr, r.stderr[-1200:]
    assert r.returncode == 1, r.stdout[-600:]
    named = [c for c in _vr().CANDIDATE_FAILURES if c in r.stdout]
    assert named, f"refused without a classification:\n{r.stdout[-900:]}"
    return named


# ---------------------------------------------------------------------------
# The guard. Without this, every case below could be passing for the wrong
# reason.
# ---------------------------------------------------------------------------

def test_the_baseline_fixture_is_otherwise_healthy(tmp_path):
    """The unmutated fixture must clear all of phase 2.

    It does not verify overall -- there is no local canonical policy match and
    no signature -- but it must not trip any CANDIDATE_FAILURE. If it did,
    every negative case below would be failing early for a reason unrelated to
    the field it claims to test.
    """
    zp, b, d = build(tmp_path)
    r = run(zp, b, d)
    assert "Traceback" not in r.stderr, r.stderr[-1200:]
    named = [c for c in _vr().CANDIDATE_FAILURES if c in r.stdout]
    assert named == [], f"baseline is not clean: {named}\n{r.stdout[-900:]}"
    assert "release zip digest matches index" in r.stdout


# ---------------------------------------------------------------------------
# A / H / I: the archive path itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,expect", [
    # reproduced: FileNotFoundError at `zb = zip_path.read_bytes()`
    ("missing", "MISSING_RELEASE_ZIP"),
    # reproduced: IsADirectoryError at the same line
    ("directory", "UNREADABLE_RELEASE_ZIP"),
    # reproduced: OSError ELOOP / ENOENT at the same line
    ("broken_symlink", "UNREADABLE_RELEASE_ZIP"),
    ("not_an_archive", "INVALID_RELEASE_ZIP"),
    ("truncated", "INVALID_RELEASE_ZIP"),
    ("empty_archive", "EMPTY_ZIP"),
    ("required_member_is_a_directory", "MISSING_REQUIRED_ZIP_MEMBER"),
    ("two_roots", "INVALID_ZIP_STRUCTURE"),
])
def test_hostile_archive_path_is_classified(tmp_path, mode, expect):
    zp, b, d = build(tmp_path, zip_mode=mode)
    r = run(zp, b, d)
    assert expect in assert_classified_refusal(r), r.stdout[-900:]


def test_a_missing_zip_names_the_path_it_looked_for(tmp_path):
    """A classification a consumer can act on, not merely a nonzero exit."""
    zp, b, d = build(tmp_path, zip_mode="missing")
    r = run(zp, b, d)
    assert_classified_refusal(r)
    assert "QTA_source.zip" in r.stdout


# ---------------------------------------------------------------------------
# B / C / D: release_index.json
# ---------------------------------------------------------------------------

def _mut(fn):
    def apply(doc):
        fn(doc)
        return doc
    return apply


@pytest.mark.parametrize("desc,fn", [
    # reproduced: KeyError 'name' / 'sha256' / 'size'
    ("artifact_no_name", lambda i: i["release_artifact"].pop("name")),
    ("artifact_no_sha", lambda i: i["release_artifact"].pop("sha256")),
    ("artifact_no_size", lambda i: i["release_artifact"].pop("size")),
    ("artifact_not_dict", lambda i: i.__setitem__("release_artifact", "x")),
    ("artifact_name_int", lambda i: i["release_artifact"].__setitem__(
        "name", 7)),
    ("artifact_size_str", lambda i: i["release_artifact"].__setitem__(
        "size", "1")),
    # reproduced: KeyError 'name' inside the files set comprehension
    ("files_empty_record", lambda i: i["files"].append({})),
    # reproduced: TypeError: 'NoneType' object is not subscriptable
    ("files_null_record", lambda i: i["files"].append(None)),
    # reproduced: TypeError: string indices must be integers
    ("files_string_record", lambda i: i["files"].append("nope")),
    # reproduced: KeyError 'sha256'
    ("files_name_only", lambda i: i["files"].append({"name": "x"})),
    # reproduced: TypeError: unhashable type: 'list'
    ("files_unhashable_name", lambda i: i["files"].append(
        {"name": ["x"], "sha256": "a" * 64})),
    ("files_bad_digest", lambda i: i["files"].append(
        {"name": "x", "sha256": "not-a-digest"})),
    ("files_not_a_list", lambda i: i.__setitem__("files", {})),
    ("files_duplicate_name", lambda i: i["files"].append(
        {"name": "QTA_source.zip", "sha256": "b" * 64})),
    # reproduced: KeyError 'provenance'
    ("no_provenance_block", lambda i: i.pop("provenance")),
    # reproduced: AttributeError: 'str' object has no attribute 'get'
    ("provenance_not_dict", lambda i: i.__setitem__("provenance", "x")),
    ("claims_missing", lambda i: i.pop("claims")),
    ("claims_not_dict", lambda i: i.__setitem__("claims", [])),
    ("signature_bundles_not_a_list", lambda i: i.__setitem__(
        "signature_bundles", "x")),
])
def test_hostile_release_index_is_classified(tmp_path, desc, fn):
    zp, b, d = build(tmp_path, index=_mut(fn))
    r = run(zp, b, d)
    assert "INVALID_RELEASE_INDEX" in assert_classified_refusal(r), \
        r.stdout[-900:]


@pytest.mark.parametrize("body", ["{oops", "", "[1,2,3]", '"str"', "null"])
def test_unparseable_release_index_is_classified(tmp_path, body):
    zp, b, d = build(tmp_path)
    (b / "release_index.json").write_text(body)
    r = run(zp, b, d)
    assert "INVALID_RELEASE_INDEX" in assert_classified_refusal(r)


@pytest.mark.parametrize("name,expect", [
    ("release_index.json", "MISSING_RELEASE_INDEX"),
    ("SHA256SUMS", "MISSING_SHA256SUMS"),
    ("sbom.cdx.json", "MISSING_SBOM"),
    ("provenance.intoto.json", "MISSING_PROVENANCE"),
    # reproduced: FileNotFoundError inside the metadata-scan genexpr
    ("release_trust_policy.json", "MISSING_TRUST_POLICY"),
])
def test_missing_bundle_member_is_classified(tmp_path, name, expect):
    zp, b, d = build(tmp_path, drop=name)
    r = run(zp, b, d)
    assert expect in assert_classified_refusal(r), r.stdout[-900:]


# ---------------------------------------------------------------------------
# E: SBOM
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("desc,fn", [
    # reproduced: KeyError 'name' inside the sbom set comprehension
    ("component_empty", lambda s: s["components"].append({})),
    # reproduced: TypeError: 'NoneType' object is not subscriptable
    ("component_null", lambda s: s["components"].append(None)),
    # reproduced: KeyError 'version'
    ("component_no_version", lambda s: s["components"].append({"name": "n"})),
    ("component_no_name", lambda s: s["components"].append({"version": "1"})),
    ("component_name_int", lambda s: s["components"].append(
        {"name": 3, "version": "1"})),
    ("components_not_a_list", lambda s: s.__setitem__("components", {})),
    ("components_absent", lambda s: s.pop("components")),
])
def test_hostile_sbom_is_classified(tmp_path, desc, fn):
    zp, b, d = build(tmp_path, sbom=_mut(fn))
    r = run(zp, b, d)
    assert "INVALID_SBOM" in assert_classified_refusal(r), r.stdout[-900:]


# ---------------------------------------------------------------------------
# F / G: provenance
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("desc,fn", [
    # reproduced: KeyError 'name' inside the subject set comprehension
    ("subject_empty", lambda p: p["subject"].append({})),
    # reproduced: TypeError: 'NoneType' object is not subscriptable
    ("subject_null", lambda p: p["subject"].append(None)),
    # reproduced: KeyError 'digest'
    ("subject_no_digest", lambda p: p["subject"].append({"name": "n"})),
    # reproduced: TypeError: string indices must be integers
    ("subject_digest_is_a_string", lambda p: p["subject"].append(
        {"name": "n", "digest": "sha"})),
    ("subject_digest_no_sha256", lambda p: p["subject"].append(
        {"name": "n", "digest": {}})),
    ("subject_bad_sha256", lambda p: p["subject"].append(
        {"name": "n", "digest": {"sha256": "zz"}})),
    ("subject_not_a_list", lambda p: p.__setitem__("subject", {})),
    ("predicate_not_a_dict", lambda p: p.__setitem__("predicate", [])),
    ("predicate_absent", lambda p: p.pop("predicate")),
    ("non_string_slsa_claim", lambda p: p["predicate"].__setitem__(
        "slsa_level_claimed", 3)),
])
def test_hostile_provenance_is_classified(tmp_path, desc, fn):
    zp, b, d = build(tmp_path, prov=_mut(fn))
    r = run(zp, b, d)
    assert "INVALID_PROVENANCE" in assert_classified_refusal(r), \
        r.stdout[-900:]


# ---------------------------------------------------------------------------
# Text members. The archive is bytes; treating a member as text is a decode
# that can fail on attacker-controlled input.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["invalid_utf8_lock", "invalid_utf8_gate"])
def test_undecodable_archive_member_is_classified(tmp_path, mode):
    """Reproduced as an uncaught UnicodeDecodeError.

    `zf.read(...).decode()` on a required member is strict by default, so a
    member carrying invalid UTF-8 crashed the verifier.
    """
    zp, b, d = build(tmp_path, zip_mode=mode)
    r = run(zp, b, d)
    assert "INVALID_RELEASE_ZIP" in assert_classified_refusal(r), \
        r.stdout[-900:]
    assert "not valid UTF-8" in r.stdout


def test_an_unreadable_gate_table_is_never_counted_as_zero_pass(tmp_path):
    """The dangerous repair, and why it was not taken.

    Decoding with errors="replace" also stops the crash -- and then counts
    zero PASS rows in the substituted text and reports "scientific PASS count
    = 0". That converts unreadable input into an affirmative safety claim,
    which is strictly worse than the traceback it replaced. Absence of
    evidence is not evidence of absence: the count must come back UNKNOWN, and
    UNKNOWN must fail.
    """
    zp, b, d = build(tmp_path, zip_mode="invalid_utf8_gate")
    r = run(zp, b, d)
    assert_classified_refusal(r)
    assert "scientific PASS count = 0" not in r.stdout
    assert "cannot be recomputed" in r.stdout
    assert "never treated as zero" in r.stdout


def test_an_unreadable_lock_file_does_not_pass_the_sbom_check_by_default(
        tmp_path):
    """Skipping a comparison is not the same as the comparison succeeding."""
    zp, b, d = build(tmp_path, zip_mode="invalid_utf8_lock")
    r = run(zp, b, d)
    assert_classified_refusal(r)
    assert "SBOM matches uv.lock" not in r.stdout
    assert "SBOM cannot be checked against uv.lock" in r.stdout


# ---------------------------------------------------------------------------
# Resource exhaustion. Not a shape defect -- an input that is perfectly
# well-formed and simply too large, or too deep, for the parser. It is still
# hostile input arriving at the same place, so it still gets a classification
# rather than a MemoryError or a RecursionError.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expect", [
    ("release_index.json", "INVALID_RELEASE_INDEX"),
    ("sbom.cdx.json", "INVALID_SBOM"),
    ("provenance.intoto.json", "INVALID_PROVENANCE"),
])
def test_deeply_nested_json_is_classified(tmp_path, name, expect):
    """Reproduced as an uncaught RecursionError from json.loads.

    RecursionError is not a ValueError, so the decoder's exception handler --
    which named UnicodeDecodeError and JSONDecodeError -- did not see it. A
    document a few hundred kilobytes long, trivial to author, produced the
    exact traceback this layer exists to prevent.
    """
    zp, b, d = build(tmp_path)
    (b / name).write_text("[" * 60000 + "]" * 60000)
    r = run(zp, b, d)
    assert expect in assert_classified_refusal(r), r.stdout[-900:]


def test_oversized_release_index_is_classified(tmp_path):
    """The oversized document must be VALID apart from its size.

    An earlier version of this test wrote 64 MiB of "x", which is refused as
    unparseable JSON whether or not the size bound exists -- so it passed
    against a build with the bound deleted and proved nothing. The padding
    below keeps the document well-formed, leaving size as the only reason to
    refuse it.
    """
    vr = _vr()
    zp, b, d = build(tmp_path)
    doc = json.loads((b / "release_index.json").read_text())
    doc["pad"] = "x" * (vr.MAX_METADATA_BYTES + 1)
    (b / "release_index.json").write_text(json.dumps(doc))
    assert (b / "release_index.json").stat().st_size > vr.MAX_METADATA_BYTES
    assert json.loads((b / "release_index.json").read_text())["pad"]
    r = run(zp, b, d)
    assert "INVALID_RELEASE_INDEX" in assert_classified_refusal(r), \
        r.stdout[-900:]
    assert "structural bound" in r.stdout


def test_oversized_sha256sums_is_classified(tmp_path):
    """Every line well-formed; only the total size is out of bounds."""
    vr = _vr()
    zp, b, d = build(tmp_path)
    line = 0
    with open(b / "SHA256SUMS", "w") as fh:
        while line * 75 <= vr.MAX_METADATA_BYTES:
            fh.write(f"{line:064x}  file_{line}\n")
            line += 1
    assert (b / "SHA256SUMS").stat().st_size > vr.MAX_METADATA_BYTES
    # Prove the content is otherwise parseable, so size is the only defect.
    probe = []
    assert vr.parse_sha256sums(probe, b / "SHA256SUMS") is None
    assert "structural bound" in " ".join(probe), probe
    r = run(zp, b, d)
    assert "INVALID_SHA256SUMS" in assert_classified_refusal(r), \
        r.stdout[-900:]


def test_a_decompression_bomb_is_refused_before_it_is_expanded(tmp_path):
    """The central directory is believed about SIZE, never about content.

    Reading declared sizes costs nothing and expands nothing; the refusal
    happens before any member is decompressed. A bomb that lied downward about
    its size would still be caught -- by the digest checks, which is where
    lying about content belongs.
    """
    vr = _vr()
    zp, b, d = build(tmp_path)
    big = vr.MAX_DECLARED_UNCOMPRESSED_BYTES + 1
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{ZIPROOT}/uv.lock", b"x")
        z.writestr(f"{ZIPROOT}/results_gate_table.csv", b"x")
        z.writestr(f"{ZIPROOT}/{POLICY_REL}", b"{}")
        z.writestr(f"{ZIPROOT}/bomb.bin", b"x" * 16)
    # writestr recomputes the size from the data it wrote, so the declared
    # size has to be forged in the central directory itself -- which is
    # precisely what a real bomb does.
    zp.write_bytes(_forge_declared_size(zp.read_bytes(), b"bomb.bin", big))
    with zipfile.ZipFile(zp) as z:
        assert max(i.file_size for i in z.infolist()) == big, \
            "fixture did not actually forge an oversized declaration"
    r = run(zp, b, d)
    assert "INVALID_RELEASE_ZIP" in assert_classified_refusal(r), \
        r.stdout[-900:]
    assert "structural bound" in r.stdout


def test_the_structural_bounds_cannot_reject_a_healthy_release(tmp_path):
    """The bounds must be unreachable in normal operation.

    A guard that a legitimate release could trip would be a liability, not a
    protection. Both bounds sit orders of magnitude above the real artifact.
    """
    vr = _vr()
    zp, b, d = build(tmp_path)
    assert zp.stat().st_size < vr.MAX_DECLARED_UNCOMPRESSED_BYTES / 1000
    for name in ("release_index.json", "SHA256SUMS", "sbom.cdx.json",
                 "provenance.intoto.json"):
        assert (b / name).stat().st_size < vr.MAX_METADATA_BYTES / 1000
    r = run(zp, b, d)
    assert [c for c in vr.CANDIDATE_FAILURES if c in r.stdout] == []


# ---------------------------------------------------------------------------
# The trust root is unaffected by any of this.
# ---------------------------------------------------------------------------

def test_hostile_input_never_reaches_online_without_a_trust_root(tmp_path):
    """Phase 1 still runs first, and still fails closed.

    Structural validation must not have created a path where a hostile
    candidate is parsed far enough to matter before the external root is
    demanded.
    """
    zp, b, d = build(tmp_path, index=_mut(lambda i: i["files"].append(None)))
    r = run(zp, b, d, "--online")
    assert "Traceback" not in r.stderr, r.stderr[-1200:]
    assert r.returncode == 1
    assert "externally supplied trust root" in r.stdout
    # Phase 2 never ran: the malformed index is not what was reported.
    assert "INVALID_RELEASE_INDEX" not in r.stdout


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")
         and callable(v)]
