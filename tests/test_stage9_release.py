"""Stage-9 release verification tests. MODEL-ONLY / FORECAST-ONLY.

Builds a tiny fixture release (zip + bundle) in tmp_path, then proves
the consumer verifier accepts the intact release and REJECTS every
tamper/identity/malformation class the directive lists. Software
verification only; scientific gate PASS remains zero.
"""
import hashlib
import io
import json
import re
import subprocess
import sys
import zipfile
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

import verify_release as VR                                      # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
DET = settings(max_examples=20, deadline=None, derandomize=True,
               suppress_health_check=[HealthCheck.too_slow])

LOCK = ('[[package]]\nname = "numpy"\nversion = "2.4.4"\n\n'
        '[[package]]\nname = "scipy"\nversion = "1.17.1"\n\n'
        '[[package]]\nname = "qutip"\nversion = "5.2.1"\n\n'
        '[[package]]\nname = "h5py"\nversion = "3.16.0"\n')
GATES = ("gate,status\nA1,CONDITIONAL\nA2,BLOCKED\n")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_release(tmp, *, zip_mut=None, gates=GATES, lock=LOCK,
                 idx_mut=None, sums_mut=None, prov_mut=None,
                 pol_mut=None, sbom_mut=None):
    inner = {"uv.lock": lock.encode(),
             "results_gate_table.csv": gates.encode(),
             "final_manifest.json": b"{}"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in sorted(inner.items()):
            z.writestr(f"fixture_src/{n}", b)
    zb = buf.getvalue()
    if zip_mut:
        zb = zip_mut(zb)
    zp = tmp / "fixture_release.zip"
    zp.write_bytes(zb)
    entries = [{"name": zp.name, "size": len(zb), "sha256": _sha(zb)}]
    for n, b in sorted(inner.items()):
        entries.append({"name": n, "size": len(b), "sha256": _sha(b)})
    bundle = tmp / "bundle"
    bundle.mkdir(exist_ok=True)
    sums = "".join(f"{e['sha256']}  {e['name']}\n" for e in entries)
    if sums_mut:
        sums = sums_mut(sums)
    (bundle / "SHA256SUMS").write_text(sums)
    sbom = {"bomFormat": "CycloneDX", "specVersion": "1.5",
            "components": [
                {"name": n, "version": v,
                 "purl": f"pkg:pypi/{n}@{v}"}
                for n, v in re.findall(
                    r'name = "([^"]+)"\nversion = "([^"]+)"', lock)]}
    if sbom_mut:
        sbom = sbom_mut(sbom)
    (bundle / "sbom.cdx.json").write_text(json.dumps(sbom))
    prov = {"_type": "https://in-toto.io/Statement/v1",
            "subject": [{"name": e["name"],
                         "digest": {"sha256": e["sha256"]}}
                        for e in entries],
            "predicateType": "https://slsa.dev/provenance/v1",
            "predicate": {"runDetails": {"builder":
                                          {"id": "qta:local-sandbox"}}}}
    if prov_mut:
        prov = prov_mut(prov)
    (bundle / "provenance.intoto.json").write_text(json.dumps(prov))
    pol = {"wildcards_forbidden": True,
           "trusted_builders": ["qta:local-sandbox"],
           "signer_identity": "PENDING: exact identity"}
    if pol_mut:
        pol = pol_mut(pol)
    (bundle / "release_trust_policy.json").write_text(json.dumps(pol))
    idx = {"release_artifact": entries[0], "files": entries,
           "provenance": {"slsa_level_claimed": "NONE"},
           "signing_status": "PENDING", "signature_bundles": [],
           "claims": {"scientific_gate_PASS_count": 0}}
    if idx_mut:
        idx = idx_mut(idx)
    (bundle / "release_index.json").write_text(json.dumps(idx))
    return zp, bundle


def run(zp, bundle, online=False, capsys=None):
    return VR.verify(zp, bundle, online)


def test_intact_release_verifies(tmp_path):
    zp, b = make_release(tmp_path)
    assert run(zp, b) == 0


def test_altered_zip_rejected(tmp_path):
    zp, b = make_release(tmp_path,
                         zip_mut=lambda x: x[:-1] + bytes([x[-1] ^ 1]))
    # bundle was built from ORIGINAL bytes -> mut applied after: rebuild
    zp2, b2 = make_release(tmp_path)
    data = bytearray(zp2.read_bytes())
    data[-1] ^= 1
    zp2.write_bytes(bytes(data))
    assert run(zp2, b2) == 1


def test_altered_inner_artifact_rejected(tmp_path):
    def mut(z):
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(z)) as src, \
                zipfile.ZipFile(buf, "w") as out:
            for n in src.namelist():
                d = src.read(n)
                if n.endswith("final_manifest.json"):
                    d = b'{"tampered":1}'
                out.writestr(n, d)
        return buf.getvalue()
    zp, b = make_release(tmp_path)
    # rebuild zip with tampered member but keep the original bundle sums
    zp.write_bytes(mut(zp.read_bytes()))
    # index zip digest now differs too -> multiple failures expected
    assert run(zp, b) == 1


def test_missing_artifact_rejected(tmp_path):
    zp, b = make_release(
        tmp_path,
        sums_mut=lambda s: s + _sha(b"ghost") + "  ghost.json\n",
        idx_mut=lambda i: {**i, "files": i["files"] + [
            {"name": "ghost.json", "size": 5, "sha256": _sha(b"ghost")}]},
        prov_mut=lambda p: {**p, "subject": p["subject"] + [
            {"name": "ghost.json",
             "digest": {"sha256": _sha(b"ghost")}}]})
    assert run(zp, b) == 1


def test_duplicate_sums_entry_rejected(tmp_path):
    zp, b = make_release(
        tmp_path, sums_mut=lambda s: s + s.splitlines()[0] + "\n")
    assert run(zp, b) == 1


def test_wrong_provenance_subject_rejected(tmp_path):
    def pm(p):
        p["subject"][0]["digest"]["sha256"] = "0" * 64
        return p
    zp, b = make_release(tmp_path, prov_mut=pm)
    assert run(zp, b) == 1


def test_slsa_level_without_hosted_rejected(tmp_path):
    zp, b = make_release(
        tmp_path,
        idx_mut=lambda i: {**i, "provenance":
                           {"slsa_level_claimed": "SLSA_BUILD_L3"}})
    assert run(zp, b) == 1


def test_wildcard_policy_rejected(tmp_path):
    zp, b = make_release(
        tmp_path, pol_mut=lambda p: {**p, "signer_identity": "*"})
    assert run(zp, b) == 1


def test_signed_claim_without_bundles_rejected(tmp_path):
    zp, b = make_release(
        tmp_path, idx_mut=lambda i: {**i, "signing_status": "SIGNED"})
    assert run(zp, b) == 1


def test_online_without_real_signature_fails(tmp_path):
    zp, b = make_release(tmp_path)
    assert run(zp, b, online=True) == 1     # absence never success


def test_secret_and_abs_path_rejected(tmp_path):
    zp, b = make_release(
        tmp_path,
        pol_mut=lambda p: {**p, "leak": "AKIA" + "A" * 16})
    assert run(zp, b) == 1
    zp2, b2 = make_release(
        tmp_path, idx_mut=lambda i: {**i, "path": "/home/x/y"})
    assert run(zp2, b2) == 1


def test_claim_violation_and_pass_injection_rejected(tmp_path):
    zp, b = make_release(
        tmp_path,
        idx_mut=lambda i: {**i, "note": "experiment performed"})
    assert run(zp, b) == 1
    zp2, b2 = make_release(tmp_path,
                           gates="gate,status\nA1,PASS\n")
    assert run(zp2, b2) == 1


def test_dependency_drift_rejected(tmp_path):
    def sm(s):
        s["components"][0]["version"] = "9.9.9"
        return s
    zp, b = make_release(tmp_path, sbom_mut=sm)
    assert run(zp, b) == 1
    # scientific pin drift via lock swap
    zp2, b2 = make_release(
        tmp_path, lock=LOCK.replace("2.4.4", "2.4.5"))
    assert run(zp2, b2) == 1


def test_real_generator_smoke(tmp_path):
    r = subprocess.run(
        [sys.executable, "build_release_artifacts.py", "--zip",
         "nonexistent.zip", "--out", str(tmp_path / "o")],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "FAIL-CLOSED" in r.stdout


@DET
@given(flip=st.integers(min_value=0, max_value=200))
def test_prop_any_zip_byteflip_rejected(flip, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("hf")
    zp, b = make_release(tmp)
    data = bytearray(zp.read_bytes())
    data[flip % len(data)] ^= 0xFF
    zp.write_bytes(bytes(data))
    assert run(zp, b) == 1


TESTS = "pytest-only module"

if __name__ == "__main__":
    print("run via pytest: .venv/bin/python -m pytest "
          "tests/test_stage9_release.py")
    sys.exit(0)
