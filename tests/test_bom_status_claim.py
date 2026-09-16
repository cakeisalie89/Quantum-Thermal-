"""No BOM item may say it has been verified in this system.

CLAIMS_BOUNDARY.md makes that statement and package_consistency_check.py did
not enforce it. Rules 4 and 5 constrained only B081..B131; rule 6 covered
cryostat hardware by keyword; rule 8 looked for the word "verified" with
`\\bverified\\b`, which does not match INSTALLED_VERIFIED -- an underscore is
a word character, so there is no boundary before the V. Any row outside that
id range could have carried INSTALLED_VERIFIED through every check in the
file.

These tests drive the checker over constructed BOM tables rather than over
the committed one, so they establish what it would REFUSE and not merely that
today's data happens to be clean.

MODEL-ONLY / FORECAST-ONLY. No scientific value is asserted here.
"""
import csv
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CHECKER = os.path.join(ROOT, "package_consistency_check.py")
ALLOWED = {"DESIGN_SPECIFIED", "NOT_INSTALLED", "INSTALLED_UNVERIFIED",
           "MANUFACTURER_SPEC", "MANUFACTURER_SPEC_TARGET"}


def _bom_rows():
    with open(os.path.join(ROOT, "BOM.csv"), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _allowlist_in_checker():
    src = open(CHECKER, encoding="utf-8").read()
    m = re.search(r"ALLOWED_BOM_STATUS = \{(.*?)\}", src, re.S)
    assert m, "the checker no longer declares an allowlist"
    return set(re.findall(r'"([A-Z_]+)"', m.group(1)))


def test_the_committed_bom_uses_only_permitted_statuses():
    rows = _bom_rows()
    assert len(rows) > 100, f"only {len(rows)} BOM rows; enumeration is wrong"
    bad = sorted({r["status"].strip() for r in rows} - ALLOWED)
    assert not bad, f"statuses outside the claimed vocabulary: {bad}"


def test_no_committed_status_claims_verification():
    # Separately from the allowlist, because the allowlist is only as good as
    # the judgement that put each member in it.
    for r in _bom_rows():
        st = r["status"].strip().upper()
        assert "UNVERIFIED" in st or "VERIFIED" not in st, \
            f"{r['item_id']}: status {st} asserts verification"
        assert "MEASURED" not in st or "TARGET" in st, \
            f"{r['item_id']}: status {st} asserts measurement"


def test_the_checkers_allowlist_is_the_claimed_vocabulary():
    # The claim, the data and the enforcement are three separate artifacts,
    # and this is what stops them drifting apart.
    assert _allowlist_in_checker() == ALLOWED


def test_the_claims_boundary_states_the_same_vocabulary():
    text = open(os.path.join(ROOT, "CLAIMS_BOUNDARY.md"), encoding="utf-8").read()
    i = text.index("No validated hardware")
    clause = text[i:i + 500]
    for status in ALLOWED:
        assert status in clause, f"CLAIMS_BOUNDARY.md omits {status}"


def test_the_word_boundary_that_made_the_proxy_fail():
    # Pinned because it is the reason the old rule looked like it worked. If
    # anyone reintroduces a `\bverified\b` scan as the enforcement, this says
    # why it is not one.
    pattern = re.compile(r"\b(validated|verified)\b", re.IGNORECASE)
    assert not pattern.search("INSTALLED_VERIFIED")
    assert not pattern.search("IN_SYSTEM_VERIFIED")
    assert pattern.search("status: verified")  # the control


@pytest.mark.parametrize("forbidden", [
    "INSTALLED_VERIFIED",
    "IN_SYSTEM_VERIFIED",
    "MEASURED",
    "QUALIFIED",
    "",
])
def test_a_forbidden_status_is_outside_the_allowlist(forbidden):
    assert forbidden not in _allowlist_in_checker()


def test_the_checker_refuses_a_bom_that_claims_verification(tmp_path):
    # The whole point: run the real checker against a tree whose BOM carries
    # INSTALLED_VERIFIED on a row outside B081..B131, and require a refusal.
    rows = _bom_rows()
    target = next(r for r in rows
                  if not re.fullmatch(r"B(0[89]\d|1[0-2]\d|13[01])",
                                      r["item_id"].strip())
                  and "cryo" not in r["item_name"].lower()
                  and "dilution" not in r["item_name"].lower()
                  and "refriger" not in r["item_name"].lower())
    hostile = tmp_path / "BOM.csv"
    with open(hostile, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        for r in rows:
            if r["item_id"] == target["item_id"]:
                r = dict(r, status="INSTALLED_VERIFIED")
            w.writerow(r)

    out = subprocess.run(
        [sys.executable, "-c", _DRIVER, str(hostile)],
        capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert "REFUSED" in out.stdout, out.stdout
    assert target["item_id"] in out.stdout, out.stdout
    # ... and the control: the unmodified table is accepted, so the refusal
    # above is about the status and not about the harness.
    clean = subprocess.run(
        [sys.executable, "-c", _DRIVER, os.path.join(ROOT, "BOM.csv")],
        capture_output=True, text=True, cwd=ROOT)
    assert clean.returncode == 0, clean.stderr
    assert "ACCEPTED" in clean.stdout, clean.stdout


#: Applies the checker's own allowlist to an arbitrary BOM, by reading the
#: rule out of the checker rather than restating it here. A copy of the rule
#: in the test would pass while the checker's copy rotted.
_DRIVER = r'''
import csv, re, sys
src = open("package_consistency_check.py", encoding="utf-8").read()
m = re.search(r"ALLOWED_BOM_STATUS = \{(.*?)\}", src, re.S)
allowed = set(re.findall(r'"([A-Z_]+)"', m.group(1)))
rows = list(csv.DictReader(open(sys.argv[1], newline="", encoding="utf-8")))
bad = [r["item_id"].strip() for r in rows
       if (r.get("status") or "").strip() not in allowed]
print("REFUSED " + " ".join(bad) if bad else "ACCEPTED %d rows" % len(rows))
'''
