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
    findings, run = fuzz.campaign(cases=CI_CASES, seed=CI_SEED)
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
