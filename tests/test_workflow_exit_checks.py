"""A workflow step that checks a command's failure must be able to see it.

D-2026-152. The supply-chain job's release-gate step ran ``admit`` and then
read ``code=$?`` to require exit 3, the refusal of a CI signature. GitHub
runs a step's script with ``bash -e``, so the refusal ended the step before
``code`` was set: at 1c5df5c the gate refused correctly and the job failed
on that correct refusal. Had ``admit`` wrongly admitted (exit 0), the same
step would have reached its check with ``code`` 0 and failed for that
reason, so only a hosted run could tell the two apart, and it was the first
hosted run of the step.

``tools/workflow_contract.py``'s ``vacuous_exit_captures`` refuses a ``$?``
read in an errexit shell outside the form ``cmd || code=$?``. These tests
hold the rule to how bash actually behaves, to the step as it was, and to
the cases it must leave alone.
"""
from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import workflow_contract as WC  # noqa: E402

BROKEN_AT = "1c5df5cfa0003c8e7dbf1ea7ae2f01b71808c324"
GATE_STEP = "the release gate must refuse a CI signature (exit 3)"


def _bash_e(script: str) -> subprocess.CompletedProcess:
    """As the runner does: ``bash -e {file}``."""
    return subprocess.run(["bash", "-e", "-c", script], capture_output=True,
                          text=True)


# ---- what bash does, which is what the rule is about --------------------------

def test_control_under_bash_e_a_failing_command_ends_the_script_before_its_capture():
    r = _bash_e("set -uo pipefail\n(exit 3)\ncode=$?\necho reached $code\n")
    assert r.returncode == 3
    assert "reached" not in r.stdout


def test_the_or_capture_sees_the_exit_status():
    r = _bash_e("code=0\n(exit 3) || code=$?\necho reached $code\n")
    assert r.returncode == 0 and r.stdout.strip() == "reached 3"


# ---- the rule ------------------------------------------------------------------

def _workflow(run: str, **step) -> dict:
    return {"w.yml": {"jobs": {"j": {"steps": [
        dict({"name": "s", "run": run}, **step)]}}}}


@pytest.mark.parametrize("run", [
    "(exit 3)\ncode=$?\n",
    "(exit 3); code=$?\n",
    "(exit 3)\nif [ $? -ne 3 ]; then exit 1; fi\n",
    "set -uo pipefail\n(exit 3)\necho \"exit $?\"\n",
])
def test_a_status_read_after_an_unguarded_command_is_refused(run):
    found = WC.vacuous_exit_captures(_workflow(run))
    assert len(found) == 1 and "w.yml/j/s" in found[0], found


@pytest.mark.parametrize("shell", ["bash", "sh"])
def test_a_named_errexit_shell_is_held_too(shell):
    assert WC.vacuous_exit_captures(_workflow("(exit 3)\ncode=$?\n",
                                              shell=shell))


def test_a_job_default_errexit_shell_is_held_too():
    docs = _workflow("(exit 3)\ncode=$?\n")
    docs["w.yml"]["jobs"]["j"]["defaults"] = {"run": {"shell": "bash"}}
    assert WC.vacuous_exit_captures(docs)


@pytest.mark.parametrize("run, step", [
    ("code=0\n(exit 3) || code=$?\n", {}),
    ("set +e\n(exit 3)\ncode=$?\n", {}),
    ("(exit 3)\ncode=$?\n", {"shell": "bash {0}"}),
    ("cat > run.sh <<'SH'\nset -u\n(exit 3); a=$?\nSH\nbash run.sh\n", {}),
])
def test_control_the_forms_that_do_see_the_status_are_left_alone(run, step):
    assert WC.vacuous_exit_captures(_workflow(run, **step)) == ()


def test_the_step_as_it_was_is_refused_and_as_it_is_holds():
    """Differential: the rule finds the shipped defect where it was, in
    the step that failed hosted, and nothing in the tree now."""
    old = subprocess.run(
        ["git", "show", f"{BROKEN_AT}:.github/workflows/supply-chain.yml"],
        cwd=ROOT, capture_output=True, text=True)
    if old.returncode != 0:
        pytest.skip(f"{BROKEN_AT[:7]} is not in this clone")
    found = WC.vacuous_exit_captures(
        {"supply-chain.yml": yaml.safe_load(old.stdout)})
    assert [f for f in found if GATE_STEP in f] and len(found) == 1, found
    now = WC._load_workflows()
    assert "supply-chain.yml" in now, "the scan would be vacuous"
    assert WC.vacuous_exit_captures(now) == ()


def test_the_offline_verifier_script_is_checked_for_what_it_is():
    """The offline-verify job writes its checks to a script it runs with
    plain bash, where ``; a=$?`` does see the status. Moving those lines
    into the step's own shell must be refused."""
    now = WC._load_workflows()
    job = now["supply-chain.yml"]["jobs"]["offline-verify"]
    step = next(s for s in job["steps"] if "a=$?" in str(s.get("run")))
    moved = copy.deepcopy(now)
    target = next(s for s in moved["supply-chain.yml"]["jobs"][
        "offline-verify"]["steps"] if s.get("run") == step["run"])
    target["run"] = "\n".join(
        line for line in step["run"].splitlines()
        if line.strip() not in ("SH",) and "<<'SH'" not in line)
    assert WC.vacuous_exit_captures(moved)


def test_the_contract_runs_the_rule():
    import inspect
    assert "vacuous_exit_captures()" in inspect.getsource(WC.problems)
