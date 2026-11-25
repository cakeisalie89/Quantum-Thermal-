"""The assurance package is built, not written by hand: a fresh build, here
and now, from the governed run as it is, verifies in a process of its own
and says what the committed one says.

This is kept apart from tests/test_assurance_package.py, whose cases copy
the committed package: a build runs the governed objective and the slab's
error budget, about thirty seconds, and the mutation matrix need not pay it
for every mutant.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import assurance_package as AP  # noqa: E402
import verify_assurance as V  # noqa: E402


def test_a_fresh_build_verifies_and_says_what_the_committed_package_says(
        tmp_path):
    out = tmp_path / "built"
    record = AP.build(out, name="pytest-assurance-build")
    rc, report = AP.run_verifier(out)
    assert rc == 0 and report["verdict"] == "VERIFIED", report.get("stderr")
    committed = json.loads((AP.PACKAGE / V.RECORD).read_bytes())
    for key in ("schema", "package_id", "verifier", "hypothesis",
                "mathematical_statement", "assumptions", "model", "inputs",
                "formal", "establishment", "authority", "trust_roots",
                "not_established", "reproduction"):
        assert record[key] == committed[key], key
    assert set(record["evidence"]) == set(committed["evidence"])
    assert [(d["name"], d["action"], d["outcome"]) for d in
            record["authorization"]["decisions"]] == [
        (d["name"], d["action"], d["outcome"]) for d in
        committed["authorization"]["decisions"]]
    # the run is the same run: the same records in the same order, though
    # its identifiers and times are its own
    built = [json.loads(x)["action"] for x in
             (out / V.LOG).read_bytes().splitlines()]
    then = [json.loads(x)["action"] for x in
            (AP.PACKAGE / V.LOG).read_bytes().splitlines()]
    assert built == then


def test_a_build_does_not_write_over_anything(tmp_path):
    (tmp_path / "taken").mkdir()
    try:
        AP.build(tmp_path / "taken")
    except SystemExit as exc:
        assert "written whole, or not" in str(exc)
    else:
        raise AssertionError("a build wrote into an existing directory")
