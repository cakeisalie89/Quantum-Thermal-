"""A compensation that RUNS, and a write nobody declared.

THE GAPS THESE CLOSE, stated as they were found:

    "SideEffect.EXTERNAL is consumed at four points ... but the compensation
     is a DECLARATION. Nothing performs it, and no compensating action is
     automated anywhere in this system"
    "declared-output collection sees only the files the CONTRACT named. A
     tool that writes an UNdeclared file inside its scope is not detected by
     it"

The first is not closed by running the undo automatically. The case a
compensation exists for is the case where nobody knows whether the effect
happened, and an automatic undo there performs an unrequested external action
in exactly the situation where there may have been nothing to undo. So the
authority to run one is a person's answer to an escalation, and the record
cites it.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.agents import (  # noqa: E402
    BOOTSTRAP, AgentError, AgentRole, PrincipalKind, identity,
)
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.governed_stage10 import (  # noqa: E402
    ACT_COMPENSATION, GovernedStage10,
)
from qta_agent.tasks import TaskState  # noqa: E402
from qta_agent.tools import (  # noqa: E402
    Determinism, Field_, OutputFile, Registry, SideEffect, ToolError, ToolSpec,
)

WS = "verification/stage10/_pytest_compensation"


@pytest.fixture()
def gov(request):
    name = request.node.name.replace("/", "_")[:60]
    base = ROOT / WS / name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    g = GovernedStage10(root=ROOT, log=EventLog(base / "log.jsonl"),
                        evidence=EvidenceStore(base / "evidence"))
    g.out_rel = f"{WS}/{name}/out"
    yield g
    if base.exists():
        shutil.rmtree(base)


def _inputs(gov, **over):
    base = {"out_dir": gov.out_rel, "name": "artifact.json",
            "payload": {"label": "MODEL_ONLY", "value": 42}}
    base.update(over)
    return base


def _undo_spec(**over):
    base = dict(
        tool_id="stage10.revoke_artifact", version="1.0.0",
        summary="revoke a published record", inputs=(Field_("task_id", "str"),),
        determinism=Determinism.NONDETERMINISTIC,
        side_effect=SideEffect.EXTERNAL, is_compensation=True,
        writable_scope=("verification/stage10",), timeout_s=10.0)
    base.update(over)
    return ToolSpec(**base)


def _external_with_undo(gov, **over):
    """The publishing tool, plus the tool that undoes it."""
    main = dict(
        tool_id="stage10.emit_artifact", version="1.0.0",
        summary="an external-effect tool",
        inputs=(Field_("out_dir", "str"), Field_("name", "str"),
                Field_("payload", "dict")),
        outputs=(Field_("path", "str"), Field_("sha256", "str")),
        output_files=(OutputFile("artifact", "{out_dir}/{name}"),),
        determinism=Determinism.BYTE_IDENTICAL,
        side_effect=SideEffect.EXTERNAL,
        compensation="revoke the published record by its citation id",
        compensating_tool="stage10.revoke_artifact",
        writable_scope=("verification/stage10",), timeout_s=5.0)
    main.update(over)
    reg = Registry([ToolSpec(**main), _undo_spec()])
    gov.registry = reg
    gov.executor = type(gov.executor)(reg, workspace=gov.root)
    return reg


def _timed_out_run(gov, monkeypatch):
    from qta_agent.execution import Limits

    _external_with_undo(gov)
    real_run = type(gov.executor).run

    def _slow(self, **kw):
        kw["argv"] = [sys.executable, "-c", "import time; time.sleep(30)"]
        kw["limits"] = Limits(wall_seconds=1.0)
        return real_run(self, **kw)

    monkeypatch.setattr(type(gov.executor), "run", _slow)
    first = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
                    idempotency_key="ext")
    assert first.state is TaskState.TIMED_OUT
    again = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
                    idempotency_key="ext")
    assert again.outcome == "UNCERTAIN" and again.escalation_id
    monkeypatch.undo()
    return again


def _human(gov, who="operator-1"):
    gov.agents.register(
        identity(agent_id=who, instance_id=who, kind=PrincipalKind.HUMAN,
                 roles={AgentRole.REVIEWER}), by=BOOTSTRAP)
    return who


# --------------------------------------------------------------------------
# The contract half
# --------------------------------------------------------------------------

def test_a_compensating_tool_must_be_marked_as_one():
    """The mark is what exempts it from needing an undo of its own."""
    reg = Registry([
        ToolSpec(tool_id="a", version="1", summary="s",
                 side_effect=SideEffect.EXTERNAL, compensation="undo it",
                 compensating_tool="b"),
        ToolSpec(tool_id="b", version="1", summary="s",
                 side_effect=SideEffect.EXTERNAL, compensation="undo it"),
    ])
    with pytest.raises(ToolError) as exc:
        reg.compensator_for("a")
    assert "is_compensation" in str(exc.value)


def test_a_compensation_may_not_name_a_compensation():
    """No chains. One that needs compensating is a design nobody can reason
    about at the moment they most need to."""
    with pytest.raises(ToolError) as exc:
        Registry([ToolSpec(tool_id="b", version="1", summary="s",
                           side_effect=SideEffect.EXTERNAL,
                           is_compensation=True, compensating_tool="c")])
    assert "regress" in str(exc.value) or "needs compensating" in str(exc.value)


def test_a_compensation_must_be_external():
    """Undoing something this system owns is an ordinary scoped write."""
    with pytest.raises(ToolError) as exc:
        Registry([ToolSpec(tool_id="b", version="1", summary="s",
                           side_effect=SideEffect.SCOPED_WRITES,
                           writable_scope=("verification/stage10",),
                           is_compensation=True)])
    assert "belongs to somebody else" in str(exc.value)


def test_naming_an_unregistered_compensating_tool_is_refused():
    """A compensation nothing can run is prose wearing a function's name."""
    reg = Registry([ToolSpec(tool_id="a", version="1", summary="s",
                             side_effect=SideEffect.EXTERNAL,
                             compensation="undo it",
                             compensating_tool="nowhere")])
    with pytest.raises(ToolError) as exc:
        reg.compensator_for("a")
    assert "not registered" in str(exc.value)


def test_a_non_external_tool_may_not_name_a_compensating_tool():
    with pytest.raises(ToolError) as exc:
        Registry([ToolSpec(tool_id="a", version="1", summary="s",
                           side_effect=SideEffect.SCOPED_WRITES,
                           writable_scope=("verification/stage10",),
                           compensating_tool="b")])
    assert "one of the two is wrong" in str(exc.value)


def test_the_compensating_tool_reaches_the_contract_digest():
    """Two tools that differ only in what undoes them are different tools."""
    a = ToolSpec(tool_id="a", version="1", summary="s",
                 side_effect=SideEffect.EXTERNAL, compensation="undo",
                 compensating_tool="b")
    c = ToolSpec(tool_id="a", version="1", summary="s",
                 side_effect=SideEffect.EXTERNAL, compensation="undo",
                 compensating_tool="d")
    assert a.digest() != c.digest()


# --------------------------------------------------------------------------
# Running it, and the authority to
# --------------------------------------------------------------------------

def test_a_compensation_runs_when_a_person_answered_COMPENSATE(
        gov, monkeypatch):
    run = _timed_out_run(gov, monkeypatch)
    who = _human(gov)
    gov.agents.answer(escalation_id=run.escalation_id, answered_by=who,
                      answer="COMPENSATE", reason="the record was published")

    out = gov.compensate(task_id=run.task_id,
                         escalation_id=run.escalation_id)

    assert out["tool_id"] == "stage10.revoke_artifact"
    assert out["answered_by"] == who
    assert "does not establish the external system's current state" \
        in out["establishes"]
    recs = [e for e in gov.log.read() if e.action == ACT_COMPENSATION]
    assert len(recs) == 1
    assert recs[0].payload["authorized_by_escalation"] == run.escalation_id
    assert recs[0].payload["compensated_tool"] == "stage10.emit_artifact"


def test_a_compensation_will_not_run_on_an_unanswered_escalation(
        gov, monkeypatch):
    """THE rule. Running the undo automatically would perform an external
    action in exactly the case where there may have been nothing to undo."""
    run = _timed_out_run(gov, monkeypatch)
    with pytest.raises(AgentError) as exc:
        gov.compensate(task_id=run.task_id, escalation_id=run.escalation_id)
    assert "nobody has decided yet" in str(exc.value)
    assert not [e for e in gov.log.read() if e.action == ACT_COMPENSATION]


def test_a_compensation_will_not_run_when_the_answer_was_not_COMPENSATE(
        gov, monkeypatch):
    """Anti-vacuity: the answer has to MEAN something."""
    run = _timed_out_run(gov, monkeypatch)
    who = _human(gov)
    gov.agents.answer(escalation_id=run.escalation_id, answered_by=who,
                      answer="ACCEPT_AS_DONE", reason="it went through")

    with pytest.raises(AgentError) as exc:
        gov.compensate(task_id=run.task_id, escalation_id=run.escalation_id)
    assert "explicitly not to perform" in str(exc.value)


def test_an_answer_about_another_task_does_not_authorize_this_one(
        gov, monkeypatch):
    """An answer given about one piece of work is not a general permission."""
    run = _timed_out_run(gov, monkeypatch)
    who = _human(gov)
    # Raised by a registered instance: an unregistered one is refused before
    # the check this test is about is ever reached.
    gov.agents.escalate(escalation_id="esc-other", task_id="task-elsewhere",
                        raised_by="stage10-submitter", question="compensate?",
                        options=("COMPENSATE", "ACCEPT_AS_DONE"))
    gov.agents.answer(escalation_id="esc-other", answered_by=who,
                      answer="COMPENSATE", reason="that other one, yes")

    with pytest.raises(AgentError) as exc:
        gov.compensate(task_id=run.task_id, escalation_id="esc-other")
    assert "does not authorize acting on another" in str(exc.value)


def test_a_tool_with_no_compensating_tool_says_so_rather_than_pretending(
        gov, monkeypatch):
    """Not every external effect has an automatable undo, and a system that
    pretended otherwise would be worse than one that says so."""
    from qta_agent.execution import Limits

    reg = Registry([ToolSpec(
        tool_id="stage10.emit_artifact", version="1.0.0", summary="s",
        inputs=(Field_("out_dir", "str"), Field_("name", "str"),
                Field_("payload", "dict")),
        output_files=(OutputFile("artifact", "{out_dir}/{name}"),),
        side_effect=SideEffect.EXTERNAL,
        compensation="telephone the registrar and ask them to withdraw it",
        writable_scope=("verification/stage10",), timeout_s=1.0)])
    gov.registry = reg
    gov.executor = type(gov.executor)(reg, workspace=gov.root)
    real_run = type(gov.executor).run

    def _slow(self, **kw):
        kw["argv"] = [sys.executable, "-c", "import time; time.sleep(30)"]
        kw["limits"] = Limits(wall_seconds=1.0)
        return real_run(self, **kw)

    monkeypatch.setattr(type(gov.executor), "run", _slow)
    gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
            idempotency_key="ext")
    again = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
                    idempotency_key="ext")
    monkeypatch.undo()
    who = _human(gov)
    gov.agents.answer(escalation_id=again.escalation_id, answered_by=who,
                      answer="COMPENSATE", reason="withdraw it")

    with pytest.raises(ToolError) as exc:
        gov.compensate(task_id=again.task_id,
                       escalation_id=again.escalation_id)
    assert "telephone the registrar" in str(exc.value)
    assert "an operator's action, not this system's" in str(exc.value)


def test_the_compensation_gets_its_own_grant_not_the_runs(gov, monkeypatch):
    """Reusing the original grant would make 'may run the thing' and 'may
    undo the thing' the same authority."""
    run = _timed_out_run(gov, monkeypatch)
    who = _human(gov)
    gov.agents.answer(escalation_id=run.escalation_id, answered_by=who,
                      answer="COMPENSATE", reason="revoke it")
    gov.compensate(task_id=run.task_id, escalation_id=run.escalation_id)

    caps = gov.capabilities
    undo_caps = [caps.in_force().issued[c] for c in caps.issued_ids()
                 if c.startswith("cap-undo-")]
    assert undo_caps, "the compensation reused a grant minted for the run"
    assert all(c.tool_id == "stage10.revoke_artifact" for c in undo_caps)
    assert all(c.task_id == run.task_id for c in undo_caps)


# --------------------------------------------------------------------------
# Files the contract never named
# --------------------------------------------------------------------------

def _scoped_tool(gov, extra_writes=()):
    """A tool that writes its declared artifact plus whatever else is asked.

    The extra writes are the point: the write allowlist permits them, because
    they are inside the tool's own scope, and until now nothing looked at
    whether the contract had named them.
    """
    reg = Registry([ToolSpec(
        tool_id="stage10.emit_artifact", version="1.0.0",
        summary="writes its artifact, and sometimes more",
        inputs=(Field_("out_dir", "str"), Field_("name", "str"),
                Field_("payload", "dict")),
        outputs=(Field_("path", "str"), Field_("sha256", "str")),
        output_files=(OutputFile("artifact", "{out_dir}/{name}"),),
        determinism=Determinism.BYTE_IDENTICAL,
        side_effect=SideEffect.SCOPED_WRITES,
        writable_scope=("verification/stage10",), timeout_s=20.0)])
    gov.registry = reg
    gov.executor = type(gov.executor)(reg, workspace=gov.root)
    if not extra_writes:
        return reg

    real_run = type(gov.executor).run
    script = "; ".join(
        f"open({p!r}, 'w').write('extra')" for p in extra_writes)

    def _also_writes(self, **kw):
        # Same artifact the honest tool writes, plus the extra file. Doing it
        # by replacing argv keeps every other part of the run real: the same
        # contract, capability, limits and collection all apply.
        kw["argv"] = [sys.executable, "-c",
                      "import json,pathlib;"
                      f"d=json.loads({kw['argv'][-1]!r});"
                      "p=pathlib.Path(d['out_dir'])/d['name'];"
                      "p.parent.mkdir(parents=True, exist_ok=True);"
                      "p.write_text(json.dumps(d['payload'],sort_keys=True));"
                      + script]
        return real_run(self, **kw)

    return reg, _also_writes


def test_a_tool_that_writes_an_undeclared_file_is_caught(gov, monkeypatch):
    """THE gap. The write allowlist bounds WHERE, and said nothing about
    WHAT: an extra artifact inside the tool's own scope passed every check
    and appeared in no provenance record."""
    out_dir = ROOT / gov.out_rel
    out_dir.mkdir(parents=True, exist_ok=True)
    extra = str(out_dir / "undeclared.txt")
    _reg, patched = _scoped_tool(gov, extra_writes=(extra,))
    monkeypatch.setattr(type(gov.executor), "run", patched)

    run = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov))

    assert run.state is TaskState.FAILED
    assert "never declared" in run.reason
    assert "undeclared.txt" in run.reason


def test_the_undeclared_writes_are_recorded_on_the_execution(gov, monkeypatch):
    """A record an auditor can read, not only a reason string."""
    out_dir = ROOT / gov.out_rel
    out_dir.mkdir(parents=True, exist_ok=True)
    _reg, patched = _scoped_tool(
        gov, extra_writes=(str(out_dir / "undeclared.txt"),))
    monkeypatch.setattr(type(gov.executor), "run", patched)
    gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov))

    recs = [e.payload for e in gov.log.read() if e.action == "task.execution"]
    assert recs and recs[-1]["undeclared_writes"]
    assert any("undeclared.txt" in w for w in recs[-1]["undeclared_writes"])


def test_an_honest_run_reports_no_undeclared_writes(gov):
    """ANTI-VACUITY, and the reason this is not just 'always fail'.

    Without this the check could be 'every run has undeclared writes', which
    would make the distinction meaningless in the other direction and would
    be invisible because the failing case still fails.
    """
    _scoped_tool(gov)
    run = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov))

    assert run.state is TaskState.VERIFIED
    recs = [e.payload for e in gov.log.read() if e.action == "task.execution"]
    assert recs[-1]["undeclared_writes"] == []


def test_a_file_that_was_already_there_is_not_a_write(gov):
    """The inventory is a BEFORE and an AFTER, so a pre-existing file is not
    something this run did."""
    _scoped_tool(gov)
    out_dir = ROOT / gov.out_rel
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "was-here-first.txt").write_text("older", encoding="utf-8")

    run = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov))
    assert run.state is TaskState.VERIFIED


def test_the_inventory_is_scoped_to_what_the_tool_may_write(gov):
    """Cost and correctness both.

    The working directory is the repository root, so inventorying it would
    make a bounded execution proportional to the checkout, and would report
    every concurrent change anywhere as this tool's write.
    """
    from qta_agent.execution import _inventory

    whole = _inventory(ROOT, ("",))
    scoped = _inventory(ROOT, ("verification/stage10",))
    assert scoped, "the scoped inventory saw nothing at all"
    assert len(scoped) < len(whole), (
        "the inventory is not scoped: it walked the whole working directory")
    assert all(p.startswith("verification/stage10") for p in scoped)


def test_deleting_a_file_it_did_not_declare_is_an_undeclared_write(
        gov, monkeypatch):
    """The more destructive half.

    An inventory that only looked for additions would miss a tool that
    REMOVES something inside its scope, which changes the workspace at least
    as much as adding a file does.
    """
    import json as _json

    out_dir = ROOT / gov.out_rel
    out_dir.mkdir(parents=True, exist_ok=True)
    doomed = out_dir / "was-here-first.txt"
    doomed.write_text("older", encoding="utf-8")

    _scoped_tool(gov)
    real_run = type(gov.executor).run

    def _also_deletes(self, **kw):
        payload = _json.dumps(_json.loads(kw["argv"][-1]), sort_keys=True)
        kw["argv"] = [sys.executable, "-c",
                      "import json,pathlib,os;"
                      f"d=json.loads({payload!r});"
                      "p=pathlib.Path(d['out_dir'])/d['name'];"
                      "p.parent.mkdir(parents=True, exist_ok=True);"
                      "p.write_text(json.dumps(d['payload'],sort_keys=True));"
                      f"os.unlink({str(doomed)!r})"]
        return real_run(self, **kw)

    monkeypatch.setattr(type(gov.executor), "run", _also_deletes)
    run = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov))

    assert run.state is TaskState.FAILED
    assert "never declared" in run.reason
    assert "was-here-first.txt" in run.reason
    assert "deleted" in run.reason
