"""An action no module writes is refused by every reducer that folds a
shared log (AUD-054, the remainder of AUD-046).

The action registry separates two cases a reducer meets in a log it shares
with other subsystems. Another subsystem's action is that subsystem's to
fold, and is skipped. An action no module in this package writes is either a
newer schema this build cannot read or something that has no business in the
log, and a reducer that read past it would be missing whatever it recorded.
The authority store and the memory store refused the second case; eight
reducers read past it. Each is handed one here, and each is handed another
subsystem's action as the control.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import actions as A  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402

#: An action another subsystem writes: the control, which every reducer
#: below that does not own it skips.
FOREIGN = "file.read"


def _capability(log, tmp):
    from agent_substrate.capability import CapabilityError, CapabilityLedger
    return (lambda: CapabilityLedger(log).load()), CapabilityError


def _role(log, tmp):
    from agent_substrate.roles import RoleDelegations, RoleError, RoleRegistry
    registry = RoleRegistry.from_document(
        {"schema": "harness/role-registry/v1", "registry_version": 1,
         "key_registry": None, "grants": []})
    return (lambda: RoleDelegations(log, registry).load()), RoleError


def _policy(log, tmp):
    from agent_substrate.policy import PolicyError, PolicyStore
    return (lambda: PolicyStore(log).load()), PolicyError


def _secret(log, tmp):
    from agent_substrate.secrets import SecretError, SecretStore
    return (lambda: SecretStore(log).load()), SecretError


def _scheduler(log, tmp):
    from agent_substrate.policy import PolicyStore
    from agent_substrate.scheduler import Scheduler, SchedulerError
    # its policy store reads a log of its own, so the refusal is the
    # scheduler's and not the policy store's
    policy = PolicyStore(EventLog(Path(tmp) / "policy.jsonl")).load()
    return (lambda: Scheduler(log, policy=policy, policy_id="p").load(),
            SchedulerError)


def _agent(log, tmp):
    from agent_substrate.agents import AgentDirectory, AgentError
    return (lambda: AgentDirectory(log).load()), AgentError


def _network(log, tmp):
    from agent_substrate.netauth import NetworkAuthority, NetworkError
    return (lambda: NetworkAuthority(log).load()), NetworkError


def _research(log, tmp):
    from agent_substrate.research import ResearchError, ResearchLedger
    return (lambda: ResearchLedger(log).load()), ResearchError


def _inference(log, tmp):
    from agent_substrate import inference as I
    from agent_substrate.policy import PolicyStore
    policies = PolicyStore(EventLog(Path(tmp) / "policy.jsonl")).load()
    return (lambda: I.InferenceGateway(
        log, transcripts=EvidenceStore(Path(tmp) / "transcripts"),
        policies=policies, provider=None).load()), I.InferenceError


REDUCERS = {"capability": _capability, "role": _role, "policy": _policy,
            "secret": _secret, "scheduler": _scheduler, "agent": _agent,
            "network": _network, "research": _research,
            "inference": _inference}


def _log_with(tmp, action):
    log = EventLog(Path(tmp) / "log.jsonl")
    log.append(actor="someone", action=action, target="t", payload={})
    return log


@pytest.mark.parametrize("name", sorted(REDUCERS))
def test_an_action_no_module_writes_is_refused(name, tmp_path):
    action = f"{name}.unheard_of"
    assert A.owner(action) is None
    load, error = REDUCERS[name](_log_with(tmp_path, action), tmp_path)
    with pytest.raises(error, match=r"seq 0: unknown action "
                                    rf"'{name}\.unheard_of'; no module"):
        load()


@pytest.mark.parametrize("name", sorted(REDUCERS))
def test_another_subsystems_action_is_skipped(name, tmp_path):
    """The control: a shared log holds every subsystem's records, and a
    reducer that refused another's would make one log impossible."""
    assert A.owner(FOREIGN) not in (None, name)
    load, _ = REDUCERS[name](_log_with(tmp_path, FOREIGN), tmp_path)
    load()


def test_the_refusal_is_worded_once():
    class Ev:
        seq, action = 7, "x.unheard_of"
    assert A.unknown_refusal(Ev) == (
        "seq 7: unknown action 'x.unheard_of'; no module in this package "
        "writes it, so a reader that read past it would be missing whatever "
        "it recorded")
    Ev.action = FOREIGN
    assert A.unknown_refusal(Ev) is None


def test_an_action_appended_after_a_load_is_refused_at_the_next_fold(
        tmp_path):
    """A reducer folds what other writers appended before it decides; one
    of them writing an action no module writes stops it there too."""
    from agent_substrate.research import ResearchError, ResearchLedger
    log = EventLog(tmp_path / "log.jsonl")
    led = ResearchLedger(log).load()
    log.append(actor="someone", action="research.unheard_of", target="t",
               payload={})
    with pytest.raises(ResearchError, match="unknown action"):
        led.register(objective_id="o1", statement="is it below 400 K?",
                     owner="owner", max_steps=5, max_inference_calls=5)
