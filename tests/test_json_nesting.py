"""Nesting: how deep a record may go, and what a boundary says of a document
that goes deeper (AUD-050).

The JSON parser recurses once for each level and raises ``RecursionError``
past the interpreter's limit: 9,997 levels on CPython 3.12, 9,998 on 3.13,
under a thousand on 3.11. That is a ``RuntimeError``, and a handler written
for "this is not JSON" catches ``ValueError``. Twenty kilobytes of brackets
went through every such handler. The defect ledger already held the rule for
the attestation reader and the two registries; the log's own line parser,
its head witness, the checkpoint store, the gateway's reading of a model's
answer and the result rules' reading of evidence did not hold it.

Three things are held here.

* A RECORD HAS A DEPTH OF ITS OWN. ``events.MAX_NESTING`` is the log's bound
  and not the interpreter's: the writer refuses a payload past it before
  anything is encoded, and every reader refuses a line past it, at the same
  level under every interpreter.
* A BOUNDARY THAT REFUSES ANSWERS IN ITS OWN WORDS. Each place that refuses
  bytes that are not JSON is handed a document nested past the parser, and
  makes the same refusal. Seven parses of evidence have no refusal to make
  for either, and are on the list below (AUD-047's remainder).
* THE SWEEP IS A TEST. The earlier repair was made where it was found, and
  the other boundaries were left. Every ``json.loads`` and ``json.load`` in
  the package is found here from its source, and each either has a handler
  for nesting around it or is in a function listed with the reason it has
  none.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import verify_assurance as V  # noqa: E402

from agent_substrate import checkpoint as C  # noqa: E402
from agent_substrate import events as E  # noqa: E402
from agent_substrate import inference as I  # noqa: E402, E741
from agent_substrate import proposals as P  # noqa: E402
from agent_substrate import rag_index  # noqa: E402
from agent_substrate import result_rules  # noqa: E402
from agent_substrate import separate_verify  # noqa: E402
from agent_substrate import witness as W  # noqa: E402
from agent_substrate.canonical import (  # noqa: E402
    CanonicalizationError, canonical_bytes, digest,
)
from agent_substrate.events import (  # noqa: E402
    Anchor, ChainBroken, EventLog, MalformedEvent,
)
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.research import ResearchLedger  # noqa: E402
from agent_substrate.store import AuthorityStore, StoreError  # noqa: E402

BOUND = E.MAX_NESTING
#: Past any interpreter's parser, and small: 200 KB and 600 KB.
LEVELS = 100_000
DEEP_ARRAY = b"[" * LEVELS + b"1" + b"]" * LEVELS
DEEP_OBJECT = b'{"a":' * LEVELS + b"1" + b"}" * LEVELS
DEEP = [DEEP_ARRAY, DEEP_OBJECT]
KINDS = ["object", "array", "mixed"]


def _nested(levels: int, kind: str = "object"):
    """A value that nests exactly ``levels`` lists and objects, itself
    counted: ``_nested(1)`` is an empty one."""
    assert levels >= 1
    make = {"object": lambda i: {}, "array": lambda i: [],
            "tuple": lambda i: [],
            "mixed": lambda i: {} if i % 2 else []}[kind]
    out = cur = make(0)
    for i in range(1, levels):
        nxt = make(i)
        if isinstance(cur, dict):
            cur["k"] = nxt
        else:
            cur.append(nxt)
        cur = nxt
    if kind == "tuple":
        out = _as_tuples(out)
    return out


def _as_tuples(chain: list):
    """The same chain of lists, as tuples, built from the inside out with a
    loop: a recursive builder would be the fault this file is about."""
    depth, cur = 0, chain
    while cur:
        depth, cur = depth + 1, cur[0]
    out: tuple = ()
    for _ in range(depth):
        out = (out,)
    return out


def _log(tmp_path: Path, n: int = 2) -> EventLog:
    log = EventLog(tmp_path / "log.jsonl")
    for i in range(n):
        log.append(actor="writer", action="note.fixture", target=f"t{i}",
                   payload={"i": i})
    return log


def _forge(path: Path, payload) -> None:
    """A correctly chained record written straight into the file, as a
    holder of it can write one: no writer's check runs."""
    recs = [json.loads(x) for x in path.read_bytes().splitlines()]
    prev = recs[-1]
    body = {"seq": prev["seq"] + 1, "event_id": f"forged-{len(recs)}",
            "wall_time": prev["wall_time"] + 1.0, "actor": "writer",
            "action": "note.fixture", "target": "forged",
            "payload": payload, "prev_hash": prev["hash"],
            "canonical_form_version": E.CANONICAL_FORM_VERSION}
    with path.open("ab") as fh:
        fh.write(canonical_bytes(dict(body, hash=digest(body))) + b"\n")


def _anchor_at(path: Path, seq: int) -> Anchor:
    lines = path.read_bytes().splitlines(keepends=True)
    start = sum(len(x) for x in lines[:seq])
    return Anchor(seq=seq, head_hash=json.loads(lines[seq])["hash"],
                  record_offset=start, next_offset=start + len(lines[seq]))


def _forget_the_head(log: EventLog) -> None:
    """Drop the head witness beside the log, so a forged line is judged as
    a line and not as a head the witness never recorded."""
    for f in log.path.parent.glob(log.path.name + ".*"):
        f.unlink()


# ---------------------------------------------------------------- the bound
def test_the_bound_is_the_logs_and_far_from_either_limit():
    assert BOUND == 64
    # above the deepest thing the package carries, by a wide margin
    assert P.MAX_DEPTH * 4 <= BOUND
    # and nowhere near where this interpreter's parser and encoder stop
    json.loads(canonical_bytes(_nested(BOUND * 4)))


@pytest.mark.parametrize("kind", KINDS + ["tuple"])
def test_a_record_at_the_bound_is_written_and_read_back(tmp_path, kind):
    log = _log(tmp_path)
    payload = {"deep": _nested(BOUND - 2, kind)}      # record, payload, ...
    ev = log.append(actor="writer", action="note.fixture", target="deep",
                    payload=payload)
    assert E._nests_deeper(ev.to_record(), BOUND - 1), "it is at the bound"
    report, events = EventLog(log.path).read_verified()
    assert report.ok and events[-1].hash == ev.hash
    assert events[-1].payload == json.loads(canonical_bytes(payload))


@pytest.mark.parametrize("kind", KINDS + ["tuple"])
@pytest.mark.parametrize("extra", [1, 2, 40, LEVELS])
def test_a_payload_past_the_bound_is_refused_before_anything_is_written(
        tmp_path, kind, extra):
    """One level past the bound, and a hundred thousand: the same refusal,
    the log's own, with nothing encoded and nothing written. The deepest
    one raised ``RecursionError`` from the encoder, and the others were
    written."""
    log = _log(tmp_path)
    before = log.path.read_bytes()
    # the writer's own words: the reader's rule would refuse the line too,
    # one step later and for a payload that can be encoded at all
    with pytest.raises(MalformedEvent,
                       match=f"event nests deeper than {BOUND} levels, the "
                             "bound every reader holds"):
        log.append(actor="writer", action="note.fixture", target="deep",
                   payload={"deep": _nested(BOUND - 2 + extra, kind)})
    assert log.path.read_bytes() == before
    # and the log is as it was: the next record is appended
    assert log.append(actor="writer", action="note.fixture", target="t2",
                      payload={}).seq == 2


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("followed", [False, True],
                         ids=["the last line", "a line with one after it"])
def test_a_line_past_the_bound_is_a_problem_on_every_read(
        tmp_path, kind, followed):
    """A record one level past the bound, hashed and chained as its forger
    would write it. Every reader refuses it, and none takes it for the torn
    final append that the next write would cut off."""
    log = _log(tmp_path)
    anchor = _anchor_at(log.path, 1)
    _forge(log.path, {"deep": _nested(BOUND - 1, kind)})
    if followed:
        _forge(log.path, {"i": 3})
    _forget_the_head(log)
    why = f"line 3: nests deeper than {BOUND} levels"

    report = EventLog(log.path).verify()
    assert not report.ok and report.problems == [why]
    assert not report.truncated_tail and not report.torn_tail_only()
    report, events = EventLog(log.path).read_verified()
    assert not report.ok and events == [] and report.problems == [why]
    with pytest.raises(MalformedEvent, match="nests deeper"):
        EventLog(log.path).read_verified_from(anchor)
    with pytest.raises(MalformedEvent, match="nests deeper"):
        EventLog(log.path).verify_from(anchor)

    before = log.path.read_bytes()
    with pytest.raises(ChainBroken, match="nests deeper"):
        EventLog(log.path).append(actor="writer", action="note.fixture",
                                  target="t9", payload={})
    assert log.path.read_bytes() == before, "the line is not cut off"


@pytest.mark.parametrize("line", DEEP, ids=["an array", "an object"])
@pytest.mark.parametrize("followed", [False, True],
                         ids=["the last line", "a line with one after it"])
def test_a_line_past_the_parsers_limit_is_a_problem_not_a_raise(
        tmp_path, line, followed):
    """AUD-050 as it was found: ``RecursionError`` out of every read and
    every append, from one line."""
    log = _log(tmp_path)
    anchor = _anchor_at(log.path, 1)
    good = log.path.read_bytes()
    log.path.write_bytes(good + line + b"\n" + (good.splitlines(True)[0]
                                                if followed else b""))
    _forget_the_head(log)
    why = f"line 3: nests deeper than {BOUND} levels"

    report = EventLog(log.path).verify()
    assert not report.ok and why in report.problems
    assert not report.truncated_tail
    report, events = EventLog(log.path).read_verified()
    assert not report.ok and events == [] and why in report.problems
    with pytest.raises(MalformedEvent, match="nests deeper"):
        EventLog(log.path).read_verified_from(anchor)
    before = log.path.read_bytes()
    with pytest.raises(ChainBroken, match="nests deeper"):
        EventLog(log.path).append(actor="writer", action="note.fixture",
                                  target="t9", payload={})
    assert log.path.read_bytes() == before


def test_wide_is_not_deep(tmp_path):
    """A record with many more brackets than the bound and three levels:
    the count of brackets only says which lines are worth walking."""
    log = _log(tmp_path)
    wide = {"rows": [{"cells": [i, [i]]} for i in range(4 * BOUND)]}
    ev = log.append(actor="writer", action="note.fixture", target="wide",
                    payload=wide)
    line = log.path.read_bytes().splitlines()[-1]
    assert line.count(b"{") + line.count(b"[") > BOUND
    report, events = EventLog(log.path).read_verified()
    assert report.ok and events[-1].hash == ev.hash


def test_brackets_inside_strings_are_not_levels(tmp_path):
    log = _log(tmp_path)
    ev = log.append(actor="writer", action="note.fixture", target="text",
                    payload={"text": "[" * (4 * BOUND) + "{" * (4 * BOUND)})
    report, events = EventLog(log.path).read_verified()
    assert report.ok and events[-1].hash == ev.hash


def test_depth_is_counted_without_recursion():
    """The walk has a stack of its own: the answer for a value a hundred
    thousand levels deep is an answer, from however deep a call."""
    deep = _nested(LEVELS)
    assert E._nests_deeper(deep, BOUND)
    assert not E._nests_deeper(_nested(BOUND), BOUND)
    assert E._nests_deeper(_nested(BOUND + 1), BOUND)
    assert not E._nests_deeper("a string", 0) and E._nests_deeper([], 0)

    def from_down(n):
        return E._nests_deeper(deep, BOUND) if n == 0 else from_down(n - 1)
    assert from_down(sys.getrecursionlimit() // 2)


# ------------------------------------------------------- the encoder's limit
def test_the_encoder_refuses_what_it_cannot_encode_as_it_refuses_the_rest():
    for deep in (_nested(LEVELS, "object"), _nested(LEVELS, "array")):
        with pytest.raises(CanonicalizationError, match="nests deeper"):
            canonical_bytes(deep)
        with pytest.raises(CanonicalizationError, match="nests deeper"):
            digest({"payload": deep})


# ------------------------------------------- the files that sit beside a log
@pytest.mark.parametrize("doc", DEEP, ids=["an array", "an object"])
def test_a_head_witness_nested_past_the_parser_is_unreadable_not_a_raise(
        tmp_path, doc):
    log = _log(tmp_path)
    assert log.head_path.exists()
    log.head_path.write_bytes(doc)
    with pytest.raises(MalformedEvent, match="head witness is unreadable"):
        EventLog(log.path).head()
    report = EventLog(log.path).verify()
    assert not report.ok
    assert report.problems == ["head witness is unreadable: RecursionError"]
    with pytest.raises(ChainBroken, match="head witness is unreadable"):
        EventLog(log.path).append(actor="writer", action="note.fixture",
                                  target="t9", payload={})


@pytest.mark.parametrize("doc", DEEP, ids=["an array", "an object"])
def test_a_checkpoint_nested_past_the_parser_is_corrupt_not_a_raise(
        tmp_path, doc):
    store = C.CheckpointStore(tmp_path / "cp")
    store.root.mkdir(parents=True)
    store._path(3).write_bytes(doc)
    with pytest.raises(C.CheckpointCorrupt, match="unparseable"):
        store.read(3)
    audit = store.audit()
    assert not audit.ok and audit.count == 0 and len(audit.problems) == 1
    # and the reader of a log's last position, which verifies nothing
    tail = tmp_path / "tail.jsonl"
    tail.write_bytes(b'{"seq": 4}\n' + doc + b"\n")
    assert C._last_seq(tail) is None


@pytest.mark.parametrize("doc", DEEP, ids=["an array", "an object"])
def test_a_snapshot_nested_past_the_parser_is_unparseable_not_a_raise(
        tmp_path, doc):
    class _Checkpoint:
        seq, state_digest = 1, "a" * 64

    class _Checkpoints:
        def latest_usable(self, log):
            return _Checkpoint()

    class _Blobs:
        def get(self, dg):
            return doc

    with pytest.raises(StoreError, match="unparseable: RecursionError"):
        AuthorityStore.load_from(_log(tmp_path), _Checkpoints(),
                                 blobs=_Blobs())


@pytest.mark.parametrize("doc", DEEP + [b"\xff not utf-8", b"{not json"],
                         ids=["an array", "an object", "not UTF-8",
                              "not JSON"])
def test_a_witness_whose_state_cannot_be_read_has_lost_it(tmp_path, doc):
    """The remainder of AUD-049: a state file that is not a document raised
    the parser's error. It is a witness that lost what it held, and it says
    so as it says everything else."""
    log = _log(tmp_path, 4)
    _, events = log.read_verified()
    wit = W.TreeWitness("w1", b"\x01" * 32, tmp_path / "state")
    head, tree = W.checkpoint_of("lab", events[:1]), W.tree_checkpoint_of(
        "lab", events[:1])
    wit.provision(head)
    wit.provision_tree(tree)
    wit._path(head.origin).write_bytes(doc)
    wit._tree_path(tree.origin).write_bytes(doc)
    for ask in (lambda: wit.latest(head.origin),
                lambda: wit.add_checkpoint(
                    W.checkpoint_of("lab", events),
                    W.proof_records(events, 0, 3)),
                lambda: wit.latest_tree(tree.origin),
                lambda: wit.add_tree_checkpoint(
                    W.tree_checkpoint_of("lab", events),
                    W.tree_proof(events, 1, 4))):
        with pytest.raises(W.WitnessError, match="cannot be read"):
            ask()
    assert wit._path(head.origin).read_bytes() == doc, "and it keeps it"


# ------------------------------------------------- the documents others write
@pytest.mark.parametrize("body", DEEP, ids=["an array", "an object"])
def test_a_models_answer_nested_past_the_parser_is_refused_not_a_raise(body):
    """The body is the model's. This raised out of the gateway after the
    call was dispatched and before it was settled."""
    with pytest.raises(P.ProposalRefused,
                       match=f"nested deeper than {P.MAX_DEPTH}"):
        I.parse_response(body)
    assert any("not JSON" in p for p in I.prompt_problems(body, None))


@pytest.mark.parametrize("doc", DEEP, ids=["an array", "an object"])
def test_evidence_nested_past_the_parser_is_a_problem_not_a_raise(
        tmp_path, doc):
    held = {"b" * 64: doc, "c" * 64: b"{}"}
    cited = {"result_bundle": "b" * 64, "verification_report": "c" * 64}
    assert result_rules.record_problems(cited, held.__getitem__) == [
        f"result_bundle {'b' * 12} cannot be read as JSON (RecursionError)"]
    cited = {"result_bundle": "c" * 64, "verification_report": "b" * 64}
    assert result_rules.record_problems(cited, held.__getitem__) == [
        f"verification_report {'b' * 12} cannot be read as JSON "
        "(RecursionError)"]

    class _Evidence:
        def get(self, sha):
            return held[sha]

    ledger = ResearchLedger(EventLog(tmp_path / "r.jsonl"),
                            evidence=_Evidence())
    assert ledger._report_of(
        {"evidence": {"verification_report": "b" * 64}}) is None
    assert ledger._report_of(
        {"evidence": {"verification_report": "c" * 64}}) == {}


@pytest.mark.parametrize("doc", DEEP, ids=["an array", "an object"])
def test_the_corpus_files_nested_past_the_parser_are_refused(tmp_path, doc):
    target = tmp_path / rag_index.CORPUS_ALLOWLIST
    target.parent.mkdir(parents=True)
    target.write_bytes(doc)
    with pytest.raises(rag_index.CorpusMembershipError, match="unparseable"):
        rag_index.load_allowlist(tmp_path)
    index = tmp_path / "index.json"
    index.write_bytes(doc)
    with pytest.raises(ValueError, match="index document is unparseable"):
        rag_index.load_index(index, ROOT)


@pytest.mark.parametrize("doc", DEEP, ids=["an array", "an object"])
def test_an_evidence_sidecar_nested_past_the_parser_says_nothing(
        tmp_path, doc):
    store = EvidenceStore(tmp_path / "evidence")
    dg = store.put(b"content", media_type="text/plain")
    assert store.info(dg).media_type == "text/plain"
    store._meta_path(dg).write_bytes(doc)
    info = store.info(dg)
    assert info.media_type == "application/octet-stream" and info.size == 7


@pytest.mark.parametrize("doc", DEEP, ids=["an array", "an object"])
def test_a_recorded_response_nested_past_the_parser_is_refused(doc):
    adapter = P.RecordedFixtureAdapter(doc.decode() + "\n", source="fx")
    with pytest.raises(P.ProposalRefused, match="fx:1: not JSON"):
        list(adapter.responses({}))


@pytest.mark.parametrize("opens", ["[", '{"a":'], ids=["an array",
                                                          "an object"])
def test_a_verdict_nested_past_the_parser_is_not_a_verdict(tmp_path, opens):
    """The separate verifier's answer is read from its standard output. One
    that cannot be parsed is not a verdict, however it fails to parse."""
    script = (f"import sys; sys.stdout.write({opens!r} * {LEVELS} + '1' + "
              f"{']' if opens == '[' else '}'!r} * {LEVELS})")
    out = separate_verify._run([sys.executable, "-c", script],
                               root=tmp_path, timeout_s=60)
    assert not out.ok and "no readable answer" in out.reason


# -------------------------------------------------- the standalone verifier
def test_the_standalone_verifier_holds_a_log_line_to_the_same_bound(
        tmp_path):
    """It restates the log's rules and imports none of them. A history the
    log refuses is one it refuses, at the same level."""
    assert V.MAX_NESTING == BOUND
    log = _log(tmp_path)
    at_the_bound = {"deep": _nested(BOUND - 2)}
    log.append(actor="writer", action="note.fixture", target="deep",
               payload=at_the_bound)
    assert len(V._parse_log(log.path.read_bytes())) == 3
    _forge(log.path, {"deep": _nested(BOUND - 1)})
    with pytest.raises(V.Refused) as refused:
        V._parse_log(log.path.read_bytes())
    assert "nests within the bound" in str(refused.value)
    assert "log line 4" in str(refused.value)


# ----------------------------------------------------------------- the fuzzer
def test_the_fuzzer_is_seeded_with_nesting_at_each_file_beside_a_log():
    """The fuzzer's nesting mutator stops at two hundred levels, so a parser
    that recurses is shown one deep enough only by a seed. The attestation
    reader and the key registry were given one when their fault was found
    by review; the log's line, its head witness and its checkpoints were
    not, and had the same fault."""
    import fuzz_substrate as F
    targets = F._targets()
    for name in ("attestations", "key_registry", "events", "log_head",
                 "checkpoint"):
        fn, declared, seeds = targets[name]
        deep = [s for s in seeds if s.startswith(b"[" * 20_000)]
        assert deep, f"{name} has no seed nested past the parser"
        for seed in deep:
            assert F.run_case(name, fn, declared, seed) is None, name


# ------------------------------------------------------- the sweep, as a test
#: Every ``json.loads`` in the package whose failure on nesting no handler
#: around it catches, and why that is so. Anything else found unguarded is
#: a failure of the test below, and so is an entry here nothing matches.
UNGUARDED = {
    ("events.py", "_write_event"):
        "parses the bytes it has just encoded, of a payload already held "
        "to the bound",
    ("proposals.py", "parse"):
        "parses the bytes it has just encoded",
    ("inference.py", "call"):
        "inside a provider's call, whose every exception is a reply "
        "(InferenceGateway._call)",
    ("reconstruct.py", "_sci_fetch"):
        "inside the second reader's fold, where a raise is a note against "
        "the record (AUD-047)",
    ("research.py", "_apply_observation"):
        "the bundle a VERIFIED record cites. A forged history reaches it "
        "with any bytes, and bytes that are not JSON raise here too: what "
        "a primary answers to such a record is AUD-047's remainder",
    ("research.py", "scientific_context"):
        "the evidence of an observation the ledger holds; no handler for "
        "bytes that are not JSON either",
    ("research.py", "observe"):
        "the caller's bytes, parsed before anything is recorded; no "
        "handler for bytes that are not JSON either",
    ("learned_lifecycle.py", "document"):
        "a document this module registered; no handler for bytes that are "
        "not JSON either",
    ("governed_model.py", "_record_of"):
        "a record the governed tools wrote, by the digest the capture "
        "recorded; no handler for bytes that are not JSON either",
    ("claim_check.py", "check"):
        "evidence whose digest the store has just re-hashed; no handler "
        "for bytes that are not JSON either",
    ("research_loop.py", "script"):
        "a scripted stand-in for a model, reading the gateway's own prompt",
    ("_ns_init.py", "main"):
        "the confinement policy its own parent passed it",
    ("_index_tool.py", "main"):
        "its own caller's argument",
    ("_artifact_tool.py", "main"):
        "its own caller's argument",
    ("_inference_worker.py", "main"):
        "the gateway's own message, and a fixture",
}
_CATCHES_IT = {"RecursionError", "RuntimeError", "Exception",
               "BaseException"}


def _handler_names(handler: ast.ExceptHandler) -> set:
    if handler.type is None:
        return {"BaseException"}
    nodes = (handler.type.elts if isinstance(handler.type, ast.Tuple)
             else [handler.type])
    return {n.id if isinstance(n, ast.Name) else getattr(n, "attr", "")
            for n in nodes}


def _json_parses(source: str) -> list:
    """``(function, line, guarded)`` for every ``json.loads`` or
    ``json.load`` call in ``source``: ``guarded`` when a ``try`` around it,
    in the same function, has a handler that catches ``RecursionError``."""
    found = []

    def visit(node, function, guards):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.Lambda)):
            function = getattr(node, "name", function)
            guards = ()           # a try around a definition guards no call
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("loads", "load") \
                and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "json":
            found.append((function, node.lineno,
                          any(g & _CATCHES_IT for g in guards)))
        if isinstance(node, ast.Try):
            names = set()
            for h in node.handlers:
                names |= _handler_names(h)
            for child in node.body:
                visit(child, function, guards + (names,))
            for part in (node.handlers, node.orelse, node.finalbody):
                for child in part:
                    visit(child, function, guards)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, function, guards)

    visit(ast.parse(source), "<module>", ())
    return found


def test_the_scan_finds_a_parse_and_reads_its_handlers():
    src = (
        "import json\n"
        "def bare(x):\n    return json.loads(x)\n"
        "def narrow(x):\n    try:\n        return json.loads(x)\n"
        "    except ValueError:\n        return None\n"
        "def wide(x):\n    try:\n        return json.load(x)\n"
        "    except (ValueError, RecursionError):\n        return None\n"
        "def outer(x):\n    try:\n        def inner():\n"
        "            return json.loads(x)\n        return inner()\n"
        "    except Exception:\n        return None\n"
        "def after(x):\n    try:\n        pass\n"
        "    except RecursionError:\n        return json.loads(x)\n")
    assert [(f, g) for f, _, g in _json_parses(src)] == [
        ("bare", False), ("narrow", False), ("wide", True),
        ("inner", False), ("after", False)]


def test_every_json_parse_in_the_package_answers_for_nesting():
    """The sweep AUD-050 was missing, kept: a ``json.loads`` added to the
    package without a handler for nesting fails here until it has one, or
    until it is listed with the reason it needs none."""
    unguarded = set()
    parses = 0
    for path in sorted((ROOT / "agent_substrate").glob("*.py")):
        for function, line, guarded in _json_parses(path.read_text()):
            parses += 1
            if not guarded:
                unguarded.add((path.name, function))
    assert parses >= 30, "the scan found the parses it is about"
    assert unguarded - set(UNGUARDED) == set(), (
        "a JSON parse with no handler for nesting past the parser")
    assert set(UNGUARDED) - unguarded == set(), (
        "listed as unguarded, and no such parse is there")
    assert all(len(why) > 20 for why in UNGUARDED.values())
