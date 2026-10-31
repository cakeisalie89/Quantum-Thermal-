"""The portable scientific assurance package and its standalone verifier
(the directive's Part X, item 34, and Part XI, items 35 and 36).

tools/verify_assurance.py is held here to what its docstring says. The
committed package verifies in a process of its own, with nothing but the
standard library. The verifier refuses to run from inside the package, and
it refuses every corruption below at the level and the check that should
catch it, and at no earlier one. Each case is a copy of the committed
package with one thing wrong:

- bytes changed, files added, removed or linked, the manifest malformed:
  INTEGRITY;
- the record and its evidence disagreeing, or the record claiming what the
  package cannot carry: CONSISTENCY, with SHA256SUMS recomputed so that
  integrity holds;
- the log re-encoded, reordered, truncated or edited: HISTORY;
- consistent forgeries, made by tools/assurance_package.forge, which
  renames every digest and hash a change touches and recomputes
  SHA256SUMS: DECISION and RECHECK.

One consistent forgery changes nothing, and passes every level. Only a
package digest obtained independently refuses it: consistency is not
origin, and the verifier says so on every run.
"""
from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import assurance_package as AP  # noqa: E402
import verify_assurance as V  # noqa: E402

PACKAGE = AP.PACKAGE


def _record(pkg: Path) -> dict:
    return json.loads((pkg / V.RECORD).read_bytes())


def _resum(pkg: Path) -> None:
    """SHA256SUMS recomputed over the files present: what someone does who
    edits a file and wants integrity to hold."""
    files = sorted(p.relative_to(pkg).as_posix() for p in pkg.rglob("*")
                   if p.is_file() and p.name != V.SUMS)
    (pkg / V.SUMS).write_text("".join(
        f"{hashlib.sha256((pkg / f).read_bytes()).hexdigest()}  {f}\n"
        for f in files), encoding="ascii")


def _copy(tmp_path: Path, name: str = "pkg") -> Path:
    dst = tmp_path / name
    shutil.copytree(PACKAGE, dst, symlinks=True)
    return dst


def _refused_at(report: dict) -> tuple:
    for lv in report["levels"]:
        for c in lv["checks"]:
            if c["status"] == "FAIL":
                return lv["level"], c["check"]
    return None, None


def _assert_refused(report: dict, level: str, check: str) -> None:
    assert report["verdict"] == "REFUSED", report["levels"]
    at, label = _refused_at(report)
    assert (at, check in (label or "")) == (level, True), (at, label)
    # every level after the one that failed is NOT_REACHED, never passed
    names = [lv["level"] for lv in report["levels"]]
    for lv in report["levels"][names.index(level) + 1:]:
        assert lv["status"] == "NOT_REACHED" and not lv["checks"]


# ------------------------------------------------------- the package as is

def test_the_committed_package_verifies_in_a_process_of_its_own():
    rc, report = AP.run_verifier(PACKAGE)
    assert rc == 0, report.get("stderr")
    assert report["verdict"] == "VERIFIED"
    assert [(lv["level"], lv["status"]) for lv in report["levels"]] == [
        (name, "PASS") for name in V.LEVELS]
    assert all(lv["checks"] for lv in report["levels"])
    assert [g["id"] for g in report["not_established"]] == [
        k for k, _ in V.NOT_ESTABLISHED]
    m = report["measured"]
    assert m["verdict_at_the_point"] == "SUPPORTED"
    assert abs(m["model_minus_exact_K"]) < 1e-3 and m["margin_K"] > 600


def test_the_verifier_needs_nothing_but_the_standard_library(tmp_path):
    """Its imports are the standard library's, and a copy of it alone, in a
    directory of its own, run with -I and an empty environment, verifies
    the package."""
    tree = ast.parse(Path(V.__file__).read_text(encoding="utf-8"))
    names = {a.name.split(".")[0] for n in ast.walk(tree)
             if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree)
              if isinstance(n, ast.ImportFrom) and n.module}
    assert names - {"__future__"} <= set(sys.stdlib_module_names), names
    alone = tmp_path / "elsewhere"
    alone.mkdir()
    shutil.copy2(V.__file__, alone / "verify_assurance.py")
    r = subprocess.run([sys.executable, "-I", "verify_assurance.py",
                        str(PACKAGE)], cwd=alone, env={},
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["verdict"] == "VERIFIED"


def test_the_package_describes_the_code_as_it_is():
    assert AP.currency(PACKAGE) == []


def test_the_record_and_the_guide_say_what_is_not_established():
    rec = _record(PACKAGE)
    assert rec["authority"] == "NON_AUTHORITATIVE"
    assert rec["formal"]["status"] == "NOT_RUN"
    for kind in V.NOT_CARRIED:
        assert rec["establishment"][kind].startswith(V.UNCLAIMED), kind
    assert rec["authorization"]["roles"].startswith("NOT_ESTABLISHED")
    guide = (PACKAGE / V.README).read_text(encoding="utf-8")
    for gap, _ in V.NOT_ESTABLISHED:
        assert gap in guide
    # the guide cites no digest: the record holds them
    assert not any(d in guide for d in rec["evidence"].values())


# ------------------------------------------------------- how it is run

def test_a_verifier_inside_the_package_refuses_to_run(tmp_path):
    pkg = _copy(tmp_path)
    shutil.copy2(V.__file__, pkg / "verify_assurance.py")
    r = subprocess.run([sys.executable, "-I", str(pkg / "verify_assurance.py"),
                        str(pkg)], capture_output=True, text=True, env={},
                       timeout=600)
    assert r.returncode == 3 and "inside the package" in r.stderr


def test_no_package_is_unreadable_and_bad_arguments_are_misuse(tmp_path):
    assert V.main([str(tmp_path / "absent")]) == 2
    (tmp_path / "file").write_text("x")
    assert V.main([str(tmp_path / "file")]) == 2
    assert V.main([str(PACKAGE), "--expect-package-digest", "XYZ"]) == 3
    assert V.main([str(PACKAGE), "--expect-head", "7"]) == 3


def test_a_verifier_error_is_never_a_pass(monkeypatch, capsys):
    def broken(*a, **k):
        raise RuntimeError("a bug inside the verifier")
    monkeypatch.setattr(V, "verify", broken)
    assert V.main([str(PACKAGE)]) == 4
    assert "not a pass" in capsys.readouterr().err


def test_expectations_obtained_independently_are_held(tmp_path):
    digest = hashlib.sha256((PACKAGE / V.SUMS).read_bytes()).hexdigest()
    head = _record(PACKAGE)["history"]["head"]
    good = V.verify(PACKAGE, expect_digest=digest,
                    expect_head=f"{head['seq']}:{head['hash']}")
    assert good["verdict"] == "VERIFIED"
    assert good["expectations"] == {"package_digest": "MATCH",
                                    "head": "MATCH"}
    gaps = {g["id"] for g in good["not_established"]}
    assert not gaps & {"PACKAGE_ORIGIN", "HISTORY_ANCHORED"}
    # an earlier head the history still holds is a head it extends
    events = [json.loads(x) for x in
              (PACKAGE / V.LOG).read_bytes().splitlines()]
    earlier = V.verify(PACKAGE, expect_head=f"5:{events[5]['hash']}")
    assert earlier["verdict"] == "VERIFIED"
    for kw in ({"expect_digest": "0" * 64},
               {"expect_head": f"{head['seq']}:{'0' * 64}"},
               {"expect_head": f"{head['seq'] + 1}:{head['hash']}"}):
        bad = V.verify(PACKAGE, **kw)
        assert bad["verdict"] == "REFUSED"
        assert all(lv["status"] == "PASS" for lv in bad["levels"])
        assert any(v.startswith("MISMATCH")
                   for v in bad["expectations"].values())
    assert V.main([str(PACKAGE), "--expect-package-digest", "0" * 64]) == 1


def test_consistency_is_not_origin(tmp_path):
    """A consistent forgery that changes nothing passes every level; the
    honest package's digest, obtained independently, refuses it."""
    s = AP.load(PACKAGE)
    AP.forge(s, tmp_path / "forged")
    assert V.verify(tmp_path / "forged")["verdict"] == "VERIFIED"
    honest = hashlib.sha256((PACKAGE / V.SUMS).read_bytes()).hexdigest()
    r = V.verify(tmp_path / "forged", expect_digest=honest)
    assert r["verdict"] == "REFUSED"
    assert r["expectations"]["package_digest"].startswith("MISMATCH")
    assert "PACKAGE_ORIGIN" in {g["id"] for g in r["not_established"]}


# ------------------------------------------------------- INTEGRITY

def _flip(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))


@pytest.mark.parametrize("which", ["record", "log", "result_bundle",
                                   "temperature_field"])
def test_one_changed_byte_is_refused_and_named(tmp_path, which):
    pkg = _copy(tmp_path)
    ev = _record(pkg)["evidence"]
    rel = {"record": V.RECORD, "log": V.LOG}.get(which) or \
        V.EVIDENCE + ev.get(which, "")
    _flip(pkg / rel)
    _assert_refused(V.verify(pkg), "INTEGRITY",
                    f"{rel} is the bytes SHA256SUMS lists")


def _fs_case(kind):
    def unlisted(pkg):
        (pkg / "evidence" / ("0" * 64)).write_bytes(b"x")

    def missing(pkg):
        (pkg / V.LOG).unlink()

    def linked(pkg):
        (pkg / "history" / "again").symlink_to(pkg / V.LOG)

    def misnamed(pkg):
        ev = _record(pkg)["evidence"]["run_identity"]
        (pkg / V.EVIDENCE / ev).rename(pkg / V.EVIDENCE / ("1" * 64))
        _resum(pkg)

    def outside(pkg):
        (pkg / "notes.txt").write_text("a note")
        _resum(pkg)

    def no_guide(pkg):
        (pkg / V.README).unlink()
        _resum(pkg)

    def deep(pkg):
        d = pkg / "evidence" / "a" / "b" / "c" / "d"
        d.mkdir(parents=True)
        (d / "e").write_text("x")

    return {"unlisted": unlisted, "missing": missing, "linked": linked,
            "misnamed": misnamed, "outside": outside, "no_guide": no_guide,
            "deep": deep}[kind]


@pytest.mark.parametrize("kind, check", [
    ("unlisted", "every file present is listed"),
    ("missing", "every listed file is present"),
    ("linked", "every entry is a file or a directory"),
    ("misnamed", "is named by its own digest"),
    ("outside", "is a file the package's layout has"),
    ("no_guide", "README.md is in the package"),
    ("deep", "no deeper than the bound"),
])
def test_the_files_are_exactly_the_listed_ones(tmp_path, kind, check):
    pkg = _copy(tmp_path)
    _fs_case(kind)(pkg)
    _assert_refused(V.verify(pkg), "INTEGRITY", check)


@pytest.mark.parametrize("edit, check", [
    (lambda t: t.replace("  ", " ", 1), "is <sha256>  <path>"),
    (lambda t: t.replace("  history/", "  ../history/", 1),
     "inside the package"),
    (lambda t: "\n".join(reversed(t.rstrip("\n").split("\n"))) + "\n",
     "each path once, in order"),
    (lambda t: t.rstrip("\n"), "ends with a newline"),
    (lambda t: t + f"{'0' * 64}  SHA256SUMS\n", "does not list itself"),
    # empty, and a bare newline: neither lists a file
    (lambda t: "", "ends with a newline"),
    (lambda t: "\n", "is <sha256>  <path>"),
])
def test_a_malformed_manifest_is_refused(tmp_path, edit, check):
    pkg = _copy(tmp_path)
    sums = pkg / V.SUMS
    sums.write_text(edit(sums.read_text(encoding="ascii")), encoding="ascii")
    _assert_refused(V.verify(pkg), "INTEGRITY", check)


def test_a_manifest_that_is_not_ascii_is_refused(tmp_path):
    pkg = _copy(tmp_path)
    (pkg / V.SUMS).write_bytes((pkg / V.SUMS).read_bytes() + "é\n".encode())
    _assert_refused(V.verify(pkg), "INTEGRITY", "SHA256SUMS is ASCII")


# ------------------------------------------------------- CONSISTENCY

def _record_case(tmp_path, edit) -> Path:
    pkg = _copy(tmp_path)
    rec = _record(pkg)
    edit(rec)
    (pkg / V.RECORD).write_bytes(AP._dump(rec))
    _resum(pkg)
    return pkg


def _first(seq, name):
    return next(x for x in seq if x["name"] == name)


RECORD_CASES = [
    ("schema", lambda r: r.update(schema="another/1"),
     "is a scientific assurance record"),
    ("version", lambda r: r["verifier"].update(version=2),
     "made for this verifier's version"),
    ("problem", lambda r: r["mathematical_statement"].update(problem="x/1"),
     "the problem this verifier rechecks"),
    ("extra field", lambda r: r.update(signed_by="the producer"),
     "exactly the record's fields"),
    ("extra role", lambda r: r["evidence"].update(transcript="0" * 64),
     "names each kind of evidence once"),
    ("input value", lambda r: r["inputs"]["q_W_m3"].update(value=1.0),
     "inputs are the bundle's parameters"),
    ("input unit", lambda r: r["inputs"]["L_m"].update(unit="mm"),
     "in the unit the equations are written in"),
    ("model", lambda r: r["model"].update(model_version="9.9.9"),
     "the bundle's model_version is the record's"),
    ("result", lambda r: r["results"]["T_at_half"].update(value=300.0),
     "the record's T_at_half is the bundle's"),
    ("toolchain", lambda r: r["toolchain"].update(stated_by="verified"),
     "toolchain is what the bundle states"),
    ("check named", lambda r: r["independent_checks"][0].update(
        check_id="another"), "the record's check is the one in the evidence"),
    ("budget total", lambda r: r["error_budget"].update(
        total_half_width=0.001), "states the budget's total and statement"),
    ("authority", lambda r: r.update(authority="AUTHORITATIVE"),
     "authorize nothing"),
    ("a kind missing", lambda r: r["establishment"].pop("MODEL_ASSESSED"),
     "says something of every kind of establishment"),
    ("a proof claimed", lambda r: r["establishment"].update(
        FORMALLY_PROVED="PROVED in Lean"),
     "claims no establishment the package cannot carry"),
    ("a validation claimed", lambda r: r["establishment"].update(
        EXPERIMENTALLY_VALIDATED="VALIDATED against a thermocouple"),
     "claims no establishment the package cannot carry"),
    ("formal", lambda r: r["formal"].update(status="PROVED"),
     "claims no formal proof"),
    ("a gap hidden", lambda r: r.update(not_established=[
        g for g in r["not_established"] if g["id"] != "PHYSICAL_VALIDITY"]),
     "names every gap this verifier knows of"),
]


@pytest.mark.parametrize("name, edit, check", RECORD_CASES,
                         ids=[c[0] for c in RECORD_CASES])
def test_a_record_that_says_otherwise_is_refused(tmp_path, name, edit, check):
    _assert_refused(V.verify(_record_case(tmp_path, edit)), "CONSISTENCY",
                    check)


def test_two_kinds_of_evidence_in_one_file_are_refused(tmp_path):
    pkg = _copy(tmp_path)
    rec = _record(pkg)
    (pkg / V.EVIDENCE / rec["evidence"]["decision_proof_supported"]).unlink()
    rec["evidence"]["decision_proof_supported"] = \
        rec["evidence"]["decision_proof_verified"]
    (pkg / V.RECORD).write_bytes(AP._dump(rec))
    _resum(pkg)
    _assert_refused(V.verify(pkg), "CONSISTENCY",
                    "each kind of evidence is its own file")


def test_evidence_the_record_does_not_name_is_refused(tmp_path):
    pkg = _copy(tmp_path)
    (pkg / V.EVIDENCE / ("2" * 64)).write_bytes(b"2")
    (pkg / V.EVIDENCE / ("2" * 64)).rename(
        pkg / V.EVIDENCE / hashlib.sha256(b"2").hexdigest())
    _resum(pkg)
    _assert_refused(V.verify(pkg), "CONSISTENCY",
                    "every evidence file is one the record names")


@pytest.mark.parametrize("raw, check", [
    (b'{"a": 1, "a": 2}', "one reading of JSON"),
    (b'{"a": NaN}', "one reading of JSON"),
    (b'{"a": 1e400}', "one reading of JSON"),
    (b'[1, 2]', "the record is an object"),
])
def test_a_record_with_two_readings_is_refused(tmp_path, raw, check):
    pkg = _copy(tmp_path)
    (pkg / V.RECORD).write_bytes(raw)
    _resum(pkg)
    _assert_refused(V.verify(pkg), "CONSISTENCY", check)


def _forged(tmp_path, edit) -> Path:
    s = AP.load(PACKAGE)
    edit(s)
    AP.forge(s, tmp_path / "forged")
    return tmp_path / "forged"


def _bundle(s):
    return s["json"]["result_bundle"]


def _th(s):
    return _first(_bundle(s)["outputs"], "T_at_half")["quantity"]


EVIDENCE_CASES = [
    ("parameter digest", lambda s: _bundle(s).update(
        parameter_digest="0" * 64), "parameter digest is its parameters'"),
    ("a measurement", lambda s: _bundle(s).update(
        observation_kind="PROCESSED_MEASUREMENT"),
     "a simulation result, not a measurement"),
    ("two artifacts", lambda s: _bundle(s)["artifacts"].append(
        dict(_bundle(s)["artifacts"][0])), "one artifact is the field"),
    ("run identity", lambda s: s["json"]["run_identity"].update(
        environment_digest="0" * 64),
     "the run identity's environment_digest is the bundle's"),
    ("check subject", lambda s: s["json"]["verification_report"].update(
        subject_digest="0" * 64), "the check's subject is this bundle"),
    ("check is the producer", lambda s: s["json"][
        "verification_report"].update(
            verifier_implementation_digest=_bundle(s)[
                "implementation_digest"]), "is not its code"),
    ("check failed", lambda s: s["json"]["verification_report"].update(
        status="FAIL"), "the check passed on what it measured"),
    ("check over threshold", lambda s: s["json"]["verification_report"][
        "measured"].update(value=2.0), "the check passed on what it measured"),
    ("check validates", lambda s: s["json"]["verification_report"].update(
        establishes="EXPERIMENTAL_VALIDATION"),
     "establishes numerical agreement and no more"),
    ("budget point", lambda s: s["json"]["error_budget"]["point"].update(
        q_W_m3=1.0), "the budget is the slab's, at the bundle's point"),
    ("budget model", lambda s: s["json"]["error_budget"]["model"].update(
        implementation_digest="0" * 64),
     "the budget is of this model's implementation"),
    ("budget value", lambda s: s["json"]["error_budget"]["budget"].update(
        value=300.0), "the budget is of the bundle's T_at_half"),
    ("budget authority", lambda s: s["json"]["error_budget"].update(
        authority="AUTHORITATIVE"), "authorize nothing"),
]


@pytest.mark.parametrize("name, edit, check", EVIDENCE_CASES,
                         ids=[c[0] for c in EVIDENCE_CASES])
def test_evidence_that_disagrees_is_refused(tmp_path, name, edit, check):
    _assert_refused(V.verify(_forged(tmp_path, edit)), "CONSISTENCY", check)


@pytest.mark.parametrize("edit", [
    lambda b: b.replace(b"QTSL1\n", b"QTSL2\n", 1),
    lambda b: b.replace(b'"<f8"', b'">f8"', 1),
    lambda b: b[:-8],
])
def test_a_field_not_in_its_stated_format_is_refused(tmp_path, edit):
    s = AP.load(PACKAGE)
    s["field"] = edit(s["field"])
    AP.forge(s, tmp_path / "forged")
    _assert_refused(V.verify(tmp_path / "forged"), "CONSISTENCY",
                    "the field artifact is in its stated format")


# ------------------------------------------------------- HISTORY

def _log_case(tmp_path, edit) -> Path:
    pkg = _copy(tmp_path)
    lines = (pkg / V.LOG).read_bytes().split(b"\n")[:-1]
    (pkg / V.LOG).write_bytes(edit(lines))
    _resum(pkg)
    return pkg


def _rehash(rec: dict) -> bytes:
    rec["hash"] = V.sha(V.canonical({f: rec[f] for f in V._EVENT_FIELDS}))
    return V.canonical(rec)


def _edit_event(lines, i, change, rehash=True):
    rec = json.loads(lines[i])
    change(rec)
    lines[i] = _rehash(rec) if rehash else V.canonical(rec)
    return b"\n".join(lines) + b"\n"


LOG_CASES = [
    ("torn", lambda ls: b"\n".join(ls), "ends with a newline"),
    ("blank", lambda ls: b"\n".join(ls[:3] + [b""] + ls[3:]) + b"\n",
     "one reading of JSON"),
    ("spaced", lambda ls: b"\n".join(
        ls[:3] + [ls[3].replace(b'":', b'": ', 1)] + ls[4:]) + b"\n",
     "canonical bytes"),
    ("reordered", lambda ls: b"\n".join([ls[1], ls[0]] + ls[2:]) + b"\n",
     "positions run from 0"),
    ("truncated", lambda ls: b"\n".join(ls[:-1]) + b"\n",
     "ends at the head the record states"),
    ("edited", lambda ls: _edit_event(
        ls, 5, lambda r: r.update(actor="someone else"), rehash=False),
     "hash is its body's digest"),
    ("unlinked", lambda ls: _edit_event(
        ls, 5, lambda r: r.update(prev_hash="0" * 64)),
     "links to the one before it"),
    ("extra field", lambda ls: _edit_event(
        ls, 5, lambda r: r.update(note="x")), "exactly the event's fields"),
    ("mistyped", lambda ls: _edit_event(
        ls, 5, lambda r: r.update(wall_time="noon")), "have their types"),
    ("another form", lambda ls: _edit_event(
        ls, 5, lambda r: r.update(canonical_form_version=2)),
     "have their types"),
]


@pytest.mark.parametrize("name, edit, check", LOG_CASES,
                         ids=[c[0] for c in LOG_CASES])
def test_a_history_that_is_not_one_chain_is_refused(tmp_path, name, edit,
                                                    check):
    _assert_refused(V.verify(_log_case(tmp_path, edit)), "HISTORY", check)


def test_a_record_that_miscounts_the_history_is_refused(tmp_path):
    pkg = _record_case(tmp_path,
                       lambda r: r["history"].update(events=7))
    _assert_refused(V.verify(pkg), "HISTORY", "counts the history's records")


# ------------------------------------------------------- DECISION

def _events_of(s, action, target=None):
    return [e for e in s["events"] if e["action"] == action
            and (target is None or e["target"] == target)]


def _decision(s, name):
    return _first(s["record"]["authorization"]["decisions"], name)


def _append(s, action, target, actor, payload):
    """A record appended at the head, as a later process would: the forger
    re-chains it, and the record's head follows."""
    last = s["events"][-1]
    e = {"seq": last["seq"] + 1, "event_id": "f" * 32,
         "wall_time": last["wall_time"] + 1.0, "actor": actor,
         "action": action, "target": target, "payload": payload,
         "prev_hash": last["hash"], "hash": "e" * 64,
         "canonical_form_version": 1}
    s["events"].append(e)
    s["original"]["hashes"].append("e" * 64)
    s["record"]["history"].update(events=len(s["events"]),
                                  head={"seq": e["seq"], "hash": "e" * 64})


def _self_verified(s):
    rr = s["record"]["authorization"]["result_record"]
    d = _decision(s, "verified")
    s["events"][d["seq"]]["actor"] = d["actor"] = rr["proposer"]
    p = s["json"]["decision_proof_verified"]
    p["decision"]["actor"] = rr["proposer"]
    p["separation_of_duties"].update(decider=rr["proposer"], distinct=False)


def _withdrawn(s):
    rid = s["record"]["authorization"]["result_record"]["record_id"]
    _append(s, "record.transition", rid, "model-result-reviewer",
            {"dst": "STALE", "record_id": rid, "role": "VERIFIER",
             "src": "VERIFIED", "evidence": {}})


def _retracted(s):
    _append(s, "research.status", "h1", "research-controller",
            {"basis": [], "reason": "retracted", "status": "INCONCLUSIVE",
             "target_id": "h1"})


def _moved_point(s):
    obj = _events_of(s, "research.objective")[0]
    obj["payload"]["objective"]["question"]["parameters"]["q_W_m3"] = 2e7


def _other_question(s):
    s["record"]["question"]["statement"] = "a question nobody asked"


def _other_prediction(s):
    hyp = _events_of(s, "research.hypothesis", "h1")[0]
    hyp["payload"]["hypothesis"]["proposed_by"] = "someone else"


def _other_hypothesis(s):
    obj = _events_of(s, "research.objective")[0]
    obj["payload"]["objective"]["question"]["prediction"]["value"] = 999.0


def _other_bundle(s):
    rid = s["record"]["authorization"]["result_record"]["record_id"]
    create = _events_of(s, "record.create", rid)[0]
    create["payload"]["evidence"]["result_bundle"] = "0" * 64


def _uncaptured(s):
    for e in _events_of(s, "task.evidence"):
        e["payload"]["artifacts"] = {}


def _other_role(s):
    d = _decision(s, "verified")
    s["events"][d["seq"]]["payload"]["role"] = "PROMOTER"


def _other_observation(s):
    for e in _events_of(s, "research.observation"):
        e["payload"]["observation"]["values"]["T_at_half"][
            "resolution"] = 1.0


def _proof_of_another(s):
    s["json"]["decision_proof_verified"]["decision"]["seq"] -= 1


def _proof_cites_other(s):
    s["json"]["decision_proof_verified"]["record"]["evidence"].pop(
        "run_identity")


def _proof_unverified(s):
    for x in s["json"]["decision_proof_supported"]["basis"]:
        x["record_state"] = "PROPOSED"


def _unreported(s):
    s["record"]["authorization"]["decisions"].pop()


def _nowhere(s):
    _decision(s, "verified")["seq"] = 10 ** 6


def _misreported(s):
    _decision(s, "supported")["actor"] = "owner"


DECISION_CASES = [
    ("self-verified", _self_verified, "not the one that proposed it"),
    ("withdrawn", _withdrawn, "the result still stands"),
    ("retracted", _retracted, "the verdict still stands"),
    ("moved point", _moved_point, "answered the point the question asked"),
    ("other question", _other_question, "the one the owner registered"),
    ("other proposer", _other_prediction,
     "the hypothesis is in the history once"),
    ("other hypothesis", _other_hypothesis,
     "the hypothesis predicts what the question asked"),
    ("other bundle", _other_bundle, "the result was proposed as the record"),
    ("uncaptured", _uncaptured, "a governed task captured"),
    ("other role", _other_role, "the verification is in the history as"),
    ("other observation", _other_observation,
     "rests on an observation of this result"),
    ("proof of another", _proof_of_another,
     "proof of the verified decision is about this history"),
    ("proof cites other", _proof_cites_other,
     "cites this package's evidence and two parties"),
    ("proof unverified", _proof_unverified,
     "the proof of the verdict rests on this result"),
    ("unreported", _unreported, "reports the result's verification"),
    ("nowhere", _nowhere, "every position the record names"),
    ("misreported", _misreported, "the verdict is in the history as"),
]


@pytest.mark.parametrize("name, edit, check", DECISION_CASES,
                         ids=[c[0] for c in DECISION_CASES])
def test_a_consistent_forgery_of_a_decision_is_refused(tmp_path, name, edit,
                                                       check):
    _assert_refused(V.verify(_forged(tmp_path, edit)), "DECISION", check)


def test_what_a_level_did_not_expect_fails_that_level(tmp_path):
    """A record shaped as no check expected makes a level raise; the raise
    is that level's failure, never a pass and never a crash."""
    pkg = _record_case(tmp_path, lambda r: r.update(
        authorization=["a list where an object belongs"]))
    _assert_refused(V.verify(pkg), "DECISION", "the level ran to its end")


@pytest.mark.parametrize("edit, says", [
    (lambda r, b, e: r["model"].update(implementation_digest="0" * 64),
     "the model's implementation has changed"),
    (lambda r, b, e: e.update(verifier_implementation_digest="0" * 64),
     "the method-of-lines check has changed"),
    (lambda r, b, e: b["budget"].update(value=0.0),
     "budget.budget.value"),
])
def test_a_package_that_no_longer_describes_the_code_is_stale(tmp_path, edit,
                                                              says):
    """Currency is the repository's question, not the verifier's: the
    package as built against the code as it is now."""
    pkg = _copy(tmp_path)
    rec = _record(pkg)
    ev = rec["evidence"]
    budget = json.loads((pkg / V.EVIDENCE / ev["error_budget"]).read_bytes())
    ver = json.loads((pkg / V.EVIDENCE /
                      ev["verification_report"]).read_bytes())
    edit(rec, budget, ver)
    (pkg / V.RECORD).write_bytes(AP._dump(rec))
    (pkg / V.EVIDENCE / ev["error_budget"]).write_bytes(AP._dump(budget))
    (pkg / V.EVIDENCE / ev["verification_report"]).write_bytes(AP._dump(ver))
    assert any(says in p for p in AP.currency(pkg)), AP.currency(pkg)


def test_a_package_whose_model_now_computes_otherwise_is_stale(tmp_path):
    pkg = _copy(tmp_path)
    ev = _record(pkg)["evidence"]
    path = pkg / V.EVIDENCE / ev["result_bundle"]
    bundle = json.loads(path.read_bytes())
    _first(bundle["outputs"], "T_at_half")["quantity"]["value"] += 1e-3
    path.write_bytes(AP._dump(bundle))
    stale = AP.currency(pkg)
    assert any(p.startswith("T_at_half:") and "in the package" in p
               for p in stale), stale


# ------------------------------------------------------- RECHECK

def _budget(s):
    return s["json"]["error_budget"]


def _contribution(s, name):
    return _first(_budget(s)["budget"]["contributions"], name)


def _warmer(s, by=0.01):
    """T_at_half moved everywhere it is stated: the bundle, the record, the
    budget, the observation and the proof."""
    th = _th(s)
    th["value"] += by
    new = th["value"]
    s["record"]["results"]["T_at_half"]["value"] = new
    _budget(s)["budget"]["value"] = new
    for e in _events_of(s, "research.observation"):
        e["payload"]["observation"]["values"]["T_at_half"]["value"] = new
    for x in s["json"]["decision_proof_supported"]["basis"]:
        x["values"]["T_at_half"]["value"] = new


def _cooler_edge(s):
    _first(_bundle(s)["outputs"], "T_at_L")["quantity"]["value"] -= 0.01


def _hot_cell(s):
    import struct
    raw = s["field"]
    end = raw.index(b"\n", 6) + 1
    n = (len(raw) - end) // 16
    ts = list(struct.unpack(f"<{n}d", raw[end + 8 * n:]))
    ts[n // 2] += 0.01
    s["field"] = raw[:end + 8 * n] + struct.pack(f"<{n}d", *ts)


def _reference(s):
    _budget(s)["reference"]["value"] += 1e-6


def _measured(s):
    _contribution(s, "discretization_against_the_series")["half_width"] *= 2


def _stat(where, key, by):
    def edit(s):
        _budget(s)["propagation"][where][key] += by
    return edit


def _redrawn(s):
    _budget(s)["propagation"]["monte_carlo"]["redrawn"] = 1


def _spread(s):
    _budget(s)["spreads"]["q_W_m3"] *= 2


def _parametric(s):
    _contribution(s, "parameters")["half_width"] *= 1.5


def _rounding(s):
    _contribution(s, "rounding")["half_width"] *= 2


def _total(s):
    b = _budget(s)["budget"]
    b["total"]["half_width"] -= 0.5
    s["record"]["error_budget"]["total_half_width"] = b["total"]["half_width"]


def _statement(s):
    b = _budget(s)["budget"]
    b["statement"] = b["statement"].replace("+/- 4.24", "+/- 0.42", 1)
    s["record"]["error_budget"]["statement"] = b["statement"]


def _coverage(s):
    _budget(s)["estimate_covers_measured"]["verdict"] = \
        "ESTIMATE_COVERS_MEASURED"


def _numbered_gap(s):
    _contribution(s, "model_form")["half_width"] = 0.0


def _measurement_replaces_measurement(s):
    _contribution(s, "discretization")["basis"] = "MEASURED"


def _threshold(value):
    def edit(s):
        for p in (_events_of(s, "research.objective")[0]["payload"][
                "objective"]["question"]["prediction"],
                _events_of(s, "research.hypothesis", "h1")[0]["payload"][
                    "hypothesis"]["prediction"],
                s["record"]["hypothesis"]["prediction"],
                s["json"]["decision_proof_supported"]["hypothesis"][
                    "prediction"]):
            p["value"] = value
    return edit


def _quantity(s):
    for p in (_events_of(s, "research.objective")[0]["payload"][
            "objective"]["question"]["prediction"],
            _events_of(s, "research.hypothesis", "h1")[0]["payload"][
                "hypothesis"]["prediction"],
            s["record"]["hypothesis"]["prediction"],
            s["json"]["decision_proof_supported"]["hypothesis"][
                "prediction"]):
        p["quantity"] = "T_at_L"


def _samples(s):
    _budget(s)["propagation"]["monte_carlo"]["samples"] = 20_001


RECHECK_CASES = [
    ("warmer", _warmer, "the model's T_at_half is the exact solution's"),
    ("cooler edge", _cooler_edge, "the model's T_at_L is the exact"),
    ("hot cell", _hot_cell, "whole final profile"),
    ("reference", _reference, "the producer's reference is the exact"),
    ("measured", _measured, "measured discretization is |model - exact|"),
    ("linear std", _stat("linear", "std", 0.01),
     "the linear_central_difference std"),
    ("mc mean", _stat("monte_carlo", "mean", 0.01), "monte_carlo mean"),
    ("mc std", _stat("monte_carlo", "std", 0.01), "monte_carlo std"),
    ("mc low", _stat("monte_carlo", "p2_5", 0.01), "monte_carlo p2_5"),
    ("mc high", _stat("monte_carlo", "p97_5", 0.01), "monte_carlo p97_5"),
    ("redrawn", _redrawn, "the same draws needed the same redraws"),
    ("spread", _spread, "the linear_central_difference std"),
    ("parametric", _parametric, "half the central 95% interval"),
    ("rounding", _rounding, "the rounding contribution is 64 eps"),
    ("total", _total, "the budget's total is the sum"),
    ("statement", _statement, "the budget's statement says its own numbers"),
    ("coverage", _coverage, "verdict on the model's estimate"),
    ("numbered gap", _numbered_gap, "carries a number exactly when"),
    ("measurement for a measurement", _measurement_replaces_measurement,
     "replaces only an estimate"),
    ("threshold below", _threshold(300.0),
     "the recorded verdict is the one the exact solution gives"),
    ("threshold at", _threshold(331.0184),
     "the recorded verdict is the one the exact solution gives"),
    ("quantity", _quantity, "a temperature this verifier computes"),
    ("samples", _samples, "the propagation is one this verifier can repeat"),
]


@pytest.mark.parametrize("name, edit, check", RECHECK_CASES,
                         ids=[c[0] for c in RECHECK_CASES])
def test_a_consistent_forgery_of_the_science_is_refused(tmp_path, name, edit,
                                                        check):
    _assert_refused(V.verify(_forged(tmp_path, edit)), "RECHECK", check)


def test_inputs_outside_the_equations_are_refused(tmp_path):
    def cold(s):
        for p in (_bundle(s)["parameters"],
                  _events_of(s, "research.objective")[0]["payload"][
                      "objective"]["question"]["parameters"],
                  _budget(s)["point"]):
            p["h_W_m2_K"] = -50.0
        _bundle(s)["parameter_digest"] = V.sha(V.canonical(
            _bundle(s)["parameters"]))
        s["json"]["run_identity"]["parameter_digest"] = \
            _bundle(s)["parameter_digest"]
        s["record"]["inputs"]["h_W_m2_K"]["value"] = -50.0
        for e in _events_of(s, "research.observation"):
            e["payload"]["observation"]["parameter_digest"] = \
                _bundle(s)["parameter_digest"]
    _assert_refused(V.verify(_forged(tmp_path, cold)), "RECHECK",
                    "inside the equations' domain")


@pytest.mark.parametrize("op, verdict", [
    ("<", "SUPPORTED"), ("<=", "SUPPORTED"), (">", "REFUTED"),
    (">=", "REFUTED")])
def test_the_verdict_is_evaluated_for_each_comparison(op, verdict):
    assert V._evaluate(op, 330.0, 332.0, 1000.0) == verdict
    assert V._evaluate(op, 330.0, 332.0, 331.0) == "INCONCLUSIVE"
    with pytest.raises(V.Refused):
        V._evaluate("!=", 330.0, 332.0, 1000.0)


def test_the_exact_solution_is_the_series_the_producer_checks_against():
    """A control on the verifier's own solver: at the documented point and
    at a far one, it agrees with scientific.checks.slab_series, written
    separately (bisection and Gauss-Legendre quadrature where the verifier
    has Newton's method and closed-form integrals)."""
    from scientific.checks.slab_series import series_temperature
    point = {k: v["value"] for k, v in _record(PACKAGE)["inputs"].items()}
    for p in (point, {**point, "q_W_m3": 2e7, "h_W_m2_K": 500.0,
                      "t_end_s": 60.0}):
        for frac in (0.0, 0.3, 0.5, 1.0):
            mine = V.exact_temperature(p, frac)
            theirs = series_temperature(p, [frac * p["L_m"]])[0]
            assert abs(mine - theirs) <= 1e-9 * abs(theirs), (p, frac)
