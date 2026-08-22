"""Manifest coverage-boundary tests: what final_manifest.json must cover.

MODEL-ONLY / FORECAST-ONLY. Software-verification results only; the scientific
gate PASS count remains zero and is asserted so below.

These exist because the committed manifest was once internally consistent —
every hash it listed was correct — while silently omitting 30 tracked files
that had been added through the GitHub web UI after it was last written.
Nothing detected that, because nothing checked membership in the direction
that mattered. The audit and the decision are recorded in
``MANIFEST_BOUNDARY.md``; these tests are what keep the boundary from drifting
again.

Two kinds of test live here:

* **Hermetic generator tests** build a throwaway git repository, copy the
  generator into it, and exercise inclusion, ordering, hashing, determinism,
  new-file pickup, and drift detection against a tree whose contents the test
  controls exactly.
* **Repository invariant tests** assert the properties the real manifest must
  hold right now — in sync, self-exclusion, and the preservation-vs-authority
  rule that keeps ``attic/`` hashed even though it is documented as outside
  the governed project.
"""
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest                                                    # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "generate_manifest.py"
MANIFEST = ROOT / "final_manifest.json"
HASHFILE = ROOT / "manifest_hash.txt"
DETACHED = {"final_manifest.json", "manifest_hash.txt"}

SEED_MANIFEST = {
    "package": "fixture",
    "verdict": "fixture verdict",
    "canonical_state": {"total_gates": 0, "PASS": 0},
    "mode_separation": "fixture",
    "pass_history": "fixture",
    "files": [],
    "self_hash_policy": "DETACHED MANIFEST HASHING. manifest_hash.txt is "
                        "detached and is not listed in files[].",
}


def _sha(path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=check)


def _run_generator(repo, *args):
    return subprocess.run([sys.executable, "generate_manifest.py", *args],
                          cwd=str(repo), capture_output=True, text=True)


@pytest.fixture
def fixture_repo(tmp_path):
    """A throwaway git repo carrying the real generator and a seed manifest."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "archive").mkdir()
    (repo / "alpha.py").write_text("alpha\n")
    (repo / "beta.csv").write_text("b,1\n")
    (repo / "pkg" / "mod.py").write_text("mod\n")
    (repo / "archive" / "old.bundle").write_bytes(b"\x00binary evidence\n")
    (repo / "final_manifest.json").write_text(
        json.dumps(SEED_MANIFEST, indent=2, ensure_ascii=True))
    (repo / "manifest_hash.txt").write_text("sha256: " + "0" * 64 + "\n")
    shutil.copy2(GENERATOR, repo / "generate_manifest.py")

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fixture")
    return repo


def _listed(repo):
    doc = json.loads((repo / "final_manifest.json").read_text())
    return [e["filename"] for e in doc["files"]]


# ------------------------- inclusion / exclusion ---------------------------

def test_generator_covers_every_tracked_file(fixture_repo):
    assert _run_generator(fixture_repo).returncode == 0
    tracked = set(_git(fixture_repo, "ls-files").stdout.split())
    assert set(_listed(fixture_repo)) == tracked - DETACHED


def test_only_the_two_detached_files_are_excluded(fixture_repo):
    """The exclusion set is exactly two, and it is not open to extension."""
    _run_generator(fixture_repo)
    tracked = set(_git(fixture_repo, "ls-files").stdout.split())
    excluded = tracked - set(_listed(fixture_repo))
    assert excluded == DETACHED


def test_archive_and_binary_evidence_are_covered(fixture_repo):
    """Preservation and authority are separate: evidence is hashed too."""
    _run_generator(fixture_repo)
    assert "archive/old.bundle" in _listed(fixture_repo)


def test_manifest_never_lists_itself(fixture_repo):
    """No self-hash recursion: a manifest cannot contain its own digest."""
    _run_generator(fixture_repo)
    assert "final_manifest.json" not in _listed(fixture_repo)
    assert "manifest_hash.txt" not in _listed(fixture_repo)


def test_detached_hash_is_written_and_correct(fixture_repo):
    _run_generator(fixture_repo)
    text = (fixture_repo / "manifest_hash.txt").read_text()
    assert _sha(fixture_repo / "final_manifest.json") in text


# ------------------------------ correctness --------------------------------

def test_recorded_sha256_and_size_match_the_bytes(fixture_repo):
    _run_generator(fixture_repo)
    doc = json.loads((fixture_repo / "final_manifest.json").read_text())
    assert doc["files"], "expected a non-empty file list"
    for entry in doc["files"]:
        path = fixture_repo / entry["filename"]
        assert entry["sha256"] == _sha(path), entry["filename"]
        assert entry["size_bytes"] == path.stat().st_size, entry["filename"]
        assert len(entry["sha256"]) == 64


def test_path_ordering_is_stable_across_runs(fixture_repo):
    _run_generator(fixture_repo)
    first = _listed(fixture_repo)
    _run_generator(fixture_repo)
    assert _listed(fixture_repo) == first


def test_generation_is_byte_deterministic(fixture_repo):
    """Same tree in, same manifest bytes out — no timestamps, no host paths."""
    _run_generator(fixture_repo)
    first = (fixture_repo / "final_manifest.json").read_bytes()
    first_hash = (fixture_repo / "manifest_hash.txt").read_bytes()
    _run_generator(fixture_repo)
    assert (fixture_repo / "final_manifest.json").read_bytes() == first
    assert (fixture_repo / "manifest_hash.txt").read_bytes() == first_hash


def test_newly_tracked_files_are_picked_up_in_sorted_position(fixture_repo):
    """The exact failure that produced the 30-file gap must now be caught."""
    _run_generator(fixture_repo)
    before = _listed(fixture_repo)
    assert "gamma.md" not in before

    (fixture_repo / "gamma.md").write_text("gamma\n")
    _git(fixture_repo, "add", "gamma.md")
    # untracked-but-present is invisible to the generator by design...
    assert _run_generator(fixture_repo).returncode == 0
    after = _listed(fixture_repo)
    assert "gamma.md" in after
    # ...and appended in sorted order among the newly seen files
    new_names = [f for f in after if f not in before]
    assert new_names == sorted(new_names)


def test_untracked_files_are_not_covered(fixture_repo):
    """Coverage follows git membership, not the filesystem."""
    (fixture_repo / "scratch.tmp").write_text("not tracked\n")
    _run_generator(fixture_repo)
    assert "scratch.tmp" not in _listed(fixture_repo)


# --------------------------- drift detection -------------------------------

def test_check_mode_passes_on_a_synchronised_tree(fixture_repo):
    _run_generator(fixture_repo)
    result = _run_generator(fixture_repo, "--check")
    assert result.returncode == 0, result.stderr
    assert "in sync" in result.stdout


def test_check_mode_detects_a_tracked_but_unlisted_file(fixture_repo):
    _run_generator(fixture_repo)
    (fixture_repo / "late.py").write_text("late\n")
    _git(fixture_repo, "add", "late.py")
    result = _run_generator(fixture_repo, "--check")
    assert result.returncode == 1
    assert "tracked but not listed: late.py" in result.stderr


def test_check_mode_detects_changed_bytes(fixture_repo):
    _run_generator(fixture_repo)
    (fixture_repo / "alpha.py").write_text("alpha modified\n")
    result = _run_generator(fixture_repo, "--check")
    assert result.returncode == 1
    assert "sha256 mismatch: alpha.py" in result.stderr


def test_check_mode_detects_a_broken_detached_hash(fixture_repo):
    _run_generator(fixture_repo)
    (fixture_repo / "manifest_hash.txt").write_text("sha256: " + "1" * 64 + "\n")
    result = _run_generator(fixture_repo, "--check")
    assert result.returncode == 1
    assert "manifest_hash.txt does not match" in result.stderr


# --------------------- invariants of the real repository -------------------

def test_repository_manifest_is_in_sync():
    result = subprocess.run([sys.executable, "generate_manifest.py", "--check"],
                            cwd=str(ROOT), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_repository_manifest_excludes_exactly_the_detached_pair():
    listed = {e["filename"]
              for e in json.loads(MANIFEST.read_text())["files"]}
    tracked = set(subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                                 capture_output=True,
                                 text=True).stdout.split("\n")) - {""}
    assert tracked - listed == DETACHED
    assert not listed - tracked


def test_repository_detached_hash_matches():
    assert _sha(MANIFEST) in HASHFILE.read_text()


def test_coverage_policy_is_declared_and_says_provenance_not_authority():
    policy = json.loads(MANIFEST.read_text())["coverage_policy"]
    assert policy["record_type"] == "PROVENANCE"
    assert sorted(policy["exclusions"]) == sorted(DETACHED)
    assert "AUTHORITIES.md" in policy["authority_register"]
    assert "--check" in policy["drift_check"]


def test_preserved_but_ungoverned_material_is_still_hashed():
    """attic/ is documented as outside the governed project — and stays hashed.

    This is the invariant that makes the boundary meaningful: if someone ever
    'cleans up' the manifest by excluding non-governed material, this fails.
    """
    listed = {e["filename"]
              for e in json.loads(MANIFEST.read_text())["files"]}
    attic = [f for f in subprocess.run(["git", "-C", str(ROOT), "ls-files",
                                        "attic"], capture_output=True,
                                       text=True).stdout.split("\n") if f]
    assert attic, "expected attic/ to be tracked"
    assert set(attic) <= listed
    assert "not part of the governed project" in (ROOT / "README.md").read_text()


def test_manifest_change_did_not_touch_scientific_state():
    import csv
    rows = list(csv.DictReader(open(ROOT / "results_gate_table.csv")))
    assert len(rows) == 83
    assert sum(r["status"] == "PASS" for r in rows) == 0
    state = json.loads(MANIFEST.read_text())["canonical_state"]
    assert state["PASS"] == 0
    assert state["total_gates"] == 83


# ------------- §12: semantic verification, distinct from byte coverage -------
#
# --check used to verify only that the listed bytes were the actual bytes. The
# manifest's narrative fields (gate counts, PASS count) were preserved verbatim
# with a stderr warning on mismatch, so a stale count could survive every
# regeneration and every check. A warning is not verification.

def _seed_gate_table(repo, statuses):
    """Write a results_gate_table.csv the generator can derive counts from."""
    lines = ["gid,status"]
    lines += [f"G{i},{s}" for i, s in enumerate(statuses)]
    (repo / "results_gate_table.csv").write_text("\n".join(lines) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "gates")


def test_generator_derives_gate_counts_from_the_gate_table(fixture_repo):
    _seed_gate_table(fixture_repo, ["CONDITIONAL"] * 3 + ["BLOCKED"] * 2)
    assert _run_generator(fixture_repo).returncode == 0
    state = json.loads((fixture_repo / "final_manifest.json").read_text())["canonical_state"]
    assert state["total_gates"] == 5
    assert state["CONDITIONAL"] == 3
    assert state["BLOCKED"] == 2
    assert state["PASS"] == 0


def test_check_fails_on_semantic_drift_not_just_bytes(fixture_repo):
    """The exact hole: bytes all correct, narrative silently stale."""
    _seed_gate_table(fixture_repo, ["CONDITIONAL"] * 3 + ["BLOCKED"] * 2)
    _run_generator(fixture_repo)
    doc = json.loads((fixture_repo / "final_manifest.json").read_text())
    doc["canonical_state"]["BLOCKED"] = 99
    (fixture_repo / "final_manifest.json").write_text(
        json.dumps(doc, indent=2, ensure_ascii=True))
    digest = hashlib.sha256(
        (fixture_repo / "final_manifest.json").read_bytes()).hexdigest()
    (fixture_repo / "manifest_hash.txt").write_text(f"sha256: {digest}\n")
    result = _run_generator(fixture_repo, "--check")
    assert result.returncode == 1, "semantic drift passed --check"
    assert "canonical_state.BLOCKED" in result.stderr


def test_check_rejects_a_pass_row(fixture_repo):
    """PASS is 0 by design; a PASS row must fail the manifest check outright."""
    _seed_gate_table(fixture_repo, ["CONDITIONAL", "PASS"])
    _run_generator(fixture_repo)
    result = _run_generator(fixture_repo, "--check")
    assert result.returncode == 1
    assert "PASS" in result.stderr


def test_documented_ordering_policy_matches_the_generator(fixture_repo):
    """Existing order preserved, new tracked files appended sorted."""
    _run_generator(fixture_repo)
    first = _listed(fixture_repo)
    (fixture_repo / "aaa_new.py").write_text("new\n")
    (fixture_repo / "zzz_new.py").write_text("new\n")
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "-qm", "add two")
    _run_generator(fixture_repo)
    second = _listed(fixture_repo)
    assert second[:len(first)] == first, "existing entries were reordered"
    assert second[len(first):] == ["aaa_new.py", "zzz_new.py"], \
        "new entries were not appended in sorted order"
    assert second != sorted(second), \
        "the list is not globally sorted; the docstring must not claim it is"


def test_generator_docstring_does_not_claim_a_sorted_file_list():
    src = GENERATOR.read_text(encoding="utf-8")
    head = src.split('"""')[1]
    assert "the file list is sorted" not in head, \
        "module docstring restates the withdrawn sorted-list claim"
