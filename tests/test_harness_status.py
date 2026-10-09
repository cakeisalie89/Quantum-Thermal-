"""The harness completion status is derived, and refuses COMPLETE.

Each test builds a small synthetic set of registries -- a contract, a stack,
a matrix whose evidence is recomputed from implementation digests, the
learned-model status and claims -- and changes exactly one thing, so a
refusal is attributable to that one thing. The audits are injected; the real
ones are run by the derive/verify step against the real repository (the last
test), which is what CI checks.

The evidence tool is tested the same way: a fake API that answers what a
real one could, and every way a run can fail to be evidence about a commit.
"""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import attach_hosted_evidence as AHE  # noqa: E402
import completion_matrix as CM  # noqa: E402
import harness_status as HS  # noqa: E402

CLEAN = {name: {"ok": True} for name in (
    "pass_semantics", "neural_legacy_semantics", "framework_boundary",
    "claims_boundary", "workflow_contract")}


def _digest(root: Path, paths):
    def read(p):
        q = root / p
        return q.read_bytes() if q.is_file() else None
    return CM.implementation_digest(paths, read, lambda p: [])


def _row(rid, root, *, classification=CM.COMPLETE, gaps=(), covered=True):
    impl = [f"impl_{rid}.py"]
    (root / impl[0]).write_text(f"# {rid}\n")
    ev = {"runs": ["1"], "commit": "a" * 40,
          "implementation_sha256": _digest(root, impl) if covered
          else "0" * 64, "note": "synthetic"}
    return {"id": rid, "classification": classification,
            "implementation": impl, "residual_gaps": list(gaps),
            "hosted_evidence": ev}


def _write(root: Path, *, contract, stack, rows):
    (root / "docs" / "neural").mkdir(parents=True, exist_ok=True)
    (root / HS.CONTRACT).write_text(json.dumps(contract))
    (root / HS.STACK).write_text(json.dumps(stack))
    (root / HS.MATRIX).write_text(json.dumps({"rows": rows}))
    (root / HS.NEURAL_STATUS).write_text(json.dumps({
        "legacy_qta_hardware_forecast": {"classification": "LEGACY_QTA_ONLY",
                                         "PASS_count": 0},
        "flagship": {"real_weights_allocated": False,
                     "large_model_trained": False}}))
    (root / HS.NEURAL_CLAIMS).write_text(json.dumps({
        "learned_refused_states": ["PROMOTED", "VERIFIED"],
        "acceptance_attempt": {"refused": True}}))


@pytest.fixture
def repo(tmp_path):
    contract = {
        "required_stack": {"core": {"accept": ["ADOPTED"], "rows": ["R1"]},
                           "rust": {"accept": ["RESOLVED"], "rows": ["R2"]}},
        "required_rows": "ALL",
        "required_audits": sorted(CLEAN),
        "required_facts": {"legacy_qta_pass_count_is_legacy_only": "",
                           "learned_outputs_refused": "",
                           "flagship_not_allocated_or_trained": ""},
        "outside_this_software_completion": [
            {"id": "x", "class": "EPISTEMIC", "statement": "s"}]}
    stack = {"elements": [{"id": "core", "status": "ADOPTED"},
                          {"id": "rust", "status": "RESOLVED"}]}
    rows = [_row("R1", tmp_path), _row("R2", tmp_path)]
    state = {"contract": contract, "stack": stack, "rows": rows}

    def build(mutate=None):
        s = copy.deepcopy(state)
        if mutate:
            mutate(s, tmp_path)
        audits = s.pop("audits", CLEAN)
        _write(tmp_path, **s)
        return HS.derive(tmp_path, audit_results=audits)
    return build


def test_everything_satisfied_derives_complete(repo):
    st = repo()
    assert st["outcome"] == HS.COMPLETE, st["unsatisfied"]
    assert st["unsatisfied"] == []


@pytest.mark.parametrize("status", ["STAGED", "DEFERRED",
                                    "ADOPTED_ADMISSION_MECHANISM_ONLY"])
def test_a_required_element_in_an_unaccepted_state_is_incomplete(repo,
                                                                 status):
    def m(s, _):
        s["stack"]["elements"][0]["status"] = status
    st = repo(m)
    assert st["outcome"] == HS.INCOMPLETE
    assert {u["id"] for u in st["unsatisfied"]} == {"core"}


def test_a_required_element_missing_from_the_stack_is_unclassified(repo):
    def m(s, _):
        s["stack"]["elements"] = s["stack"]["elements"][1:]
    st = repo(m)
    assert st["outcome"] == HS.INCOMPLETE
    assert st["stack"]["core"]["status"] == "UNCLASSIFIED"


def test_a_stack_element_the_contract_does_not_mention_is_refused(repo):
    def m(s, _):
        s["stack"]["elements"].append({"id": "extra", "status": "ADOPTED"})
    st = repo(m)
    assert st["outcome"] == HS.INCOMPLETE
    assert [u["id"] for u in st["unsatisfied"]] == ["extra"]
    assert "UNCLASSIFIED" in st["unsatisfied"][0]["status"]


def test_evidence_that_predates_the_implementation_is_incomplete(repo):
    def m(s, root):
        (root / "impl_R1.py").write_text("# changed after the run\n")
    st = repo(m)
    assert st["outcome"] == HS.INCOMPLETE
    assert st["stack"]["core"]["rows"]["R1"]["evidence"] == CM.EV_PREDATES
    assert {u["id"] for u in st["unsatisfied"]} == {"core", "R1"}


def test_a_row_never_run_is_incomplete(repo):
    def m(s, _):
        s["rows"][1]["hosted_evidence"] = {}
    st = repo(m)
    assert st["outcome"] == HS.INCOMPLETE
    assert st["stack"]["rust"]["rows"]["R2"]["evidence"] == CM.EV_NEVER_RUN


def test_an_unrecorded_commit_is_not_evidence(repo):
    def m(s, _):
        s["rows"][0]["hosted_evidence"]["commit"] = "UNRECORDED"
    assert repo(m)["outcome"] == HS.INCOMPLETE


def test_a_residual_gap_keeps_a_complete_row_incomplete(repo):
    def m(s, _):
        s["rows"][0]["residual_gaps"] = ["one actionable thing"]
    st = repo(m)
    assert st["outcome"] == HS.INCOMPLETE
    assert "1 gaps" in [u for u in st["unsatisfied"]
                        if u["id"] == "R1"][0]["status"]


def test_a_row_below_complete_is_incomplete(repo):
    def m(s, _):
        s["rows"][0]["classification"] = \
            "DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS"
    assert repo(m)["outcome"] == HS.INCOMPLETE


def test_a_required_row_that_does_not_exist_is_absent(repo):
    def m(s, _):
        s["contract"]["required_stack"]["core"]["rows"] = ["R1", "R99"]
    st = repo(m)
    assert st["stack"]["core"]["rows"]["R99"]["classification"] == "ABSENT"
    assert st["outcome"] == HS.INCOMPLETE


def test_an_audit_finding_is_incomplete(repo):
    def m(s, _):
        s["audits"] = dict(CLEAN, framework_boundary={"ok": False})
    st = repo(m)
    assert st["outcome"] == HS.INCOMPLETE
    assert [u["id"] for u in st["unsatisfied"]] == ["framework_boundary"]


def test_a_missing_audit_is_not_a_clean_one(repo):
    def m(s, _):
        s["audits"] = {k: v for k, v in CLEAN.items()
                       if k != "claims_boundary"}
    assert repo(m)["outcome"] == HS.INCOMPLETE


@pytest.mark.parametrize("path, key, value", [
    (HS.NEURAL_STATUS, ("legacy_qta_hardware_forecast", "classification"),
     "CURRENT"),
    (HS.NEURAL_STATUS, ("flagship", "large_model_trained"), True),
    (HS.NEURAL_CLAIMS, ("learned_refused_states",), ["PROMOTED"]),
    (HS.NEURAL_CLAIMS, ("acceptance_attempt", "refused"), False),
])
def test_a_fact_that_does_not_hold_is_incomplete(repo, tmp_path, path, key,
                                                 value):
    repo()
    doc = json.loads((tmp_path / path).read_text())
    d = doc
    for k in key[:-1]:
        d = d[k]
    d[key[-1]] = value
    (tmp_path / path).write_text(json.dumps(doc))
    st = HS.derive(tmp_path, audit_results=CLEAN)
    assert st["outcome"] == HS.INCOMPLETE
    assert [u["kind"] for u in st["unsatisfied"]] == ["fact"]


def test_blocked_only_when_everything_unsatisfied_is_external(repo):
    def m(s, _):
        s["stack"]["elements"][1]["status"] = HS.EXTERNALLY_BLOCKED
        s["rows"][1]["classification"] = HS.EXTERNALLY_BLOCKED
    assert repo(m)["outcome"] == HS.BLOCKED

    def m2(s, root):
        m(s, root)
        s["rows"][0]["residual_gaps"] = ["actionable"]
    assert repo(m2)["outcome"] == HS.INCOMPLETE


def test_an_actionable_row_backing_no_element_is_not_blocked(repo,
                                                            tmp_path):
    def m(s, root):
        s["stack"]["elements"][1]["status"] = HS.EXTERNALLY_BLOCKED
        s["rows"][1]["classification"] = HS.EXTERNALLY_BLOCKED
        s["rows"].append(_row("R3", root, gaps=["actionable"]))
    st = repo(m)
    assert st["outcome"] == HS.INCOMPLETE
    r3 = [u for u in st["unsatisfied"] if u["id"] == "R3"]
    assert r3 and r3[0]["external"] is False


def test_the_derivation_is_deterministic_and_records_its_inputs(repo,
                                                               tmp_path):
    a = json.dumps(repo(), sort_keys=True)
    b = json.dumps(HS.derive(tmp_path, audit_results=CLEAN), sort_keys=True)
    assert a == b
    st = json.loads(a)
    assert st["derived_from"][str(HS.MATRIX)] == hashlib.sha256(
        (tmp_path / HS.MATRIX).read_bytes()).hexdigest()


def test_render_names_every_unsatisfied_item(repo):
    def m(s, _):
        s["stack"]["elements"][0]["status"] = "STAGED"
    md = HS.render(repo(m))
    assert "**Outcome: INCOMPLETE**" in md
    assert "* stack core: STAGED" in md


def test_verify_refuses_a_stale_or_missing_document(repo, tmp_path):
    st = repo()
    for rel, text in HS.documents(st).items():
        (tmp_path / rel).write_text(text)
    assert HS.stale(tmp_path, st) == []
    md = tmp_path / HS.OUT_MD
    md.write_text(md.read_text() + "* a line added by hand\n")
    assert HS.stale(tmp_path, st) == [str(HS.OUT_MD)]
    (tmp_path / HS.OUT_JSON).unlink()
    assert HS.stale(tmp_path, st) == [str(HS.OUT_JSON), str(HS.OUT_MD)]


def test_require_complete_refuses_anything_but_complete(repo, tmp_path):
    def m(s, _):
        s["stack"]["elements"][0]["status"] = "STAGED"
    st = repo(m)
    for rel, text in HS.documents(st).items():
        (tmp_path / rel).write_text(text)
    _, problems = HS.verify(tmp_path, audit_results=CLEAN)
    assert problems == []
    _, problems = HS.verify(tmp_path, require_complete=True,
                            audit_results=CLEAN)
    assert problems == ["REQUIRED: COMPLETE, derived INCOMPLETE"]


def test_the_committed_status_is_what_the_repository_derives():
    """The committed documents are current: no hand edit, no stale copy."""
    r = subprocess.run([sys.executable, str(ROOT / "tools" /
                                            "harness_status.py"), "verify"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


# ----------------------------------------------------------------- evidence

SHA = "b" * 40


def _api(run=None, jobs=None):
    run = {"head_sha": SHA, "status": "completed", "conclusion": "success",
           "name": "wf", "event": "push", **(run or {})}
    jobs = {"jobs": [{"name": "a", "conclusion": "success"}]} \
        if jobs is None else jobs

    def fetch(path):
        return jobs if path.endswith("per_page=100") else run
    return fetch


def test_a_green_run_on_the_commit_is_accepted():
    assert AHE.check_run("1", SHA, _api())["jobs"] == 1


@pytest.mark.parametrize("run, jobs, why", [
    ({"head_sha": "c" * 40}, None, "ran on"),
    ({"status": "in_progress", "conclusion": None}, None, "not completed"),
    ({"conclusion": "failure"}, None, "not completed"),
    (None, {"jobs": [{"name": "a", "conclusion": "success"},
                     {"name": "b", "conclusion": "skipped"}]}, "['b']"),
    (None, {"jobs": []}, "none listed"),
])
def test_a_run_that_is_not_evidence_about_the_commit_is_refused(run, jobs,
                                                                why):
    with pytest.raises(AHE.EvidenceRefused, match=__import__("re").escape(
            why)):
        AHE.check_run("1", SHA, _api(run, jobs))


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_evidence_is_digested_at_the_commit_not_the_working_tree(tmp_path):
    """The run tested the commit; a file edited since must not be stamped
    as covered by it."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "impl.py").write_text("as committed\n")
    _git(tmp_path, "add", "impl.py")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-qm", "c")
    head = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "impl.py").write_text("edited after the run\n")
    doc = {"rows": [{"id": "R1", "implementation": ["impl.py"]}]}

    def fetch(path):
        if path.endswith("per_page=100"):
            return {"jobs": [{"name": "a", "conclusion": "success"}]}
        return {"head_sha": head, "status": "completed",
                "conclusion": "success", "name": "wf", "event": "push"}
    [(rid, digest)] = AHE.attach(doc, ["R1"], "HEAD", ["9"], "", fetch,
                                 root=tmp_path)
    assert doc["rows"][0]["hosted_evidence"]["commit"] == head
    committed = CM.implementation_digest(
        ["impl.py"], lambda p: b"as committed\n", lambda p: [])
    assert digest == committed

    def read(p):
        q = tmp_path / p
        return q.read_bytes() if q.is_file() else None
    assert CM.evidence_state(doc["rows"][0], read, lambda p: []) == \
        CM.EV_PREDATES


def test_attaching_to_a_row_that_does_not_exist_is_refused(tmp_path):
    with pytest.raises(AHE.EvidenceRefused, match="no row R404"):
        AHE.attach({"rows": []}, ["R404"], "HEAD", [], "", _api())
