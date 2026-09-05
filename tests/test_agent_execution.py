"""Capabilities, tool contracts, and bounded execution.

Written adversarially. The system must resist a mistaken, stale or hostile
caller WITHOUT relying on that caller's cooperation, so almost every test here
is an attempt to get authority the caller was not granted.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from qta_agent.capability import (  # noqa: E402
    Action, Capability, CapabilityDenied, CapabilityError,
    CapabilityExpired, CapabilityRevoked, CapabilitySet, CapabilityUnknown,
    Request, capability_from_record, digest_is_consistent, issue,
)
from qta_agent.execution import (  # noqa: E402
    RETRYABLE, SUCCESSFUL, CancellationToken, Executor, Limits, Outcome,
    run_bounded,
)
from qta_agent.tools import (  # noqa: E402
    Determinism, Field_, Registry, SideEffect, ToolContractViolation,
    ToolError, ToolNotRegistered, ToolSpec,
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
