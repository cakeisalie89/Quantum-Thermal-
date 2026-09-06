"""Capabilities, tool contracts, and bounded execution.

Written adversarially. The system must resist a mistaken, stale or hostile
caller WITHOUT relying on that caller's cooperation, so almost every test here
is an attempt to get authority the caller was not granted.
"""
from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from qta_agent.capability import (  # noqa: E402
    Action, Capability, CapabilityDenied, CapabilityError,
    CapabilityExpired, CapabilityNotYetIssued, CapabilityRevoked,
    CapabilitySet, CapabilityUnknown, Request, capability_from_record,
    digest_is_consistent, issue,
)
from qta_agent.execution import (  # noqa: E402
    RETRYABLE, SUCCESSFUL, CancellationToken, ExecutionResult, Executor,
    Limits, Outcome, run_bounded,
)
from qta_agent.tools import (  # noqa: E402
    Determinism, Field_, OutputFile, Registry, SideEffect,
    ToolContractViolation, ToolError, ToolNotRegistered, ToolSpec,
)

PY = sys.executable
SCOPE = ("verification/stage10/probe",)


def _spec(**kw):
    base = dict(tool_id="probe", version="1.0", summary="a probe",
                inputs=(Field_("n", "int"),),
                determinism=Determinism.BYTE_IDENTICAL,
                writable_scope=SCOPE, timeout_s=5.0)
    base.update(kw)
    return ToolSpec(**base)


def _cap(**kw):
    base = dict(capability_id="c1", subject="agent-1",
                action=Action.EXECUTE_TOOL, task_id="t1", tool_id="probe",
                scope=SCOPE, issued_seq=1)
    base.update(kw)
    return issue(**base)


@pytest.fixture()
def env(tmp_path):
    spec = _spec()
    cap = _cap()
    return {
        "spec": spec,
        "registry": Registry([spec]),
        "caps": CapabilitySet(issued={"c1": cap}, at_seq=2),
        "cap": cap,
        "ws": tmp_path,
        "ex": Executor(Registry([spec]), workspace=tmp_path),
    }


def _run(env, argv, **kw):
    kw.setdefault("inputs", {"n": 1})
    kw.setdefault("env", {"PATH": "/usr/bin:/bin"})
    return env["ex"].run(tool_id="probe", actor="agent-1", task_id="t1",
                         capability_id="c1", capabilities=env["caps"],
                         argv=argv, **kw)


# --- capabilities are bounded grants, not flags -----------------------------

def test_a_grant_authorizes_exactly_what_it_names(env):
    edge = env["caps"].check("c1", Request(
        "agent-1", Action.EXECUTE_TOOL, "t1", "probe",
        ("verification/stage10/probe/out.json",)))
    assert edge.capability_id == "c1"


@pytest.mark.parametrize("req,exc,match", [
    (Request("agent-2", Action.EXECUTE_TOOL, "t1", "probe"),
     CapabilityDenied, "not 'agent-2'"),
    (Request("agent-1", Action.EXECUTE_TOOL, "t2", "probe"),
     CapabilityDenied, "confined to task"),
    (Request("agent-1", Action.EXECUTE_TOOL, "t1", "other_tool"),
     CapabilityDenied, "permits tool"),
    (Request("agent-1", Action.WRITE_PATHS, "t1"),
     CapabilityDenied, "permits EXECUTE_TOOL"),
    (Request("agent-1", Action.EXECUTE_TOOL, "t1", "probe", ("README.md",)),
     CapabilityDenied, "does not cover"),
])
def test_a_grant_is_not_portable(env, req, exc, match):
    """Each dimension separately: actor, task, tool, action, path.

    A grant that leaks along any one of these is a confused deputy waiting to
    happen -- the holder does something legitimate on behalf of a request that
    was not the one authorized.
    """
    with pytest.raises(exc, match=match):
        env["caps"].check("c1", req)


def test_scope_matching_is_by_path_component_not_by_string_prefix(env):
    """``stage10/probe2`` is not inside ``stage10/probe``.

    It IS a string prefix of it. A scope check written with ``startswith``
    grants the sibling directory, which is the kind of near-miss that looks
    right in review and is wrong in production.
    """
    with pytest.raises(CapabilityDenied, match="does not cover"):
        env["caps"].check("c1", Request(
            "agent-1", Action.EXECUTE_TOOL, "t1", "probe",
            ("verification/stage10/probe2/out.json",)))
    # ...and the real child is covered.
    env["caps"].check("c1", Request(
        "agent-1", Action.EXECUTE_TOOL, "t1", "probe",
        ("verification/stage10/probe/deep/out.json",)))


def test_a_capability_that_was_never_issued_authorizes_nothing(env):
    """Unforgeability comes from the issuing record, not from a secret value."""
    with pytest.raises(CapabilityUnknown, match="was ever issued"):
        env["caps"].check("forged", Request(
            "agent-1", Action.EXECUTE_TOOL, "t1", "probe"))


def test_revocation_takes_effect_immediately(env):
    revoked = CapabilitySet(issued={"c1": env["cap"]},
                            revoked=frozenset({"c1"}), at_seq=2)
    with pytest.raises(CapabilityRevoked, match="was revoked"):
        revoked.check("c1", Request("agent-1", Action.EXECUTE_TOOL, "t1",
                                    "probe"))


def test_expiry_is_measured_in_sequence_numbers_not_wall_time(env):
    """Wall clocks move backwards; the log's order does not.

    An expiry in wall time has a different answer depending on whose clock you
    ask. Expressed in seq, every reader of the same log agrees.
    """
    cap = _cap(expires_after_seq=10)
    assert CapabilitySet(issued={"c1": cap}, at_seq=10).check(
        "c1", Request("agent-1", Action.EXECUTE_TOOL, "t1", "probe"))
    with pytest.raises(CapabilityExpired, match="expired after seq 10"):
        CapabilitySet(issued={"c1": cap}, at_seq=11).check(
            "c1", Request("agent-1", Action.EXECUTE_TOOL, "t1", "probe"))


def test_revocation_is_checked_before_expiry(env):
    """The first thing that is wrong is the thing an operator can act on."""
    cap = _cap(expires_after_seq=5)
    both = CapabilitySet(issued={"c1": cap}, revoked=frozenset({"c1"}),
                         at_seq=99)
    with pytest.raises(CapabilityRevoked):
        both.check("c1", Request("agent-1", Action.EXECUTE_TOOL, "t1", "probe"))


@pytest.mark.parametrize("scope", [
    ["/etc/passwd"], ["../secret"], ["a/../../b"], [], ["."], [""],
])
def test_a_scope_that_could_mean_two_things_is_refused(scope):
    """Refused rather than normalised: normalising changes the grant."""
    with pytest.raises(CapabilityError):
        _cap(scope=scope)


def test_a_grant_that_expires_before_it_was_issued_is_refused():
    with pytest.raises(CapabilityError, match="never valid"):
        _cap(issued_seq=10, expires_after_seq=3)


def test_execute_tool_without_a_tool_id_is_refused():
    """A grant to run 'some tool' is a grant to run any tool."""
    with pytest.raises(CapabilityError, match="requires a tool_id"):
        issue(capability_id="c", subject="a", action=Action.EXECUTE_TOOL,
              task_id="t", scope=SCOPE, issued_seq=1)


def test_a_non_execute_grant_may_not_carry_a_tool_id():
    with pytest.raises(CapabilityError, match="does not take a tool_id"):
        issue(capability_id="c", subject="a", action=Action.WRITE_PATHS,
              task_id="t", tool_id="probe", scope=SCOPE, issued_seq=1)


def test_a_capability_digest_is_not_a_bearer_token(env):
    """Pinned as a LIMIT so nobody later treats the digest as authorization.

    It is derived from public fields, so anyone who can read a grant can
    recompute it. It is useful for noticing a record whose fields and digest
    disagree, and useless for proving anyone was granted anything.
    """
    cap = env["cap"]
    assert digest_is_consistent(cap, cap.digest())
    assert not digest_is_consistent(cap, "0" * 64)
    forged = Capability(**{**cap.__dict__, "subject": "attacker"})
    assert forged.digest() != cap.digest(), "fields must bind the digest"
    # Recomputing a digest for the forged grant does not make it usable: the
    # check consults the issued set, never the digest.
    with pytest.raises(CapabilityUnknown):
        env["caps"].check("whatever", Request("attacker",
                                              Action.EXECUTE_TOOL, "t1",
                                              "probe"))


def test_a_grant_round_trips_through_its_log_record(env):
    rebuilt = capability_from_record(env["cap"].body())
    assert rebuilt.digest() == env["cap"].digest()


@pytest.mark.parametrize("drop", ["capability_id", "subject", "task_id",
                                  "scope", "issued_seq"])
def test_a_malformed_capability_record_is_refused(env, drop):
    rec = env["cap"].body()
    del rec[drop]
    with pytest.raises(CapabilityError):
        capability_from_record(rec)


# --- tools: default deny ----------------------------------------------------

def test_an_unregistered_tool_does_not_run(env):
    """Conforming to a schema is not permission."""
    with pytest.raises(ToolNotRegistered, match="not registered"):
        env["registry"].get("rm_rf")


def test_the_registry_cannot_grow_after_construction(env):
    """A tool that appears at runtime is a tool nobody reviewed."""
    with pytest.raises(ToolError, match="frozen"):
        env["registry"]._add(_spec(tool_id="late"))


@pytest.mark.parametrize("bad", [None, 42, "", b"probe", ("probe",)])
def test_a_non_string_tool_id_is_refused_not_coerced(env, bad):
    with pytest.raises(ToolNotRegistered):
        env["registry"].get(bad)


@pytest.mark.parametrize("inputs,match", [
    ({"n": "1"}, "expected int"),
    ({"n": True}, "is a bool"),
    ({}, "missing 'n'"),
    ({"n": 1, "extra": 2}, "undeclared fields"),
    ([], "must be an object"),
])
def test_the_contract_is_the_whole_interface(env, inputs, match):
    with pytest.raises(ToolContractViolation, match=match):
        env["spec"].validate_inputs(inputs)


def test_an_unversioned_tool_cannot_be_registered():
    """A citation that does not say WHICH tool ran is not a citation."""
    with pytest.raises(ToolError, match="version"):
        Registry([_spec(version="")])


def test_a_tool_that_cannot_time_out_cannot_be_registered():
    with pytest.raises(ToolError, match="cannot time out"):
        Registry([_spec(timeout_s=0)])


def test_a_contract_that_contradicts_itself_is_refused():
    with pytest.raises(ToolError, match="one of the two is wrong"):
        Registry([_spec(side_effect=SideEffect.NONE)])
    with pytest.raises(ToolError, match="scope is what makes"):
        Registry([_spec(writable_scope=())])


def test_the_tool_digest_covers_the_contract_not_the_callable():
    """Two builds of the same contract must cite the same tool."""
    a = _spec(run=lambda: 1)
    b = _spec(run=lambda: 2)
    assert a.digest() == b.digest()
    assert _spec(version="2.0").digest() != a.digest()


# --- execution: what is, and is not, success --------------------------------

def test_a_clean_run_completes_and_records_the_real_exit_status(env):
    r = _run(env, [PY, "-c", "print('hello')"])
    assert r.outcome is Outcome.COMPLETED
    assert r.exit_status == 0 and r.signal_number is None
    assert r.succeeded and not r.retryable
    assert r.stdout_bytes == 6 and r.stdout_digest


def test_a_nonzero_exit_is_a_failure_and_is_not_retryable(env):
    """A tool that rejected its input will reject it again."""
    r = _run(env, [PY, "-c", "import sys; sys.exit(3)"])
    assert r.outcome is Outcome.FAILED and r.exit_status == 3
    assert not r.succeeded and not r.retryable


def test_a_timeout_is_not_a_success(env):
    """THE property. It may have finished one instruction before the deadline.

    Nothing observed it finish, so nothing may treat its output as finished.
    """
    started = time.time()
    r = _run(env, [PY, "-c", "import time; time.sleep(30)"],
             limits=Limits(wall_seconds=1.0))
    assert r.outcome is Outcome.TIMED_OUT
    assert not r.succeeded
    assert r.retryable, "a timeout is retryable; a rejection is not"
    assert time.time() - started < 15, "the bound must actually bound"
    assert "not a success" in r.reason


def test_cancellation_before_start_means_the_process_never_starts(env, tmp_path):
    """A cancellation that cannot prevent the work is not a cancellation."""
    marker = tmp_path / "ran"
    tok = CancellationToken()
    tok.cancel("operator stopped it")
    r = _run(env, [PY, "-c", f"open({str(marker)!r},'w').write('x')"],
             cancel=tok)
    assert r.outcome is Outcome.CANCELLED and not r.succeeded
    assert not marker.exists(), "the cancelled process ran anyway"


def test_cancellation_during_a_run_stops_it(env):
    import threading
    tok = CancellationToken()
    threading.Timer(0.4, lambda: tok.cancel("stopped mid-flight")).start()
    started = time.time()
    r = _run(env, [PY, "-c", "import time; time.sleep(30)"],
             limits=Limits(wall_seconds=20.0), cancel=tok)
    assert r.outcome is Outcome.CANCELLED and not r.succeeded
    assert time.time() - started < 15


def test_a_denied_run_is_not_reported_as_a_failed_run(env):
    """A failed run implies something was attempted. Nothing was."""
    r = env["ex"].run(tool_id="probe", actor="impostor", task_id="t1",
                      capability_id="c1", capabilities=env["caps"],
                      inputs={"n": 1}, argv=[PY, "-c", "print(1)"])
    assert r.outcome is Outcome.DENIED
    assert r.exit_status is None and r.signal_number is None
    assert not r.succeeded and not r.retryable


def test_registration_is_checked_before_authorization(env):
    """'No such tool' and 'you may not' are different answers.

    Checking the capability first would tell a prober which tool ids are real
    by the shape of the refusal.
    """
    with pytest.raises(ToolNotRegistered):
        env["ex"].run(tool_id="ghost", actor="impostor", task_id="t9",
                      capability_id="nope", capabilities=env["caps"],
                      inputs={}, argv=[PY, "-c", "print(1)"])


def test_the_child_does_not_inherit_the_callers_environment(env):
    """Inheritance is how a tool acquires credentials nobody granted it."""
    os.environ["QTA_TEST_SECRET"] = "do-not-leak"
    try:
        r = _run(env, [PY, "-c",
                       "import os; print(os.environ.get('QTA_TEST_SECRET','ABSENT'))"])
        assert r.outcome is Outcome.COMPLETED
        # The digest of b"ABSENT\n" -- the secret did not cross the boundary.
        from qta_agent.canonical import digest_bytes
        assert r.stdout_digest == digest_bytes(b"ABSENT\n")
    finally:
        del os.environ["QTA_TEST_SECRET"]


def test_the_output_cap_is_enforced_by_the_kernel(env):
    """A counter caps what you KEEP; RLIMIT_FSIZE caps what is produced."""
    r = _run(env, [PY, "-c", "print('x' * 10_000_000)"],
             limits=Limits(wall_seconds=20.0, output_bytes=4096))
    assert not r.succeeded
    assert r.stdout_bytes <= 4096, "the kernel did not stop the writer"


def test_the_address_space_cap_is_enforced(env):
    r = _run(env, [PY, "-c", "x = bytearray(4 * 1024**3)"],
             limits=Limits(wall_seconds=30.0,
                           address_space_bytes=256 * 1024 * 1024))
    assert not r.succeeded, "a memory bomb completed"


def test_partial_output_from_a_killed_process_is_captured(env):
    """A killed process that wrote half a file said where it got to."""
    r = _run(env, [PY, "-c",
                   "import sys,time; sys.stdout.write('partial'); "
                   "sys.stdout.flush(); time.sleep(30)"],
             limits=Limits(wall_seconds=1.0))
    assert r.outcome is Outcome.TIMED_OUT
    assert r.stdout_bytes == 7, "partial output was discarded"


def test_a_child_process_group_is_killed_with_its_parent(env, tmp_path):
    """Otherwise an orphaned grandchild outlives the timeout that killed it.

    The grandchild writes a marker on a delay. If the process GROUP is not
    signalled, it survives its parent and the marker appears after the run
    has been declared timed out.
    """
    marker = tmp_path / "orphan"
    script = (
        "import subprocess, sys, time;"
        f"subprocess.Popen([sys.executable,'-c',"
        f"\"import time; time.sleep(3); open({str(marker)!r},'w').write('x')\"]);"
        "time.sleep(30)"
    )
    r = _run(env, [PY, "-c", script], limits=Limits(wall_seconds=1.0))
    assert r.outcome is Outcome.TIMED_OUT
    time.sleep(5)
    assert not marker.exists(), (
        "an orphaned grandchild outlived the timeout that killed its parent")


def test_the_success_set_is_defined_once(env):
    """'Did it work' must not be re-decided differently at each call site."""
    assert SUCCESSFUL == frozenset({Outcome.COMPLETED})
    assert Outcome.TIMED_OUT not in SUCCESSFUL
    assert Outcome.CANCELLED not in SUCCESSFUL
    assert Outcome.DENIED not in SUCCESSFUL
    assert Outcome.FAILED not in RETRYABLE, (
        "retrying a deterministic rejection turns it into a load problem")


def test_every_result_carries_enough_to_judge_it(env):
    r = _run(env, [PY, "-c", "print(1)"])
    rec = r.to_record()
    for key in ("outcome", "tool_id", "tool_version", "tool_digest",
                "exit_status", "stdout_digest", "duration_s", "limits",
                "determinism", "side_effect", "reason"):
        assert key in rec, f"{key} absent from the execution record"
    assert rec["tool_digest"] == env["spec"].digest()
    assert rec["determinism"] == Determinism.BYTE_IDENTICAL.value


def test_run_bounded_leaves_no_temporary_files_behind(env, tmp_path):
    before = set(p.name for p in tmp_path.iterdir())
    run_bounded([PY, "-c", "print(1)"], spec=env["spec"], cwd=tmp_path,
                limits=Limits(wall_seconds=5.0), env={"PATH": "/usr/bin:/bin"})
    after = set(p.name for p in tmp_path.iterdir())
    assert after == before, f"left behind: {sorted(after - before)}"


def test_a_signalled_process_records_the_signal_not_an_exit_status(env):
    r = _run(env, [PY, "-c",
                   f"import os,signal; os.kill(os.getpid(), {signal.SIGUSR1})"],
             limits=Limits(wall_seconds=10.0))
    assert r.outcome is Outcome.FAILED
    assert r.signal_number == signal.SIGUSR1
    assert r.exit_status is None, "a signalled death is not an exit status"


# ---------------------------------------------------------------------------
# Mutation-isolating additions.
# ---------------------------------------------------------------------------

def test_invalid_inputs_are_refused_before_the_process_starts(env, tmp_path):
    """X14: the contract is checked through the Executor, not only directly.

    The contract tests above call ``validate_inputs`` themselves, which proves
    the validator works and nothing about whether the executor uses it.
    Removing the call from ``Executor.run`` survived the matrix for exactly
    that reason. The marker file is what makes this a real check: it proves
    the tool did not run, rather than that an exception happened somewhere.
    """
    marker = tmp_path / "should-not-exist"
    with pytest.raises(ToolContractViolation, match="expected int"):
        _run(env, [PY, "-c", f"open({str(marker)!r},'w').write('x')"],
             inputs={"n": "not-an-int"})
    assert not marker.exists(), "a tool ran on inputs its contract rejects"


def test_the_executor_never_signals_its_own_process_group(env, tmp_path):
    """The guard that stops the executor killing its own caller.

    ``setsid`` is forced to fail by pre-creating the situation it cannot
    recover from: the child is started WITHOUT a new session, so it shares this
    process group. A timeout must then signal the child alone. If the guard is
    absent, this test kills the test runner -- which is precisely how the real
    defect presented, and why the assertion below is that we are still here.
    """
    import subprocess as sp

    from qta_agent import execution as X

    class _SameGroup:
        """A stand-in whose pid really is in our process group."""

        def __init__(self, proc):
            self._proc = proc
            self.pid = proc.pid
            self.signalled = []

        def send_signal(self, sig):
            self.signalled.append(sig)
            self._proc.send_signal(sig)

    child = sp.Popen([PY, "-c", "import time; time.sleep(10)"],
                     stdout=sp.DEVNULL, stderr=sp.DEVNULL)
    try:
        assert os.getpgid(child.pid) == os.getpgrp(), (
            "fixture is wrong: the child must share our group for this to test "
            "anything")
        shim = _SameGroup(child)
        X._kill_group(shim, signal.SIGTERM)
        assert shim.signalled == [signal.SIGTERM], (
            "the child was not signalled individually")
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    # Reaching this line at all is the assertion: killpg would have killed us.
    assert True


def test_the_task_limit_is_relative_to_what_the_user_already_uses(env):
    """RLIMIT_NPROC is per-UID and counts threads, so it cannot be absolute.

    A hosted run proved this the expensive way: at an absolute 64, OpenBLAS
    could not create its worker threads on a runner whose user already held
    most of that budget, numpy's import died mid-way, and the governed run
    reported SIGINT with no obvious cause. The limit now means "this much MORE
    than the user is already using".
    """
    from qta_agent.execution import count_user_tasks

    baseline = count_user_tasks()
    assert baseline > 0, "this process is owned by someone"
    rec = Limits().to_record()
    assert rec["additional_tasks"] >= 64
    assert rec["nproc_baseline"] >= 1
    assert "processes" not in rec, (
        "the absolute name is gone; it described a limit that could not exist")


def test_a_tool_that_imports_a_threaded_numerical_stack_still_runs(env):
    """The regression test for the hosted failure, as close as it gets here.

    This sandbox runs as a user with few processes, so the ABSOLUTE limit
    happened to work here and failed on the runner -- which is exactly why
    this test asserts the import succeeds under DEFAULT limits rather than
    asserting a number. If the budget is ever made absolute again, a busy
    machine breaks it and this is the test that has a chance of noticing.
    """
    r = _run(env, [PY, "-c",
                   "import numpy; print(numpy.__version__)"],
             limits=Limits(wall_seconds=60.0))
    assert r.outcome is Outcome.COMPLETED, (
        f"numpy could not import under the default task budget: "
        f"{r.reason}\n{r.stderr_excerpt}")
    assert r.stdout_bytes > 0


def test_a_failure_carries_an_excerpt_a_human_can_act_on(env):
    """A digest is not a diagnosis.

    Excerpts are deliberately absent from to_record(), so they never enter the
    log or the result digest: the digests are the provenance, and a tool's raw
    output does not belong in a hash-chained record where anything it happened
    to print becomes permanent.
    """
    r = _run(env, [PY, "-c",
                   "import sys; sys.stderr.write('the reason it failed\\n'); "
                   "sys.exit(7)"])
    assert r.outcome is Outcome.FAILED and r.exit_status == 7
    assert "the reason it failed" in r.stderr_excerpt
    assert "stderr_excerpt" not in r.to_record()
    assert "stdout_excerpt" not in r.to_record()


def test_a_very_large_excerpt_is_elided_in_the_middle(env):
    """Bounded, and honest about what it dropped."""
    r = _run(env, [PY, "-c",
                   "import sys; sys.stderr.write('x' * 50000); sys.exit(1)"],
             limits=Limits(wall_seconds=30.0))
    assert len(r.stderr_excerpt) < 5000
    assert "chars elided" in r.stderr_excerpt


# ---------------------------------------------------------------------------
# CapabilityLedger: a grant is what the log says, not what the caller holds
# ---------------------------------------------------------------------------

def _ledger(tmp_path):
    from qta_agent.capability import CapabilityLedger
    from qta_agent.events import EventLog

    log = EventLog(tmp_path / "caps.jsonl")
    return log, CapabilityLedger(log).load()


def test_a_grant_the_log_never_recorded_is_not_in_force(tmp_path):
    """THE defect the ledger closes.

    ``CapabilitySet`` is a decision object: hand it grants and it authorizes.
    That is right for a checker and wrong for a system, because a caller that
    assembles the set can put anything in it. Issuing must go through the log
    or the log record is decorative.
    """
    log, ledger = _ledger(tmp_path)
    cap = _cap()
    ledger.issue(cap, actor="scheduler")

    assert ledger.issued_ids() == ("c1",)
    assert [ev.action for ev in log.read()] == ["capability.issue"]
    # A second ledger, built only from the log, reaches the same verdict.
    from qta_agent.capability import CapabilityLedger
    rebuilt = CapabilityLedger(log).load()
    assert rebuilt.in_force(2).check(
        "c1", Request(actor="agent-1", action=Action.EXECUTE_TOOL,
                      task_id="t1", tool_id="probe",
                      paths=("verification/stage10/probe/x",))
    ).capability_id == "c1"

    # And one nobody recorded is unknown, however well-formed it is.
    forged = _cap(capability_id="c2")
    assert forged.capability_id not in rebuilt.issued_ids()
    with pytest.raises(CapabilityUnknown):
        rebuilt.in_force(2).check(
            "c2", Request(actor="agent-1", action=Action.EXECUTE_TOOL,
                          task_id="t1", tool_id="probe",
                          paths=("verification/stage10/probe/x",)))


def test_revocation_recorded_in_the_log_stops_a_grant(tmp_path):
    log, ledger = _ledger(tmp_path)
    ledger.issue(_cap(), actor="scheduler")
    ledger.revoke("c1", actor="owner", reason="withdrawn")

    from qta_agent.capability import CapabilityLedger
    rebuilt = CapabilityLedger(log).load()
    assert rebuilt.revoked_ids() == ("c1",)
    with pytest.raises(CapabilityRevoked):
        rebuilt.in_force(3).check(
            "c1", Request(actor="agent-1", action=Action.EXECUTE_TOOL,
                          task_id="t1", tool_id="probe",
                          paths=("verification/stage10/probe/x",)))


def test_revoking_a_grant_that_was_never_issued_is_refused(tmp_path):
    log, ledger = _ledger(tmp_path)
    with pytest.raises(CapabilityError, match="no capability"):
        ledger.revoke("c-nope", actor="owner", reason="withdrawn")
    assert list(log.read()) == []


def test_one_capability_id_cannot_name_two_different_grants(tmp_path):
    """Two grants sharing an id cannot be told apart by anything citing one."""
    log, ledger = _ledger(tmp_path)
    ledger.issue(_cap(), actor="scheduler")
    with pytest.raises(CapabilityError, match="already exists"):
        ledger.issue(_cap(tool_id="other"), actor="scheduler")

    # And a log that already contains such a pair is refused on projection,
    # rather than silently resolving to whichever came last.
    from qta_agent.capability import ACT_ISSUE, CapabilityLedger
    conflicting = _cap(tool_id="other")
    log.append(actor="mallory", action=ACT_ISSUE, target="t1",
               payload={"task_id": "t1", **conflicting.body()})
    with pytest.raises(CapabilityError, match="issued twice"):
        CapabilityLedger(log).load()


def test_the_same_grant_recorded_twice_is_not_a_conflict(tmp_path):
    """Byte-identical re-issue is a replay, not two grants.

    Refusing it would make the projection reject a log that a retried append
    could legitimately produce.

    "Byte-identical" means identical to what was RECORDED, which is what
    :meth:`issue` returns -- the ledger stamps the grant's start with the seq
    its record lands at, so a caller's pre-stamp copy is a different grant.
    """
    from qta_agent.capability import ACT_ISSUE, CapabilityLedger

    log, ledger = _ledger(tmp_path)
    cap = ledger.issue(_cap(), actor="scheduler")
    log.append(actor="scheduler", action=ACT_ISSUE, target="t1",
               payload={"task_id": "t1", **cap.body()})
    assert CapabilityLedger(log).load().issued_ids() == ("c1",)


def test_a_revocation_naming_no_capability_is_refused(tmp_path):
    from qta_agent.capability import ACT_REVOKE, CapabilityLedger

    log, ledger = _ledger(tmp_path)
    ledger.issue(_cap(), actor="scheduler")
    log.append(actor="mallory", action=ACT_REVOKE, target="c1", payload={})
    with pytest.raises(CapabilityError, match="names no capability"):
        CapabilityLedger(log).load()


def test_the_ledger_refuses_to_project_an_unverified_log(tmp_path):
    """Grants read out of a history that may have been rewritten are not
    grants. Fail closed, as every other projection in this package does."""
    import json

    from qta_agent.capability import CapabilityLedger
    from qta_agent.events import ChainBroken

    log, ledger = _ledger(tmp_path)
    ledger.issue(_cap(), actor="scheduler")
    lines = log.path.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["payload"]["tool_id"] = "something-else"
    lines[0] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    log.path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ChainBroken):
        CapabilityLedger(log).load()


def test_the_ledger_skips_foreign_events_without_dropping_unknown_ones(
        tmp_path):
    """Several subsystems share one log; that must not make the ledger blind
    to an action nothing in this package writes."""
    from qta_agent.capability import CapabilityLedger

    log, ledger = _ledger(tmp_path)
    log.append(actor="w", action="task.create", target="t1",
               payload={"task_id": "t1"})
    ledger.issue(_cap(), actor="scheduler")
    assert CapabilityLedger(log).load().issued_ids() == ("c1",)
    assert ledger.apply(log.read()[0]) is False


# --- a grant has two ends, and only one was checked --------------------------

def test_a_grant_does_not_authorize_what_happened_before_it_existed():
    """THE MISSING HALF OF THE VALIDITY WINDOW.

    Expiry was checked. Issuance was not, so a capability recorded at seq 90
    answered "was this permitted at seq 20?" with yes -- and that is exactly
    the question an auditor asks. A grant written after an incident would
    have retroactively covered it.
    """
    cap = _cap(issued_seq=50)
    caps = CapabilitySet(issued={"c1": cap}, at_seq=20)
    with pytest.raises(CapabilityNotYetIssued, match="reach backwards"):
        caps.check("c1", Request("agent-1", Action.EXECUTE_TOOL, "t1",
                                 "probe"))


def test_the_window_is_closed_at_both_ends_and_open_between_them():
    """Stated as the window, so neither end can be dropped unnoticed."""
    cap = _cap(issued_seq=10, expires_after_seq=20)
    req = Request("agent-1", Action.EXECUTE_TOOL, "t1", "probe")
    for at in (10, 15, 20):
        assert CapabilitySet(issued={"c1": cap}, at_seq=at).check("c1", req)
    with pytest.raises(CapabilityNotYetIssued):
        CapabilitySet(issued={"c1": cap}, at_seq=9).check("c1", req)
    with pytest.raises(CapabilityExpired):
        CapabilitySet(issued={"c1": cap}, at_seq=21).check("c1", req)


def test_the_ledger_stamps_the_start_rather_than_taking_it_from_the_caller(
        tmp_path):
    """Where a grant begins is the log's to decide.

    A caller that could choose it could backdate one over work already done,
    and the digest would agree with the backdated body -- content-binding
    catches tampering, not a self-consistent lie.
    """
    log, ledger = _ledger(tmp_path)
    stored = ledger.issue(_cap(issued_seq=0), actor="scheduler")
    seq = [e.seq for e in log.read()][-1]
    assert stored.issued_seq == seq
    assert ledger.in_force().issued["c1"].issued_seq == seq


def test_a_record_that_names_its_own_start_is_refused_on_replay(tmp_path):
    """And the same rule from the other side: a record written around
    :meth:`issue` cannot claim a start of its own choosing."""
    from qta_agent.capability import ACT_ISSUE, CapabilityLedger

    log, ledger = _ledger(tmp_path)
    for i in range(3):
        log.append(actor="x", action="record.create", target=f"r{i}",
                   payload={})
    backdated = _cap(capability_id="c-back", issued_seq=0)
    log.append(actor="mallory", action=ACT_ISSUE, target="t1",
               payload={"task_id": "t1", **backdated.body()})
    with pytest.raises(CapabilityError, match="claims it was issued at seq"):
        CapabilityLedger(log).load()


def test_the_ledger_records_who_granted_each_capability(tmp_path):
    """Attribution, and deliberately NOT authorization.

    Nothing in this build constrains who may issue a grant -- there is no
    issuer authority, and this is stated rather than implied. What an auditor
    gets is the actor the grant is attributable to, which is strictly more
    than the log position it appeared at.
    """
    log, ledger = _ledger(tmp_path)
    ledger.issue(_cap(), actor="scheduler")
    assert ledger.issuer_of("c1") == "scheduler"
    assert ledger.issuer_of("never-issued") is None


# --- the child's identity, and the bound that is not the wall clock ---------

def test_an_idle_tool_is_abandoned_without_consuming_the_wall_bound(env):
    """A wall bound answers "how long may this take", which is the wrong
    question for a tool that has stopped making progress.

    One that writes a byte a minute runs to the wall bound and consumes it;
    one that deadlocks in the first second consumes it too. Recovery by
    lease expiry then waits out a timeout nothing was using.
    """
    idle = [PY, "-c", "import sys,time; sys.stdout.write('start'); "
                      "sys.stdout.flush(); time.sleep(30)"]
    started = time.time()
    r = run_bounded(idle, spec=_spec(), cwd=env["ws"],
                    limits=Limits(wall_seconds=25.0, idle_seconds=2.0),
                    env={"PATH": "/usr/bin:/bin"})
    took = time.time() - started
    assert r.outcome is Outcome.TIMED_OUT, (r.outcome, r.reason)
    assert "no output for" in r.reason
    assert took < 12, (
        f"took {took:.1f}s: the idle bound did not fire and the run was "
        "held until the wall bound, which is the behaviour this replaces")


def test_a_tool_that_keeps_working_is_not_killed_as_idle(env):
    """The guard must refuse IDLENESS, not slowness.

    Progress is measured as output because that is the only signal this
    executor has -- a tool thinking hard and silently is indistinguishable
    from one that has deadlocked, and the wall bound is what covers it.
    """
    busy = [PY, "-c", "import sys,time\n"
                      "for i in range(12):\n"
                      "    sys.stdout.write('.'); sys.stdout.flush()\n"
                      "    time.sleep(0.4)\n"]
    r = run_bounded(busy, spec=_spec(), cwd=env["ws"],
                    limits=Limits(wall_seconds=25.0, idle_seconds=2.0),
                    env={"PATH": "/usr/bin:/bin"})
    assert r.outcome is Outcome.COMPLETED, (r.outcome, r.reason)


def test_no_idle_bound_configured_leaves_the_old_behaviour(env):
    """Zero means "rely on the wall bound", so existing callers are unchanged."""
    r = run_bounded([PY, "-c", "pass"], spec=_spec(), cwd=env["ws"],
                    limits=Limits(wall_seconds=10.0),
                    env={"PATH": "/usr/bin:/bin"})
    assert r.outcome is Outcome.COMPLETED
    assert r.limits["idle_seconds"] == 0.0


def test_the_childs_identity_is_recorded_for_a_supervisor_that_dies(env):
    """Recovery by lease expiry reclaims the WORK, not the PROCESS.

    A supervisor that dies mid-run leaves a child nothing else can name.
    The pid and process-group id are recorded the moment the child exists,
    so a later operator can at least look for the group and signal it.

    They are DIAGNOSTIC and the record says so: a pid means something only
    on the host that produced it and only until it is reused, which is why
    the run's start time sits beside it.
    """
    r = run_bounded([PY, "-c", "pass"], spec=_spec(), cwd=env["ws"],
                    limits=Limits(wall_seconds=10.0),
                    env={"PATH": "/usr/bin:/bin"})
    assert isinstance(r.pid, int) and r.pid > 0
    assert isinstance(r.pgid, int) and r.pgid > 0
    rec = r.to_record()
    assert rec["pid"] == r.pid and rec["pgid"] == r.pgid, (
        "the identity is not in the durable record, so it is not available "
        "to anyone reading the log after the supervisor is gone")
    assert rec["started_wall"] > 0, "a pid with no time is not identifying"


def test_a_killed_idle_tool_leaves_no_surviving_process_group(env):
    """The group, not just the child: a grandchild must not outlive the run."""
    spawner = [PY, "-c",
               "import subprocess,sys,time\n"
               "subprocess.Popen([sys.executable,'-c','import time;"
               "time.sleep(60)'])\n"
               "sys.stdout.write('spawned'); sys.stdout.flush()\n"
               "time.sleep(60)\n"]
    r = run_bounded(spawner, spec=_spec(), cwd=env["ws"],
                    limits=Limits(wall_seconds=20.0, idle_seconds=2.0),
                    env={"PATH": "/usr/bin:/bin"})
    assert r.outcome is Outcome.TIMED_OUT
    time.sleep(0.5)
    assert r.pgid, "no group was recorded, so none could be signalled"

    # THE ACCURATE PROPERTY. An earlier version of this asserted that
    # signalling the group raises ProcessLookupError, and it did not -- not
    # because a descendant survived, but because a ZOMBIE still occupies a
    # process-table entry. The grandchild was terminated by the group kill;
    # what nobody did was reap it, and reaping a process whose parent is gone
    # is init's job rather than this executor's.
    #
    # So the check is that nothing in the group is still RUNNABLE. That is
    # the containment claim; "no entry remains" is a claim about a different
    # system's bookkeeping.
    ps = subprocess.run(["ps", "-o", "pid,pgid,stat", "-e"],
                        capture_output=True, text=True).stdout
    live = []
    for line in ps.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3 and parts[1] == str(r.pgid):
            if not parts[2].startswith("Z"):
                live.append(line.strip())
    assert not live, f"still-running descendants in the group: {live}"


# --- declared outputs: checked against a contract, not swept up -------------

def _out_spec(**kw):
    """A probe whose contract names the file it is supposed to produce."""
    base = dict(inputs=(Field_("dir", "str"), Field_("name", "str")),
                output_files=(OutputFile("artifact", "{dir}/{name}"),))
    base.update(kw)
    return _spec(**base)


def _write(path: str, text: str = "hello") -> list:
    return [PY, "-c", f"open({path!r},'w').write({text!r})"]


def test_a_declared_output_is_hashed_where_the_contract_said_it_would_be(
        tmp_path):
    spec = _out_spec()
    (tmp_path / "d").mkdir()
    collect = spec.resolve_outputs({"dir": "d", "name": "a.json"})
    r = run_bounded(_write("d/a.json"), spec=spec, cwd=tmp_path,
                    limits=Limits(wall_seconds=20.0), env={},
                    collect=collect)
    assert r.outcome is Outcome.COMPLETED
    assert r.output_digests["artifact"] == hashlib.sha256(
        b"hello").hexdigest(), (
        "the recorded digest must be of the file's actual bytes; a digest "
        "computed from anything else is a claim about a file rather than the "
        "file")
    assert r.output_paths["artifact"] == "d/a.json", (
        "a digest with no path says what was hashed and not what it was "
        "hashed from, and the path came from the inputs rather than the "
        "contract")
    assert r.to_record()["output_digests"] == r.output_digests


def test_exiting_zero_without_the_declared_output_is_not_a_completion(
        tmp_path):
    """The contract says what the process was FOR. Exit status does not."""
    spec = _out_spec()
    (tmp_path / "d").mkdir()
    r = run_bounded([PY, "-c", "pass"], spec=spec, cwd=tmp_path,
                    limits=Limits(wall_seconds=20.0), env={},
                    collect=spec.resolve_outputs({"dir": "d",
                                                  "name": "gone.json"}))
    assert r.outcome is Outcome.FAILED
    assert not r.succeeded
    assert "artifact" in r.missing_outputs
    assert "declared output" in r.reason
    assert "d/gone.json" in r.reason, (
        "a failure that does not name the file it wanted is a failure "
        "somebody has to re-run to diagnose")


def test_an_optional_declared_output_is_recorded_and_changes_nothing(
        tmp_path):
    spec = _out_spec(output_files=(
        OutputFile("artifact", "{dir}/{name}"),
        OutputFile("extra", "{dir}/extra.json", required=False),
    ))
    (tmp_path / "d").mkdir()
    r = run_bounded(_write("d/a.json"), spec=spec, cwd=tmp_path,
                    limits=Limits(wall_seconds=20.0), env={},
                    collect=spec.resolve_outputs({"dir": "d",
                                                  "name": "a.json"}))
    assert r.outcome is Outcome.COMPLETED, (
        "an output declared optional is one the contract says may be absent; "
        "failing the run for it would make required mean nothing")
    assert "extra" in r.missing_outputs and "artifact" in r.output_digests


def test_a_declared_output_that_is_a_symlink_out_is_refused_not_hashed(
        tmp_path):
    """The string had no '..' in it. The filesystem is where that is decided.

    This is the whole reason collection resolves against the real filesystem
    rather than trusting the resolved relative path: a tool that writes a
    symlink chooses where its "output" lives, and hashing the target would
    record a file outside the workspace as this run's product.
    """
    spec = _out_spec()
    (tmp_path / "d").mkdir()
    outside = tmp_path.parent / "outside-the-workspace.txt"
    outside.write_text("not this run's output")
    r = run_bounded([PY, "-c",
                     f"import os; os.symlink({str(outside)!r}, 'd/a.json')"],
                    spec=spec, cwd=tmp_path,
                    limits=Limits(wall_seconds=20.0), env={},
                    collect=spec.resolve_outputs({"dir": "d",
                                                  "name": "a.json"}))
    assert r.outcome is Outcome.FAILED
    assert not r.output_digests, "the file outside the workspace was hashed"
    assert "outside the working directory" in r.missing_outputs["artifact"]


class _CollectorHung(Exception):
    """Raised by the alarm below. NOT an OSError, deliberately.

    ``_collect_outputs`` catches OSError around its read, and TimeoutError is
    an OSError in Python -- so an alarm raising one would be swallowed and
    recorded as "could not be read", turning a hang into a pass. The whole
    point of this test is that the difference is visible.
    """


def test_a_declared_output_that_is_a_fifo_is_refused_rather_than_read(
        tmp_path):
    """Refused, and refused PROMPTLY. The bound is what is being tested.

    Opening a FIFO with no writer blocks in the kernel forever. A test that
    only asserts the outcome cannot tell "refused" from "still blocked" --
    it just never finishes, and the mutation harness reported exactly that:
    the mutation removing the regular-file check was killed by a 300-second
    global timeout rather than by anything here. A hang is not an assertion.

    So the alarm IS the assertion. If collection has not returned in fifteen
    seconds it is blocked, which is the failure this guard exists to prevent,
    and the test says so instead of waiting.
    """
    spec = _out_spec()
    (tmp_path / "d").mkdir()

    def _hung(_signum, _frame):
        raise _CollectorHung(
            "collection was still running 15s after a FIFO was put in place "
            "of a declared output; it is blocked on the open, which is the "
            "denial of service the regular-file check exists to refuse")

    previous = signal.signal(signal.SIGALRM, _hung)
    signal.setitimer(signal.ITIMER_REAL, 15.0)
    try:
        r = run_bounded([PY, "-c", "import os; os.mkfifo('d/a.json')"],
                        spec=spec, cwd=tmp_path,
                        limits=Limits(wall_seconds=20.0), env={},
                        collect=spec.resolve_outputs({"dir": "d",
                                                      "name": "a.json"}))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    assert r.outcome is Outcome.FAILED
    assert "not a regular file" in r.missing_outputs["artifact"]


def test_partial_output_from_a_killed_run_is_still_collected(tmp_path):
    """Same reasoning that keeps partial stdout: it says where it got to."""
    spec = _out_spec()
    (tmp_path / "d").mkdir()
    r = run_bounded([PY, "-c",
                     "import time\n"
                     "open('d/a.json','w').write('half')\n"
                     "time.sleep(60)\n"],
                    spec=spec, cwd=tmp_path,
                    limits=Limits(wall_seconds=2.0), env={},
                    collect=spec.resolve_outputs({"dir": "d",
                                                  "name": "a.json"}))
    assert r.outcome is Outcome.TIMED_OUT
    assert r.output_digests["artifact"] == hashlib.sha256(
        b"half").hexdigest(), (
        "a killed tool's partial declared output is evidence of how far it "
        "got, and discarding it leaves no record of that")


def test_a_contract_that_declares_no_outputs_collects_nothing(tmp_path):
    """Silence, not a claim that the tool wrote nothing."""
    (tmp_path / "d").mkdir()
    r = run_bounded(_write("d/a.json"), spec=_spec(), cwd=tmp_path,
                    limits=Limits(wall_seconds=20.0), env={})
    assert r.outcome is Outcome.COMPLETED
    assert r.output_digests == {} and r.missing_outputs == {}


def test_an_input_cannot_relocate_a_declared_output(tmp_path):
    spec = _out_spec()
    with pytest.raises(ToolContractViolation, match="leaves the working"):
        spec.resolve_outputs({"dir": "d", "name": "../escape.json"})
    with pytest.raises(ToolContractViolation, match="leaves the working"):
        spec.resolve_outputs({"dir": "/etc", "name": "passwd"})


def test_a_traversing_input_is_denied_and_returned_not_raised(tmp_path):
    """The most security-relevant refusal must not be the one that escapes.

    An exception out of Executor.run would leave the caller's job dispatched,
    its lease to lapse, and the work retried -- with an input that can only
    ever be refused again. It comes back as a result so it can be classified
    like every other refusal.
    """
    spec = _out_spec(tool_id="probe")
    ex = Executor(Registry([spec]), workspace=tmp_path)
    r = ex.run(tool_id="probe", actor="agent-1", task_id="t1",
               capability_id="c1",
               capabilities=CapabilitySet(issued={"c1": _cap()}, at_seq=2),
               inputs={"dir": "d", "name": "../escape.json"},
               argv=[PY, "-c", "pass"], env={})
    assert r.outcome is Outcome.DENIED, (
        "DENIED because nothing was attempted; FAILED would imply a run")
    assert "leaves the working directory" in r.reason


# --- SideEffect.EXTERNAL is load-bearing, not a label -----------------------

def test_an_external_tool_cannot_be_registered_without_a_compensation():
    with pytest.raises(ToolError, match="no compensation"):
        Registry([_spec(side_effect=SideEffect.EXTERNAL, writable_scope=())])


def test_a_compensation_without_an_external_effect_is_refused():
    with pytest.raises(ToolError, match="declares a compensation"):
        Registry([_spec(compensation="undo the charge")])


def test_an_external_tool_is_never_automatically_retryable():
    """A written rollback is not a performed one.

    TIMED_OUT is retryable for a scoped tool and must not be for an external
    one: nothing observed the tool finish, which is exactly the case where it
    may already have changed state this system does not own.
    """
    ext = _spec(side_effect=SideEffect.EXTERNAL, writable_scope=(),
                compensation="issue a refund for the charge")
    for outcome in RETRYABLE:
        scoped = ExecutionResult(outcome=outcome, tool_id="probe",
                                 tool_version="1.0", tool_digest="d",
                                 side_effect=SideEffect.SCOPED_WRITES.value)
        external = ExecutionResult(outcome=outcome, tool_id="probe",
                                   tool_version="1.0", tool_digest="d",
                                   side_effect=SideEffect.EXTERNAL.value,
                                   compensation=ext.compensation)
        assert scoped.retryable, f"{outcome} is retryable for a scoped tool"
        assert not external.retryable, (
            f"{outcome} must not be retryable for a tool that may already "
            "have changed state nobody here owns")
        assert not external.succeeded


def test_the_compensation_travels_in_the_durable_record():
    """An operator reading the log learns what to do from the log."""
    ext = _spec(side_effect=SideEffect.EXTERNAL, writable_scope=(),
                compensation="issue a refund for the charge")
    r = run_bounded([PY, "-c", "import time; time.sleep(60)"], spec=ext,
                    cwd=Path.cwd(), limits=Limits(wall_seconds=1.0), env={})
    assert r.outcome is Outcome.TIMED_OUT
    rec = r.to_record()
    assert rec["compensation"] == "issue a refund for the charge"
    assert rec["side_effect"] == SideEffect.EXTERNAL.value
    assert rec["retryable"] is False, (
        "the decision is recorded, not only derivable; a later reader should "
        "see what this system concluded and not have to re-derive it under "
        "whatever rule is current then")


# --- output templates are checked at registration, not at run time ----------

@pytest.mark.parametrize("out,match", [
    (OutputFile("a", "{nope}/x"), "not a declared input"),
    (OutputFile("a", "{n}/x"), "has to be a str"),
    (OutputFile("a", "/abs/x"), "is absolute"),
    (OutputFile("a", "{dir!r}/x"), "format spec or conversion"),
    (OutputFile("a", "{dir[0]}/x"), "only a bare"),
    (OutputFile("a", ""), "non-empty str"),
])
def test_an_unresolvable_output_template_is_refused_at_registration(
        out, match):
    """Before any run, because the alternative is finding out afterwards."""
    with pytest.raises(ToolError, match=match):
        Registry([_spec(inputs=(Field_("dir", "str"), Field_("n", "int")),
                        output_files=(out,))])


def test_an_output_placed_by_an_optional_input_has_no_location():
    with pytest.raises(ToolError, match="which is optional"):
        Registry([_spec(inputs=(Field_("dir", "str", required=False),),
                        output_files=(OutputFile("a", "{dir}/x"),))])


def test_declaring_output_files_with_no_side_effects_is_incoherent():
    with pytest.raises(ToolError, match="agree with itself"):
        Registry([_spec(side_effect=SideEffect.NONE, writable_scope=(),
                        inputs=(Field_("dir", "str"),),
                        output_files=(OutputFile("a", "{dir}/x"),))])


def test_duplicate_output_names_are_refused():
    with pytest.raises(ToolError, match="duplicate output file name"):
        Registry([_spec(inputs=(Field_("dir", "str"),),
                        output_files=(OutputFile("a", "{dir}/x"),
                                      OutputFile("a", "{dir}/y")))])


def test_the_declaration_is_part_of_the_contract_digest():
    """Two tools that collect different files are not the same tool."""
    plain = _spec(inputs=(Field_("dir", "str"),))
    with_out = _spec(inputs=(Field_("dir", "str"),),
                     output_files=(OutputFile("a", "{dir}/x"),))
    assert plain.digest() != with_out.digest(), (
        "a contract change that a citation cannot distinguish is a citation "
        "that does not identify what ran")
