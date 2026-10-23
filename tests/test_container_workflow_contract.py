"""The container workflow verifies the declared image and nothing else.

container-verify.yml must not acquire privilege it does not need, must test
the image the Dockerfile declares rather than a convenient substitute, and
must not write anything back. And every hosted workflow pins every action it
uses to an immutable commit.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
CONTAINER = WF / "container-verify.yml"


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _on(doc):
    # PyYAML parses the bare key `on` as the boolean True.
    return doc.get("on", doc.get(True))


def _executable_text(path):
    """Workflow text without comment lines: the header comments NAME what
    the workflow must not do, and a scan for it should not find the
    prohibition itself."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(line.split("  #")[0] if "  #" in line else line)
    return "\n".join(out)


def test_container_verify_runs_per_push_to_this_branch_and_is_read_only():
    """Evidence about the container has to be about the commit it ran on, so
    it runs on push -- to this branch only, never on a fork's pull_request
    -- and stays read-only."""
    doc = _load(CONTAINER)
    on = _on(doc)
    assert set(on) == {"push", "workflow_dispatch"}
    assert on["push"]["branches"] == ["claude/scientific-python-stack-7rsq3m"]
    assert doc["permissions"] == {}
    perms = doc["jobs"]["container-verify"]["permissions"]
    assert perms == {"contents": "read"}


def test_container_verify_tests_the_declared_image_not_a_substitute():
    t = _executable_text(CONTAINER)
    assert "grep -E '^FROM ' Dockerfile" in t
    assert "docker build -t harness:hosted-verify ." in t
    assert not re.search(r"FROM\s+python:", t), (
        "the workflow restates a base image instead of reading the "
        "Dockerfile")


def test_container_verify_asserts_the_non_root_user_and_no_host_venv():
    t = _executable_text(CONTAINER)
    assert '[ "$U" = "harness" ]' in t
    assert "/opt/venv" in t
    assert ".venv" not in t, "must not mount or reference a host virtualenv"


def test_container_verify_writes_nothing_back():
    t = _executable_text(CONTAINER)
    for forbidden in ("git push", "git commit", "generate_manifest.py"):
        assert forbidden not in t, f"container workflow runs {forbidden!r}"


def test_every_action_in_every_workflow_is_sha_pinned():
    workflows = sorted(WF.glob("*.yml"))
    assert workflows, "no workflows found: the scan would be vacuous"
    for path in workflows:
        for m in re.finditer(r"uses:\s*([^\s@]+)@([^\s#]+)",
                             path.read_text(encoding="utf-8")):
            assert re.fullmatch(r"[0-9a-f]{40}", m.group(2)), (
                f"{path.name}: {m.group(1)}@{m.group(2)} is not a commit "
                "SHA")


def test_the_pin_scan_finds_a_tag(tmp_path):
    """CONTROL: a tag reference is not a pin, and the scan says so."""
    text = "steps:\n  - uses: actions/checkout@v4\n"
    refs = [m.group(2) for m in
            re.finditer(r"uses:\s*([^\s@]+)@([^\s#]+)", text)]
    assert refs == ["v4"]
    assert not re.fullmatch(r"[0-9a-f]{40}", refs[0])


# --- the image the workflow builds -----------------------------------------

DOCKERFILE = ROOT / "Dockerfile"
VERIFY = ROOT / "container_verify.sh"


def _dockerfile_lines():
    return [ln.strip() for ln in DOCKERFILE.read_text(
        encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def test_the_image_runs_as_the_declared_non_root_user():
    lines = _dockerfile_lines()
    users = [ln for ln in lines if ln.startswith("USER ")]
    assert users == ["USER harness"], users
    assert any(ln.startswith("RUN useradd") and ln.endswith(" harness")
               for ln in lines)


def test_the_base_image_is_pinned_by_digest():
    froms = [ln for ln in _dockerfile_lines() if ln.startswith("FROM ")]
    assert len(froms) == 1, froms
    assert re.fullmatch(r"FROM \S+@sha256:[0-9a-f]{64}", froms[0]), froms[0]


def _verify_commands():
    return [ln.strip() for ln in VERIFY.read_text(
        encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def test_the_in_image_verification_fails_closed():
    """Every command's failure stops it, and a suite that collects too few
    tests is a failure, not a smaller pass."""
    cmds = _verify_commands()
    assert "set -euo pipefail" in cmds
    text = "\n".join(cmds)
    assert '-lt 300 ]' in text and "exit 1" in text


@pytest.mark.parametrize("command", [
    "python tools/neural.py verify",
    "python tools/repo_content_audit.py --check",
    "python tools/repo_inventory.py check --applied",
    "python tools/claims_enforcement.py",
    "python tools/completion_matrix.py",
    "python tools/harness_demo.py run",
    "python -m pytest tests/ -q",
    "python generate_manifest.py --check",
    "python ro_crate_tools.py validate",
])
def test_the_in_image_verification_runs(command):
    assert any(command in c for c in _verify_commands()), command


def test_the_suite_runs_in_the_image_and_is_not_only_collected():
    """Counting what pytest would collect is not running it: the collection
    guard and the run are two commands, and the run must be one of them."""
    runs = [c for c in _verify_commands()
            if "python -m pytest tests/" in c and "--collect-only" not in c]
    assert runs, "the image collects the suite but never runs it"


# --- the step record, readable from the tail of a job log ------------------

def test_the_container_script_reports_every_step():
    """Steps once recorded as passing by INFERENCE from `set -e` ordering:
    the job-logs API served only the pytest tail, so their output was never
    read. A marker per step turns the inference into a record, and a summary
    in an EXIT trap puts it in the tail the API serves."""
    src = VERIFY.read_text(encoding="utf-8")
    for step in ("environment", "git-available", "harness-generic",
                 "harness-demo", "pytest", "manifest_freshness", "ro_crate"):
        assert f'step "{step}"' in src, f"{step} does not announce itself"
        assert f'done_ "{step}"' in src, f"{step} does not report success"
    assert "trap _harness_summary EXIT" in src
    for marker in ("::HARNESS-STEPS-COMPLETED::", "::HARNESS-STEP-FAILED::",
                   "::HARNESS-EXIT::"):
        assert marker in src, marker


def test_the_summary_prints_on_failure_and_names_the_failing_step(tmp_path):
    """Run the trap for real. A trap nobody fires is a comment, and the
    failing case is the one where knowing how far the script got matters."""
    import subprocess

    src = VERIFY.read_text(encoding="utf-8")
    start = src.index("HARNESS_STEPS_OK=")
    end = src.index("trap _harness_summary EXIT") + len(
        "trap _harness_summary EXIT")
    demo = tmp_path / "demo.sh"
    demo.write_text(
        "set -euo pipefail\n" + src[start:end] + "\n"
        'step "one"; true; done_ "one"\n'
        'step "two"; true; done_ "two"\n'
        'step "three"; false; done_ "three"\n')
    proc = subprocess.run(["bash", str(demo)], capture_output=True,
                          text=True)
    assert proc.returncode == 1, proc.returncode
    out = proc.stdout
    assert "::HARNESS-STEPS-COMPLETED:: one,two" in out, out
    assert "::HARNESS-STEP-FAILED:: three" in out, out
    assert "::HARNESS-EXIT:: 1" in out, out
    # ANTI-VACUITY: the summary must not claim a step that did not finish.
    assert "three" not in out.split("::HARNESS-STEPS-COMPLETED::")[1].split(
        "\n")[0]


def test_the_container_installs_git_and_ships_the_repository():
    """Governance tests died with FileNotFoundError in hosted run
    33113363458 because the image had no git. Installing git without
    shipping .git would be worse than neither: `git ls-files` exits 128
    outside a repository, and a scan built on it returns an EMPTY set -- so
    every structural guard passes having examined nothing."""
    assert "install -y --no-install-recommends git" in DOCKERFILE.read_text(
        encoding="utf-8")
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert not any(line.strip() == ".git" for line in ignore.splitlines())
