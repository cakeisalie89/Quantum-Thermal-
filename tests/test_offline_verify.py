"""The portable verifier needs nothing but the bundle and what its operator
provisions -- and refuses hostile bundles on their shape before trusting
anything in them.

D-2026-144. verify() compared the SBOM with the VERIFYING machine's
uv.lock, checked the signature against whatever trust root that machine's
sigstore package held, and read archive members whole with no bound. The
verifier is now tools/offline_verify.py, standard library only, and:

* runs from a directory holding nothing but itself, with an empty
  environment, inside a network namespace with no interfaces;
* derives the expected SBOM from the dependency declarations INSIDE the
  signed archive;
* passes Sigstore an explicit --trust-config and --offline, with an empty
  environment, and refuses offline verification without a trust config;
* bounds and shape-checks every input before trusting it.

The real Sigstore path runs in the hosted supply-chain job (offline-verify,
hostile suite); here the signature step is either not reached (digests
mode) or captured to check exactly what would be asked of Sigstore.
"""
from __future__ import annotations

import ast
import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import offline_verify as OV  # noqa: E402
import supply_chain as SC  # noqa: E402

VERIFIER = ROOT / "tools" / "offline_verify.py"
CI_POLICY = ROOT / "docs" / "supply_chain_ci_policy.json"


@pytest.fixture(scope="module")
def rc(tmp_path_factory):
    out = tmp_path_factory.mktemp("rc")
    SC.build(out, allow_dirty=True)
    return out


def _copy(rc: Path, tmp: Path) -> Path:
    c = tmp / "rc"
    shutil.copytree(rc, c)
    return c


def _resum(c: Path) -> None:
    (c / OV.SIGNED).write_text("".join(
        f"{OV._sha((c / n).read_bytes())}  {n}\n" for n in OV.ARTIFACTS))


def _repack(c: Path, edit) -> None:
    """Rebuild source.zip from edit(members) with the manifest, the index
    and the sums all following, so only the property under test differs."""
    with zipfile.ZipFile(c / "source.zip") as zf:
        members = {i.filename: zf.read(i) for i in zf.infolist()}
    members = edit(dict(members))
    doc = json.loads(members["final_manifest.json"])
    doc["files"] = [{"filename": n, "size_bytes": len(b),
                     "sha256": OV._sha(b)} for n, b in sorted(members.items())
                    if n not in OV.MANIFEST_DETACHED]
    members["final_manifest.json"] = json.dumps(doc).encode()
    members["manifest_hash.txt"] = (
        f"sha256: {OV._sha(members['final_manifest.json'])}\n".encode())
    with zipfile.ZipFile(c / "source.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        for n, b in sorted(members.items()):
            zf.writestr(zipfile.ZipInfo(n, date_time=SC.ZIP_TIME), b)
    prov = json.loads((c / "provenance.intoto.json").read_text())
    for s in prov["subject"]:
        if s["name"] == "source.zip":
            s["digest"]["sha256"] = OV._sha((c / "source.zip").read_bytes())
    (c / "provenance.intoto.json").write_text(json.dumps(prov))
    idx = json.loads((c / "index.json").read_text())
    idx["files_in_source_zip"] = len(members)
    idx["final_manifest_sha256"] = OV._sha(members["final_manifest.json"])
    for n in ("source.zip", "provenance.intoto.json"):
        idx["artifacts"][n] = OV._sha((c / n).read_bytes())
    (c / "index.json").write_text(json.dumps(idx))
    _resum(c)


def _verify(c: Path, **kw):
    return OV.verify_bundle(c, OV.load_policy(CI_POLICY), mode=kw.pop(
        "mode", "digests"), **kw)


# ---- portability -------------------------------------------------------------

def test_the_verifier_imports_nothing_but_the_standard_library():
    tree = ast.parse(VERIFIER.read_text(encoding="utf-8"))
    tops = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            tops.add(node.module.split(".")[0])
    assert tops - {"__future__"} <= set(sys.stdlib_module_names), \
        sorted(tops - set(sys.stdlib_module_names))


def _no_network_prefix() -> list:
    probe = subprocess.run(["unshare", "-rn", "true"], capture_output=True)
    return ["unshare", "-rn"] if probe.returncode == 0 else []


def test_it_verifies_from_a_bare_directory_with_no_network(rc, tmp_path):
    """The verifier ALONE, copied into an empty directory; an empty
    environment; a network namespace with no interfaces when the kernel
    allows one. Nothing of this repository is reachable from it."""
    alone = tmp_path / "verifier"
    alone.mkdir()
    shutil.copy(VERIFIER, alone / "offline_verify.py")
    shutil.copy(CI_POLICY, alone / "policy.json")
    bundle = shutil.copytree(rc, tmp_path / "bundle")
    prefix = _no_network_prefix()
    cmd = prefix + [sys.executable, "-I", "offline_verify.py", "verify",
                    "--bundle", str(bundle), "--policy", "policy.json",
                    "--mode", "digests", "--require", "PROVENANCE_STRUCTURE",
                    "--report", "report.json"]
    r = subprocess.run(cmd, cwd=str(alone), env={}, capture_output=True,
                       text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    rep = json.loads((alone / "report.json").read_text())
    assert rep["highest_established"] == "PROVENANCE_STRUCTURE"
    assert rep["release_authorized"] is False
    if prefix:
        net = subprocess.run(prefix + [sys.executable, "-I", "-c",
                                       "import socket; s=socket.socket(); "
                                       "s.settimeout(2); s.connect(("
                                       "'127.0.0.1', 9))"],
                             capture_output=True, text=True)
        assert net.returncode != 0, "the namespace still had a network"


def test_the_sbom_is_judged_against_the_releases_own_declarations(rc,
                                                                  tmp_path):
    """Regression for D-2026-144: the archive's uv.lock is changed (and the
    manifest, index and sums follow), so the SBOM no longer describes the
    release -- while it still describes the VERIFYING checkout's uv.lock.
    The old check compared against the checkout and accepted this."""
    c = _copy(rc, tmp_path)

    def bump(m):
        m["uv.lock"] = m["uv.lock"].replace(b'name = "numpy"\nversion = "',
                                            b'name = "numpy"\nversion = "9',
                                            1)
        return m
    _repack(c, bump)
    assert SC.sbom_problems(json.loads((c / "sbom.cdx.json").read_text())) \
        == [], "the forgery must still match the checkout's lock"
    with pytest.raises(OV.Refused, match="SBOM"):
        _verify(c)


def test_a_release_whose_declarations_do_not_parse_is_refused(rc, tmp_path):
    c = _copy(rc, tmp_path)

    def broken(m):
        m["integrations/fenicsx/environment.sha256.json"] = b"{not json"
        return m
    _repack(c, broken)
    with pytest.raises(OV.Refused, match="declarations do not parse"):
        _verify(c)


# ---- the Sigstore call is explicit -------------------------------------------

def test_offline_verification_without_a_trust_config_is_refused(rc,
                                                                tmp_path):
    c = _copy(rc, tmp_path)
    (c / OV.BUNDLE).write_text("{}")
    with pytest.raises(OV.Refused, match="independently provisioned trust"):
        _verify(c, mode="offline", trust_config=None,
                sigstore_python=sys.executable)


def test_the_sigstore_call_is_offline_pinned_and_environment_free(
        rc, tmp_path, monkeypatch):
    c = _copy(rc, tmp_path)
    (c / OV.BUNDLE).write_text("{}")
    tc = tmp_path / "trust_config.json"
    tc.write_text("{}")
    seen = {}

    class Done:
        returncode = 0
        stdout = stderr = ""

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw.get("env")
        seen["tc"] = Path(cmd[cmd.index("--trust-config") + 1]).read_bytes()
        seen["sums"] = Path(cmd[-1]).read_bytes()
        return Done()
    monkeypatch.setattr(OV.subprocess, "run", fake_run)
    pol = OV.load_policy(CI_POLICY)
    rep = OV.verify_bundle(c, pol, mode="offline", trust_config=tc,
                           sigstore_python="/opt/sigstore/bin/python")
    cmd = seen["cmd"]
    assert cmd[:4] == ["/opt/sigstore/bin/python", "-I", "-m", "sigstore"]
    assert seen["tc"] == tc.read_bytes()
    assert cmd.index("--trust-config") < cmd.index("verify"), (
        "--trust-config is a global option and must precede the command")
    assert "--offline" in cmd
    assert cmd[cmd.index("--cert-identity") + 1] == pol["signer_identity"]
    assert cmd[cmd.index("--cert-oidc-issuer") + 1] == pol["oidc_issuer"]
    assert seen["sums"] == (c / OV.SIGNED).read_bytes()
    for arg in (cmd[-1], cmd[cmd.index("--bundle") + 1],
                cmd[cmd.index("--trust-config") + 1]):
        assert not Path(arg).resolve().is_relative_to(c.resolve()), arg
    assert set(seen["env"]) == {"HOME", "PATH", "LC_ALL"}
    assert rep["levels"]["SIGNER_TRUSTED"]["status"] == OV.ESTABLISHED
    assert rep["levels"]["SIGNATURE_VERIFIED"]["detail"].count(
        OV._sha(tc.read_bytes())) == 1


def test_sigstore_judges_the_list_that_was_parsed_not_a_later_swap(
        rc, tmp_path, monkeypatch):
    """Whoever controls the bundle directory swaps SHA256SUMS between the
    parse and the signature check. The signature must be checked over the
    parsed bytes, so a signed list cannot vouch for a forged one."""
    c = _copy(rc, tmp_path)
    (c / OV.BUNDLE).write_text("{}")
    tc = tmp_path / "trust_config.json"
    tc.write_text("{}")
    parsed = (c / OV.SIGNED).read_bytes()
    seen = {}

    class Done:
        returncode = 0
        stdout = stderr = ""

    def racing_run(cmd, **kw):
        (c / OV.SIGNED).write_bytes(b"a genuinely signed list, swapped in\n")
        seen["judged"] = Path(cmd[-1]).read_bytes()
        return Done()
    monkeypatch.setattr(OV.subprocess, "run", racing_run)
    OV.verify_bundle(c, OV.load_policy(CI_POLICY), mode="offline",
                     trust_config=tc, sigstore_python="/x/python")
    assert seen["judged"] == parsed


@pytest.mark.parametrize("name, edit", [
    ("provenance.intoto.json", lambda d: d["predicate"]["runDetails"]
     ["metadata"].update(invocationId="another/run")),
    ("index.json", lambda d: d.update(
        working_tree_dirty=not d["working_tree_dirty"])),
])
def test_a_field_no_other_layer_reads_is_still_bound_by_the_signed_digest(
        rc, tmp_path, name, edit):
    """Found by mutation SC1, which survived once source.zip was stored:
    every forgery the suites made was also caught by a deeper layer, so
    deleting the digest comparison changed nothing they could see. These
    fields are read by no other check -- the run that built the provenance,
    whether the tree was dirty -- so only the signed digest binds them."""
    c = _copy(rc, tmp_path)
    doc = json.loads((c / name).read_text())
    edit(doc)
    (c / name).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    with pytest.raises(OV.Refused, match=f"{name}: its bytes are not the "
                                         "signed digest"):
        _verify(c, mode="digests")


def test_a_failing_sigstore_call_refuses(rc, tmp_path, monkeypatch):
    c = _copy(rc, tmp_path)
    (c / OV.BUNDLE).write_text("{}")
    tc = tmp_path / "trust_config.json"
    tc.write_text("{}")

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "certificate identity mismatch"
    monkeypatch.setattr(OV.subprocess, "run", lambda *a, **k: Failed())
    with pytest.raises(OV.Refused, match="identity mismatch"):
        OV.verify_bundle(c, OV.load_policy(CI_POLICY), mode="offline",
                         trust_config=tc, sigstore_python="/x/python")


# ---- SHA256SUMS is parsed strictly -------------------------------------------

@pytest.mark.parametrize("edit, why", [
    (lambda t: t + t.splitlines(True)[0], "twice"),
    (lambda t: t.upper(), "malformed line"),
    (lambda t: t.replace("\n", "\r\n"), "malformed line"),
    (lambda t: t.rstrip("\n"), "does not end with a newline"),
    (lambda t: t.replace("  ", " ", 1), "malformed line"),
    (lambda t: t.replace("source.zip", "../source.zip"), "malformed line"),
])
def test_sha256sums_is_parsed_strictly(rc, tmp_path, edit, why):
    c = _copy(rc, tmp_path)
    (c / OV.SIGNED).write_text(edit((c / OV.SIGNED).read_text()))
    with pytest.raises(OV.Refused, match=why):
        _verify(c)


# ---- every input is bounded and shape-checked before it is trusted -----------

def _zip_with(members, *, links=()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            info = zipfile.ZipInfo(name, date_time=SC.ZIP_TIME)
            # a ZipInfo is STORED unless told otherwise; the bomb case needs
            # real compression for its ratio to mean anything
            info.compress_type = zipfile.ZIP_DEFLATED
            if name in links:
                info.external_attr = (0o120777) << 16
            zf.writestr(info, data)
    return buf.getvalue()


@pytest.mark.parametrize("name", ["/etc/passwd", "../outside.py",
                                  "a/../../b", "a\\b", "dir/", "a//b"])
def test_an_archive_member_that_is_not_a_plain_relative_path_is_refused(
        name):
    with pytest.raises(OV.Refused, match="not a plain relative path"):
        OV.read_archive(_zip_with({name: b"x"}))


def test_a_link_member_is_refused():
    with pytest.raises(OV.Refused, match="symbolic link"):
        OV.read_archive(_zip_with({"link": b"/etc/passwd"}, links={"link"}))


def test_a_member_over_its_bound_is_refused(monkeypatch):
    monkeypatch.setattr(OV, "MAX_MEMBER", 10)
    with pytest.raises(OV.Refused, match="over the 10-byte bound"):
        OV.read_archive(_zip_with({"big": b"x" * 11}))


def test_the_total_uncompressed_size_is_bounded(monkeypatch):
    monkeypatch.setattr(OV, "MAX_TOTAL", 15)
    with pytest.raises(OV.Refused, match="declares over 15 bytes"):
        OV.read_archive(_zip_with({"a": b"x" * 10, "b": b"y" * 10}))


def test_a_decompression_bomb_is_refused(monkeypatch):
    monkeypatch.setattr(OV, "MAX_RATIO", 10)
    with pytest.raises(OV.Refused, match="decompression bomb"):
        OV.read_archive(_zip_with({"zeros": b"\0" * (2 << 20)}))


def test_too_many_members_are_refused(monkeypatch):
    monkeypatch.setattr(OV, "MAX_MEMBERS", 3)
    with pytest.raises(OV.Refused, match="over the 3 bound"):
        OV.read_archive(_zip_with({str(i): b"" for i in range(4)}))


def test_a_member_that_lies_about_its_size_is_refused():
    raw = bytearray(_zip_with({"m": b"hello world"}))
    # the central directory's uncompressed size field, set one too large
    cd = raw.rindex(b"PK\x01\x02")
    size_at = cd + 24
    raw[size_at] += 1
    with pytest.raises(OV.Refused):
        OV.read_archive(bytes(raw))


@pytest.mark.parametrize("garbage", [b"", b"PK", b"not a zip at all" * 10])
def test_a_garbage_archive_is_refused_not_raised(garbage):
    with pytest.raises(OV.Refused, match="not a readable zip"):
        OV.read_archive(garbage)


def test_an_artifact_that_is_a_link_is_refused(rc, tmp_path):
    c = _copy(rc, tmp_path)
    real = tmp_path / "elsewhere.json"
    shutil.move(c / "sbom.cdx.json", real)
    (c / "sbom.cdx.json").symlink_to(real)
    with pytest.raises(OV.Refused, match="symbolic link"):
        _verify(c)


def test_an_oversized_artifact_is_refused_before_it_is_read(rc, tmp_path,
                                                            monkeypatch):
    c = _copy(rc, tmp_path)
    monkeypatch.setattr(OV, "MAX_JSON", 100)
    with pytest.raises(OV.Refused, match="over the 100-byte bound"):
        _verify(c)


def test_a_policy_supplied_by_the_bundle_is_refused(rc, tmp_path):
    c = _copy(rc, tmp_path)
    shutil.copy(CI_POLICY, c / "policy.json")
    with pytest.raises(OV.Refused, match="inside the bundle"):
        OV.load_policy(c / "policy.json", bundle_dir=c)


def test_a_provenance_naming_another_builder_is_refused(rc, tmp_path):
    c = _copy(rc, tmp_path)
    prov = json.loads((c / "provenance.intoto.json").read_text())
    prov["predicate"]["buildDefinition"]["externalParameters"][
        "workflow"] = ".github/workflows/claude.yml"
    (c / "provenance.intoto.json").write_text(json.dumps(prov))
    idx = json.loads((c / "index.json").read_text())
    idx["artifacts"]["provenance.intoto.json"] = OV._sha(
        (c / "provenance.intoto.json").read_bytes())
    (c / "index.json").write_text(json.dumps(idx))
    _resum(c)
    with pytest.raises(OV.Refused, match="different builder"):
        _verify(c)
