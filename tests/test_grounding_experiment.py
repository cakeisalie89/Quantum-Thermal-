"""Gate D: the preregistered grounding experiment's design holds.

What is held, on one seed and on the registered protocol:

* the protocol registers the confirmatory parameters' digest, and a run
  whose parameters differ refuses to start;
* the answerer holds no reference to the world or its oracle;
* each condition is what the protocol says: D4 and D5 gate D1's answer and
  never replace it, D6 asks no model, D5 to D7 assert nothing unsupported,
  and an answer the grammar cannot read is never asserted by a gate;
* the negative control defeats the state, as registered (H9);
* every model answer went through the gateway and was accounted;
* once the confirmatory results are committed, a re-run of their first
  seed reproduces them exactly.

The answerer is a fixture. Nothing here measures a language model.
"""
from __future__ import annotations

import gc
import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import grounding_experiment as G  # noqa: E402

SEED = G.CONFIRMATORY["seeds"][0]


def test_the_protocol_registers_the_confirmatory_parameters():
    assert G.registered_digest() == G.params_digest(G.CONFIRMATORY)
    assert set(G.PILOT["seeds"]).isdisjoint(G.CONFIRMATORY["seeds"])


def test_a_run_with_other_parameters_refuses(monkeypatch):
    monkeypatch.setattr(G, "CONFIRMATORY", {**G.CONFIRMATORY,
                                            "poison": 0.0})
    monkeypatch.setattr(G, "run", lambda *a, **k: pytest.fail(
        "a confirmatory run started under unregistered parameters"))
    assert G.main(["--confirmatory"]) == 2


def test_the_answerer_holds_no_reference_to_the_oracle():
    with tempfile.TemporaryDirectory() as d:
        w = G.World(Path(d), SEED, G.CONFIRMATORY)
        a = G.Answerer(w.prior, G.ErrorModel(), SEED)
        seen, todo = set(), [a]
        while todo:
            obj = todo.pop()
            if id(obj) in seen or isinstance(obj, type):
                continue
            seen.add(id(obj))
            assert obj is not w and obj is not w.oracle
            todo.extend(gc.get_referents(obj))


@pytest.fixture(scope="module")
def rows():
    return G.run_seed(SEED, G.CONFIRMATORY)


def _by(rows, cond):
    return {r["subject"]: r for r in rows if r["condition"] == cond}


def test_the_gates_check_d1_s_answer_and_never_replace_it(rows):
    d1, d4, d5 = (_by(rows, c) for c in (
        "D1_model_alone", "D4_evidence_gate", "D5_semantic_gate"))
    for sid in d1:
        assert d4[sid]["pre_gate_correct"] == d1[sid]["pre_gate_correct"]
        for g in (d4, d5):
            assert not g[sid]["asserted"] or d1[sid]["asserted"]
            assert not g[sid]["correct"] or d1[sid]["correct"]


def test_what_is_asserted_after_a_gate_is_supported(rows):
    for cond in ("D5_semantic_gate", "D6_state_answer", "D7_governed"):
        for r in _by(rows, cond).values():
            assert not r["unsupported"], (cond, r)
            assert not (r["asserted"] and r["extraction_failure"]), r


def test_the_state_answers_only_where_the_log_supports_one(rows):
    for r in _by(rows, "D6_state_answer").values():
        assert r["asserted"] == r["supported"], r
        assert r["tokens"] == 0 and r["confidence"] is None


def test_the_negative_control_defeats_the_state(rows):
    nc = [r for r in _by(rows, "D6_state_answer").values()
          if r["kind"] in G.NEGATIVE_CONTROLS]
    assert nc and all(r["false"] for r in nc)


def test_every_model_answer_went_through_the_gateway(rows):
    for cond in ("D1_model_alone", "D2_model_retrieval", "D3_model_memory",
                 "D7_governed"):
        assert all(r["tokens"] > 0 and r["cost_micro"] > 0
                   for r in _by(rows, cond).values()), cond


def test_the_committed_confirmatory_results_reproduce(rows):
    if not G.RESULTS.exists():
        pytest.skip("the confirmatory results are not committed yet")
    got = json.loads(G.RESULTS.read_text(encoding="utf-8"))
    assert got["params_digest"] == G.registered_digest()
    assert got["analysis"]["per_seed"][str(SEED)] == G.per_seed(rows, SEED)
