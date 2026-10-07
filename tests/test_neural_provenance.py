"""NF-1T s.5, s.33-35, s.48-49, s.60: lineage, checkpoints, authority."""
from __future__ import annotations

import copy
import hashlib
import json
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from neural_support import require_jax  # noqa: E402

jax = require_jax()

from qta_agent import learned_rules  # noqa: E402
from qta_agent.authority import Role, State  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.learned_lifecycle import LearnedLedger, LedgerError  # noqa
from qta_agent.reconstruct import reconstruct  # noqa: E402
from qta_agent.store import AuthorityStore, StoreError  # noqa: E402
from scientific.identity import digest  # noqa: E402
from scientific_ai.neural import documents, family, manifests  # noqa: E402
from scientific_ai.neural.model import checkpoint as C  # noqa: E402
from scientific_ai.neural.model import meta  # noqa: E402

M = manifests


def blank(schema: str) -> dict:
    return {k: None for k in M.KEYS[schema]} | {"schema": schema}


def linked(cfg_digest: str | None = None) -> dict:
    cfg_digest = cfg_digest or family.development().digest()
    norm = {"rows": 10, "fitted_on_split_digest": "a" * 64}
    ds = blank(M.DATASET_MANIFEST) | {
        "dataset_digest": "1" * 64, "feature_schema_digest": "2" * 64,
        "target_schema_digest": "3" * 64, "normalization_statistics": norm}
    tr = blank(M.TRAINING_MANIFEST) | {
        "run_id": "run-1", "model_configuration_digest": cfg_digest,
        "dataset_digest": ds["dataset_digest"],
        "feature_schema_digest": ds["feature_schema_digest"],
        "target_schema_digest": ds["target_schema_digest"],
        "normalization_digest": digest(norm),
        "resume_semantics": "FRESH_TRAINING", "completion_state":
        "COMPLETED", "reproducibility": {"claimed": ["SEEDED_EXECUTION"]},
        "checkpoints": [{"checkpoint_digest": "5" * 64}]}
    ck = blank(M.CHECKPOINT_MANIFEST) | {
        "checkpoint_digest": "5" * 64, "model_config_digest": cfg_digest,
        "dataset_digest": ds["dataset_digest"], "training_run_id": "run-1",
        "training_manifest_digest": digest(tr), "shard_count": 1,
        "shards": [{"digest": "5" * 64}], "optimizer_state_presence": False,
        "tensor_index_digest": "6" * 64}
    ev = blank(M.EVALUATION_REPORT) | {
        "model_config_digest": cfg_digest, "checkpoint_digest": "5" * 64,
        "checkpoint_manifest_digest": digest(ck),
        "dataset_digest": ds["dataset_digest"],
        "normalization_digest": digest(norm),
        "semantics": list(M.PREDICTION_SEMANTICS),
        "reload": {"digest_verified": True, "outputs_equal": True}}
    for d in (ds, tr, ck, ev):
        assert M.problems(d) == [], (d["schema"], M.problems(d))
    return {"dataset": ds, "training": tr, "checkpoint": ck,
            "evaluation": ev}


# -- the chain ---------------------------------------------------------

def test_a_consistent_chain_has_no_problems():
    d = linked()
    assert M.check_chain(**d, checkpoint_bytes_digest="5" * 64) == []


@pytest.mark.parametrize("edit,expect", [
    (("training", "dataset_digest", "9" * 64), "different dataset"),
    (("training", "normalization_digest", "9" * 64), "normalisation"),
    (("checkpoint", "training_run_id", "run-2"), "different training run"),
    (("checkpoint", "training_manifest_digest", "9" * 64),
     "training manifest digest"),
    (("checkpoint", "model_config_digest", "9" * 64),
     "different configurations"),
    (("evaluation", "checkpoint_digest", "9" * 64), "different checkpoint"),
    (("evaluation", "checkpoint_manifest_digest", "9" * 64),
     "different checkpoint manifest"),
    (("evaluation", "dataset_digest", "9" * 64), "different dataset"),
])
def test_every_broken_link_is_named(edit, expect):
    d = linked()
    doc, key, value = edit
    d[doc][key] = value
    probs = M.check_chain(**d)
    assert any(expect in p for p in probs), probs


def test_bytes_that_are_not_the_recorded_checkpoint_are_refused():
    probs = M.check_chain(**linked(), checkpoint_bytes_digest="7" * 64)
    assert any("bytes" in p for p in probs)


def test_unavailable_is_an_explicit_value_with_a_reason():
    u = M.unavailable("no git checkout")
    assert M.is_unavailable(u) and not M.is_unavailable(None)
    with pytest.raises(M.ManifestError):
        M.unavailable("  ")


# -- resume semantics --------------------------------------------------

@pytest.mark.parametrize("sem,parent_ckpt,parent_run,ok", [
    ("FRESH_TRAINING", None, None, True),
    ("FRESH_TRAINING", "5" * 64, None, False),
    ("EXACT_RESUME", "5" * 64, "run-0", True),
    ("EXACT_RESUME", "5" * 64, None, False),
    ("WARM_START", "5" * 64, None, True),
    ("WARM_START", "5" * 64, "run-0", False),
    ("FINE_TUNE", None, None, False),
    ("CONTINUED_PRETRAINING", "5" * 64, None, True),
    ("RESUME", "5" * 64, "run-0", False),
])
def test_resume_semantics_are_distinct_and_checked(sem, parent_ckpt,
                                                   parent_run, ok):
    tr = linked()["training"] | {"resume_semantics": sem,
                                 "parent_checkpoint": parent_ckpt,
                                 "parent_run": parent_run}
    assert (M.problems(tr) == []) == ok, M.problems(tr)


# -- the checkpoint container ------------------------------------------

def test_tensors_round_trip_exactly_including_scalars_and_bfloat16():
    import ml_dtypes
    t = {"a": np.arange(6, dtype=np.float32).reshape(2, 3),
         "s": np.asarray(7, dtype=np.int64),
         "h": np.ones((4,), dtype=ml_dtypes.bfloat16)}
    back, meta_ = C.decode(C.encode(t, {"k": "v"}))
    assert meta_ == {"k": "v"}
    for k in t:
        assert back[k].shape == t[k].shape and back[k].dtype == t[k].dtype
        assert np.array_equal(back[k], t[k])


def _header(raw):
    (n,) = struct.unpack("<Q", raw[:8])
    return json.loads(raw[8:8 + n]), raw[8 + n:]


def _rebuild(header, data):
    hb = json.dumps(header).encode()
    return struct.pack("<Q", len(hb)) + hb + data


@pytest.mark.parametrize("break_", [
    "short", "length", "dupe", "dtype", "gap", "overlap", "trailing",
    "size", "meta"])
def test_a_malformed_container_is_refused(break_):
    raw = C.encode({"a": np.zeros(4, np.float32),
                    "b": np.ones(2, np.float32)}, {})
    h, data = _header(raw)
    if break_ == "short":
        bad = raw[:5]
    elif break_ == "length":
        bad = struct.pack("<Q", 10 ** 9) + raw[8:]
    elif break_ == "dupe":
        hb = raw[8:8 + struct.unpack("<Q", raw[:8])[0]].decode().rstrip()
        hb = hb[:-1] + ',"a":' + json.dumps(h["a"]) + "}"
        bad = struct.pack("<Q", len(hb)) + hb.encode() + data
    elif break_ == "dtype":
        h["a"]["dtype"] = "PICKLE"
        bad = _rebuild(h, data)
    elif break_ == "gap":
        h["b"]["data_offsets"] = [20, 28]
        bad = _rebuild(h, data + b"\0" * 4)
    elif break_ == "overlap":
        h["b"]["data_offsets"] = [8, 16]
        bad = _rebuild(h, data[:16])
    elif break_ == "trailing":
        bad = raw + b"\0"
    elif break_ == "size":
        h["a"]["shape"] = [5]
        bad = _rebuild(h, data)
    else:
        h["__metadata__"] = {"k": 1}
        bad = _rebuild(h, data)
    with pytest.raises(C.CheckpointError):
        C.decode(bad)


@pytest.fixture(scope="module")
def dev_state():
    cfg = family.development()
    p, b = meta.materialize(cfg, jax.random.key(0))
    return cfg, p, b


def test_load_refuses_other_bytes_other_configurations_and_other_tensors(
        dev_state):
    cfg, p, b = dev_state
    raw = C.save(cfg, p, b)
    d = hashlib.sha256(raw).hexdigest()
    with pytest.raises(C.CheckpointError, match="hash"):
        C.load(cfg, raw, expected_digest="0" * 64)
    other = family.development()
    from dataclasses import replace
    other = replace(other, num_layers=3)
    with pytest.raises(C.CheckpointError, match="configuration"):
        C.load(other, raw, expected_digest=d)
    t, m = C.decode(raw)
    t.pop("params/heads/var_b")
    short = C.encode(t, m)
    with pytest.raises(C.CheckpointError, match="missing"):
        C.load(cfg, short, expected_digest=hashlib.sha256(short).hexdigest())
    t2, m2 = C.decode(raw)
    t2["params/padding"] = np.zeros(10, np.float32)
    extra = C.encode(t2, m2)
    with pytest.raises(C.CheckpointError, match="extra"):
        C.load(cfg, extra, expected_digest=hashlib.sha256(extra).hexdigest())
    t3, m3 = C.decode(raw)
    t3["params/heads/var_b"] = np.zeros(5, np.float32)
    bent = C.encode(t3, m3)
    with pytest.raises(C.CheckpointError):
        C.load(cfg, bent, expected_digest=hashlib.sha256(bent).hexdigest())


def test_optimizer_state_is_present_only_when_saved(dev_state):
    cfg, p, b = dev_state
    from scientific_ai.neural.model.train import init_optimizer
    raw = C.save(cfg, p, b)
    with pytest.raises(C.CheckpointError, match="optimizer"):
        C.load(cfg, raw, expected_digest=hashlib.sha256(raw).hexdigest(),
               with_optimizer=True)
    raw2 = C.save(cfg, p, b, opt_state=init_optimizer(p))
    _, _, opt, _ = C.load(cfg, raw2, expected_digest=hashlib.sha256(
        raw2).hexdigest(), with_optimizer=True)
    assert opt["step"] == 0


def test_a_shard_that_is_not_its_recorded_bytes_is_refused(dev_state):
    cfg, p, b = dev_state
    shards, cd = C.save_sharded(cfg, p, b, 3)
    sd = [hashlib.sha256(s).hexdigest() for s in shards]
    C.load_sharded(cfg, shards, shard_digests=sd, expected_digest=cd)
    with pytest.raises(C.CheckpointError):
        C.load_sharded(cfg, [shards[1], shards[0], shards[2]],
                       shard_digests=sd, expected_digest=cd)
    with pytest.raises(C.CheckpointError):
        C.load_sharded(cfg, shards, shard_digests=sd[::-1],
                       expected_digest=cd)


# -- the authority history ---------------------------------------------

@pytest.fixture()
def ledger(tmp_path):
    log = EventLog(tmp_path / "authority.log")
    ev = EvidenceStore(tmp_path / "evidence")
    store = AuthorityStore(log, evidence=ev).load()
    return log, ev, store, LearnedLedger(store, ev)


@pytest.fixture(scope="module")
def arch_docs():
    cfg = family.development()
    rep = meta.validate(cfg)
    man = documents.model_manifest(cfg, solve=None, evidence=[rep],
                                   source_commit="0" * 40)
    return man, rep


def _register_all(led, arch_docs):
    man, rep = arch_docs
    d = linked()
    ids = {"dataset": led.register(d["dataset"], actor="p"),
           "arch": led.register(man, actor="p")}
    ids["meta"] = led.register(rep, actor="p", depends_on=(ids["arch"],))
    ids["training"] = led.register(d["training"], actor="p",
                                   depends_on=(ids["dataset"], ids["arch"]))
    ids["checkpoint"] = led.register(d["checkpoint"], actor="p",
                                     depends_on=(ids["training"],))
    ids["evaluation"] = led.register(d["evaluation"], actor="p",
                                     depends_on=(ids["checkpoint"],
                                                 ids["dataset"]))
    return ids, d


def test_the_whole_lineage_records_and_reads_back(ledger, arch_docs):
    log, ev, store, led = ledger
    ids, d = _register_all(led, arch_docs)
    assert store.get(ids["checkpoint"]).depends_on == (ids["training"],)
    docs = led.documents()
    assert d["evaluation"] in docs and d["training"] in docs
    assert all(store.get(r).kind.startswith("learned_") for r in
               ids.values())


@pytest.mark.parametrize("what", ["meta", "training", "checkpoint",
                                  "evaluation"])
def test_a_broken_link_is_refused_and_nothing_is_written(ledger, arch_docs,
                                                         what):
    log, ev, store, led = ledger
    man, rep = arch_docs
    d = linked()
    ids = {"dataset": led.register(d["dataset"], actor="p"),
           "arch": led.register(man, actor="p")}
    ids["training"] = led.register(d["training"], actor="p",
                                   depends_on=(ids["dataset"], ids["arch"]))
    ids["checkpoint"] = led.register(d["checkpoint"], actor="p",
                                     depends_on=(ids["training"],))
    before = len(store.all_records())
    if what == "meta":
        bad, deps = dict(rep, config_digest="9" * 64), (ids["arch"],)
    elif what == "training":
        bad = dict(d["training"], dataset_digest="9" * 64, run_id="x")
        deps = (ids["dataset"], ids["arch"])
    elif what == "checkpoint":
        bad = dict(d["checkpoint"], training_run_id="other")
        deps = (ids["training"],)
    else:
        bad = dict(d["evaluation"], checkpoint_digest="9" * 64)
        deps = (ids["checkpoint"], ids["dataset"])
    with pytest.raises(LedgerError):
        led.register(bad, actor="p", depends_on=deps)
    assert len(store.all_records()) == before


def test_a_record_needs_the_upstream_it_links_to(ledger):
    *_, led = ledger
    with pytest.raises(LedgerError, match="depends on"):
        led.register(linked()["training"], actor="p")
    with pytest.raises(LedgerError, match="not a learned"):
        led.register({"schema": "something-else"}, actor="p")


@pytest.mark.parametrize("dst", [State.VERIFIED, State.PROMOTED])
def test_the_store_refuses_to_make_a_learned_record_authority(ledger,
                                                              arch_docs, dst):
    log, ev, store, led = ledger
    ids, _ = _register_all(led, arch_docs)
    rid = ids["evaluation"]
    store.transition(record_id=rid, dst=State.UNDER_REVIEW, actor="rev",
                     role=Role.VERIFIER)
    n = len(log.read_verified()[1])
    with pytest.raises(StoreError, match="no admission policy"):
        store.transition(record_id=rid, dst=State.VERIFIED, actor="rev",
                         role=Role.VERIFIER, evidence={
                             "verification_report":
                                 store.get(rid).evidence["document"]})
    assert len(log.read_verified()[1]) == n
    assert store.get(rid).state is State.UNDER_REVIEW
    assert learned_rules.refusal("learned_x", dst) is not None
    assert learned_rules.refusal("scientific_result", dst) is None
    assert learned_rules.refusal("learned_x", State.REJECTED) is None


def test_replay_and_the_second_reader_refuse_a_forbidden_edge(ledger,
                                                              arch_docs):
    log, ev, store, led = ledger
    ids, _ = _register_all(led, arch_docs)
    rid = ids["evaluation"]
    store.transition(record_id=rid, dst=State.UNDER_REVIEW, actor="rev",
                     role=Role.VERIFIER)
    doc = store.get(rid).evidence["document"]
    # written around the store, as a tampered or foreign writer would
    log.append(actor="rev", action="record.transition", target=rid,
               payload={"record_id": rid, "src": "UNDER_REVIEW",
                        "dst": "VERIFIED", "role": "VERIFIER",
                        "evidence": {"verification_report": doc},
                        "policy_id": None, "stale_reason": None,
                        "edge_reason": "forged",
                        "idempotency_key": None})
    with pytest.raises(StoreError, match="no admission policy"):
        AuthorityStore(log, evidence=ev).load()
    rec = reconstruct(log, evidence=ev)
    assert any("learned record has no admission policy" in u
               for u in rec.unauthorized)
    assert rec.records[rid]["state"] == "UNDER_REVIEW"


def test_rejection_is_recorded_and_nothing_is_built_on_it(ledger,
                                                          arch_docs):
    log, ev, store, led = ledger
    ids, d = _register_all(led, arch_docs)
    reason = ev.put(b'{"why":"miscalibrated on OOD"}')
    led.reject(ids["checkpoint"], actor="reviewer", reason_digest=reason)
    assert store.get(ids["checkpoint"]).state is State.REJECTED
    ev2 = copy.deepcopy(d["evaluation"])
    ev2["limitations"] = ["second look"]
    with pytest.raises(LedgerError, match="REJECTED"):
        led.register(ev2, actor="p", depends_on=(ids["checkpoint"],
                                                 ids["dataset"]))
