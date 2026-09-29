"""Three roles, one attacker: what separation of duties assumes.

WHAT THE HOSTILE CAMPAIGN ALREADY ESTABLISHED

tests/test_agent_hostile_campaign.py runs a registered participant through
every attack it can reach as ITSELF, and every one is refused -- including
the direct attempt to execute and verify its own work, which the substrate
catches because one identity cannot hold two roles on one record.

WHAT R54 SAID WAS MISSING, AND WHY IT MATTERS

"The attacker is a registered participant. A compromised SUBMITTER, WORKER
and VERIFIER acting together are not modelled, and separation of duties is
exactly the assumption that would not survive it."

So this file models it. Three DISTINCT registered identities, each holding
one role, all controlled by the same adversary -- an operator whose three
service accounts share a laptop, or three containers with the same leaked
key. The result is not a defect and it is not reassuring either: it is the
shape of the assumption, and the point of writing it down is that nobody
should be surprised by it later.

WHAT COLLAPSES AND WHAT DOES NOT

Separation of duties collapses completely. It is enforced by comparing
ACTOR IDENTITIES, and three identities that are genuinely distinct to the
substrate are genuinely distinct however they are operated. Telling
independent principals from cooperating ones needs an external identity
authority, and this repository deliberately contains none.

Everything content-addressed holds. The artifact is still the bytes the
record names, the evidence still has to exist and hash correctly, the write
allowlist still binds the path, egress is still refused, the history still
says exactly which identity did which part, and PASS is still zero. A
colluding ring gets a VERIFIED task and gains no reach it did not already
have -- which is worth knowing, because it says the blast radius of the
assumption failing is bounded by everything else.
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
    AgentRole, PrincipalKind, identity,
)
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_stage10 import GovernedStage10  # noqa: E402
from qta_agent.netauth import NetworkDenied  # noqa: E402
from qta_agent.tasks import TaskState  # noqa: E402

WS = "verification/stage10/_pytest_collusion"

#: One adversary, three registered identities. Distinct to the substrate in
#: every way it can measure, and the same person underneath.
RING = {"ring-submitter": AgentRole.PROPOSER,
        "ring-worker": AgentRole.EXECUTOR,
        "ring-verifier": AgentRole.VERIFIER}


@pytest.fixture(scope="module")
def world():
    base = ROOT / WS
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    gov = GovernedStage10(root=ROOT, log=EventLog(base / "log.jsonl"),
                          evidence=EvidenceStore(base / "evidence"))
    gov.out_rel = f"{WS}/out"
    for agent_id, role in RING.items():
        gov.agents.register(
            identity(agent_id=agent_id, instance_id=agent_id,
                     kind=PrincipalKind.AGENT, roles={role}), by="system")
    yield gov
    if base.exists():
        shutil.rmtree(base)


def _inputs(gov, name="collusion.json"):
    return {"out_dir": gov.out_rel, "name": name,
            "payload": {"label": "MODEL_ONLY", "value": 1}}


@pytest.fixture(scope="module")
def ring_run(world):
    """The colluding run, done once and asked about below."""
    return world.run(tool_id="stage10.emit_artifact",
                     inputs=_inputs(world),
                     submitter="ring-submitter", worker="ring-worker",
                     verifier="ring-verifier")


# ---- what collusion defeats ---------------------------------------------
def test_a_colluding_ring_reaches_verified(ring_run):
    """Stated first and without hedging.

    Three identities the substrate cannot tell apart from three independent
    ones drive a task to VERIFIED. That is what separation of duties by
    identity comparison means, and pretending otherwise would be the more
    dangerous documentation.
    """
    assert ring_run.state is TaskState.VERIFIED


def test_the_same_attacker_acting_as_itself_is_still_refused(world):
    """The control, and the reason the ring needed three identities.

    Refused earlier than expected, which is the stronger answer: the
    identity cannot even be REGISTERED holding both EXECUTOR and VERIFIER,
    so a single principal never reaches the point of trying. The ring is a
    way past a check that exists, not a way past one that was never there.
    """
    from qta_agent.agents import IdentityError

    with pytest.raises(IdentityError, match="separation"):
        world.agents.register(
            identity(agent_id="solo-mallory", instance_id="solo-mallory",
                     kind=PrincipalKind.AGENT,
                     roles={AgentRole.PROPOSER, AgentRole.EXECUTOR,
                            AgentRole.VERIFIER}), by="system")


# ---- what it does not ----------------------------------------------------
def test_the_history_names_which_identity_did_which_part(world, ring_run):
    """Collusion buys the ring a verdict, not anonymity.

    Every part of the run is attributed, so an investigator who later learns
    the three accounts were one person can find every record they touched --
    which is the difference between an assumption that fails silently and one
    that fails with a trail.
    """
    actors = {e.actor for e in world.log.read()}
    assert {"ring-submitter", "ring-worker", "ring-verifier"} <= actors


def test_the_artifact_is_still_the_bytes_the_record_names(world, ring_run):
    """Content addressing does not care who agreed with whom."""
    import json

    from qta_agent.canonical import digest_bytes

    produced = ROOT / world.out_rel / "collusion.json"
    assert produced.is_file()
    on_disk = digest_bytes(produced.read_bytes())
    cited = set(json.loads(json.dumps(ring_run.artifacts)).values())
    assert on_disk in cited, (
        f"the record cites {sorted(cited)} and the file hashes to {on_disk}")


def test_the_write_allowlist_still_binds_the_ring(world):
    """The path guard is not a matter of opinion among participants.

    A refusal here is EITHER an exception or a run that did not reach
    VERIFIED; counting only exceptions would score a refused run as a
    success for the ring, which is the opposite of what it means.
    """
    try:
        outcome = world.run(
            tool_id="stage10.emit_artifact",
            inputs={"out_dir": "../../etc", "name": "passwd",
                    "payload": {"label": "MODEL_ONLY"}},
            submitter="ring-submitter", worker="ring-worker",
            verifier="ring-verifier")
    except Exception:                          # noqa: BLE001 - a refusal
        return
    assert outcome.state is not TaskState.VERIFIED, outcome
    assert not (ROOT.parent.parent / "etc" / "passwd").exists()


def test_the_ring_still_cannot_reach_the_network(world):
    """Three cooperating roles hold three roles, not a grant."""
    from qta_agent.netauth import (
        NetworkAuthority, NetworkRequest, parse_target,
    )

    net = NetworkAuthority(world.log).load()
    target = parse_target("https://example.invalid/exfiltrate")
    # authorize() is TOTAL and returns a decision rather than raising, so
    # the refusal is read from the verdict. A test that only asserted an
    # exception would pass over a decision that said ALLOW.
    decision = net.authorize(
        NetworkRequest(actor="ring-worker", task_id="t-ring",
                       tool_id="stage10.emit_artifact", target=target),
        grant_id="no-such-grant")
    assert not decision.allowed, decision
    with pytest.raises(NetworkDenied):
        decision.raise_if_denied()


def test_the_ring_cannot_cite_evidence_that_does_not_exist(world):
    """The one thing three cooperating roles most want is to agree that
    something was checked. Agreement is not a digest."""
    from qta_agent.store import StoreError

    with pytest.raises((StoreError, Exception)):
        world.store.create(
            record_id="ring-fabricated", kind="claim",
            proposer="ring-submitter",
            evidence={"verification_report": "f" * 64})


def test_nothing_the_ring_did_moved_a_gate(world, ring_run):
    """The bound on the blast radius, asserted rather than assumed.

    The isolation suite is what enforces automatic_gate_effect = NONE
    structurally; this asserts the other half, which is that the ring's
    reach is bounded by it -- everything it wrote lives under the governed
    workspace and no gate-computing module can see any of it.
    """
    written = {a for a in ring_run.artifacts}
    assert written, "the run produced nothing, so this asserts nothing"
    assert all(a.startswith(world.out_rel) or "/" not in a
               for a in written), sorted(written)
    assert ring_run.state is TaskState.VERIFIED, (
        "and the run really did succeed, so this is a statement about what "
        "success is worth here rather than about a failed attack")


def test_the_boundary_is_written_where_somebody_will_read_it():
    """A limit recorded only in a test report is a limit nobody finds.

    Separation of duties assumes the identities are independent, and the
    module that enforces it has to say so where the enforcement is.
    """
    src = (ROOT / "qta_agent" / "authority.py").read_text(encoding="utf-8")
    assert "requires_distinct_actor" in src
    text = (ROOT / "AGENT_SUBSTRATE.md").read_text(encoding="utf-8")
    assert "colluding" in text.lower(), (
        "the assumption is documented in a test and nowhere a reader of the "
        "architecture would look")
