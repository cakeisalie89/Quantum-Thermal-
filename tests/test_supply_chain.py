"""The release candidate: built from the commit, verified fail-closed, every
tampered copy refused.

The keyless signature itself can only be made by a hosted workflow holding a
GitHub OIDC token (supply-chain.yml); what these tests hold offline is
everything around it -- the deterministic archive, the SBOM against the
locks, the provenance's subjects, the index, the policy's exactness -- and
that verification REFUSES a missing signature rather than skipping it.
"""
from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import supply_chain as SC  # noqa: E402

POLICY = ROOT / "docs" / "supply_chain_ci_policy.json"


@pytest.fixture(scope="module")
def rc(tmp_path_factory):
    out = tmp_path_factory.mktemp("rc")
    SC.build(out, allow_dirty=True)
    return out


def test_the_candidate_verifies_by_digest_against_the_commit(rc):
    rep = SC.verify(rc, SC.load_policy(POLICY), mode="digests")
    assert rep["accepted"]
    assert rep["source_tree"].startswith("MATCHES ")
    idx = json.loads((rc / "index.json").read_text())
    assert idx["slsa_level_claimed"] is None
    assert "scientific validity of any result" in \
        idx["signature"]["does_not_attest"]


def test_the_archive_is_deterministic(rc, tmp_path):
    again = tmp_path / "again"
    SC.build(again, allow_dirty=True)
    for n in ("source.zip", "sbom.cdx.json", "SHA256SUMS"):
        assert (again / n).read_bytes() == (rc / n).read_bytes(), n


def test_the_sbom_names_every_locked_environment(rc):
    doc = json.loads((rc / "sbom.cdx.json").read_text())
    envs = {p["value"] for c in doc["components"]
            for p in c.get("properties", ())}
    assert {"project (uv.lock)", "fenicsx-conda", "fmi-runtime",
            "sigstore-runtime"} <= envs
    assert SC.sbom_problems(doc) == []


@pytest.mark.parametrize("mode", ["online", "offline"])
def test_a_missing_signature_is_refused_not_skipped(rc, mode):
    with pytest.raises(SC.Refused, match="no signature bundle"):
        SC.verify(rc, SC.load_policy(POLICY), mode=mode)


def test_no_sigstore_runtime_is_refused(rc, tmp_path, monkeypatch):
    c = tmp_path / "rc"
    shutil.copytree(rc, c)
    (c / SC.BUNDLE).write_text("{}")
    monkeypatch.delenv("QTA_SIGSTORE_PYTHON", raising=False)
    with pytest.raises(SC.Refused, match="sigstore runtime"):
        SC.verify(c, SC.load_policy(POLICY), mode="online")


def test_every_content_tamper_is_refused(rc):
    res = SC.tamper(rc, SC.load_policy(POLICY), mode="digests")
    for case in ("payload_byte_flipped", "manifest_digest_changed",
                 "sbom_component_changed", "provenance_subject_changed"):
        assert res[case]["refused"], (case, res[case])


def test_a_consistently_re_summed_forgery_is_caught_beneath_the_sums(
        rc, tmp_path):
    """An attacker who edits the SBOM AND rewrites SHA256SUMS to match
    breaks the signature in a signed run; in digests mode the layers under
    the sums must still catch it."""
    c = tmp_path / "rc"
    shutil.copytree(rc, c)
    doc = json.loads((c / "sbom.cdx.json").read_text())
    doc["components"].append({"type": "library", "name": "evil",
                              "version": "1", "properties": [
                                  {"name": "harness:environment",
                                   "value": "project (uv.lock)"}]})
    (c / "sbom.cdx.json").write_text(json.dumps(doc))
    prov = json.loads((c / "provenance.intoto.json").read_text())
    for s in prov["subject"]:
        if s["name"] == "sbom.cdx.json":
            s["digest"]["sha256"] = SC._sha((c / "sbom.cdx.json").read_bytes())
    (c / "provenance.intoto.json").write_text(json.dumps(prov))
    idx = json.loads((c / "index.json").read_text())
    for n in ("sbom.cdx.json", "provenance.intoto.json"):
        idx["artifacts"][n] = SC._sha((c / n).read_bytes())
    (c / "index.json").write_text(json.dumps(idx))
    (c / SC.SIGNED).write_text("".join(
        f"{SC._sha((c / n).read_bytes())}  {n}\n" for n in SC.ARTIFACTS))
    with pytest.raises(SC.Refused, match="SBOM"):
        SC.verify(c, SC.load_policy(POLICY), mode="digests")


def _repack(rc, tmp_path, edit, *, manifest_follows=True,
            hash_follows=True, index_count=None):
    """A copy of ``rc`` whose source.zip is rebuilt from ``edit(members)``
    -- a dict of name -> bytes -- with every digest above it recomputed, so
    the forgery is consistent everywhere except where it is meant not to be.

    ``manifest_follows`` rewrites the archived final_manifest.json to
    describe the edited members (the forgery a builder that lost a file
    and re-described what it had would make); ``hash_follows`` rewrites
    manifest_hash.txt beside it.
    """
    c = tmp_path / "rc"
    shutil.copytree(rc, c)
    src = c / "source.zip"
    with zipfile.ZipFile(src) as zf:
        members = {i.filename: zf.read(i) for i in zf.infolist()}
    members = edit(dict(members))
    if manifest_follows:
        doc = json.loads(members["final_manifest.json"])
        doc["files"] = [{"filename": n, "size_bytes": len(b),
                         "sha256": SC._sha(b)}
                        for n, b in sorted(members.items())
                        if n not in SC.MANIFEST_DETACHED]
        members["final_manifest.json"] = json.dumps(doc).encode()
    if hash_follows:
        members["manifest_hash.txt"] = (
            f"sha256: {SC._sha(members['final_manifest.json'])}\n".encode())
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, b in sorted(members.items()):
            zf.writestr(zipfile.ZipInfo(n, date_time=SC.ZIP_TIME), b)
    prov = json.loads((c / "provenance.intoto.json").read_text())
    for s in prov["subject"]:
        if s["name"] == "source.zip":
            s["digest"]["sha256"] = SC._sha(src.read_bytes())
    (c / "provenance.intoto.json").write_text(json.dumps(prov))
    idx = json.loads((c / "index.json").read_text())
    idx["files_in_source_zip"] = (len(members) if index_count is None
                                  else index_count)
    idx["final_manifest_sha256"] = SC._sha(members["final_manifest.json"])
    for n in ("source.zip", "provenance.intoto.json"):
        idx["artifacts"][n] = SC._sha((c / n).read_bytes())
    (c / "index.json").write_text(json.dumps(idx))
    (c / SC.SIGNED).write_text("".join(
        f"{SC._sha((c / n).read_bytes())}  {n}\n" for n in SC.ARTIFACTS))
    return c


def _with(name, data):
    def edit(m):
        m[name] = data
        return m
    return edit


def _without(name):
    def edit(m):
        del m[name]
        return m
    return edit


def test_a_source_member_not_in_the_commit_is_refused(rc, tmp_path):
    c = _repack(rc, tmp_path, _with("planted.py", b"print('x')\n"))
    with pytest.raises(SC.Refused, match="planted.py"):
        SC.verify(c, SC.load_policy(POLICY), mode="digests")


def test_an_archive_that_lost_a_tracked_file_is_refused(rc, tmp_path):
    """D-2026-127. Each member was compared with the commit, and nothing
    compared the commit with the members: an archive missing
    qta_agent/authority.py, whose manifest had been rewritten to match,
    verified as "MATCHES <commit>"."""
    c = _repack(rc, tmp_path, _without("qta_agent/authority.py"))
    with pytest.raises(SC.Refused, match="missing.*qta_agent/authority.py"):
        SC.verify(c, SC.load_policy(POLICY), mode="digests")


def test_a_member_whose_bytes_are_not_the_commits_is_refused(rc, tmp_path):
    """The member set is the commit's and the archived manifest describes the
    archive, so neither of those layers can refuse: only the comparison of
    each member with the commit's blob sees that authority.py was replaced.
    Without this case, removing that comparison killed nothing once the
    set comparison caught every planted file first."""
    c = _repack(rc, tmp_path, _with("qta_agent/authority.py",
                                    b"# replaced\n"))
    with pytest.raises(SC.Refused, match="is not the commit's"):
        SC.verify(c, SC.load_policy(POLICY), mode="digests")


# ---- the archive against the manifest it carries (D-2026-127) -----------
#
# repo=None: no checkout, so the commit cannot refuse anything and only the
# manifest layer can. The signature binds the archive; these say that the
# archive is what its own manifest says the release is.

@pytest.mark.parametrize("name, edit, kw, why", [
    ("lost", _without("qta_agent/authority.py"),
     {"manifest_follows": False}, "which source.zip does not contain"),
    ("changed", _with("qta_agent/authority.py", b"# replaced\n"),
     {"manifest_follows": False}, "not the ones its manifest lists"),
    ("unlisted", _with("planted.py", b"print('x')\n"),
     {"manifest_follows": False}, "which its manifest does not list"),
    ("hash", lambda m: m, {"hash_follows": False},
     "manifest_hash.txt in source.zip"),
    ("count", lambda m: m, {"index_count": 1}, "the index says 1"),
    # an archive of nothing but the two detached files, whose manifest lists
    # nothing: every per-file comparison passes by examining zero files
    ("empty", lambda m: {n: m[n] for n in SC.MANIFEST_DETACHED},
     {}, "lists no files"),
])
def test_the_archive_is_what_its_own_manifest_says(rc, tmp_path, name, edit,
                                                   kw, why):
    c = _repack(rc, tmp_path, edit, **kw)
    with pytest.raises(SC.Refused, match=why):
        SC.verify(c, SC.load_policy(POLICY), mode="digests", repo=None)


def test_the_unforged_archive_describes_itself_without_a_checkout(rc):
    rep = SC.verify(rc, SC.load_policy(POLICY), mode="digests", repo=None)
    assert rep["source_tree"] == "NOT_CHECKED (no checkout)"
    assert rep["manifest"].startswith("DESCRIBES THE ARCHIVE")


def test_a_member_named_twice_is_refused(rc, tmp_path):
    """Both copies can match the commit's blob; which one is the release is
    then decided by the extractor, not by the bytes that were signed."""
    c = tmp_path / "rc"
    shutil.copytree(rc, c)
    src = c / "source.zip"
    with zipfile.ZipFile(src) as zf:
        data = zf.read("README.md")
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")      # zipfile warns: Duplicate name
        with zipfile.ZipFile(src, "a") as zf:
            zf.writestr(zipfile.ZipInfo("README.md", date_time=SC.ZIP_TIME),
                        data)
    prov = json.loads((c / "provenance.intoto.json").read_text())
    for s in prov["subject"]:
        if s["name"] == "source.zip":
            s["digest"]["sha256"] = SC._sha(src.read_bytes())
    (c / "provenance.intoto.json").write_text(json.dumps(prov))
    idx = json.loads((c / "index.json").read_text())
    idx["files_in_source_zip"] += 1
    for n in ("source.zip", "provenance.intoto.json"):
        idx["artifacts"][n] = SC._sha((c / n).read_bytes())
    (c / "index.json").write_text(json.dumps(idx))
    (c / SC.SIGNED).write_text("".join(
        f"{SC._sha((c / n).read_bytes())}  {n}\n" for n in SC.ARTIFACTS))
    with pytest.raises(SC.Refused, match="more than once"):
        SC.verify(c, SC.load_policy(POLICY), mode="digests", repo=None)


@pytest.mark.parametrize("field, value, why", [
    ("signer_identity", "https://github.com/*/*", "exact"),
    ("signer_identity", "PENDING: later", "exact"),
    ("oidc_issuer", "https://accounts.google.com", "issuer"),
    ("ref", "refs/heads/main", "derived"),
])
def test_a_policy_that_is_not_exact_is_refused(tmp_path, field, value, why):
    p = json.loads(POLICY.read_text())
    p[field] = value
    f = tmp_path / "p.json"
    f.write_text(json.dumps(p))
    with pytest.raises(SC.Refused, match=why):
        SC.load_policy(f)


def test_the_ci_policy_is_labelled_and_is_not_a_release_root():
    p = json.loads(POLICY.read_text())
    assert p["label"] == "CI_VALIDATION_ONLY"
    assert "Not a release trust root" in p["what_this_is_not"]
    assert p["workflow_path"] == ".github/workflows/supply-chain.yml"


# ---- each layer beneath the sums, broken alone ---------------------------
#
# A forgery that is consistent everywhere except one layer: every other
# digest is recomputed, so only the layer under test can refuse it. Without
# these, a layer whose check was deleted is masked by the one above it and
# nothing notices.

def _forge(rc, tmp_path, edit, *, reindex: bool = True):
    c = tmp_path / "rc"
    shutil.copytree(rc, c)
    edit(c)
    if reindex:
        idx = json.loads((c / "index.json").read_text())
        for n in list(idx["artifacts"]):
            idx["artifacts"][n] = SC._sha((c / n).read_bytes())
        (c / "index.json").write_text(json.dumps(idx))
    (c / SC.SIGNED).write_text("".join(
        f"{SC._sha((c / n).read_bytes())}  {n}\n" for n in SC.ARTIFACTS))
    return c


def _json_edit(name, fn):
    def edit(c):
        doc = json.loads((c / name).read_text())
        fn(doc)
        (c / name).write_text(json.dumps(doc))
    return edit


def _subject(doc):
    for s in doc["subject"]:
        if s["name"] == "source.zip":
            s["digest"]["sha256"] = "0" * 64


def _commit(doc):
    deps = doc["predicate"]["buildDefinition"]["resolvedDependencies"]
    deps[0]["digest"]["gitCommit"] = "f" * 40


@pytest.mark.parametrize("name, edit, reindex, why", [
    ("subject", _json_edit("provenance.intoto.json", _subject), True,
     "provenance subject source.zip"),
    ("commit", _json_edit("provenance.intoto.json", _commit), True,
     "source commit"),
    ("index", _json_edit("index.json", lambda d: d["artifacts"].update(
        {"sbom.cdx.json": "0" * 64})), False, "index digest of"),
    ("manifest", _json_edit("index.json", lambda d: d.update(
        {"final_manifest_sha256": "0" * 64})), True,
     "final_manifest_sha256"),
])
def test_each_layer_refuses_a_forgery_consistent_everywhere_else(
        rc, tmp_path, name, edit, reindex, why):
    c = _forge(rc, tmp_path, edit, reindex=reindex)
    with pytest.raises(SC.Refused, match=why):
        SC.verify(c, SC.load_policy(POLICY), mode="digests")


def test_sums_that_list_another_set_of_files_are_refused(rc, tmp_path):
    c = tmp_path / "rc"
    shutil.copytree(rc, c)
    (c / "extra.txt").write_text("x")
    with (c / SC.SIGNED).open("a") as fh:
        fh.write(f"{SC._sha(b'x')}  extra.txt\n")
    with pytest.raises(SC.Refused, match="SHA256SUMS lists"):
        SC.verify(c, SC.load_policy(POLICY), mode="digests")


@pytest.mark.parametrize("text", ["[]", '"x"', "7", "null"])
def test_a_policy_that_is_not_an_object_is_refused(tmp_path, text):
    """Found by fuzzing: a JSON array crashed load_policy with an
    AttributeError instead of a refusal."""
    f = tmp_path / "p.json"
    f.write_text(text)
    with pytest.raises(SC.Refused, match="JSON object"):
        SC.load_policy(f)
