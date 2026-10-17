"""Which release bytes are reproducible is a contract, and this holds it.

docs/security/reproducibility_contract.json names the files that must be
byte-identical when the same commit is built again, the files that may
differ only in named run-specific fields, and the files that belong to one
run. Two builds here run in SEPARATE fresh clones at different paths, with
different time zones, locales and umasks, and different invocation ids --
the conditions under which an accidental dependency on the environment
would show. Byte identity of files is all that is compared; nothing here
is a numerical claim (D-2026-145).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "security" / "reproducibility_contract.json"


def _build(clone_parent: Path, name: str, env_over: dict) -> Path:
    clone = clone_parent / name
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(ROOT),
                    str(clone)], check=True)
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                          check=True, capture_output=True,
                          text=True).stdout.strip()
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", head],
                   check=True)
    out = clone_parent / f"{name}-rc"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GITHUB_", "TZ", "LANG", "LC_"))}
    env.update(env_over)
    code = (f"import os; os.umask({env_over.pop('_UMASK', '0o022')}); "
            "import runpy, sys; sys.argv=['supply_chain.py','build','--out',"
            f"{str(out)!r}]; runpy.run_path("
            f"{str(clone / 'tools' / 'supply_chain.py')!r}, "
            "run_name='__main__')")
    subprocess.run([sys.executable, "-c", code], cwd=str(clone), env=env,
                   check=True, capture_output=True, timeout=600)
    return out


@pytest.fixture(scope="module")
def two_builds(tmp_path_factory):
    base = tmp_path_factory.mktemp("repro")
    a = _build(base, "alpha", {"TZ": "UTC", "LANG": "C.UTF-8",
                               "GITHUB_RUN_ID": "1001",
                               "GITHUB_RUN_ATTEMPT": "1",
                               "_UMASK": "0o022"})
    b = _build(base, "a-different-and-longer-path",
               {"TZ": "Pacific/Kiritimati", "LANG": "de_DE.UTF-8",
                "GITHUB_RUN_ID": "2002", "GITHUB_RUN_ATTEMPT": "3",
                "_UMASK": "0o077"})
    return a, b


def _contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def _drop(doc: dict, dotted: str) -> None:
    keys = dotted.split(".")
    # the index's artifact key is itself dotted: rejoin the tail
    if keys[0] == "artifacts":
        keys = ["artifacts", ".".join(keys[1:])]
    cur = doc
    for k in keys[:-1]:
        cur = cur[k]
    cur.pop(keys[-1])


def test_the_byte_identical_files_are_byte_identical(two_builds):
    a, b = two_builds
    for name in _contract()["byte_identical"]:
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_the_other_files_differ_only_in_their_named_fields(two_builds):
    a, b = two_builds
    for name, spec in _contract()["identical_except"].items():
        da = json.loads((a / name).read_text())
        db = json.loads((b / name).read_text())
        assert da != db, (f"{name} is identical across two invocations; "
                          "the contract's run-specific field is not run-"
                          "specific")
        for f in spec["fields"]:
            _drop(da, f)
            _drop(db, f)
        assert da == db, f"{name} differs outside {spec['fields']}"


def test_the_signed_list_is_run_specific_and_says_why(two_builds):
    """SHA256SUMS lists the provenance's digest, which carries the run id,
    so two invocations sign different lists -- the contract must not call
    it reproducible."""
    a, b = two_builds
    assert (a / "SHA256SUMS").read_bytes() != (b / "SHA256SUMS").read_bytes()
    assert "SHA256SUMS" in _contract()["run_specific"]
    la = dict(reversed(ln.split("  ")) for ln in
              (a / "SHA256SUMS").read_text().splitlines())
    lb = dict(reversed(ln.split("  ")) for ln in
              (b / "SHA256SUMS").read_text().splitlines())
    differ = {n for n in la if la[n] != lb[n]}
    assert differ == {"provenance.intoto.json", "index.json"}


def test_every_artifact_is_classified_exactly_once():
    c = _contract()
    names = (list(c["byte_identical"]) + list(c["identical_except"])
             + list(c["run_specific"]))
    assert len(names) == len(set(names))
    assert {"source.zip", "sbom.cdx.json", "provenance.intoto.json",
            "index.json", "SHA256SUMS", "SHA256SUMS.sigstore.json"} <= \
        set(names)


def test_a_build_reports_the_digests_the_contract_holds(two_builds,
                                                         tmp_path):
    """build() reports the deterministic payload's digests, so the hosted
    log of every run carries them and runs can be compared without their
    artifacts."""
    sys.path.insert(0, str(ROOT / "tools"))
    import supply_chain as SC
    out = SC.build(tmp_path / "rc", allow_dirty=True)
    assert set(out["deterministic_payload"]) == \
        set(_contract()["byte_identical"])


def test_the_shipped_comparator_agrees_and_is_not_vacuous(two_builds,
                                                          tmp_path):
    """tools/supply_chain.py compare is what the hosted rebuild job runs.
    It must agree with the comparison above on two real builds, and say
    the contract is broken when one byte of a byte-identical file, or one
    field outside the named ones, differs."""
    import shutil
    sys.path.insert(0, str(ROOT / "tools"))
    import supply_chain as SC
    a, b = two_builds
    contract = _contract()
    assert SC.compare_builds(a, b, contract)["holds"] is True

    flipped = tmp_path / "flipped"
    shutil.copytree(b, flipped)
    z = bytearray((flipped / "source.zip").read_bytes())
    z[500] ^= 0x01
    (flipped / "source.zip").write_bytes(bytes(z))
    rep = SC.compare_builds(a, flipped, contract)
    assert rep["holds"] is False
    assert rep["files"]["source.zip"]["holds"] is False

    drifted = tmp_path / "drifted"
    shutil.copytree(b, drifted)
    prov = json.loads((drifted / "provenance.intoto.json").read_text())
    prov["predicate"]["runDetails"]["builder"]["id"] = "elsewhere"
    (drifted / "provenance.intoto.json").write_text(json.dumps(prov))
    rep = SC.compare_builds(a, drifted, contract)
    assert rep["files"]["provenance.intoto.json"]["holds"] is False


def test_a_contract_field_the_build_lacks_is_refused(two_builds):
    sys.path.insert(0, str(ROOT / "tools"))
    import supply_chain as SC
    a, b = two_builds
    contract = _contract()
    contract["identical_except"]["index.json"]["fields"] = [
        "artifacts.no-such-artifact"]
    with pytest.raises(SC.Refused, match="does not have"):
        SC.compare_builds(a, b, contract)
