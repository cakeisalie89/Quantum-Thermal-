"""The bundled trust policy is attacker-controlled bytes.

It was the last candidate structure parsed outside `CandidateBundle`. Phase 2
checked only `path.exists()`, and four later sites re-opened and re-parsed the
same file with four different exception lists -- so whether a hostile policy
crashed the verifier depended on which caller reached it first. A policy that
was a directory crashed `enforce_trust_policy`; one nested deeply enough to
exhaust the stack crashed both the offline path and the digest-only root; and
the externally supplied trust root crashed on invalid UTF-8.

Two claims are under test here, and they are different claims:

  STRUCTURAL   the document can be handled safely -- it parsed, it is an
               object, it matches the schema. Established in phase 0.
  AUTHORIZED   someone the consumer trusts vouched for these exact bytes.
               Established only by an external digest or an external file.

Schema-valid is not trusted. But untrusted does not mean allowed to crash the
verifier.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import release_trust as RT   # noqa: E402

POLICY_REL = str(RT.CANONICAL_POLICY_PATH)
ZIPROOT = "QTA_source"
CANON_TEXT = open(os.path.join(ROOT, POLICY_REL)).read()
CANON = json.loads(CANON_TEXT)
GOOD_DIGEST = RT.policy_digest(RT.canonical_bytes(CANON))

#: Deep enough to exhaust the decoder's stack; a few hundred KiB of text.
DEEP_JSON = "[" * 60000 + "]" * 60000


def _vr():
    spec = importlib.util.spec_from_file_location(
        "_vr_pol", os.path.join(ROOT, "verify_release.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["_vr_pol"] = m
    spec.loader.exec_module(m)
    return m


def oversized_policy() -> str:
    vr = _vr()
    return json.dumps({**CANON, "note": "x" * (vr.MAX_METADATA_BYTES + 8)})


#: Each case: how to write the policy, and the code it must be refused with.
#: "DIR" and "LOOP" are filesystem shapes, not contents.
HOSTILE = {
    "directory":       ("DIR", "UNREADABLE_TRUST_POLICY"),
    "symlink_loop":    ("LOOP", "UNREADABLE_TRUST_POLICY"),
    "invalid_utf8":    (b"\xff\xfe{\"schema_version\": \"3.0.0\"}",
                        "INVALID_TRUST_POLICY"),
    "malformed_json":  ("{not json,,", "INVALID_TRUST_POLICY"),
    "deep_nesting":    (DEEP_JSON, "INVALID_TRUST_POLICY"),
    "not_an_object":   ("[1, 2, 3]", "INVALID_TRUST_POLICY"),
    "unknown_field":   (json.dumps({**CANON, "surprise": 1}),
                        "INVALID_TRUST_POLICY"),
    "empty":           ("", "INVALID_TRUST_POLICY"),
}


def write_policy(path, body):
    if body == "DIR":
        path.mkdir()
    elif body == "LOOP":
        os.symlink(str(path), str(path))
    elif isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body)


def build(tmp_path, policy_body=CANON_TEXT):
    """A coherent release; only the BUNDLED policy is mutated."""
    d = tmp_path / "rel"
    if d.exists():
        shutil.rmtree(d)
    b = d / "bundle"
    b.mkdir(parents=True)
    zp = d / "QTA_source.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr(f"{ZIPROOT}/uv.lock", b'name = "n"\nversion = "1"\n')
        z.writestr(f"{ZIPROOT}/results_gate_table.csv", b"gate,status\n")
        z.writestr(f"{ZIPROOT}/{POLICY_REL}", CANON_TEXT.encode())
    zb = zp.read_bytes()
    zh = hashlib.sha256(zb).hexdigest()
    (b / "release_index.json").write_text(json.dumps({
        "release_artifact": {"name": "QTA_source.zip", "size": len(zb),
                             "sha256": zh},
        "files": [{"name": "QTA_source.zip", "sha256": zh}],
        "claims": {"scientific_gate_PASS_count": 0},
        "provenance": {"slsa_level_claimed": "NONE"},
        "signing_status": "PENDING", "signature_bundles": []}))
    (b / "SHA256SUMS").write_text(f"{zh}  QTA_source.zip\n")
    (b / "sbom.cdx.json").write_text(
        json.dumps({"components": [{"name": "n", "version": "1"}]}))
    (b / "provenance.intoto.json").write_text(json.dumps({
        "subject": [{"name": "QTA_source.zip", "digest": {"sha256": zh}}],
        "predicate": {"slsa_level_claimed": "NONE"}}))
    write_policy(b / "release_trust_policy.json", policy_body)
    return zp, b, d


def run(zp, b, cwd, *args):
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "verify_release.py"),
         "--zip", str(zp), "--bundle", str(b), *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=600)


def assert_refused(r, code):
    assert "Traceback" not in r.stderr, r.stderr[-1200:]
    assert r.returncode == 1, r.stdout[-500:]
    assert code in r.stdout, r.stdout[-900:]


# ---------------------------------------------------------------------------
# The guard: the unmutated fixture must clear phase 0 and phase 2 cleanly.
# ---------------------------------------------------------------------------

def test_the_baseline_fixture_is_otherwise_healthy(tmp_path):
    vr = _vr()
    zp, b, d = build(tmp_path)
    r = run(zp, b, d)
    assert "Traceback" not in r.stderr, r.stderr[-1200:]
    named = [c for c in vr.CANDIDATE_FAILURES + vr.TRUST_ROOT_FAILURES
             if c in r.stdout]
    assert named == [], f"baseline is not clean: {named}\n{r.stdout[-900:]}"


# ---------------------------------------------------------------------------
# 1. Offline path.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_hostile_bundled_policy_offline(tmp_path, case):
    body, code = HOSTILE[case]
    zp, b, d = build(tmp_path, body)
    assert_refused(run(zp, b, d), code)


# ---------------------------------------------------------------------------
# 2. Digest-only online path -- the same parser, so the same refusals.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_hostile_bundled_policy_digest_root(tmp_path, case):
    body, code = HOSTILE[case]
    zp, b, d = build(tmp_path, body)
    assert_refused(
        run(zp, b, d, "--online", "--trusted-policy-sha256", GOOD_DIGEST),
        code)


def test_oversized_bundled_policy_is_refused(tmp_path):
    """Valid JSON, valid schema, simply too large. Size is the only defect."""
    body = oversized_policy()
    json.loads(body)                      # prove it is well formed
    zp, b, d = build(tmp_path, body)
    r = run(zp, b, d)
    assert_refused(r, "INVALID_TRUST_POLICY")
    assert "structural bound" in r.stdout


# ---------------------------------------------------------------------------
# 3. External file trust root. Owner-supplied, still never a crash.
# ---------------------------------------------------------------------------

EXTERNAL = {
    "directory":      ("DIR", "UNREADABLE_TRUST_ROOT"),
    "symlink_loop":   ("LOOP", "UNREADABLE_TRUST_ROOT"),
    "missing":        (None, "MISSING_TRUST_ROOT"),
    "invalid_utf8":   (b"\xff\xfe{}", "INVALID_TRUST_ROOT"),
    "malformed_json": ("{oops", "INVALID_TRUST_ROOT"),
    "deep_nesting":   (DEEP_JSON, "INVALID_TRUST_ROOT"),
    "not_an_object":  ('"a string"', "INVALID_TRUST_ROOT"),
    "unresolved":     (CANON_TEXT, "INVALID_TRUST_ROOT"),
}


@pytest.mark.parametrize("case", sorted(EXTERNAL))
def test_hostile_external_trust_root(tmp_path, case):
    """A bad trust root is an INVALID TRUST ROOT, not a verifier crash.

    The consumer supplied material that cannot authorize anything. That is a
    bad question, not a bad release -- and it must be reported as such rather
    than surfacing as a traceback that looks like a broken tool.
    """
    body, code = EXTERNAL[case]
    zp, b, d = build(tmp_path)
    ext = d / "external_policy.json"
    if body is not None:
        write_policy(ext, body)
    r = run(zp, b, d, "--online", "--trusted-policy", str(ext))
    assert_refused(r, code)


def test_oversized_external_trust_root_is_refused(tmp_path):
    zp, b, d = build(tmp_path)
    ext = d / "external_policy.json"
    ext.write_text(oversized_policy())
    r = run(zp, b, d, "--online", "--trusted-policy", str(ext))
    assert_refused(r, "INVALID_TRUST_ROOT")
    assert "structural bound" in r.stdout


# ---------------------------------------------------------------------------
# 4. One read, one validated byte sequence.
# ---------------------------------------------------------------------------

def test_the_policy_file_is_opened_exactly_once(tmp_path):
    """Every consumer reads the retained bytes, so the file is read once.

    A second read is not merely wasteful. It is a TOCTOU window: the bytes
    that were validated and the bytes that are used stop being the same bytes,
    and the digest that authorized them no longer describes what is in use.
    """
    import io
    vr = _vr()
    zp, b, d = build(tmp_path)
    target = str((b / "release_trust_policy.json").resolve())
    real_open = io.open
    opened = []

    def counting_open(path, *a, **kw):
        # pathlib routes through io.open, so patching os.open sees nothing --
        # an earlier version of this probe counted zero opens against code
        # that really did read the file, and would have "passed" no matter
        # how many times the policy was re-read.
        try:
            if os.path.realpath(path) == target:
                opened.append(str(path))
        except (TypeError, ValueError):
            pass
        return real_open(path, *a, **kw)

    io.open = counting_open
    try:
        vr.verify(zp, b, False, None, None)
    finally:
        io.open = real_open
    assert len(opened) == 1, f"policy opened {len(opened)} times: {opened}"


def test_the_open_probe_actually_observes_reads(tmp_path):
    """Guard for the test above: prove the probe can see a read at all.

    A counter that observes nothing passes trivially. This asserts the probe
    registers a deliberate extra read, so a zero count in the test above means
    "read once", not "instrumentation blind".
    """
    import io
    zp, b, d = build(tmp_path)
    target = str((b / "release_trust_policy.json").resolve())
    real_open = io.open
    seen = []

    def counting_open(path, *a, **kw):
        try:
            if os.path.realpath(path) == target:
                seen.append(str(path))
        except (TypeError, ValueError):
            pass
        return real_open(path, *a, **kw)

    io.open = counting_open
    try:
        (b / "release_trust_policy.json").read_bytes()
        (b / "release_trust_policy.json").read_text()
    finally:
        io.open = real_open
    assert len(seen) == 2, seen


def test_source_has_a_single_bundled_policy_read_site(tmp_path):
    """Structural: the filename appears once in the module.

    Four re-read sites were the actual defect. Pinning the count keeps a
    future edit from quietly reintroducing one.
    """
    src = open(os.path.join(ROOT, "verify_release.py")).read()
    assert src.count('"release_trust_policy.json"') == 1, \
        "the bundled policy path must be referenced exactly once"


def test_candidate_bundle_carries_the_validated_policy(tmp_path):
    vr = _vr()
    zp, b, d = build(tmp_path)
    problems = []
    doc = vr.load_policy_document(
        problems, b / "release_trust_policy.json",
        label="candidate", require_resolved=False,
        missing="MISSING_TRUST_POLICY", unreadable="UNREADABLE_TRUST_POLICY",
        invalid="INVALID_TRUST_POLICY")
    assert doc is not None and problems == []
    cand = vr.parse_candidate_bundle(problems, b, doc)
    assert cand is not None and problems == []
    assert cand.policy == doc.policy
    assert cand.policy_bytes == (b / "release_trust_policy.json").read_bytes()
    assert isinstance(cand.policy_doc, vr.PolicyDocument)


# ---------------------------------------------------------------------------
# 5. Structural validity is not authorization.
# ---------------------------------------------------------------------------

def test_a_schema_valid_candidate_policy_still_authorizes_nothing(tmp_path):
    """The whole governing rule, in one test.

    The bundled policy parses and matches the schema. Online verification must
    still refuse without an external root: a malicious artifact can always
    ship a well-formed policy naming its own signer.
    """
    vr = _vr()
    zp, b, d = build(tmp_path)
    problems = []
    doc = vr.load_policy_document(
        problems, b / "release_trust_policy.json",
        label="candidate", require_resolved=False,
        missing="MISSING_TRUST_POLICY", unreadable="UNREADABLE_TRUST_POLICY",
        invalid="INVALID_TRUST_POLICY")
    assert doc is not None, problems          # STRUCTURAL: yes
    r = run(zp, b, d, "--online")             # AUTHORIZED: no
    assert "Traceback" not in r.stderr
    assert r.returncode == 1
    assert "externally supplied trust root" in r.stdout


def test_digest_must_match_before_candidate_values_are_authorized(tmp_path):
    """Ordering: structural parse, then digest, then authorization.

    With a wrong digest the candidate's values must never become trusted, and
    the refusal must name the digest comparison -- not some later check that
    happened to read an unauthorized value first.
    """
    vr = _vr()
    zp, b, d = build(tmp_path)
    problems = []
    doc = vr.load_policy_document(
        problems, b / "release_trust_policy.json",
        label="candidate", require_resolved=False,
        missing="MISSING_TRUST_POLICY", unreadable="UNREADABLE_TRUST_POLICY",
        invalid="INVALID_TRUST_POLICY")
    assert doc is not None
    root = vr.load_trusted_policy(problems, None, "b" * 64, doc)
    assert root is None
    assert any("digest" in p for p in problems), problems


# ---------------------------------------------------------------------------
# 6. The guards that mutation testing showed were untested.
#
# Each of these closed a SURVIVING mutation. Before them, deleting the check
# left the suite green -- the existing tests were passing for a different
# reason than the one they named.
# ---------------------------------------------------------------------------

def test_a_fifo_policy_is_refused_without_hanging(tmp_path):
    """Why is_file() matters, and why a crash was not the worst case.

    A directory is caught by the read itself (IsADirectoryError), so deleting
    the regular-file check left every other test green. A FIFO is different:
    opening one for reading BLOCKS until a writer appears. Without the
    is_file() gate the verifier does not crash -- it hangs forever, which no
    exception handler can classify and no timeout in CI distinguishes from a
    slow machine.
    """
    zp, b, d = build(tmp_path)
    pol = b / "release_trust_policy.json"
    pol.unlink()
    os.mkfifo(pol)
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "verify_release.py"),
             "--zip", str(zp), "--bundle", str(b)],
            cwd=str(d), capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        pytest.fail("the verifier blocked on a FIFO policy instead of "
                    "refusing it")
    assert_refused(r, "UNREADABLE_TRUST_POLICY")
    assert "not a regular file" in r.stdout


def test_invalid_utf8_inside_a_valid_document_is_refused(tmp_path):
    """Isolates strict decoding from JSON parsing.

    Invalid bytes at the START of the file break JSON parsing too, so that
    fixture passed whether decoding was strict or lenient -- it proved nothing
    about the decoder. Here the bad bytes sit inside a string value, so
    lenient decoding yields a perfectly parseable document with a silently
    corrupted value, and only strict decoding refuses it.
    """
    doc = json.dumps({**CANON, "note": "PLACEHOLDER"}).encode()
    body = doc.replace(b"PLACEHOLDER", b"caf\xe9 not utf8")
    # Prove the premise: lenient decoding really does yield valid JSON.
    assert isinstance(json.loads(body.decode("utf-8", "replace")), dict)
    with pytest.raises(UnicodeDecodeError):
        body.decode("utf-8")
    zp, b, d = build(tmp_path, body)
    r = run(zp, b, d)
    assert_refused(r, "INVALID_TRUST_POLICY")
    assert "not valid UTF-8" in r.stdout


@pytest.mark.parametrize("body", ['[1, 2, 3]', '"a string"', '42', 'true',
                                  'null'])
def test_a_non_object_policy_is_named_as_such(tmp_path, body):
    """Isolates the object requirement from schema validation.

    validate_policy also rejects a list, so the end-to-end test could not tell
    which check refused it. Asserting the specific message pins the object
    requirement itself.
    """
    vr = _vr()
    zp, b, d = build(tmp_path, body)
    problems = []
    doc = vr.load_policy_document(
        problems, b / "release_trust_policy.json",
        label="candidate", require_resolved=False,
        missing="MISSING_TRUST_POLICY", unreadable="UNREADABLE_TRUST_POLICY",
        invalid="INVALID_TRUST_POLICY")
    assert doc is None
    joined = " ".join(problems)
    assert "not an object" in joined, problems


# ---------------------------------------------------------------------------
# 7. The signed release binding (§12).
#
# Authenticated input, not a security boundary -- but a malformed signed
# release must still be refused with a reason rather than crash.
# ---------------------------------------------------------------------------

def _zip_with_binding(tmp_path, body):
    zp = tmp_path / "signed.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr(f"{ZIPROOT}/x.txt", b"1")
        if body is not None:
            z.writestr(f"{ZIPROOT}/{RT.RELEASE_BINDING_NAME}", body)
    return zipfile.ZipFile(zp)


@pytest.mark.parametrize("body,why", [
    (None, "absent"),
    (b"{not json", "malformed"),
    (b"\xff\xfe{}", "invalid UTF-8"),
    (("[" * 60000 + "]" * 60000).encode(), "deeply nested"),
    (b'[1,2,3]', "a JSON array"),
    (b'"text"', "a JSON string"),
    (b"", "empty"),
])
def test_malformed_signed_binding_is_refused_not_crashed(tmp_path, body, why):
    vr = _vr()
    zf = _zip_with_binding(tmp_path, body)
    problems = []
    assert vr.read_release_binding(problems, zf, ZIPROOT) is None, why
    assert problems, why


def test_a_well_formed_signed_binding_is_returned(tmp_path):
    vr = _vr()
    zf = _zip_with_binding(tmp_path, json.dumps({"schema_version": "3.0.0"}
                                                ).encode())
    problems = []
    got = vr.read_release_binding(problems, zf, ZIPROOT)
    assert got == {"schema_version": "3.0.0"} and problems == []


# ---------------------------------------------------------------------------
# 8. Validate once, consume the same bytes (§5).
#
# The policy was already retained. The index, SBOM and provenance were still
# re-read from disk during the secret / absolute-path / claim-boundary scan,
# which is the same TOCTOU window one layer over: a file could be validated in
# one form and scanned in another. It also forced errors="replace" onto bytes
# whose strict decode had already succeeded, which can only mask content.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["release_index.json", "sbom.cdx.json",
                                  "provenance.intoto.json",
                                  "release_trust_policy.json"])
def test_replacing_metadata_after_validation_cannot_change_the_scan(
        tmp_path, name):
    """Swap in a secret AFTER parsing; this run must not see it.

    If the scan re-read from disk, the planted AWS key would be found and
    reported -- proving the scanned bytes were not the validated bytes. The
    run must instead complete against what it actually parsed.
    """
    vr = _vr()
    zp, b, d = build(tmp_path)
    problems = []
    doc = vr.load_policy_document(
        problems, b / "release_trust_policy.json",
        label="candidate", require_resolved=False,
        missing="MISSING_TRUST_POLICY", unreadable="UNREADABLE_TRUST_POLICY",
        invalid="INVALID_TRUST_POLICY")
    assert doc is not None, problems
    cand = vr.parse_candidate_bundle(problems, b, doc)
    assert cand is not None and problems == [], problems

    planted = "AKIAIOSFODNN7EXAMPLE"
    assert any(re.search(p, planted) for p in vr.SECRET_PATTERNS), \
        "the planted value must actually match a secret pattern"
    (b / name).write_text(json.dumps({"stolen": planted}))

    # What the run retained is unchanged...
    assert planted not in cand.scanned_text
    # ...even though the file on disk now contains it.
    assert planted in (b / name).read_text()


def test_the_scan_covers_every_validated_metadata_document(tmp_path):
    """Retention must not quietly shrink what is scanned.

    Consuming retained text instead of re-reading is only safe if the retained
    text still covers all four documents; otherwise this would trade a TOCTOU
    window for a blind spot.
    """
    vr = _vr()
    zp, b, d = build(tmp_path)
    problems = []
    doc = vr.load_policy_document(
        problems, b / "release_trust_policy.json",
        label="candidate", require_resolved=False,
        missing="MISSING_TRUST_POLICY", unreadable="UNREADABLE_TRUST_POLICY",
        invalid="INVALID_TRUST_POLICY")
    cand = vr.parse_candidate_bundle(problems, b, doc)
    assert cand is not None, problems
    scanned = cand.scanned_text
    for marker in ("release_artifact", "components", "subject",
                   "signer_identity"):
        assert marker in scanned, f"{marker!r} missing from the scanned text"
    for name in ("release_index.json", "sbom.cdx.json",
                 "provenance.intoto.json", "release_trust_policy.json"):
        assert (b / name).read_text().strip()[:40] in scanned or \
            json.loads((b / name).read_text()) is not None


def test_retained_metadata_text_is_strictly_decoded(tmp_path):
    """No errors="replace" on already-validated metadata.

    Substituted characters in a scanned document could hide a secret or an
    absolute path behind U+FFFD. Strict decoding already succeeded during
    validation, so the retained text is exact.
    """
    vr = _vr()
    zp, b, d = build(tmp_path)
    problems = []
    doc = vr.load_policy_document(
        problems, b / "release_trust_policy.json",
        label="candidate", require_resolved=False,
        missing="MISSING_TRUST_POLICY", unreadable="UNREADABLE_TRUST_POLICY",
        invalid="INVALID_TRUST_POLICY")
    cand = vr.parse_candidate_bundle(problems, b, doc)
    assert cand is not None
    assert "\ufffd" not in cand.scanned_text
    for d_ in (cand.index_doc, cand.sbom_doc, cand.provenance_doc):
        assert d_.raw.decode("utf-8") == d_.text


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")
         and callable(v)]
