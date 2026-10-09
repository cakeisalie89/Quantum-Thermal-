"""Which question a byte comparison answers, and what each answer may say.

``scientific/reproduction.py`` splits one boolean into four properties --
package integrity, byte reproduction, cross-environment decision stability,
scientific equivalence -- and decides between two policies:

* ``strict-reproduction``: only exact bytes, only on a backend the witness
  profile saw reproduce this corpus. Anything else is
  CANONICAL_REPRODUCTION_ENVIRONMENT_REQUIRED -- never "stale outputs".
* ``ci``: exact bytes pass; a witnessed backend with ANY byte difference
  fails with no fallback; a resolved, unwitnessed backend passes only when
  the cross-environment comparison finds every decision stable; an
  unresolved backend fails.

The CASE numbers are directive 7's (section 38). Cases 18-20 are the
comparator's and are in ``tests/test_cross_env_semantics.py``.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from scientific import reproduction as R  # noqa: E402

import reproduction_witness as W  # noqa: E402

DECLARED = ["a.json", "b.csv", "stub.json"]
EXEMPT = ["stub.json"]
CORPUS = {"a.json": "1" * 64, "b.csv": "2" * 64}
CLOSURE = {"gen.py": "3" * 64, "lib/m.py": "4" * 64}
LOCK = {"uv.lock": "5" * 64, "pyproject.toml": "6" * 64}


def _env(**over):
    """An environment record as scientific.run_identity builds one, with a
    resolved runtime probe."""
    rec = {
        "python": "3.12.3", "implementation": "cpython", "machine": "x86_64",
        "distributions": {"numpy": "2.4.4", "scipy": "1.17.1"},
        "cpu": {"source": "cpuinfo", "simd": ["avx2", "avx512f", "fma"],
                "core": {"vendor_id": "GenuineIntel", "cpu family": "6",
                         "model": "143"}},
        "backend": {"cpu_count": 4, "variables": {"OMP_NUM_THREADS": None}},
        "native": {d: {"status": "RESOLVED", "native_files": 10,
                       "native_sha256": d * 8, "installed_sha256": d * 9}
                   for d in ("numpy", "scipy")},
        "runtime": {
            "status": "RESOLVED", "unresolved": [],
            "numpy": {"version": "2.4.4", "cpu_features": ["AVX2"],
                      "simd": {"baseline": ["X86_V2"]},
                      "dispatch": {"functions": 3, "targets": {"X86_V4": 3},
                                   "sha256": "7" * 64}},
            "blas": {"build": {}, "bundled": [
                {"distribution": "numpy", "kernel": "SkylakeX",
                 "config": "OpenBLAS DYNAMIC_ARCH", "threads": 4,
                 "parallel": 1, "sha256": "8" * 64}]},
            "system_libraries": {"m": {"name": "libm.so.6",
                                       "sha256": "9" * 64}},
            "interpreter": {"name": "python3.12", "sha256": "a" * 64}},
        "backend_status": "RESOLVED",
    }
    for path, value in over.items():
        cur = rec
        keys = path.split("__")
        for k in keys[:-1]:
            cur = cur[k]
        cur[keys[-1]] = value
    return rec


def _other_backend():
    return _env(runtime__numpy__dispatch={"functions": 3,
                                         "targets": {"X86_V3": 3},
                                         "sha256": "b" * 64})


def _profile(record=None):
    return R.build_profile(
        declared=DECLARED, exempt=EXEMPT, corpus=CORPUS, closure=CLOSURE,
        lock=LOCK, entry="gen.py", argv=[], record=record or _env(),
        observed={"compared": 2, "byte_identical": 2, "regenerated": 3})


def _decide(policy, *, identical, record=None, profile="default",
            problems=None, inapplicable=(), cross=None, drift=None,
            compared=2):
    prof = _profile() if profile == "default" else profile
    problems = R.validate_profile(prof) if (problems is None and prof) \
        else (problems or [])
    calls = []

    def cross_env():
        calls.append(1)
        return {"status": cross or R.DECISION_STABLE}
    v = R.decide(policy, byte_identical=identical, files_compared=compared,
                 drift=[] if identical else (drift or ["a.json"]),
                 record=_env() if record is None else record,
                 profile=prof, profile_problems=problems,
                 inapplicable=list(inapplicable), cross_env=cross_env)
    v["_cross_env_called"] = bool(calls)
    return v


# ---- CASE 1-2: the witnessed backend ---------------------------------------------
@pytest.mark.parametrize("policy", [R.POLICY_CI, R.POLICY_STRICT])
def test_case_1_the_witnessed_backend_reproducing_passes(policy):
    v = _decide(policy, identical=True)
    assert (v["PACKAGE_STATUS"], v["REPRODUCTION_STATUS"]) == \
        (R.CONSISTENT, R.BYTE_IDENTICAL)
    assert v["witness"] == v["backend_digest"]


@pytest.mark.parametrize("policy", [R.POLICY_CI, R.POLICY_STRICT])
def test_case_2_one_changed_byte_on_the_witnessed_backend_fails_absolutely(
        policy):
    v = _decide(policy, identical=False)
    assert v["PACKAGE_STATUS"] == R.REFUSED
    assert v["REPRODUCTION_STATUS"] == R.BYTE_DRIFT_COMPARABLE_BACKEND
    assert not v["_cross_env_called"], "no semantic fallback, ever, here"
    assert v["CROSS_ENV_STATUS"] == R.NOT_CHECKED


# ---- CASE 3-9: a different resolved backend -------------------------------------
def test_case_3_numeric_drift_on_another_backend_is_consistent_not_reproduced():
    v = _decide(R.POLICY_CI, identical=False, record=_other_backend())
    assert v["_cross_env_called"]
    assert v["PACKAGE_STATUS"] == R.CONSISTENT
    assert v["REPRODUCTION_STATUS"] == R.DIFFERENT_RESOLVED_BACKEND
    assert v["REPRODUCTION_STATUS"] != R.BYTE_IDENTICAL
    assert v["CROSS_ENV_STATUS"] == R.DECISION_STABLE
    assert v["SCIENTIFIC_EQUIVALENCE_STATUS"] == R.NOT_ESTABLISHED
    assert v["witness"] is None


@pytest.mark.parametrize("status", [
    "DECISION_DRIFT",            # CASE 4 (and 6: NaN/Inf is DECISION_DRIFT)
    "STRUCTURAL_DRIFT",          # CASE 5
    "RESOLUTION_AMBIGUITY",      # CASE 7, 8
    "UNCLASSIFIED_DIVERGENCE",   # CASE 9
])
def test_cases_4_to_9_any_cross_environment_failure_refuses(status):
    v = _decide(R.POLICY_CI, identical=False, record=_other_backend(),
                cross=status)
    assert v["PACKAGE_STATUS"] == R.REFUSED
    assert v["REPRODUCTION_STATUS"] == R.DIFFERENT_RESOLVED_BACKEND
    assert v["CROSS_ENV_STATUS"] == status


# ---- CASE 10: unresolved --------------------------------------------------------
@pytest.mark.parametrize("identical", [False, True])
@pytest.mark.parametrize("policy", [R.POLICY_CI, R.POLICY_STRICT])
def test_case_10_an_unresolved_backend_is_refused(policy, identical):
    """Unknown is neither the witness nor different. Fail-closed even when
    the bytes happen to match: the reproduction is claimed only under a
    backend somebody could name."""
    rec = _env(backend_status="UNRESOLVED",
               runtime__status="UNRESOLVED",
               runtime__unresolved=["numpy runtime dispatch"])
    v = _decide(policy, identical=identical, record=rec)
    assert (v["PACKAGE_STATUS"], v["BACKEND_STATUS"],
            v["REPRODUCTION_STATUS"]) == (R.REFUSED, R.UNRESOLVED,
                                          R.BACKEND_UNRESOLVED)
    assert any("numpy runtime dispatch" in r for r in v["reasons"])
    assert not v["_cross_env_called"]


def test_a_modified_native_build_alone_leaves_the_backend_unresolved():
    """The runtime probe resolved; the installed native bytes no longer
    match their RECORD. The record's own verdict is what counts."""
    rec = _env(backend_status="UNRESOLVED",
               native__numpy={"status": "MODIFIED", "native_files": 10,
                              "native_sha256": "x", "installed_sha256": "y"})
    assert rec["runtime"]["status"] == "RESOLVED"
    assert R.backend_identity(rec) is None
    v = _decide(R.POLICY_CI, identical=False, record=rec)
    assert v["REPRODUCTION_STATUS"] == R.BACKEND_UNRESOLVED
    assert any("MODIFIED" in r for r in v["reasons"])


def test_no_record_at_all_is_unresolved():
    v = R.decide(R.POLICY_CI, byte_identical=False, files_compared=2,
                 drift=["a.json"], record=None, profile=_profile(),
                 profile_problems=[], inapplicable=[],
                 cross_env=lambda: {"status": R.DECISION_STABLE})
    assert v["REPRODUCTION_STATUS"] == R.BACKEND_UNRESOLVED


# ---- CASE 11: strict on another backend -------------------------------------------
@pytest.mark.parametrize("identical", [False, True])
def test_case_11_strict_on_another_backend_needs_the_reference(identical):
    v = _decide(R.POLICY_STRICT, identical=identical,
                record=_other_backend())
    assert v["PACKAGE_STATUS"] == R.REFUSED
    assert v["REPRODUCTION_STATUS"] == R.REFERENCE_ENVIRONMENT_REQUIRED
    assert not v["_cross_env_called"], (
        "cross-environment stability never substitutes for reproduction")
    assert v["backend_differences"], "and it says how the backend differs"


# ---- CASE 12: nothing compared ------------------------------------------------------
def test_case_12_zero_files_compared_is_refused():
    v = _decide(R.POLICY_CI, identical=True, compared=0)
    assert (v["PACKAGE_STATUS"], v["REPRODUCTION_STATUS"]) == \
        (R.REFUSED, R.NOTHING_COMPARED)


# ---- CASE 13, 14, 16: the profile no longer applies ----------------------------------
def _applies(**change):
    kw = dict(declared=DECLARED, exempt=EXEMPT, corpus=dict(CORPUS),
              closure=dict(CLOSURE), lock=dict(LOCK))
    kw.update(change)
    return R.applicability(_profile(), **kw)


def test_the_profile_applies_to_what_it_was_built_from():
    assert _applies() == []


def test_case_13_a_changed_corpus_makes_the_profile_inapplicable():
    out = _applies(corpus={**CORPUS, "a.json": "f" * 64})
    assert len(out) == 1 and out[0].startswith("CORPUS_CHANGED")


def test_case_14_a_changed_generator_makes_the_profile_inapplicable():
    out = _applies(closure={**CLOSURE, "lib/m.py": "f" * 64})
    assert len(out) == 1 and out[0].startswith("GENERATOR_CHANGED")
    out = _applies(closure={**CLOSURE, "lib/new.py": "f" * 64})
    assert out and out[0].startswith("GENERATOR_CHANGED"), (
        "a file newly read by the generator changes it too")


def test_case_16_a_changed_exemption_set_makes_the_profile_inapplicable():
    assert "EXEMPTION_SET_CHANGED" in _applies(exempt=[])


def test_a_changed_lock_makes_the_profile_inapplicable():
    assert _applies(lock={**LOCK, "uv.lock": "f" * 64}) == [
        "ENVIRONMENT_LOCK_CHANGED"]


def test_an_inapplicable_profile_refuses_drift_rather_than_calling_it_cross_env():
    v = _decide(R.POLICY_CI, identical=False, record=_other_backend(),
                inapplicable=["CORPUS_CHANGED (1: ['a.json'])"])
    assert (v["PACKAGE_STATUS"], v["REPRODUCTION_STATUS"]) == \
        (R.REFUSED, R.WITNESS_INAPPLICABLE)
    assert not v["_cross_env_called"]


def test_an_inapplicable_profile_does_not_block_a_reproduction_in_ci():
    """The bytes reproduced: that is a fact about this regeneration, whatever
    became of the witness."""
    v = _decide(R.POLICY_CI, identical=True,
                inapplicable=["GENERATOR_CHANGED (1: ['gen.py'])"])
    assert v["REPRODUCTION_STATUS"] == R.BYTE_IDENTICAL


def test_strict_refuses_when_the_profile_does_not_apply():
    v = _decide(R.POLICY_STRICT, identical=True,
                inapplicable=["CORPUS_CHANGED (1: ['a.json'])"])
    assert v["REPRODUCTION_STATUS"] == R.REFERENCE_ENVIRONMENT_REQUIRED


# ---- CASE 15, 17: the profile as a document ----------------------------------------
def test_a_built_profile_is_valid():
    assert R.validate_profile(_profile()) == []


@pytest.mark.parametrize("breakage", [
    lambda d: d.update(schema="byte-reproduction-witness/0"),
    lambda d: d.pop("corpus"),
    lambda d: d["corpus"]["files"].update({"a.json": "0" * 64}),
    lambda d: d["corpus"]["files"].pop("b.csv"),
    lambda d: d["corpus"].update(exempt=["nope.json"]),
    lambda d: d["generator"]["closure"].update({"x.py": "0" * 64}),
    lambda d: d["generator"].update(closure={}),
    lambda d: d["environment"]["lock"].update({"uv.lock": "0" * 64}),
    lambda d: d.update(witnesses=[]),
    lambda d: d["witnesses"][0]["observed"].update(byte_identical=1),
    lambda d: d["witnesses"].append(copy.deepcopy(d["witnesses"][0])),
    lambda d: d.update(corpus="nothing"),
])
def test_case_15_a_malformed_profile_is_refused(breakage):
    doc = _profile()
    breakage(doc)
    assert R.validate_profile(doc), "every digest is recomputed, not read"


def test_case_17_a_witness_claiming_another_identity_is_refused():
    """The witness says it ran on one backend and its recorded identity is
    another's: the digest is recomputed from the identity and disagrees."""
    doc = _profile()
    doc["witnesses"][0]["backend"] = R.backend_identity(_other_backend())
    assert any("does not match the identity" in p
               for p in R.validate_profile(doc))


def test_case_15_a_malformed_profile_refuses_drift_in_ci():
    v = _decide(R.POLICY_CI, identical=False, record=_other_backend(),
                profile={"schema": "junk"})
    assert (v["PACKAGE_STATUS"], v["REPRODUCTION_STATUS"]) == \
        (R.REFUSED, R.WITNESS_INVALID)
    assert not v["_cross_env_called"]


def test_a_missing_profile_refuses_drift_in_ci():
    v = R.decide(R.POLICY_CI, byte_identical=False, files_compared=2,
                 drift=["a.json"], record=_other_backend(), profile=None,
                 profile_problems=["no witness profile"], inapplicable=[],
                 cross_env=lambda: {"status": R.DECISION_STABLE})
    assert v["REPRODUCTION_STATUS"] == R.WITNESS_INVALID


def test_a_profile_is_never_built_for_an_unresolved_backend():
    with pytest.raises(ValueError, match="UNRESOLVED"):
        _profile(_env(backend_status="UNRESOLVED"))


def test_a_profile_is_never_built_over_a_partial_corpus():
    with pytest.raises(ValueError, match="every declared"):
        R.build_profile(declared=DECLARED, exempt=EXEMPT,
                        corpus={"a.json": "1" * 64}, closure=CLOSURE,
                        lock=LOCK, entry="gen.py", argv=[], record=_env(),
                        observed={})


def test_a_second_witness_is_another_backend_on_the_same_corpus():
    doc = R.add_witness(_profile(), _other_backend(),
                        {"compared": 2, "byte_identical": 2})
    assert R.validate_profile(doc) == []
    assert len(doc["witnesses"]) == 2
    v = R.decide(R.POLICY_STRICT, byte_identical=True, files_compared=2,
                 drift=[], record=_other_backend(), profile=doc,
                 profile_problems=[], inapplicable=[])
    assert v["REPRODUCTION_STATUS"] == R.BYTE_IDENTICAL


# ---- the backend identity ------------------------------------------------------------
def test_the_identity_is_what_selects_the_arithmetic():
    a = R.backend_identity(_env())
    for path, value in [
            ("runtime__numpy__dispatch", {"sha256": "c" * 64}),
            ("runtime__blas__bundled", []),
            ("runtime__system_libraries", {}),
            ("runtime__interpreter", {"name": "python3.12",
                                      "sha256": "d" * 64}),
            ("cpu__simd", ["avx2"]),
            ("cpu__core", {"vendor_id": "AuthenticAMD"}),
            ("backend__variables", {"OMP_NUM_THREADS": "1"}),
            ("native__numpy", {"status": "RESOLVED", "native_files": 10,
                               "native_sha256": "x",
                               "installed_sha256": "y"}),
            ("native__numpy__native_sha256", "x"),
            ("native__scipy__installed_sha256", "y"),
            ("distributions", {"numpy": "2.4.5", "scipy": "1.17.1"}),
            ("python", "3.12.4")]:
        b = R.backend_identity(_env(**{path: value}))
        assert R.identity_digest(a) != R.identity_digest(b), path


def test_the_cpu_model_alone_is_not_a_different_backend():
    """Same features, same selections, same instructions."""
    a = R.backend_identity(_env())
    b = R.backend_identity(_env(cpu__core={"vendor_id": "GenuineIntel",
                                           "cpu family": "6",
                                           "model": "106"}))
    assert a == b


@pytest.mark.parametrize("change", [
    {"backend_status": "UNRESOLVED"},
    {"runtime__status": "UNRESOLVED"},
    {"runtime": None},
    {"cpu": {"source": "UNAVAILABLE", "host": "h"}},
    {"runtime__numpy": None},
])
def test_an_unresolved_part_is_no_identity(change):
    assert R.backend_identity(_env(**change)) is None
    assert R.unresolved_parts(_env(**change))


# ---- the witness tool against this tree ------------------------------------------------
def test_the_committed_witness_profile_applies_to_this_tree():
    """The profile is a derived record of the corpus, the generator's
    closure and the lock: a commit that changes any of them without
    re-establishing it (tools/reproduction_witness.py establish) fails here,
    rather than as an inexplicable refusal on the next runner that differs."""
    declared, exempt = W.declared_scope(ROOT)
    doc, problems, inapplicable = W.profile_for_tree(ROOT, declared, exempt)
    assert doc is not None and problems == [], problems
    assert inapplicable == [], inapplicable
    assert doc["witnesses"], "a profile witnessed by nobody"


def test_a_generation_record_binds_only_the_outputs_it_names(tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "a.json").write_text("{}")
    rec = {"schema": W.RECORD_SCHEMA, "environment": _env(),
           "outputs": {"a.json": R.sha256_file(out / "a.json")}}
    p = tmp_path / "rec.json"
    p.write_text(json.dumps(rec))
    assert W.generation_record_for(str(p), out) == rec
    (out / "a.json").write_text("{\"changed\": 1}")
    assert W.generation_record_for(str(p), out) is None, (
        "a supplied tree does not inherit a record of other bytes")
    assert W.generation_record_for(None, out) is None
    p.write_text(json.dumps(dict(rec, schema="other")))
    assert W.generation_record_for(str(p), out) is None


def test_the_early_strict_check_refuses_an_unwitnessed_backend(monkeypatch):
    monkeypatch.setattr(W, "probe_environment",
                        lambda root, dists: _other_backend())
    r = W.reference_environment(ROOT)
    assert not r["ok"] and r["backend_status"] == R.RESOLVED
    assert any("not one the profile witnessed" in x for x in r["reasons"])


def test_the_early_strict_check_refuses_a_tree_the_profile_does_not_fit(
        monkeypatch):
    """Even on the witnessed backend itself: the witness saw a different
    corpus."""
    real = W.profile_for_tree

    def stale(root, declared, exempt, closure=None):
        doc, problems, _ = real(root, declared, exempt, closure=closure)
        return doc, problems, ["CORPUS_CHANGED (1: ['x.csv'])"]
    monkeypatch.setattr(W, "profile_for_tree", stale)
    monkeypatch.setattr(W, "probe_environment", lambda root, dists: (
        pytest.fail("a tree the profile does not fit needs no probe")))
    r = W.reference_environment(ROOT)
    assert not r["ok"] and any("CORPUS_CHANGED" in x for x in r["reasons"])


def test_the_early_strict_check_refuses_an_unresolved_backend(monkeypatch):
    monkeypatch.setattr(W, "probe_environment", lambda root, dists: None)
    r = W.reference_environment(ROOT)
    assert not r["ok"] and r["backend_status"] == R.UNRESOLVED


def test_the_verdict_line_names_every_status():
    line = R.verdict_line(_decide(R.POLICY_CI, identical=False,
                                  record=_other_backend()))
    for k in ("PACKAGE_STATUS=CONSISTENT", "BACKEND_STATUS=RESOLVED",
              "REPRODUCTION_STATUS=DIFFERENT_RESOLVED_BACKEND",
              "CROSS_ENV_STATUS=DECISION_STABLE_WITH_NUMERIC_DRIFT",
              "SCIENTIFIC_EQUIVALENCE_STATUS=NOT_ESTABLISHED"):
        assert k in line
