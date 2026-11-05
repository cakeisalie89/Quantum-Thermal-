"""Semantic claim verification (section 6.3): three questions kept apart,
a deterministic route that says no more than the research ledger would, and
an assessed route that is reported and never established.

The world is :class:`tools.claim_bench.World`: real authority records,
verified by a second actor, read into the research ledger through its own
gates. The benchmark's scores are in docs/research/claim_bench_results.json
and are reproduced here; what they mean is in docs/research/CLAIM_CHECK.md.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import claim_check as C  # noqa: E402
from agent_substrate import research as R  # noqa: E402
from agent_substrate.evidence_terms import Property  # noqa: E402
from agent_substrate.research import evaluate  # noqa: E402
from agent_substrate.trust_labels import TrustError, establish  # noqa: E402
from tools import claim_bench as B  # noqa: E402

V = C.Verdict


@pytest.fixture
def w(tmp_path):
    return B.World(tmp_path)


# ------------------------------------------------------------- the grammar

#: What each phrase means, written here rather than read from the module:
#: a test that took its expectation from the table it checks would pass
#: whatever the table said.
MEANS = {"at most": "<=", "no more than": "<=", "at least": ">=",
         "no less than": ">=", "less than": "<", "below": "<", "under": "<",
         "greater than": ">", "more than": ">", "above": ">", "over": ">",
         "equal to": "=", "<=": "<=", ">=": ">=", "<": "<", ">": ">",
         "=": "="}
IDS = st.from_regex(r"[A-Za-z][A-Za-z0-9_]{0,8}", fullmatch=True)
PHRASES = st.sampled_from(sorted(MEANS))
UNITS = st.sampled_from(["K", "W/m^2", "Pa", "s"])
NUMBERS = st.floats(-1e6, 1e6, allow_nan=False).map(lambda x: round(x, 3))


@settings(max_examples=300, deadline=None, derandomize=True, database=None)
@given(IDS, IDS, PHRASES, NUMBERS, UNITS)
def test_the_grammar_reads_what_it_states_and_nothing_added(q, m, phrase,
                                                            value, unit):
    text = f"{q} of {m} is {phrase} {value!r} {unit}"
    got = C.read_claim(text)
    assert got == C.Structured(q, m, MEANS[phrase], value, unit)
    for rider in (", so it is safe", " because of radiation", " (reviewed)",
                  " under every load", "; confirmed twice"):
        assert C.read_claim(text + rider) is None, rider


def test_the_grammar_knows_exactly_the_phrases_written_here():
    assert {p for p, _ in C._PHRASES} == set(MEANS)


def test_a_bare_value_is_a_claim_of_equality_within_resolution(w):
    assert C.read_claim("T_at_half of slab-1 is 312.4 K").op == "="
    assert w.check("T_at_half of slab-1 is 312.5 K").verdict is V.SUPPORTED
    assert w.check("T_at_half of slab-1 is 312.7 K").verdict is \
        V.CONTRADICTED


# ------------------------------- the deterministic route says no more than
# ------------------------------- the research ledger's own evaluation

OBS = st.tuples(st.floats(200, 400), st.floats(0, 2))


@settings(max_examples=400, deadline=None, derandomize=True, database=None)
@given(st.lists(OBS, min_size=1, max_size=3), st.sampled_from(["<", "<=",
                                                               ">", ">="]),
       st.floats(195, 405))
def test_the_verdict_agrees_with_research_evaluate(obs, op, t):
    observations = [{"observation_id": f"o{i}", "model_id": "m",
                     "values": {"q": {"value": v, "unit": "K",
                                      "resolution": r}}}
                    for i, (v, r) in enumerate(obs)]
    claim = C.Structured("q", "m", op, t, "K")
    got, _, used = C.judge(claim, observations)
    ledger, _ = evaluate({"model_id": "m", "quantity": "q", "op": op,
                          "value": t, "unit": "K"}, observations)
    assert (got is V.SUPPORTED) == (ledger == "SUPPORTED")
    assert (got is V.CONTRADICTED) == (ledger == "REFUTED")
    singly = {evaluate({"model_id": "m", "quantity": "q", "op": op,
                        "value": t, "unit": "K"}, [o])[0]
              for o in observations}
    assert (got is V.CONFLICTING_EVIDENCE) == \
        ({"SUPPORTED", "REFUTED"} <= singly)
    assert used == tuple(o["observation_id"] for o in observations)


def test_each_verdict_for_the_reason_it_states(w):
    assert w.check("T_at_half of slab-1 is below 313 K").verdict is \
        V.SUPPORTED
    assert w.check("T_at_half of slab-1 is above 313 K").verdict is \
        V.CONTRADICTED
    assert w.check("T_at_half of slab-1 is below 312.5 K").verdict is \
        V.INSUFFICIENT_EVIDENCE                  # within the resolution
    assert w.check("T_edge of slab-1 is below 400 K").verdict is \
        V.INSUFFICIENT_EVIDENCE                  # nothing reports it
    assert w.check("T_at_half of slab-1 is below 313 degC").verdict is \
        V.UNVERIFIABLE                           # nothing is converted
    assert w.check("T_at_half of slab-2 is below 307 K").verdict is \
        V.CONFLICTING_EVIDENCE                   # 305 K and 309 K
    assert w.check("slab-1 is cool enough").verdict is V.UNVERIFIABLE


# ------------------------------------------- units (D-2026-177)

def test_a_claim_in_another_unit_of_the_quantity_is_rescaled_exactly(w):
    text = "q_in of slab-1 is below 1.215 kW/m^2"        # 1215 W/m^2
    plain = w.check(text, cites=["o-slab-1-a"])
    assert plain.verdict is V.UNVERIFIABLE                # nothing converted
    assert "nothing is converted" in plain.why
    scaled = w.check(text, cites=["o-slab-1-a"], convert=B.units)
    assert (scaled.verdict, scaled.established) == (V.SUPPORTED, True)
    over = w.check("q_in of slab-1 is above 1.215 kW/m^2",
                   cites=["o-slab-1-a"], convert=B.units)
    assert over.verdict is V.CONTRADICTED


@pytest.mark.parametrize("text, why", [
    ("T_at_half of slab-1 is below 40 degC", "temperature difference"),
    ("q_in of slab-1 is below 2 kg", "differ in dimension"),
    ("q_in of slab-1 is below 2 furlongs", "no unit 'furlongs'"),
])
def test_a_conversion_that_cannot_be_made_soundly_decides_nothing(w, text,
                                                                  why):
    r = w.check(text, cites=["o-slab-1-a"], convert=B.units)
    assert r.verdict is V.UNVERIFIABLE and why in r.why, r.why
    assert not r.established


def test_the_substrate_holds_no_unit_semantics_of_its_own():
    """Units are the scientific layer's; the substrate reaches it only
    through the workspace guard, and is handed a converter."""
    src = (ROOT / "agent_substrate" / "claim_check.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    mods = {getattr(n, "module", "") or "" for n in ast.walk(tree)} | {
        a.name for n in ast.walk(tree) for a in getattr(n, "names", ())}
    assert not any(m.split(".")[0] == "scientific" for m in mods)


# ------------------------------------------- three questions, kept apart

def test_a_contradiction_cannot_be_avoided_by_leaving_it_out(w):
    r = w.check("T_at_half of slab-2 is below 307 K", cites=["o-slab-2-a"])
    assert r.citation == "INTACT"
    assert r.verdict is V.CONFLICTING_EVIDENCE
    assert r.uncited == ("o-slab-2-b",)
    assert not r.established


def test_a_withdrawn_source_is_cited_intact_and_weighs_nothing(w):
    r = w.check("T_probe of probe-1 is below 300 K", cites=["o-probe-1-a"])
    assert r.citation == "INTACT"                # the bytes are what they were
    assert r.admission["o-probe-1-a"].startswith("its standing is WITHDRAWN")
    assert r.verdict is V.INSUFFICIENT_EVIDENCE  # and are not evidence now
    assert not r.established


def test_a_broken_citation_is_reported_and_the_claim_is_not_established(w):
    text = "T_at_half of slab-1 is below 313 K"
    honest = w.check(text, cites=["o-slab-1-a"])
    assert honest.established and honest.citation == "INTACT"
    forged = w.check(text, cites=["o-slab-1-a", "o-slab-7-a"])
    assert forged.citation == "BROKEN"
    assert forged.citation_problems == ("o-slab-7-a: no observation of "
                                        "bench",)
    assert forged.verdict is V.SUPPORTED         # the semantics did not move
    assert not forged.established                # and nothing is granted
    uncited = w.check(text)
    assert uncited.citation == "NONE" and not uncited.established


def _bundle_of(*outputs) -> dict:
    return {"outputs": [{"name": n, "status": "OK", "quantity": {
        "value": v, "unit": u, "resolution": r}} for n, v, u, r in outputs]}


def test_the_bytes_say_an_observation_their_check_widened():
    """AUD-025: an observation of a verified result carries, for each output
    its check compared, the resolution that check supports -- never less
    than the producer's -- and no output the check did not compare. Its
    bundle's bytes still say it; another value, unit or a narrower interval
    is not what they say."""
    from agent_substrate.research import values_of
    bundle = _bundle_of(("T", 312.4, "K", 1e-3), ("q", 1215.0, "W m^-2",
                                                 1e-6))
    own = values_of(bundle)
    assert C.bytes_say(bundle, own)
    assert C.bytes_say(bundle, {"T": {**own["T"], "resolution": 2.5e-3}})
    for bad in ({"T": {**own["T"], "resolution": 5e-4}},
                {"T": {**own["T"], "value": 301.4}},
                {"T": {**own["T"], "unit": "degC"}},
                {"T_other": own["T"]},
                {"T": {**own["T"], "resolution": float("nan")}},
                {"T": {**own["T"], "resolution": True}},
                {"T": {**own["T"], "resolution": 10 ** 400}},
                {"T": {**own["T"], "extra": 1}},
                {"T": None}, None, [own["T"]]):
        assert not C.bytes_say(bundle, bad), bad


def test_a_planted_observation_is_not_what_its_bytes_say(w):
    """A ledger loaded without evidence cannot hold an observation to its
    bundle; the citation check can. An observation planted beside slab-1's
    with half the resolution its bytes state -- the precision AUD-025 says
    nothing verified -- is cited BROKEN, and the genuine one, read the same
    way, is cited INTACT."""
    _, events = w.log.read_verified()
    genuine = [e for e in events if e.action == R.ACT_OBSERVATION
               and e.target == "o-slab-1-a"][-1]
    obs = genuine.payload["observation"]
    assert any(v["resolution"] > 0 for v in obs["values"].values())
    planted = {**obs, "observation_id": "o-slab-1-planted", "values": {
        n: {**v, "resolution": v["resolution"] / 2}
        for n, v in obs["values"].items()}}
    w.log.append(actor=genuine.actor, action=R.ACT_OBSERVATION,
                 target=planted["observation_id"],
                 payload={**genuine.payload, "observation": planted})
    blind = R.ResearchLedger(w.log).load()      # holds no values to bytes
    text = "T_at_half of slab-1 is below 313 K"
    for oid, citation in (("o-slab-1-planted", "BROKEN"),
                          ("o-slab-1-a", "INTACT")):
        r = C.check(text, cites=[oid], objective_id=B.OBJECTIVE,
                    ledger=blind, store=w.store, evidence=w.evidence)
        assert r.citation == citation, (oid, r.citation_problems)
    assert "say something other than the observation recorded" in \
        C.check(text, cites=["o-slab-1-planted"], objective_id=B.OBJECTIVE,
                ledger=blind, store=w.store,
                evidence=w.evidence).citation_problems[0]


def test_bytes_altered_after_observation_break_the_citation(w):
    dg = w.ledger.get("o-slab-1-a").body["result_bundle"]
    path = w.evidence.root / dg[:2] / dg[2:]
    path.chmod(0o644)
    path.write_bytes(path.read_bytes().replace(b"312.4", b"301.4"))
    r = w.check("T_at_half of slab-1 is below 313 K", cites=["o-slab-1-a"])
    assert r.citation == "BROKEN"
    assert "missing or no longer re-hash" in r.citation_problems[0]
    assert not r.established


def test_a_result_whose_admission_no_longer_holds_weighs_nothing(w):
    """The ledger reads a record's state; only the authority store
    re-derives whether a scientific result's admission holds here. A store
    that finds slab-1's one run UNVERIFIABLE (its verification report lost,
    D-2026-150) takes that run out of the evidence, whatever its standing
    in the ledger."""
    real = w.store

    class LostReport:
        def get(self, rid):
            rec = real.get(rid)
            return dataclasses.replace(rec, admission="UNVERIFIABLE") \
                if rid == "r-slab-1-a" else rec

    text = "T_at_half of slab-1 is below 313 K"
    assert w.check(text, cites=["o-slab-1-a"]).established      # control
    r = C.check(text, cites=["o-slab-1-a"], objective_id=B.OBJECTIVE,
                ledger=w.ledger, store=LostReport(), evidence=w.evidence)
    assert r.admission["o-slab-1-a"] == ("its record r-slab-1-a is not "
                                         "admitted here (UNVERIFIABLE)")
    assert r.citation == "INTACT"
    assert r.verdict is V.INSUFFICIENT_EVIDENCE and not r.established


# --------------------------------------- an opinion is reported, not granted

def _always(verdict):
    def checker(text, observations):
        return verdict, "it reads well"
    return checker


def test_an_assessed_answer_is_reported_with_its_basis_and_grants_nothing(w):
    r = w.check("slab-1 is cool enough, so the panel is safe.",
                cites=["o-slab-1-a"], checker=_always("SUPPORTED"))
    assert r.verdict is V.SUPPORTED and r.basis is C.Basis.ASSESSED
    assert r.citation == "INTACT"
    assert not r.established
    with pytest.raises(TrustError, match="ASSESSED"):
        establish(r.label, Property.SEMANTICALLY_SUPPORTED, check=C.ASSESS,
                  ref="o-slab-1-a")


def test_the_grammar_decides_before_any_checker_is_asked(w):
    asked = []

    def checker(text, observations):
        asked.append(text)
        return "CONTRADICTED", "contrary"
    r = w.check("T_at_half of slab-1 is below 313 K", cites=["o-slab-1-a"],
                checker=checker)
    assert (r.verdict, r.basis, asked) == (V.SUPPORTED,
                                           C.Basis.DETERMINISTIC, [])


@pytest.mark.parametrize("bad", [
    lambda t, o: ("TRUE", "yes"), lambda t, o: None,
    lambda t, o: (_ for _ in ()).throw(ValueError("no")),
])
def test_a_checker_that_gives_no_usable_answer_gives_no_verdict(w, bad):
    r = w.check("slab-1 is cool enough", checker=bad)
    assert (r.verdict, r.basis) == (V.UNVERIFIABLE, C.Basis.ASSESSED)


# ------------------------------------------------ it is not an authority

def test_checking_a_claim_writes_nothing(w):
    before = w.log.path.read_bytes()
    w.check("T_at_half of slab-1 is below 313 K", cites=["o-slab-1-a"],
            checker=_always("SUPPORTED"))
    assert w.log.path.read_bytes() == before


def test_nothing_that_decides_imports_it():
    users = []
    for path in sorted((ROOT / "agent_substrate").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in getattr(node, "names", ())] + \
                [getattr(node, "module", "") or ""]
            if any("claim_check" in n for n in names):
                users.append(path.name)
    assert users == []


# ------------------------------------------------------------ the benchmark

def test_the_committed_benchmark_results_reproduce():
    assert B.main(["--check"]) == 0


def test_the_benchmark_says_where_its_labels_came_from_and_what_did_not_run():
    got = json.loads(B.RESULTS.read_text(encoding="utf-8"))
    assert got["label_sources"] == {"structured": "CONSTRUCTION",
                                    "prose": "AUTHOR"}
    assert "NOT human labels" in got["note"]
    assert got["model"] == "NOT_RUN"
    det = got["checkers"]["deterministic"]
    assert det["structured"]["exact"] == det["structured"]["n"]
    assert det["structured"]["false_positive"] == 0
    assert det["prose"]["false_positive"] == 0
    for mode, scores in got["checkers"].items():
        for fam in ("structured", "prose"):
            assert scores[fam]["established_false_positive"] == 0, mode
    # the fallible checker is shown failing where it would: on claims that
    # read as supported and are not
    assert got["checkers"]["lexical"]["prose"]["false_positive"] > 0
