"""Evidence that is not a JSON object is refused in each primary's own words
(AUD-055, the remainder of AUD-047).

Evidence is content-addressed, so the bytes a record cites are the bytes it
was written with; what they hold is still the writer's. Four readers parsed
them with no answer for bytes that are not a JSON object, and raised the
parser's error, or AttributeError on the first ``.get``: the research ledger,
reading a bundle on replay and from its caller; the learned-lifecycle ledger,
reading a document; the governed model runs, reading a record their tools
wrote; and the claim check, reading the bundle a citation names. Each is
handed bytes that are not JSON, a JSON list, a JSON number, a constant JSON
does not have, and nesting past the parser.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import claim_check as C  # noqa: E402
from agent_substrate import research as R  # noqa: E402
from agent_substrate.authority import Role, State  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.store import AuthorityStore  # noqa: E402

NOT_OBJECTS = {"not JSON": b"not json", "a list": b"[1, 2]",
               "a number": b"7", "a constant": b'{"v": NaN}',
               "nested past the parser": b"[" * 100_000 + b"]" * 100_000}
CASES = pytest.mark.parametrize("raw", list(NOT_OBJECTS.values()),
                                ids=list(NOT_OBJECTS))
BUNDLE = json.dumps({"model_id": "m", "outputs": [
    {"name": "T", "status": "OK",
     "quantity": {"value": 300.0, "unit": "K", "resolution": 0.5}}]}
).encode()


class World:
    """A log, an evidence store, an objective and a verified record citing
    ``bundle`` -- written through the authority store, as an honest writer
    writes them."""

    def __init__(self, tmp, bundle: bytes, report: bytes = b"checked"):
        self.log = EventLog(Path(tmp) / "log.jsonl")
        self.evidence = EvidenceStore(Path(tmp) / "evidence")
        self.ledger = R.ResearchLedger(self.log, evidence=self.evidence,
                                       actor="controller").load()
        self.ledger.register(objective_id="o1", statement="what does it say?",
                             owner="owner", max_steps=4,
                             max_inference_calls=1)
        self.digest = self.evidence.put(bundle)
        store = AuthorityStore(self.log, evidence=self.evidence).load()
        store.create(record_id="r1", kind="measurement", proposer="ingest",
                     evidence={"result_bundle": self.digest})
        store.transition(record_id="r1", dst=State.UNDER_REVIEW,
                         actor="reviewer", role=Role.VERIFIER)
        store.transition(record_id="r1", dst=State.VERIFIED,
                         actor="reviewer", role=Role.VERIFIER, evidence={
                             "verification_report": self.evidence.put(
                                 report)})

    def forge_observation(self):
        """What a writer that never read the bundle appends."""
        self.log.append(actor="controller", action=R.ACT_OBSERVATION,
                        target="obs-1", payload={"observation": {
                            "observation_id": "obs-1", "objective_id": "o1",
                            "record_id": "r1", "result_bundle": self.digest,
                            "model_id": "m", "values": {}}})


# ------------------------------------------------------- the research ledger
@CASES
def test_the_ledger_refuses_to_observe_what_is_not_a_bundle(tmp_path, raw):
    w = World(tmp_path, raw)
    head = w.log.head().seq
    with pytest.raises(R.ResearchError,
                       match="'obs-1': its result bundle is not a JSON "
                             "object"):
        w.ledger.observe(observation_id="obs-1", objective_id="o1",
                         record_id="r1", bundle_bytes=raw)
    assert w.log.head().seq == head, "nothing is recorded"


@CASES
def test_a_history_observing_what_is_not_a_bundle_is_refused(tmp_path, raw):
    w = World(tmp_path, raw)
    w.forge_observation()
    with pytest.raises(R.ResearchError,
                       match=r"seq \d+: 'obs-1': its result bundle is not a "
                             "JSON object"):
        R.ResearchLedger(w.log, evidence=w.evidence).load()
    # a ledger with no evidence store reads no bundle, as before
    R.ResearchLedger(w.log).load()


def test_an_honest_bundle_is_observed(tmp_path):
    """The control for both of the above."""
    w = World(tmp_path, BUNDLE)
    body = w.ledger.observe(observation_id="obs-1", objective_id="o1",
                            record_id="r1", bundle_bytes=BUNDLE)
    assert body["values"] == {"T": {"value": 300.0, "unit": "K",
                                    "resolution": 0.5}}
    R.ResearchLedger(w.log, evidence=w.evidence).load()


@CASES
def test_a_report_that_is_not_an_object_states_nothing(tmp_path, raw):
    """The scientific context reads the record's report as _report_of
    reads it: bytes that are no JSON object state nothing."""
    w = World(tmp_path, BUNDLE, report=raw)
    w.ledger.observe(observation_id="obs-1", objective_id="o1",
                     record_id="r1", bundle_bytes=BUNDLE)
    check = w.ledger.scientific_context("obs-1")["verification"]["check"]
    assert set(check.values()) == {None}


@pytest.mark.parametrize("bundle, values", [
    ({"outputs": "x"}, {}),
    ({"outputs": [1, None, "T"]}, {}),
    ({"outputs": [{"status": "OK", "quantity": {"value": 1.0}}]}, {}),
    ({"outputs": [{"name": ["T"], "status": "OK",
                   "quantity": {"value": 1.0}}]}, {}),
    ({"outputs": [{"name": "T", "status": "OK", "quantity": "x"}]}, {}),
    ({"outputs": [{"name": "T", "status": "OK",
                   "quantity": {"value": 1.0, "resolution": "0.5"}}]}, {}),
    ({"outputs": [{"name": "T", "status": "OK",
                   "quantity": {"value": 1.0, "unit": "K"}}]},
     {"T": {"value": 1.0, "unit": "K", "resolution": 0.0}}),
])
def test_a_bundle_is_read_for_its_ok_outputs_whatever_else_it_holds(
        bundle, values):
    assert R.values_of(bundle) == values
    assert R.observed_values(bundle, {"agreement": [["T", {
        "value": 1.0, "resolution": 0.1}]]}) == {
        n: dict(v, resolution=max(v["resolution"], 0.1))
        for n, v in values.items()}


# ------------------------------------------------- the learned-model ledger
@CASES
def test_the_learned_ledger_refuses_a_document_that_is_not_an_object(
        tmp_path, raw):
    from agent_substrate.learned_lifecycle import LearnedLedger, LedgerError
    log = EventLog(tmp_path / "log.jsonl")
    ev = EvidenceStore(tmp_path / "evidence")
    store = AuthorityStore(log, evidence=ev).load()
    store.create(record_id="learned_architecture:x",
                 kind="learned_architecture", proposer="trainer",
                 evidence={"document": ev.put(raw)})
    ledger = LearnedLedger(store, ev)
    with pytest.raises(LedgerError, match="learned_architecture:x: its "
                                          "document is not a JSON object"):
        ledger.document("learned_architecture:x")
    with pytest.raises(LedgerError, match="not a JSON object"):
        ledger.documents()


# ------------------------------------------------------ governed model runs
@CASES
def test_a_governed_record_that_is_not_an_object_is_refused(tmp_path, raw):
    from agent_substrate.governed_model import (
        GovernedModelRuns, ModelRunRefused,
    )
    ev = EvidenceStore(tmp_path / "evidence")
    runs = GovernedModelRuns(root=tmp_path, log=EventLog(tmp_path / "l.jsonl"),
                             evidence=ev)
    sha = ev.put(raw)
    with pytest.raises(ModelRunRefused, match=f"evidence {sha[:12]} is not "
                                              "a JSON record"):
        runs._record_of(sha)
    assert runs._record_of(ev.put(BUNDLE))["model_id"] == "m"


# ------------------------------------------------------------ the claim check
@CASES
def test_a_citation_of_bytes_that_are_not_a_bundle_is_broken(tmp_path, raw):
    """The claim check reads the bundle a citation names. A ledger with no
    evidence store of its own holds an observation of any bytes; the check
    is handed the store that holds them, and reports the citation broken
    rather than raising."""
    w = World(tmp_path, raw)
    w.forge_observation()
    r = C.check("T of m is below 400 K", cites=["obs-1"], objective_id="o1",
                ledger=R.ResearchLedger(w.log).load(),
                store=AuthorityStore(w.log, evidence=w.evidence).load(),
                evidence=w.evidence)
    assert r.citation == "BROKEN"
    assert r.citation_problems == ("obs-1: its result bytes are not a result "
                                   "bundle",)
    assert not r.established
