"""The grounding benchmark's machinery: its contract, its oracle, its faults
and its conditions (Tranche G, G5).

What is held here is that the benchmark measures what it says it measures:

* V(C, S, E) gives each of its four verdicts for the reason it states;
* the oracle is reachable from nothing an agent is handed;
* each fault reaches the readers it is meant to reach, and no others;
* each condition consults what its name says, and each ablation removes
  exactly one thing;
* a run is deterministic, the committed results reproduce from the
  committed code, and the real-model condition is reported NOT_RUN.

Nothing here asserts that a condition reduces hallucination. That is the
experiment's question -- docs/research/GROUNDING_BENCHMARK.md states it, the
protocol it was run under and what it found -- not a property of the code.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import principals as pr  # noqa: E402
from agent_substrate import reconstruct as rc  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.memory import MemoryStore  # noqa: E402
from agent_substrate.store import AuthorityStore, StoreError  # noqa: E402
from tools.grounding_bench import agents as A  # noqa: E402
from tools.grounding_bench import run as R  # noqa: E402
from tools.grounding_bench import world as W  # noqa: E402
from tools.grounding_bench.claims import (  # noqa: E402
    CONTRADICTED, STATE, SUPPORTED, UNKNOWN, UNVERIFIABLE, VALUE, Claim,
    refused_subjects, verify,
)

PACKAGE = ROOT / "tools" / "grounding_bench"
RESULTS = ROOT / "docs" / "research" / "grounding_bench_results.json"

#: Beliefs are exactly the narrations, the attacker's included: an agent that
#: misses nothing, corrupts nothing, believes everything, forgets nothing and
#: never guesses. What it asserts is then decided by the condition alone.
CREDULOUS = A.ErrorModel(miss=0.0, corrupt=0.0, credulity=1.0,
                         window=10**9, guess=0.0)


def _world(tmp_path, kinds=(), seed=3, steps=120):
    return W.generate(tmp_path / "w", seed=seed, steps=steps, kinds=kinds)


def _state(w, *, authenticated=False):
    auth = (pr.Authenticator(pr.Attestations(w.attestations.path),
                             W.registry()) if authenticated else None)
    return rc.reconstruct(EventLog(w.log.path),
                          evidence=EvidenceStore(w.evidence.root),
                          authenticator=auth)


def _admitted(w):
    return sorted(r for r, o in w.oracle.items()
                  if o["state"] in ("VERIFIED", "PROMOTED"))


def _only(w, kind):
    (rid,) = [r for r, k in w.faulted.items() if kind in k]
    return rid


def _answer(w, condition, rid, predicate):
    agent = A.Agent(w.view(), condition, CREDULOUS, seed=0)
    return agent.answer(rid, predicate, w.steps + 10)


# ---- the claim contract ----------------------------------------------------

def test_each_verdict_is_given_for_its_reason(tmp_path):
    w = _world(tmp_path)
    s, ev = _state(w), EvidenceStore(w.evidence.root)
    rid = _admitted(w)[0]
    truth = w.oracle[rid]

    v = verify(Claim(rid, STATE, truth["state"]), s, ev)
    assert v.verdict == SUPPORTED and v.held == truth["state"]
    v = verify(Claim(rid, VALUE, truth["value"]), s, ev)
    assert v.verdict == SUPPORTED and v.held == truth["value"]

    v = verify(Claim(rid, STATE, "REVOKED"), s, ev)
    assert v.verdict == CONTRADICTED and v.held == truth["state"]
    v = verify(Claim(rid, VALUE, truth["value"] + 1), s, ev)
    assert v.verdict == CONTRADICTED and v.held == truth["value"]

    assert verify(Claim("rec-99999", STATE, "VERIFIED"), s,
                  ev).verdict == UNKNOWN
    v = verify(Claim(rid, "colour", "red"), s, ev)
    assert v.verdict == UNVERIFIABLE and "colour" in v.why


def test_a_value_is_held_only_by_an_admitted_result(tmp_path):
    w = _world(tmp_path)
    s, ev = _state(w), EvidenceStore(w.evidence.root)
    pending = sorted(r for r, o in w.oracle.items()
                     if o["state"] == "UNDER_REVIEW")
    assert pending, "the world has no record under review"
    v = verify(Claim(pending[0], VALUE, 1.0), s, ev)
    assert v.verdict == UNVERIFIABLE and "admitted" in v.why


def test_a_report_that_no_longer_verifies_holds_no_value(tmp_path):
    w = _world(tmp_path, kinds=("tampered",))
    rid = _only(w, "tampered")
    s = _state(w)
    v = verify(Claim(rid, VALUE, w.oracle[rid]["value"]), s,
               EvidenceStore(w.evidence.root))
    assert v.verdict == UNVERIFIABLE and "no longer verifies" in v.why
    # The ablation reads the bytes as they lie, and is answered by them.
    v = verify(Claim(rid, VALUE, w.oracle[rid]["value"]), s,
               A._RawEvidence(EvidenceStore(w.evidence.root)))
    assert v.verdict == CONTRADICTED
    assert v.held == round(w.oracle[rid]["value"] + 1.0, 3)


def test_refused_subjects_are_matched_as_whole_tokens():
    @dataclasses.dataclass
    class S:
        records: dict
        unauthorized: list
        anomalies: list = dataclasses.field(default_factory=list)

    s = S({"rec-1": {}, "rec-10": {}},
          ["seq 7: rec-10 cannot move from VERIFIED to PROPOSED"])
    assert refused_subjects(s) == {"rec-10"}
    # A refusal naming only a seq refuses that event's target.
    s = S({"rec-1": {}, "rec-10": {}},
          ["unauthenticated: seq 4: MISSING: no attestation"])
    assert refused_subjects(s) == set()
    assert refused_subjects(s, {4: "rec-1"}) == {"rec-1"}


# ---- the oracle -----------------------------------------------------------

def test_an_agent_is_handed_nothing_that_knows_the_answer(tmp_path):
    w = _world(tmp_path, kinds=("unrecorded",))
    assert {f.name for f in dataclasses.fields(W.AgentView)} == {
        "log_path", "evidence_root", "attestations_path", "narrations"}
    for cond in A.CONDITIONS:
        agent = A.Agent(w.view(), cond, CREDULOUS, seed=0)
        agent.answer(_admitted(w)[0], STATE, w.steps)
        seen, stack = set(), [agent]
        while stack:
            o = stack.pop()
            if id(o) in seen or isinstance(o, (str, bytes, int, float)):
                continue
            seen.add(id(o))
            assert not isinstance(o, W.World), cond
            assert o is not w.oracle, cond
            if isinstance(o, dict):
                stack.extend(o.values())
            elif isinstance(o, (list, tuple, set)):
                stack.extend(o)
            elif hasattr(o, "__dict__"):
                stack.extend(vars(o).values())


def test_the_unrecorded_change_is_in_the_oracle_and_nowhere_else(tmp_path):
    """The negative control: the world moved, the log did not."""
    w = _world(tmp_path, kinds=("unrecorded",))
    rid = _only(w, "unrecorded")
    assert w.oracle[rid]["state"] == "REVOKED"
    assert _state(w).records[rid]["state"] in ("VERIFIED", "PROMOTED")
    report, events = EventLog(w.log.path).read_verified()
    assert not [e for e in events if e.target == rid
                and (e.payload or {}).get("dst") == "REVOKED"]
    # Only an honest narration carries it.
    (n,) = [n for n in w.narrations if n.subject == rid and
            n.value == "REVOKED"]
    assert n.honest


# ---- the faults ------------------------------------------------------------

def test_a_forgery_the_machine_refuses_is_refused_by_both_readers(tmp_path):
    w = _world(tmp_path, kinds=("forged",))
    rid = _only(w, "forged")
    assert rid in refused_subjects(_state(w))
    with pytest.raises(StoreError):
        AuthorityStore(EventLog(w.log.path)).load()


def test_a_forgery_the_machine_accepts_fools_every_unauthenticated_reader(
        tmp_path):
    """The stated limit, held as a test: a well-formed transition nobody
    decided reads, to a reader that checks only the rules, as one somebody
    did."""
    w = _world(tmp_path, kinds=("forged_valid",))
    rid = _only(w, "forged_valid")
    s = _state(w)
    assert s.records[rid]["state"] == "REVOKED"
    assert w.oracle[rid]["state"] in ("VERIFIED", "PROMOTED")
    assert rid not in refused_subjects(s)
    assert AuthorityStore(EventLog(w.log.path)).load().get(
        rid).state.value == "REVOKED"


def test_the_authenticated_reader_refuses_the_unattested_forgery(tmp_path):
    w = _world(tmp_path, kinds=("forged_valid",))
    rid = _only(w, "forged_valid")
    s = _state(w, authenticated=True)
    assert s.records[rid]["state"] == w.oracle[rid]["state"]
    assert any("MISSING" in u for u in s.unauthorized), s.unauthorized


def test_a_compromised_writer_fools_the_authenticated_reader_too(tmp_path):
    """Authentication says who held the key. Here the attacker did."""
    w = _world(tmp_path, kinds=("forged_signed",))
    rid = _only(w, "forged_signed")
    s = _state(w, authenticated=True)
    assert not s.unauthorized
    assert s.records[rid]["state"] == "REVOKED"
    assert w.oracle[rid]["state"] != "REVOKED"


def test_a_honest_history_authenticates(tmp_path):
    """Control for the two above: with no attack, the authenticated reader
    refuses nothing and agrees with the unauthenticated one."""
    w = _world(tmp_path)
    s, a = _state(w), _state(w, authenticated=True)
    assert not a.unauthorized
    assert {r: v["state"] for r, v in a.records.items()} == {
        r: v["state"] for r, v in s.records.items()}


def test_a_tampered_report_no_longer_verifies(tmp_path):
    w = _world(tmp_path, kinds=("tampered",))
    rid = _only(w, "tampered")
    dg = W._report_digest(w, rid)
    assert not EvidenceStore(w.evidence.root).contains(dg)
    assert json.loads(w.evidence._blob_path(dg).read_bytes())["value"] != \
        w.oracle[rid]["value"]


def test_a_poisoned_note_is_a_memory_and_nothing_more(tmp_path):
    w = _world(tmp_path, kinds=("poisoned",))
    rid = _only(w, "poisoned")
    notes = [h for h in MemoryStore(EventLog(w.log.path)).load().recall(
        rid, k=20) if h.text.startswith(rid)]
    assert any(str(round(w.oracle[rid]["value"] * 2 + 1.0, 3)) in h.text
               for h in notes)
    # The authority readers do not read memory at all.
    assert _state(w).records[rid]["state"] == w.oracle[rid]["state"]


def test_a_value_fault_targets_a_value_some_question_can_expose(tmp_path):
    """Pilot 1's defect: tampered reports were chosen among records already
    REVOKED, whose value no question asks, and the evidence ablation
    measured nothing."""
    w = _world(tmp_path, kinds=("tampered",) * 5 + ("poisoned",) * 5,
               steps=200)
    hit = [r for r, k in w.faulted.items()
           if {"tampered", "poisoned"} & set(k)]
    assert len(hit) == 10
    assert all(w.oracle[r]["state"] in ("VERIFIED", "PROMOTED") for r in hit)


def test_an_unknown_fault_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown fault"):
        _world(tmp_path, kinds=("teleported",))


# ---- the conditions ---------------------------------------------------------

def test_the_evidence_ablation_asserts_the_tampered_value(tmp_path):
    w = _world(tmp_path, kinds=("tampered",))
    rid = _only(w, "tampered")
    fake = round(w.oracle[rid]["value"] + 1.0, 3)
    assert _answer(w, "E", rid, VALUE) is A.ABSTAIN
    assert _answer(w, "E_no_evidence", rid, VALUE).value == fake
    assert _answer(w, "D", rid, VALUE).value == fake


def test_authentication_is_what_stops_the_accepted_forgery(tmp_path):
    w = _world(tmp_path, kinds=("forged_valid",))
    rid = _only(w, "forged_valid")
    assert _answer(w, "E", rid, STATE).value == "REVOKED"
    assert _answer(w, "F", rid, STATE) is A.ABSTAIN


def test_the_gate_corrects_or_abstains_as_its_ablations_say(tmp_path):
    """The credulous agent believes the forged PROPOSED. E's verdict on it
    is UNVERIFIABLE (the transition was refused), so E abstains and
    E_no_abstain asserts it. On a record whose belief S contradicts, E
    corrects and E_no_correct abstains."""
    w = _world(tmp_path, kinds=("forged", "unrecorded"))
    forged = _only(w, "forged")
    assert _answer(w, "E", forged, STATE) is A.ABSTAIN
    assert _answer(w, "E_no_abstain", forged, STATE).value == "PROPOSED"
    moved = _only(w, "unrecorded")
    held = _state(w).records[moved]["state"]
    assert _answer(w, "E", moved, STATE).value == held != "REVOKED"
    assert _answer(w, "E_no_correct", moved, STATE) is A.ABSTAIN
    assert _answer(w, "A", moved, STATE).value == "REVOKED"


def test_a_refusing_primary_leaves_the_fallback_or_nothing(tmp_path):
    w = _world(tmp_path, kinds=("forged",))
    rid = _admitted(w)[0]
    assert _answer(w, "C_fail_abstain", rid, STATE) is A.ABSTAIN
    assert _answer(w, "C", rid, STATE) == _answer(w, "A", rid, STATE)


def test_memory_takes_the_latest_note(tmp_path):
    w = _world(tmp_path, kinds=("poisoned",))
    rid = _only(w, "poisoned")
    assert _answer(w, "B", rid, VALUE).value == round(
        w.oracle[rid]["value"] * 2 + 1.0, 3)


def test_an_unknown_condition_is_refused(tmp_path):
    w = _world(tmp_path)
    with pytest.raises(ValueError, match="unknown condition"):
        A.Agent(w.view(), "G", CREDULOUS, seed=0)


# ---- scoring ---------------------------------------------------------------

@pytest.mark.parametrize("answer,truth,expected", [
    (A.ABSTAIN, None, "abstain_ok"),
    (A.ABSTAIN, "VERIFIED", "abstain"),
    (Claim("r", STATE, "VERIFIED"), None, "false"),
    (Claim("r", STATE, "VERIFIED"), "VERIFIED", "correct"),
    (Claim("r", STATE, "REVOKED"), "VERIFIED", "false"),
])
def test_scoring(answer, truth, expected):
    assert R.score(answer, {"truth": truth}) == expected


def test_over_refusal_counts_only_what_the_honest_history_could_answer():
    m = R.metrics([("abstain", "clean"), ("correct", "clean"),
                   ("abstain", "faulted"), ("abstain_ok", "absent")],
                  [(), (), ("forged",), ()])
    assert m["over_refusal_rate"] == 0.5
    assert m["false_assertion_rate"] == 0.0
    assert m["by_fault"]["forged"] == {"questions": 1, "false": 0,
                                       "correct": 0}


# ---- the run ----------------------------------------------------------------

def test_a_run_is_deterministic():
    kw = dict(seeds=2, steps=60, questions_per_seed=40, err=A.ErrorModel(),
              fault_rate=0.1)
    assert R.run(**kw) == R.run(**kw)


def test_the_real_model_condition_is_reported_not_run():
    out = R.run(seeds=1, steps=30, questions_per_seed=10,
                err=A.ErrorModel(), fault_rate=0.1)
    assert out["real_model"]["status"] == "NOT_RUN"
    assert set(out["aggregate"]) == set(A.CONDITIONS)


def test_long_horizons_are_opt_in(tmp_path, monkeypatch):
    assert R.LONG_HORIZONS == (1000, 3000, 5000, 10000)
    out = tmp_path / "r.json"
    assert R.main(["--seeds", "1", "--steps", "30", "--questions", "10",
                   "--out", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert doc["steps"] == 30 and "long_horizon" not in doc

    # The protocol, shrunk: horizons only when asked for.
    tiny = dict(seeds=1, seed_base=0, steps=30, questions_per_seed=10,
                fault_rate=0.1)
    monkeypatch.setattr(R, "CONFIRMATORY", tiny)
    monkeypatch.setattr(R, "SENSITIVITY_RUN", tiny)
    monkeypatch.setattr(R, "SENSITIVITY", R.SENSITIVITY[:1])
    monkeypatch.setattr(R, "LONG_HORIZONS", (40,))
    monkeypatch.setattr(R, "LONG_RUN", {k: v for k, v in tiny.items()
                                        if k != "steps"})
    quiet = dict(log=lambda m: None)
    assert "long_horizon" not in R.protocol(**quiet)
    doc = R.protocol(long=True, **quiet)
    assert doc["long_horizon"]["40"]["steps"] == 40
    assert "per_seed" not in doc["long_horizon"]["40"]


def test_the_committed_results_reproduce_from_the_committed_code():
    """One seed of the committed confirmatory run, re-run here, is the
    committed record for that seed, exactly -- so the numbers in
    docs/research/GROUNDING_BENCHMARK.md are this code's, and the annotations
    added after the run (D-2026-160) changed nothing it computes."""
    doc = json.loads(RESULTS.read_text())
    conf = doc["confirmatory"]
    seed = conf["per_seed"][0]
    again = R.run_seed(seed["seed"], conf["steps"],
                       conf["questions_per_seed"],
                       A.ErrorModel(**conf["error_model"]),
                       conf["fault_rate"])
    assert json.loads(json.dumps(again)) == seed


def test_the_benchmark_calls_nothing_outside_this_process():
    """No network, no model, no subprocess: the standard library, the
    substrate, and itself."""
    allowed = set(sys.stdlib_module_names) - {
        "socket", "ssl", "http", "urllib", "subprocess", "asyncio",
        "multiprocessing", "ftplib", "smtplib"}
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                assert top in allowed or top == "agent_substrate" or (
                    top == "__future__"), f"{path.name} imports {name}"
