"""Fuzzing, run as a bounded campaign in CI and as a corpus replay always.

TWO DIFFERENT JOBS IN ONE FILE

The corpus replay is the regression test: every input that ever produced a
finding is committed, and it must still be refused correctly. That part is
deterministic and cheap and runs on every commit.

The campaign is the search. It is bounded so CI stays predictable, seeded so a
finding is reproducible, and deliberately modest -- a few hundred cases is not
a fuzzing programme, and this file does not pretend otherwise. What it does
give is a floor: a change that makes a parser accept malformed input, crash
outside its declared refusals, or hang has to get past it.

WHAT IS NOT CLAIMED

Exhaustiveness. Nothing here says the parsers are correct; it says the
campaign that ran found nothing, and names the seed so the same campaign can
be run again.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CORPUS = ROOT / "tests" / "fuzz_corpus"
HARNESS = ROOT / "tools" / "fuzz_substrate.py"


def _fuzz():
    spec = importlib.util.spec_from_file_location("fuzz_substrate", HARNESS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fuzz():
    return _fuzz()


# ---- the regression corpus ----------------------------------------------
def test_every_committed_corpus_case_is_still_refused_correctly(fuzz):
    """The regression half. A fixed defect must stay fixed.

    Fixing the code and discarding the input that found it means the next
    person to touch that parser gets to rediscover the same thing.
    """
    findings = fuzz.replay_corpus(CORPUS)
    assert findings == [], findings


def test_the_corpus_directory_exists_and_is_committed():
    assert CORPUS.is_dir()
    readme = CORPUS / "README.md"
    assert readme.exists(), (
        "the corpus needs a README saying what a file in it means, or it "
        "reads as a directory of noise")


@pytest.mark.parametrize("path", sorted(CORPUS.glob("*.json")))
def test_each_corpus_case_is_well_formed(path, fuzz):
    case = json.loads(path.read_text(encoding="utf-8"))
    assert case["target"] in fuzz._targets(), (
        f"{path.name} names a target that no longer exists; a corpus entry "
        "nothing runs is a regression test nothing runs")
    assert case["kind"] in (fuzz.ACCEPTED, fuzz.CRASHED, fuzz.HUNG)
    base64.b64decode(case["input_b64"])
    assert case.get("detail"), "a corpus case must record what went wrong"


# ---- the campaign --------------------------------------------------------
#: Fixed so this test is deterministic. A longer, randomly-seeded campaign is
#: run by hand and by the CI step; this is the floor, not the programme.
CI_SEED = 20260905
CI_CASES = 360


def test_a_bounded_campaign_finds_nothing(fuzz):
    findings, run, _stats = fuzz.campaign(cases=CI_CASES, seed=CI_SEED)
    assert run == CI_CASES
    assert findings == [], (
        "reproduce with: python3 tools/fuzz_substrate.py "
        f"--cases {CI_CASES} --seed {CI_SEED}\n" + json.dumps(findings[:3],
                                                              indent=2))


def test_every_trust_boundary_has_a_target(fuzz):
    """A parser with no fuzz target is one nothing has tried to break."""
    targets = set(fuzz._targets())
    required = {
        "events", "log_head", "checkpoint", "evidence_name", "capability",
        "policy", "job", "memory", "identity", "message", "escalation",
        "egress_grant", "context_manifest", "url", "canonical", "rag_index",
        "scheduler_sequence",
    }
    missing = required - targets
    assert not missing, f"trust boundaries with no fuzz target: {missing}"


# ---- the harness's own guarantees ---------------------------------------
def test_a_refusal_is_not_a_finding(fuzz):
    """The distinction the whole harness rests on."""
    class Declared(Exception):
        pass

    def refuses(data):
        raise Declared("this is the correct behaviour")

    assert fuzz.run_case("t", refuses, (Declared,), b"x") is None


def test_an_undeclared_exception_is_a_finding(fuzz):
    def crashes(data):
        raise ZeroDivisionError("nobody chose this")

    found = fuzz.run_case("t", crashes, (ValueError,), b"x")
    assert found is not None
    assert found["kind"] == fuzz.CRASHED
    assert "ZeroDivisionError" in found["detail"]


def test_a_hang_is_classified_separately_and_never_as_a_pass(fuzz):
    """A hang is not a test result -- the same rule as the mutation harness.

    Reported as HUNG rather than folded into CRASHED, because the operational
    response differs: a crash names a line, and a hang names nothing until
    somebody goes looking.
    """
    def hangs(data):
        import time
        time.sleep(fuzz.CASE_TIMEOUT_S + 5)

    found = fuzz.run_case("t", hangs, (ValueError,), b"x")
    assert found is not None and found["kind"] == fuzz.HUNG


def test_the_deadline_is_removed_after_each_case(fuzz):
    """A leaked alarm would fire during an unrelated later test."""
    import signal

    def hangs(data):
        import time
        time.sleep(fuzz.CASE_TIMEOUT_S + 5)

    fuzz.run_case("t", hangs, (ValueError,), b"x")
    assert signal.alarm(0) == 0, "an alarm was left armed"
    assert signal.getsignal(signal.SIGALRM) in (
        signal.SIG_DFL, signal.SIG_IGN) or callable(
            signal.getsignal(signal.SIGALRM))


def test_a_finding_carries_enough_to_reproduce_it(fuzz):
    def crashes(data):
        raise ZeroDivisionError("boom")

    found = fuzz.run_case("t", crashes, (ValueError,), b"\x00\xffpayload")
    assert base64.b64decode(found["input_b64"]) == b"\x00\xffpayload"


def test_mutations_stay_within_the_size_bound(fuzz):
    import random

    rng = random.Random(99)
    for _ in range(500):
        out = fuzz._mutate(rng, b'{"a": 1}')
        assert len(out) <= fuzz.MAX_INPUT_BYTES
        assert isinstance(out, bytes)


def test_the_campaign_is_reproducible_from_its_seed(fuzz):
    import random

    def sample(seed):
        rng = random.Random(seed)
        return [fuzz._mutate(rng, b'{"a": 1}') for _ in range(20)]

    assert sample(4242) == sample(4242)
    assert sample(4242) != sample(4243)


# ---- coverage, feedback, and the floor ----------------------------------
#
# R50 said the campaign was "random mutation only: no coverage feedback, so
# deep parser states are reached by luck", that nothing fuzzed the state
# machines as SEQUENCES, and that the harness itself had no mutation matrix.
# These are the first two. The third is tools/mutations/fuzz_harness.json.

def test_a_campaign_reports_what_it_actually_reached(fuzz):
    """"No findings" is the same sentence over a full run and an empty one.

    A target that raises at import, a mutation operator that started
    returning b"", a refusal tuple widened until everything is "correctly
    refused" -- each turns this harness into a green tick over nothing. What
    it reached is the number that tells those apart.
    """
    _, run, stats = fuzz.campaign(cases=200, seed=99)
    assert run == 200
    assert stats["lines_reached"] > 200, stats
    assert stats["targets"] >= 20, stats


def test_the_floor_is_enforced_by_the_tool_and_not_only_reported():
    """A number printed and not checked is a number nobody reads.

    Run as a real process, because the thing being tested is the EXIT CODE:
    a version that prints the shortfall and returns zero passes every
    inspection of its output and fails nothing.
    """
    import subprocess

    low = subprocess.run(
        [sys.executable, str(HARNESS), "--cases", "60", "--seed", "1",
         "--target", "canonical", "--min-lines", "999999"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    assert low.returncode == 1, (
        f"a campaign below its floor exited {low.returncode}\n{low.stdout}")
    assert "FLOOR" in low.stdout

    ok = subprocess.run(
        [sys.executable, str(HARNESS), "--cases", "60", "--seed", "1",
         "--target", "canonical", "--min-lines", "1"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    assert ok.returncode == 0, (
        f"a campaign above its floor exited {ok.returncode}\n{ok.stdout}")


def test_coverage_guidance_is_measured_rather_than_assumed(fuzz):
    """WHAT THE MEASUREMENT ACTUALLY SAID, which is not what was expected.

    R50 asked for coverage feedback because "deep parser states are reached
    by luck". It is implemented -- line coverage from sys.monitoring, plus
    the (state, operation, outcome) triples the sequence target declares,
    with inputs that reach something new kept and mutated from.

    Then it was measured, over eight seeds at 300 and 600 cases:

        300 cases   guided mean 84.8   unguided mean 84.6   3 wins, 5 losses
        600 cases   guided mean 89.1   unguided mean 88.5   3 wins, 3 losses

    A wash. The reachable set saturates from the declared seeds inside a few
    hundred cases, so there is nothing for feedback to steer towards, and an
    earlier three-seed sample that appeared to show a win was noise. The
    first version of the feedback was worse than nothing -- it chose parents
    uniformly, diluted the seeds, and LOWERED coverage; SEED_SHARE fixed
    that much.

    So this test asserts what is true: the mechanism works, it measures both
    signals, and it does not COST reach. It does not assert a win, because
    there is not one to assert.
    """
    _, _, guided = fuzz.campaign(cases=300, seed=5,
                                 only="scheduler_sequence")
    _, _, blind = fuzz.campaign(cases=300, seed=5,
                                only="scheduler_sequence", guided=False)
    assert guided["inputs_kept"] > 0, (
        "the feedback kept nothing, so it is not running at all")
    assert blind["inputs_kept"] == 0
    assert blind["features_reached"] > 20, (
        "the unguided arm has to do real work too, or this compares a "
        "campaign against nothing")
    assert guided["features_reached"] >= blind["features_reached"] * 0.9, (
        f"guided reached {guided['features_reached']} state transition(s) "
        f"against {blind['features_reached']} unguided -- feedback is "
        "allowed to be a wash and is not allowed to be a cost")
    assert guided["lines_reached"] > 0 and blind["lines_reached"] > 0


def test_the_sequence_target_checks_the_replay_outside_its_refusals(fuzz):
    """The invariant must not be swallowed by the refusal handler.

    A fuzzed sequence is mostly illegal moves, so the operations run inside
    a tolerant loop -- and if the replay check ran inside it too, a log
    nobody could rebuild would be reported as a correctly refused case. That
    is the vacuous-verifier defect, inside the fuzzer.
    """
    src = HARNESS.read_text(encoding="utf-8")
    body = src[src.index("def _scheduler_sequence"):
               src.index("def _record_target")]
    assert "except refusals as exc:" in body
    assert "except Exception" not in body, (
        "a bare except in the operation loop would swallow the "
        "AssertionError the invariant raises")
    after = body[body.index("# OUTSIDE the tolerant loop"):]
    assert "replay_invariants(path)" in after
    assert "try:" not in after, (
        "the replay check must not be wrapped in anything that could "
        "tolerate its failure")


def test_the_sequence_target_actually_drives_the_machines(fuzz):
    """Anti-vacuity for the target itself: a sequence that refused every
    operation and built nothing would pass every assertion in it."""
    fn, declared, seeds = fuzz._targets()["scheduler_sequence"]
    fuzz._take_features()
    built = fn(bytes([0, 1, 2, 3, 7, 8]))
    marks = fuzz._take_features()
    assert built and built > 0, "no job survived a sequence that enqueues"
    assert len(marks) >= 4, marks
    assert any(m[3] == "ok" for m in marks), (
        f"every operation was refused, so nothing was exercised: {marks}")


def test_a_sequence_that_leaves_an_unreplayable_log_is_a_finding(fuzz):
    """The positive control. A target whose invariant cannot fire is not an
    invariant, and this is the only way to know which of the two it is."""
    def broken(data):
        raise AssertionError("primary and independent reconstruction "
                             "disagree after a fuzzed sequence")

    found = fuzz.run_case("scheduler_sequence", broken,
                          (ValueError, TypeError), b"\x00")
    assert found is not None and found["kind"] == fuzz.CRASHED
    assert "disagree" in found["detail"]


def test_the_replay_invariant_fires_on_a_log_that_cannot_be_verified(fuzz,
                                                                     tmp_path):
    """The positive control for the sequence target's invariant.

    An invariant inlined in a fuzz target is only ever exercised by inputs
    the fuzzer happens to generate, so nothing establishes it can fire at
    all -- and one that cannot fire is indistinguishable from one that
    holds. It is a function for exactly this reason.
    """
    from qta_agent.canonical import digest
    from qta_agent.events import EventLog
    from qta_agent.policy import PolicyStore
    from qta_agent.scheduler import Scheduler, default_policy

    path = tmp_path / "log.jsonl"
    log = EventLog(path)
    pol = PolicyStore(log).load()
    pol.publish(default_policy(), actor="owner")
    sched = Scheduler(log, policy=pol, policy_id="scheduler.default",
                      capacity={"slots": 4}).load()
    sched.enqueue(job_id="j0", work_digest=digest({"j": 0}), submitter="p1")
    assert fuzz.replay_invariants(path) == 1

    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[-1])
    rec["wall_time"] = rec["wall_time"] + 1e-4
    lines[-1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="unverifiable log"):
        fuzz.replay_invariants(path)


def test_the_replay_invariant_consults_the_second_reader(fuzz, tmp_path,
                                                         monkeypatch):
    """Two readers agreeing is what makes a rebuilt projection evidence.

    Forced rather than waited for: a divergence between the primary and the
    independent reconstruction is exactly the thing no test can produce on
    purpose, so the comparison is made to report one and the invariant has
    to act on it.
    """
    import qta_agent.reconstruct as R

    from qta_agent.canonical import digest
    from qta_agent.events import EventLog
    from qta_agent.policy import PolicyStore
    from qta_agent.scheduler import Scheduler, default_policy

    path = tmp_path / "log.jsonl"
    log = EventLog(path)
    pol = PolicyStore(log).load()
    pol.publish(default_policy(), actor="owner")
    Scheduler(log, policy=pol, policy_id="scheduler.default",
              capacity={"slots": 4}).load().enqueue(
        job_id="j0", work_digest=digest({"j": 0}), submitter="p1")

    monkeypatch.setattr(R, "compare",
                        lambda a, b: ["record r1: state PROMOTED != VERIFIED"])
    with pytest.raises(AssertionError, match="disagree"):
        fuzz.replay_invariants(path)


def test_the_campaign_and_not_only_the_mutator_is_reproducible(fuzz):
    """A finding nobody can re-run is a rumour.

    The mutator's determinism was already tested. The CAMPAIGN's was not,
    and it is the campaign a person re-runs from a printed seed.
    """
    def measure(seed):
        # The sequence target, because a campaign over a tiny parser can
        # reach the same handful of lines from any seed -- a comparison
        # that coincides is not a comparison.
        _, _, stats = fuzz.campaign(cases=60, seed=seed,
                                    only="scheduler_sequence")
        return (stats["lines_reached"], stats["features_reached"],
                stats["inputs_kept"])

    assert measure(31337) == measure(31337)
    assert measure(31337) != measure(31338), (
        "two different seeds produced identical campaigns, so the seed is "
        "not what decides what runs")


def test_a_mutation_of_an_oversized_input_is_still_bounded(fuzz):
    """The bound has to hold where it MATTERS -- on an input already at it.

    The existing bound test mutates small seeds, which stay small whether
    the truncation is there or not.
    """
    import random

    rng = random.Random(9)
    big = b"x" * (fuzz.MAX_INPUT_BYTES + 512)
    for _ in range(40):
        out = fuzz._mutate(rng, big)
        assert len(out) <= fuzz.MAX_INPUT_BYTES, (
            f"a mutation of an oversized input returned {len(out)} bytes, "
            f"above the {fuzz.MAX_INPUT_BYTES} bound; case cost becomes "
            "unbounded and a slow run is indistinguishable from a hang")
