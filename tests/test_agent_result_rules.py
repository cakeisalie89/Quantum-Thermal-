"""A scientific result is VERIFIED only on evidence that supports it -- at the store.

Until this, the rule lived in ``governed_model.decide``, and a caller writing
the transition to the store directly got past it: the edge checked that the
cited report EXISTED, and a FAIL report exists. Now the store reads what the
record cites and refuses the edge. Every refusal is exercised here against
documents the ``scientific`` package actually writes, with a control that the
genuine pair passes and that other record kinds are untouched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import result_rules  # noqa: E402
from agent_substrate.authority import Role, State, TransitionError  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.store import AuthorityStore  # noqa: E402


@pytest.fixture(scope="module")
def pair():
    """A real ResultBundle and the real independent check of it."""
    from scientific.checks.slab_mol import run_check
    from scientific.model import run_model
    from scientific.models.slab_transient import SlabTransientModel
    from governed_case import PARAMS
    bundle = run_model(SlabTransientModel(), PARAMS)
    report = run_check(bundle, verifier_id="checker")
    return bundle.to_record(), report.to_record()


def _put(ev, doc) -> str:
    return ev.put(json.dumps(doc, sort_keys=True).encode())


class _EveryArtefactGoverned:
    """A view of governed execution under which every cited artefact came
    from separated governed tasks. This module holds the CONTENT rule, so
    origin is taken as satisfied here; it is held against the real governed
    path, where it is not, in ``tests/test_scientific_authority_replay.py``.
    """

    def problems(self, **_):
        return []


@pytest.fixture()
def world(tmp_path, pair):
    ev = EvidenceStore(tmp_path / "evidence")
    store = AuthorityStore(EventLog(tmp_path / "log.jsonl"), evidence=ev,
                           origins=_EveryArtefactGoverned())
    bundle, report = pair
    return store, ev, bundle, report


def _under_review(store, ev, bundle, rid="r1", kind=result_rules.KIND):
    store.create(record_id=rid, kind=kind, proposer="proposer",
                 evidence={"result_bundle": _put(ev, bundle)})
    store.transition(record_id=rid, dst=State.UNDER_REVIEW,
                     actor="reviewer", role=Role.VERIFIER)
    return rid


def _verify(store, rid, report_sha):
    return store.transition(record_id=rid, dst=State.VERIFIED,
                            actor="reviewer", role=Role.VERIFIER,
                            evidence={"verification_report": report_sha})


def test_the_genuine_pair_satisfies_the_rule(pair):
    """Held to what the scientific package writes, not to a fixture."""
    assert result_rules.verification_problems(*pair) == []


def test_a_supported_result_is_verified(world):
    store, ev, bundle, report = world
    rid = _under_review(store, ev, bundle)
    assert _verify(store, rid, _put(ev, report)).state is State.VERIFIED


@pytest.mark.parametrize("change,why", [
    ({"status": "FAIL"}, "reported FAIL"),
    ({"status": "NOT_RUN"}, "reported NOT_RUN"),
    ({"subject_digest": "c" * 64}, "different bundle"),
    ({"check_type": "INVARIANT"}, "not an independent check"),
    ({"producer_implementation_digest": "d" * 64}, "another producer"),
    ({"verifier_implementation_digest": "PRODUCER"}, "producer's own code"),
    ({"verifier_implementation_digest": "not-a-digest"},
     "producer's own code"),
    ({"establishes": "EXPERIMENTAL_VALIDATION"},
     "cannot establish experimental validation"),
    ({"establishes": "NUMERICAL_CONSISTENCY"},
     "claims to establish NUMERICAL_CONSISTENCY"),
    ({"independence": "NONE"}, "declares no independence"),
    ({"surplus": 1}, "not a VerificationResult record"),
])
def test_the_store_refuses_a_report_that_does_not_support_it(world, change,
                                                             why):
    """THE bypass that was open: a direct transition citing a report that
    exists and does not support the result."""
    store, ev, bundle, report = world
    if change.get("verifier_implementation_digest") == "PRODUCER":
        change = {"verifier_implementation_digest":
                  bundle["implementation_digest"]}
    rid = _under_review(store, ev, bundle)
    with pytest.raises(TransitionError, match=why):
        _verify(store, rid, _put(ev, {**report, **change}))
    assert store.get(rid).state is State.UNDER_REVIEW


def test_a_bundle_whose_invariant_failed_is_not_verified(world):
    store, ev, bundle, report = world
    from agent_substrate.canonical import digest
    broken = json.loads(json.dumps(bundle))
    broken["invariants"][0]["holds"] = False
    rid = _under_review(store, ev, broken)
    with pytest.raises(TransitionError, match="invariants not holding"):
        _verify(store, rid, _put(ev, {**report,
                                      "subject_digest": digest(broken)}))


def test_a_store_without_evidence_cannot_verify_a_result(tmp_path, pair):
    store = AuthorityStore(EventLog(tmp_path / "log.jsonl"),
                           origins=_EveryArtefactGoverned())
    store.create(record_id="r", kind=result_rules.KIND, proposer="p",
                 evidence={"result_bundle": "a" * 64})
    store.transition(record_id="r", dst=State.UNDER_REVIEW, actor="v",
                     role=Role.VERIFIER)
    with pytest.raises(TransitionError,
                       match="no evidence store is attached"):
        store.transition(record_id="r", dst=State.VERIFIED, actor="v",
                         role=Role.VERIFIER,
                         evidence={"verification_report": "b" * 64})
    assert store.get("r").state is State.UNDER_REVIEW


def test_a_store_that_cannot_see_governed_execution_cannot_verify(
        tmp_path, pair):
    """The documents are genuine and say everything the content rule asks.
    Without a view of governed execution nobody can say a governed
    verification produced the report, so live, the edge is refused -- a
    claim that cannot be checked does not become a fact of the log."""
    bundle, report = pair
    ev = EvidenceStore(tmp_path / "evidence")
    store = AuthorityStore(EventLog(tmp_path / "log.jsonl"), evidence=ev)
    rid = _under_review(store, ev, bundle)
    with pytest.raises(TransitionError,
                       match="no view of governed execution"):
        _verify(store, rid, _put(ev, report))
    assert store.get(rid).state is State.UNDER_REVIEW


def test_a_bundle_that_is_not_a_simulation_result_is_not_verified(world):
    """Numerical verification admits a simulation result and nothing else.
    A bundle presented as an observation is refused even with a report
    that is about it and says PASS."""
    store, ev, bundle, report = world
    from agent_substrate.canonical import digest
    measured = {**json.loads(json.dumps(bundle)),
                "observation_kind": "PROCESSED_OBSERVATION"}
    rid = _under_review(store, ev, measured)
    with pytest.raises(TransitionError, match="simulation results only"):
        _verify(store, rid, _put(ev, {**report,
                                      "subject_digest": digest(measured)}))


@pytest.mark.parametrize("policy", [None, "", "scientific_result.admission/0",
                                    "scientific_result.admission/3"])
def test_a_policy_this_code_does_not_know_is_refused(pair, policy):
    """Old authority is not reinterpreted under a newer rule, and a name
    nothing here defines is not read as the current one."""
    bundle, report = pair
    problems = result_rules.admission_problems(
        policy, {}, fetch=None, origins=None, proposer="p", actor="v",
        before_seq=None)
    assert problems and "not one this code can decide under" in problems[0]


def test_the_current_policy_is_the_one_transitions_record(world):
    store, ev, bundle, report = world
    rid = _under_review(store, ev, bundle)
    _verify(store, rid, _put(ev, report))
    _, events = store.log.read_verified()
    last = [e for e in events if e.action == "record.transition"][-1]
    assert last.payload["admission_policy"] == result_rules.CURRENT_POLICY
    assert result_rules.CURRENT_POLICY == "scientific_result.admission/2"
    rec = store.get(rid)
    assert rec.admission == result_rules.ADMITTED
    assert rec.admission_basis == {"policy": result_rules.CURRENT_POLICY,
                                   "seq": last.seq, "actor": "reviewer"}


def test_other_kinds_carry_no_admission(world):
    store, ev, bundle, report = world
    rid = _under_review(store, ev, bundle, kind="stage10_artifact")
    _verify(store, rid, _put(ev, report))
    _, events = store.log.read_verified()
    last = [e for e in events if e.action == "record.transition"][-1]
    assert "admission_policy" not in last.payload
    assert store.get(rid).admission is None


def test_evidence_that_is_not_json_is_a_problem_not_a_pass(world):
    store, ev, bundle, report = world
    rid = _under_review(store, ev, bundle)
    with pytest.raises(TransitionError, match="cannot be read"):
        _verify(store, rid, ev.put(b"\x00 not json"))


def test_other_record_kinds_are_untouched(world):
    """Control: the rule is scoped to scientific results."""
    store, ev, bundle, report = world
    rid = _under_review(store, ev, bundle, kind="stage10_artifact")
    fail = _put(ev, {**report, "status": "FAIL"})
    assert _verify(store, rid, fail).state is State.VERIFIED


def test_promotion_is_held_to_the_same_rule(world):
    store, ev, bundle, report = world
    rid = _under_review(store, ev, bundle)
    good = _put(ev, report)
    _verify(store, rid, good)
    with pytest.raises(TransitionError, match="reported FAIL"):
        store.transition(record_id=rid, dst=State.PROMOTED, actor="promoter",
                         role=Role.PROMOTER, policy_id="p",
                         evidence={"verification_report":
                                   _put(ev, {**report, "status": "FAIL"}),
                                   "policy_id": "p"})
    rec = store.transition(record_id=rid, dst=State.PROMOTED,
                           actor="promoter", role=Role.PROMOTER,
                           policy_id="p",
                           evidence={"verification_report": good,
                                     "policy_id": "p"})
    assert rec.state is State.PROMOTED


def test_a_bundle_with_no_invariants_is_not_verified(world):
    """An empty invariant list is not 'every invariant holds'."""
    store, ev, bundle, report = world
    from agent_substrate.canonical import digest
    bare = {**json.loads(json.dumps(bundle)), "invariants": []}
    rid = _under_review(store, ev, bare)
    with pytest.raises(TransitionError, match="none run"):
        _verify(store, rid, _put(ev, {**report,
                                      "subject_digest": digest(bare)}))


def test_a_record_citing_no_bundle_is_not_verified(world):
    store, ev, bundle, report = world
    store.create(record_id="nobundle", kind=result_rules.KIND,
                 proposer="proposer")
    store.transition(record_id="nobundle", dst=State.UNDER_REVIEW,
                     actor="reviewer", role=Role.VERIFIER)
    with pytest.raises(TransitionError, match="cites no result_bundle"):
        _verify(store, "nobundle", _put(ev, report))


# ---- AUD-027: numbers JSON does not have ------------------------------------

def _with_token(bundle: dict, token: str) -> bytes:
    """The bundle's bytes with one parameter written as ``token``: NaN,
    Infinity and -Infinity, which Python's json reads and JSON (RFC 8259)
    has no place for, or a numeral past the float range, which reads as
    infinity."""
    doc = json.loads(json.dumps(bundle))
    name = sorted(doc["parameters"])[0]
    doc["parameters"][name] = 123456.789
    return json.dumps(doc, sort_keys=True).replace(
        "123456.789", token).encode()


TOKENS = ["NaN", "Infinity", "-Infinity", "1e400"]


@pytest.mark.parametrize("token", TOKENS)
def test_a_number_json_does_not_have_is_a_problem_not_an_exception(
        world, token):
    """Found in review (Stage 5): such a bundle made the rule raise
    CanonicalizationError -- neither the problem its docstring promises for
    bytes that are not JSON, nor EvidenceUnavailable -- so the store's live
    edge and its replay both met an exception nobody catches."""
    store, ev, bundle, report = world
    sha = ev.put(_with_token(bundle, token))
    problems = result_rules.record_problems(
        {"result_bundle": sha, "verification_report": _put(ev, report)},
        ev.get)
    # the token is no JSON at all; the numeral is JSON with no canonical form
    expected = ("no canonical form" if token == "1e400"
                else "cannot be read as JSON")
    assert any(expected in p for p in problems), problems
    store.create(record_id="r1", kind=result_rules.KIND, proposer="proposer",
                 evidence={"result_bundle": sha})
    store.transition(record_id="r1", dst=State.UNDER_REVIEW,
                     actor="reviewer", role=Role.VERIFIER)
    with pytest.raises(TransitionError, match="does not support"):
        _verify(store, "r1", _put(ev, report))
    assert store.get("r1").state is State.UNDER_REVIEW


@pytest.mark.parametrize("token", TOKENS)
def test_replay_refuses_a_planted_admission_of_such_a_bundle(world, token):
    """A VERIFIED transition written past the store, citing such a bundle:
    replay refuses it as it refuses any inadmissible one -- StoreError,
    "presence in the log is not authority" -- not with an exception of
    another kind."""
    from agent_substrate.store import StoreError
    store, ev, bundle, report = world
    good = _under_review(store, ev, bundle, rid="good")
    _verify(store, good, _put(ev, report))
    _, events = store.log.read_verified()
    template = [e for e in events if e.action == "record.transition"
                and e.payload.get("dst") == "VERIFIED"][-1]
    store.create(record_id="bad", kind=result_rules.KIND,
                 proposer="proposer",
                 evidence={"result_bundle": ev.put(_with_token(bundle,
                                                               token))})
    store.transition(record_id="bad", dst=State.UNDER_REVIEW,
                     actor="reviewer", role=Role.VERIFIER)
    store.log.append(actor=template.actor, action="record.transition",
                     target="bad",
                     payload={**template.payload, "record_id": "bad"})
    with pytest.raises(StoreError, match="not admissible"):
        AuthorityStore(EventLog(store.log.path), evidence=ev,
                       origins=_EveryArtefactGoverned()).load()


# ---- admission/2: the check's side of the agreement (AUD-025) -------------

def _without_agreement(report: dict) -> dict:
    return {k: v for k, v in report.items() if k != "agreement"}


def test_the_package_states_the_agreement_the_rule_asks_for(pair):
    """Held to what the scientific package writes: the check names each
    output it compared, with its own value and error, in the output's
    unit, and the current rule accepts exactly that."""
    bundle, report = pair
    outs = {o["name"]: o["quantity"] for o in bundle["outputs"]
            if o["status"] == "OK"}
    assert report["agreement"]
    for name, q in report["agreement"]:
        assert q["unit"] == outs[name]["unit"]
        assert q["resolution"] > 0
    assert result_rules.agreement_problems(bundle, report) == []


def test_a_pass_under_admission_2_must_state_its_agreement(world):
    store, ev, bundle, report = world
    for doc, why in ((_without_agreement(report),
                      "not a VerificationResult record under "
                      "scientific_result.admission/2"),
                     ({**report, "agreement": []}, "states no agreement")):
        rid = _under_review(store, ev, bundle, rid=f"r-{len(why)}")
        with pytest.raises(TransitionError, match=why):
            _verify(store, rid, _put(ev, doc))
        assert store.get(rid).state is State.UNDER_REVIEW


def _agreement_edit(kind: str):
    def edit(report: dict, bundle: dict) -> dict:
        agree = [list(e) for e in report["agreement"]]
        name, q = agree[0]
        if kind == "not a pair":
            agree[0] = [name]
        elif kind == "twice":
            agree.append([name, dict(q)])
        elif kind == "no such output":
            agree[0] = ["T_nowhere", q]
        elif kind == "infinite error":
            agree[0] = [name, {**q, "resolution": float("inf")}]
        elif kind == "negative error":
            agree[0] = [name, {**q, "resolution": -1.0}]
        elif kind == "no value":
            agree[0] = [name, {**q, "value": None}]
        elif kind == "a boolean":
            agree[0] = [name, {**q, "value": True}]
        elif kind == "another unit":
            agree[0] = [name, {**q, "unit": "degC"}]
        elif kind == "an integer no float holds":
            agree[0] = [name, {**q, "value": 10 ** 400}]
        elif kind == "an error no float holds":
            agree[0] = [name, {**q, "resolution": 10 ** 400}]
        else:                                            # pragma: no cover
            raise AssertionError(kind)
        return {**report, "agreement": agree}
    return edit


#: (edit, what the rule says, what the store's edge says). An infinite
#: error never reaches the rule through the store: JSON has no Infinity, and
#: the reader refuses the bytes first. An integer of 400 digits does reach
#: it: valid JSON, it parses to an int that no float holds.
AGREEMENT_CASES = [
    ("not a pair", "not [output, quantity]", None),
    ("twice", "twice", None),
    ("no such output", "no OK output of the bundle", None),
    ("infinite error", "no finite value and error",
     "cannot be read as JSON"),
    ("negative error", "no finite value and error", None),
    ("no value", "no finite value and error", None),
    ("a boolean", "no finite value and error", None),
    ("another unit", "is in 'degC'", None),
    ("an integer no float holds", "no finite value and error", None),
    ("an error no float holds", "no finite value and error", None),
]


@pytest.mark.parametrize("kind,says,edge_says", AGREEMENT_CASES,
                         ids=[c[0] for c in AGREEMENT_CASES])
def test_an_agreement_the_rule_cannot_use_is_refused(world, kind, says,
                                                     edge_says):
    """Every way an agreement could mislead a reader bounding the producer's
    value -- a name read twice, an output that was never OK, a number that
    is no number, a unit the comparison would silently mix -- refuses the
    edge, and the record stays where it was."""
    import re
    store, ev, bundle, report = world
    doc = _agreement_edit(kind)(report, bundle)
    problems = result_rules.agreement_problems(bundle, doc)
    assert any(says in p for p in problems), problems
    rid = _under_review(store, ev, bundle)
    with pytest.raises(TransitionError, match=re.escape(edge_says or says)):
        _verify(store, rid, _put(ev, doc))
    assert store.get(rid).state is State.UNDER_REVIEW


@pytest.mark.parametrize("keep_number", [False, True])
def test_an_agreement_on_an_output_that_failed_is_refused(pair, keep_number):
    """An output the producer did not compute has no value to bound; an
    agreement naming it is no agreement -- whether the output carries no
    quantity, as a failed output is written, or still carries its number,
    as a forger could write one, so that only its status says so."""
    bundle, report = (json.loads(json.dumps(d)) for d in pair)
    name = report["agreement"][0][0]
    out = next(o for o in bundle["outputs"] if o["name"] == name)
    out.update(status="FAILED", reason="test")
    if not keep_number:
        out["quantity"] = None
    assert any(f"{name!r}, which is no OK output" in p
               for p in result_rules.agreement_problems(bundle, report))


def test_admission_1_decides_as_it_did_and_neither_rule_reads_the_other(
        pair):
    """A report written before the agreement is decided under the policy
    its admission names, as it was; the newer shape is not read under the
    older rule, nor the older under the newer."""
    bundle, report = pair
    old = _without_agreement(report)
    assert result_rules.verification_problems(
        bundle, old, result_rules.POLICY_V1) == []
    assert any("under scientific_result.admission/1" in p
               for p in result_rules.verification_problems(
                   bundle, report, result_rules.POLICY_V1))
    assert any("under scientific_result.admission/2" in p
               for p in result_rules.verification_problems(
                   bundle, old, result_rules.POLICY_V2))


def test_a_history_admitted_under_admission_1_replays_admitted(world):
    """Migration, not reinterpretation: a VERIFIED transition stamped
    admission/1, citing a report written before the agreement, is admitted
    on replay under admission/1 -- and the same stamp on a report of the
    newer shape is not."""
    from agent_substrate.store import StoreError
    store, ev, bundle, report = world
    good = _under_review(store, ev, bundle, rid="good")
    _verify(store, good, _put(ev, report))
    _, events = store.log.read_verified()
    template = [e for e in events if e.action == "record.transition"
                and e.payload.get("dst") == "VERIFIED"][-1]
    assert template.payload["admission_policy"] == result_rules.POLICY_V2
    old = _under_review(store, ev, bundle, rid="old")
    store.log.append(actor=template.actor, action="record.transition",
                     target=old, payload={
                         **template.payload, "record_id": old,
                         "admission_policy": result_rules.POLICY_V1,
                         "evidence": {"verification_report": _put(
                             ev, _without_agreement(report))}})
    replayed = AuthorityStore(EventLog(store.log.path), evidence=ev,
                              origins=_EveryArtefactGoverned()).load()
    assert replayed.get(old).admission == result_rules.ADMITTED
    assert replayed.get(old).admission_basis["policy"] == \
        result_rules.POLICY_V1
    assert replayed.get(good).admission_basis["policy"] == \
        result_rules.POLICY_V2
    newer = _under_review(replayed, ev, bundle, rid="newer")
    replayed.log.append(actor=template.actor, action="record.transition",
                        target=newer, payload={
                            **template.payload, "record_id": newer,
                            "admission_policy": result_rules.POLICY_V1})
    with pytest.raises(StoreError, match="not admissible"):
        AuthorityStore(EventLog(store.log.path), evidence=ev,
                       origins=_EveryArtefactGoverned()).load()
