"""Who may start the AI agent, what each job holds, and who may sign.

D-2026-146 (CI-4). The agent workflow's author gate was on this branch and
nothing in the repository held it there; the workflow contract checked
action pins but not the condition, the permission set, a PR-head checkout,
or that the identities able to sign -- the agent's and the CI signer's --
can never be a release identity. A system prompt is not a security
boundary, so each rule here is a check over the parsed workflows, each with
a negative control that breaks exactly that rule in a copy and requires it
reported.

What this does NOT do: a comment-triggered workflow runs from the DEFAULT
branch's file, and repository settings (branch protection, rulesets,
environments, the app's permissions) are not in any file. Those are owner
actions, listed in docs/security/owner_actions.md; the last test here
reads the default branch's workflows and records what is still wrong there
rather than claiming the controls deployed.
"""
from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import offline_verify as OV  # noqa: E402
import workflow_contract as WC  # noqa: E402


@pytest.fixture(scope="module")
def tree():
    return WC._load_workflows()


def _problems(docs) -> tuple:
    return WC.ai_authority_problems(docs)


def test_the_tree_holds_every_authority_rule(tree):
    assert set(tree) >= {"claude.yml", "supply-chain.yml",
                         "agent-substrate.yml"}, "the scan would be vacuous"
    assert _problems(tree) == ()


def _broken(tree, edit):
    docs = copy.deepcopy(tree)
    edit(docs)
    return _problems(docs)


def _agent_job(docs):
    return docs["claude.yml"]["jobs"]["claude"]


def _agent_action(docs):
    return next(s for s in _agent_job(docs)["steps"]
                if str(s.get("uses", "")).startswith(
                    "anthropics/claude-code-action@"))


def _checkout(docs, wf="claude.yml", job="claude"):
    return next(s for s in docs[wf]["jobs"][job]["steps"]
                if str(s.get("uses", "")).startswith("actions/checkout@"))


@pytest.mark.parametrize("edit, why", [
    # who may start the agent
    (lambda d: _agent_job(d).update(
        {"if": "contains(github.event.comment.body, '@claude')"}),
     "OWNER, MEMBER or COLLABORATOR"),
    (lambda d: _agent_job(d).update({"if": _agent_job(d)["if"].replace(
        '"COLLABORATOR"', '"COLLABORATOR", "CONTRIBUTOR"')}),
     "OWNER, MEMBER or COLLABORATOR"),
    (lambda d: _agent_job(d).update({"if": _agent_job(d)["if"].replace(
        ") && (", ") || (")}),
     "not a conjunct"),
    (lambda d: _agent_action(d)["with"].update(allowed_bots="*"),
     "sets allowed_bots"),
    (lambda d: _agent_action(d)["with"].update(
        allowed_non_write_users="*"), "sets allowed_non_write_users"),
    # what it may reach
    (lambda d: _checkout(d)["with"].update(
        ref="${{ github.event.pull_request.head.sha }}"),
     "the checkout names a ref"),
    (lambda d: _agent_job(d)["permissions"].update(
        {"administration": "write"}), "claude.yml/claude: permissions"),
    (lambda d: _agent_job(d)["permissions"].update(
        {"actions": "write"}), "claude.yml/claude: permissions"),
    # what every other job may hold
    (lambda d: d["agent-substrate.yml"]["jobs"]["full-suite"].update(
        permissions={"contents": "write"}), "full-suite: permissions"),
    (lambda d: d["agent-substrate.yml"]["jobs"]["full-suite"].update(
        permissions={"contents": "read", "id-token": "write"}),
     "full-suite: permissions"),
    (lambda d: d["supply-chain.yml"]["jobs"]["offline-verify"].update(
        permissions={"contents": "read", "id-token": "write"}),
     "offline-verify: permissions"),
    (lambda d: d["agent-substrate.yml"].update(permissions="write-all"),
     "not default-deny"),
    (lambda d: d["agent-substrate.yml"].update(
        {True: {**d["agent-substrate.yml"][True],
                "pull_request_target": None}}),
     "triggered by pull_request_target"),
    (lambda d: d["stack-verify.yml"].update(
        {True: {**d["stack-verify.yml"][True], "workflow_run": None}}),
     "triggered by workflow_run"),
    (lambda d: _checkout(d, "stack-verify.yml", "stack-verify")[
        "with"].update({"persist-credentials": True}),
     "leaves its token in the workspace"),
    (lambda d: d["stack-verify.yml"]["jobs"]["stack-verify"]["steps"]
     .append({"run": "echo ${{ github.event.comment.body }}"}),
     "expands github.event"),
    (lambda d: d["stack-verify.yml"]["jobs"]["stack-verify"]["steps"]
     .append({"run": "x", "env": {"K": "${{ secrets.ANTHROPIC_API_KEY }}"}}),
     "names secrets.ANTHROPIC_API_KEY"),
    # the CI signer never signs on a release trigger
    (lambda d: d["supply-chain.yml"][True]["push"].update(tags=["v*"]),
     "runs on a tag push"),
])
def test_each_rule_reports_its_own_violation(tree, edit, why):
    found = _broken(tree, edit)
    assert any(why in f for f in found), found


def test_the_agent_and_the_ci_signer_can_never_be_release_identities():
    for wf in (".github/workflows/claude.yml",
               ".github/workflows/supply-chain.yml"):
        assert wf in OV.NON_RELEASE_WORKFLOWS


def test_the_rule_reports_a_signer_a_release_policy_may_name(monkeypatch,
                                                            tree):
    """CONTROL for the test above, through the contract: drop the agent's
    workflow from the set and the contract says so."""
    monkeypatch.setattr(OV, "NON_RELEASE_WORKFLOWS",
                        OV.CI_VALIDATION_WORKFLOWS)
    assert any("claude.yml: a release policy may name it" in f
               for f in _problems(tree))


def test_the_contract_runs_the_authority_rules():
    """problems() -- what CI's workflow-contract step calls -- includes
    them, so deleting the call is a contract failure, not a quiet loss."""
    import inspect
    assert "ai_authority_problems()" in inspect.getsource(WC.problems)


def test_the_default_branch_is_judged_by_the_same_rules():
    """The deployed agent is the default branch's file, and the rule must
    read it right: its verdict on the gate is compared with an independent,
    crude reading of the raw file (does the condition mention
    author_association at all). Observed at the audit (main 096fb90): no
    gate, and two retired workflows minting OIDC tokens -- CI-1 and CI-3,
    owner actions in docs/security/owner_actions.md. This does not pass
    because the branch is fixed; it passes when the rule and the raw file
    agree, before and after the owner acts."""
    ok = subprocess.run(["git", "rev-parse", "--verify", "-q",
                         "origin/main^{commit}"], cwd=ROOT,
                        capture_output=True).returncode == 0
    if not ok:
        pytest.skip("the default branch's commit is not in this clone")
    raw = subprocess.run(["git", "show",
                          "origin/main:.github/workflows/claude.yml"],
                         cwd=ROOT, capture_output=True, text=True)
    if raw.returncode != 0:
        pytest.skip("the default branch carries no agent workflow")
    found = WC.ai_authority_problems(WC._load_workflows("origin/main"))
    rule_says_ungated = any("claude.yml: the job's condition" in f
                            for f in found)
    raw_mentions_gate = "author_association" in raw.stdout
    assert rule_says_ungated is (not raw_mentions_gate), found
