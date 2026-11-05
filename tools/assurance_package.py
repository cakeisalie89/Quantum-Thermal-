#!/usr/bin/env python3
"""The portable scientific assurance package for the slab's mid-plane
temperature, and its build.

    python tools/assurance_package.py --out DIR   # build a package into DIR
    python tools/assurance_package.py --write     # rebuild the committed one
    python tools/assurance_package.py --check     # CI: see below

WHAT IS PACKAGED. One governed answer to the owner's question, "does
T_at_half stay below 1000 K at the documented point?": the scripted agent's
model run through the governed path, its independent check, the reviewer's
VERIFIED decision and the research ledger's SUPPORTED verdict, as the run
leaves them. Beside them the slab's error budget (tools/slab_uq.py) and the
second reader's proofs of both decisions (decision-proof/1). Nothing is
written for the occasion: the run is the adversarial evaluation's
legitimate one (tools/adversarial_eval.World), and every file but the
record, the guide, the budget and the proofs is the run's own bytes.

    assurance_record.json    the record (scientific-assurance-record/2)
    README.md                the reproduction guide
    history/log.jsonl        the run's event log, as written: a GOVERNED
                             history, every record signed
    history/attestations.jsonl  each record's attestation, as written
    trust/key_registry.json  the key registry its first record declares
    trust/role_registry.json the role registry its first record declares
    evidence/<sha256>        each piece of evidence, named by its digest
    SHA256SUMS               every other file's digest

WHAT CHECKS IT. tools/verify_assurance.py, which imports the standard
library and nothing else, never this module, and never comes from the
package. It holds the package at seven levels and lists what it cannot
establish. ``--check`` runs it on the committed package in a process of its
own (``python -I``, an empty environment), then holds the package to the
code as it is now (the model's implementation digest, the check's, the
committed budget, a fresh run of the model at the recorded inputs) and
demands meaningful refusals of corrupted copies: one byte changed in each
file, and three consistent forgeries (a forger who rewrites every digest,
every hash and SHA256SUMS) of the temperature, of the deciding party and of
nothing at all. The last of them passes every level, and only the package
digest obtained independently refuses it: that is what the trust roots are
for, and the guide says so.

WHAT IT IS NOT. Evidence, not authority: NON_AUTHORITATIVE. MODEL_ONLY: no
temperature of a real slab was observed. The agent is a script. No formal
proof exists. The record says each of these, and the verifier refuses a
record that does not.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import verify_assurance as V  # noqa: E402

PACKAGE = ROOT / "docs" / "assurance" / "package" / "slab-mid-plane"
VERIFIER = ROOT / "tools" / "verify_assurance.py"
PACKAGE_ID = "slab-mid-plane"
HYPOTHESIS = "h1"
#: how the JSON files the build writes are laid out; the verifier reads any
#: layout, and only the bytes' digests bind them
_INDENT = 1


def _dump(obj) -> bytes:
    return (json.dumps(obj, indent=_INDENT, sort_keys=True) + "\n").encode()


# ------------------------------------------------------------- the record

EQUATIONS = (
    "rho_c dT/dt = k d2T/dx2 + q, for 0 < x < L and t > 0",
    "dT/dx = 0 at x = 0: the face is insulated (the slab's symmetry plane)",
    "-k dT/dx = h (T - T_inf) at x = L: convection to the surroundings",
    "T = T0 at t = 0, everywhere")
EXACT = ("T = T_s(x) + sum_n c_n exp(-k t z_n^2 / (rho_c L^2)) cos(z_n x / L),"
         " T_s = T_inf + q L / h + q (L^2 - x^2) / (2 k),"
         " z_n tan z_n = h L / k,"
         " c_n the projection of T0 - T_s on cos(z_n x / L)")
ASSUMPTIONS = (
    "conduction in one dimension, with k and rho_c constant",
    "a uniform volumetric source q",
    "an insulated face at x = 0 and convection with a constant h at x = L, "
    "with no radiation",
    "a uniform initial temperature T0",
    "the inputs are exact at the documented point; the budget's parametric "
    "part is conditional on declared, illustrative spreads")
TRUST_ROOTS = (
    "the verifier, tools/verify_assurance.py, obtained independently of this "
    "package, from the repository at a commit you trust; this package does "
    "not carry it",
    "a Python 3 interpreter and its standard library",
    "for the package's origin: the sha256 of SHA256SUMS, obtained from a "
    "channel independent of the package, such as the repository's history "
    "at a commit you trust; the package is not signed, because signing needs "
    "an identity only the owner can provision",
    "for the history: a head (seq and hash) from a witness independent of "
    "the producer; none is offered here",
    "for authority: the key and role registries the history's first "
    "record declares, which the package carries and the verifier holds "
    "every record and every governed act to. Their keys are TEST "
    "identities, whose secrets anyone can derive from their names; a "
    "production registry is an owner decision")


def _reproduction() -> list:
    return [
        "1. Obtain the package: the directory docs/assurance/package/"
        f"{PACKAGE_ID} of the repository at a commit you trust, and "
        "tools/verify_assurance.py from that commit or another you trust.",
        "2. Verify its integrity and origin: python3 -I verify_assurance.py "
        "PACKAGE --expect-package-digest DIGEST, with DIGEST the sha256 of "
        "SHA256SUMS as your trusted channel gives it.",
        "3. Identify its dependencies: verifying needs only Python 3 and its "
        "standard library. Re-running the model needs the repository at a "
        "commit whose model closure digests to the record's "
        "implementation_digest, with its locked environment (uv sync).",
        "4. Run the independent check: the verifier's RECHECK level solves "
        "the stated equations in closed form and reports what it measured.",
        "5. Inspect the assumptions: the record's mathematical_statement and "
        "assumptions, the budget's contributions and the check's limitations "
        "(evidence files named in the record).",
        "6. Confirm the authority decision: the verifier's DECISION level; "
        "with the repository, python tools/assurance_package.py --check also "
        "re-runs the model at the recorded inputs.",
        "7. Corrupt an artifact and observe the refusal: change one byte of "
        "any file of a copy and run the verifier again; it refuses at "
        "INTEGRITY and names the file.",
    ]


def _readme() -> bytes:
    """The guide. It cites no digest: the record holds them, and a guide
    that restated one could disagree with it."""
    steps = "\n".join(_reproduction())
    gaps = "\n".join(f"- {k}: {v}" for k, v in V.NOT_ESTABLISHED)
    roots = "\n".join(f"- {r}" for r in TRUST_ROOTS)
    return f"""# Assurance package: the slab's mid-plane temperature

A portable package of evidence for one answer: whether the transient slab's
temperature at x = L/2 stays below 1000 K at the documented point. It is
evidence and authorizes nothing (NON_AUTHORITATIVE), and it is a statement
about a model (MODEL_ONLY): no temperature of a real slab was observed.

The record is `assurance_record.json`. The run's event log is
`history/log.jsonl`. Each piece of evidence is `evidence/<sha256>`, named by
its own digest, and the record says which is which. `SHA256SUMS` lists
every other file.

## Checking it

    python3 -I verify_assurance.py PATH/TO/THIS/DIRECTORY \\
        [--expect-package-digest SHA256] [--expect-head SEQ:SHA256]

The verifier imports the Python standard library and nothing else. Obtain
it independently of this package: a package cannot vouch for its own
checker. It reports seven levels (INTEGRITY, CONSISTENCY, HISTORY,
AUTHENTICATION, AUTHORIZATION, DECISION, RECHECK) and exits 0 only when every
one holds and every expectation you supplied matched. At AUTHENTICATION and
AUTHORIZATION it holds every record of the history to a key, and every act
the role registry governs to a grant, under the two registries the
history's first record declares and this package carries. At RECHECK it
solves the slab equations itself, in closed form, and holds the package's
numbers to its own.

## What you must trust

{roots}

## What no offline check establishes

The verifier lists these on every run. Without the two expectations, a
package anyone assembled consistently passes every level: consistency is
not origin.

{gaps}

## Reproducing it

{steps}
""".encode()


# ------------------------------------------------------------- the build

def _find(events, action, **where):
    out = [e for e in events if e.action == action and all(
        (e.target if k == "target" else e.payload.get(k)) == v
        for k, v in where.items())]
    if not out:
        raise SystemExit(f"the run recorded no {action} {where}")
    return out


def build(out: Path, *, name: str | None = None) -> dict:
    """Run the objective, gather its evidence and write a package to
    ``out``, which must not exist. The package is then verified in a
    process of its own, and a package that does not verify is not left."""
    import adversarial_eval as E
    import slab_uq as Q

    from agent_substrate.reconstruct import derive_decision_proof

    out = Path(out)
    if out.exists():
        raise SystemExit(f"{out} exists; a package is written whole, or not")
    budget = Q.derive()
    world = E.World(name or f"assurance-{os.getpid()}", E.legitimate(),
                    governed=True)
    try:
        world.controller().run(E.OBJECTIVE)
        events = world.events()
        obj = _find(events, "research.objective", target=E.OBJECTIVE)[-1]
        hyp = _find(events, "research.hypothesis", target=HYPOTHESIS)[-1]
        verdict = _find(events, "research.status", target=HYPOTHESIS,
                        status="SUPPORTED")[-1]
        seen = [e for e in events if e.action == "research.observation"
                and e.target in verdict.payload["basis"]]
        rid = seen[0].payload["observation"]["record_id"]
        create = _find(events, "record.create", target=rid)[-1]
        verified = _find(events, "record.transition", record_id=rid,
                         dst="VERIFIED")[-1]
        final = _find(events, "record.transition", record_id=rid)[-1]
        ev = dict(create.payload["evidence"])
        ev["verification_report"] = \
            verified.payload["evidence"]["verification_report"]
        blobs = {role: world.evidence.get(d) for role, d in ev.items()}
        bundle = json.loads(blobs["result_bundle"])
        ev["temperature_field"] = bundle["artifacts"][0]["digest"]
        blobs["temperature_field"] = world.evidence.get(
            ev["temperature_field"])
        blobs["error_budget"] = _dump(budget)
        for role, at in (("decision_proof_verified", verified),
                         ("decision_proof_supported", verdict)):
            blobs[role] = _dump(derive_decision_proof(
                world.log, at.seq, evidence=world.evidence,
                **world.roots()))
        log_bytes = (world.base / "log.jsonl").read_bytes()
        # the history's roots, as the package carries them: each record's
        # attestation, and the two registries its first record declares
        if world.attestations is None:
            raise SystemExit("the package is built from a governed world, "
                             "and this one has no attestations")
        roots_files = {
            V.ATTESTATIONS: world.attestations.path.read_bytes(),
            V.KEY_REGISTRY: _dump(
                world.log.authenticator.registry.to_document()),
            V.ROLE_REGISTRY: _dump(world.log.roles.to_document())}
    finally:
        world.clean()
    ev = {role: V.sha(b) for role, b in blobs.items()}
    ver = json.loads(blobs["verification_report"])
    env = bundle["provenance"]["environment"]
    th = next(o for o in bundle["outputs"] if o["name"] == "T_at_half")
    q = th["quantity"]
    b = budget["budget"]
    record = {
        "schema": V.RECORD_SCHEMA,
        "package_id": PACKAGE_ID,
        "verifier": {"program": "tools/verify_assurance.py",
                     "version": V.VERSION,
                     "obtain": "from the repository at a commit you trust; "
                               "never from this package, which does not "
                               "carry it"},
        "question": {"objective_id": obj.target,
                     "statement": obj.payload["objective"]["statement"],
                     "asked_by": obj.actor, "registered_seq": obj.seq},
        "hypothesis": {"hypothesis_id": hyp.target,
                       "statement": hyp.payload["hypothesis"]["statement"],
                       "prediction": hyp.payload["hypothesis"]["prediction"],
                       "proposed_by": hyp.payload["hypothesis"][
                           "proposed_by"]},
        "mathematical_statement": {
            "problem": V.PROBLEM, "equations": list(EQUATIONS),
            "output": "T_at_half = T(L/2, t_end)",
            "exact_solution": EXACT,
            "discretization": "the model: cell-centred finite volume with "
                              "Crank-Nicolson in time, at n_cells and "
                              "n_steps; samples interpolated linearly "
                              "between cell centres"},
        "assumptions": list(ASSUMPTIONS),
        "model": {"model_id": bundle["model_id"],
                  "model_version": bundle["model_version"],
                  "implementation_digest": bundle["implementation_digest"],
                  "method": bundle["convergence"]["method"]},
        "inputs": {k: {"value": v, "unit": V.UNITS[k]}
                   for k, v in sorted(bundle["parameters"].items())},
        "toolchain": {"stated_by": "producer",
                      "environment_digest": bundle["environment_digest"],
                      "python": env.get("python"),
                      "implementation": env.get("implementation"),
                      "machine": env.get("machine"),
                      "distributions": env.get("distributions")},
        "results": {"T_at_half": {k: q[k] for k in (
            "value", "unit", "resolution", "resolution_basis")}},
        "error_budget": {"evidence": "error_budget",
                         "total_half_width": b["total"]["half_width"],
                         "statement": b["statement"]},
        "independent_checks": [
            {"check_id": ver["check_id"], "evidence": "verification_report",
             "status": ver["status"], "independence": ver["independence"],
             "establishes": ver["establishes"],
             "run_by": "the producer's governed check task, verified "
                       "separately before the reviewer decided"},
            {"check_id": "thermal.slab_series", "evidence": "error_budget",
             "status": "MEASURED",
             "establishes": "the model's discretization error at the point, "
                            "against the closed-form series",
             "run_by": "tools/slab_uq.py"},
            {"check_id": "verify_assurance.exact_solution", "evidence": None,
             "status": "RUN_BY_THE_VERIFIER",
             "establishes": "agreement with the verifier's own closed-form "
                            "solution, and the verdict it gives",
             "run_by": "whoever runs the verifier: the result is in their "
                       "report, never in this record"}],
        "formal": {"status": "NOT_RUN",
                   "reason": "no machine-checked proof of any statement here "
                             "exists: the Lean toolchain was not permitted to "
                             "execute where this was produced, an owner "
                             "decision that is pending"},
        "evidence": ev,
        "authorization": {
            "result_record": {"record_id": rid,
                              "kind": create.payload["kind"],
                              "proposer": create.payload["proposer"],
                              "created_seq": create.seq},
            "decisions": [
                {"name": "verified", "seq": verified.seq,
                 "hash": verified.hash, "action": verified.action,
                 "actor": verified.actor, "role": verified.payload["role"],
                 "outcome": "VERIFIED", "proof": "decision_proof_verified"},
                {"name": "supported", "seq": verdict.seq,
                 "hash": verdict.hash, "action": verdict.action,
                 "actor": verdict.actor, "outcome": "SUPPORTED",
                 "proof": "decision_proof_supported"}],
            "final_state": final.payload["dst"],
            "roots": _roots(roots_files)},
        "history": {"log": V.LOG, "events": len(events),
                    "head": {"seq": events[-1].seq,
                             "hash": events[-1].hash},
                    "anchored_by": "nothing outside this package: compare "
                                   "the head with one obtained "
                                   "independently (--expect-head)"},
        "establishment": {
            "FORMALLY_PROVED": "NOT_RUN: no proof exists",
            "NUMERICALLY_VERIFIED": "CLAIMED: an independent method-of-lines "
                                    "check passed, and the closed-form series "
                                    "measured the discretization; the "
                                    "verifier re-derives the agreement",
            "INDEPENDENTLY_REPRODUCED": "NOT_ESTABLISHED: no party but the "
                                        "producer has run the verifier on "
                                        "this package; a reader who runs it "
                                        "holds their own evidence, which "
                                        "this record does not claim",
            "EXPERIMENTALLY_VALIDATED": "NOT_RUN: no temperature of a real "
                                        "slab was observed",
            "MODEL_ASSESSED": "NOT_RUN: every proposal came from a scripted "
                              "agent; no language model assessed anything"},
        "authority": "NON_AUTHORITATIVE",
        "trust_roots": list(TRUST_ROOTS),
        "not_established": [{"id": k, "why": v}
                            for k, v in V.NOT_ESTABLISHED],
        "reproduction": _reproduction(),
    }
    files = {V.RECORD: _dump(record), V.README: _readme(), V.LOG: log_bytes,
             **roots_files}
    files.update({V.EVIDENCE + ev[r]: blobs[r] for r in ev})
    _write(out, files)
    rc, report = run_verifier(out)
    if rc != 0:
        shutil.rmtree(out, ignore_errors=True)
        raise SystemExit("the package built does not verify:\n"
                         + json.dumps(report, indent=1)[:4000])
    return record


def _roots(roots_files: dict) -> dict:
    """What the record says of the history's roots: the profile, its
    requirement, and each registry's file and the digest of its document,
    which is what the history's first record declares."""
    def digest(rel):
        return V.sha(V.canonical(json.loads(roots_files[rel])))
    return {"profile": V.GOVERNED, "requirement": V.GOVERNED_REQUIREMENT,
            "attestations": V.ATTESTATIONS,
            "key_registry": {"file": V.KEY_REGISTRY,
                             "sha256": digest(V.KEY_REGISTRY)},
            "role_registry": {"file": V.ROLE_REGISTRY,
                              "sha256": digest(V.ROLE_REGISTRY)}}


def _write(out: Path, files: dict) -> None:
    for rel, data in sorted(files.items()):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    sums = "".join(f"{V.sha(files[rel])}  {rel}\n" for rel in sorted(files))
    (out / V.SUMS).write_bytes(sums.encode("ascii"))


def run_verifier(package: Path, *extra: str) -> tuple:
    """The verifier in a process of its own: ``python -I``, an empty
    environment, a working directory outside the repository."""
    with tempfile.TemporaryDirectory() as cwd:
        r = subprocess.run([sys.executable, "-I", str(VERIFIER),
                            str(package), *extra], capture_output=True,
                           text=True, env={}, cwd=cwd, timeout=600)
    try:
        report = json.loads(r.stdout) if r.stdout.strip() else {}
    except ValueError:
        report = {"stdout": r.stdout[-2000:]}
    report["stderr"] = r.stderr[-2000:]
    return r.returncode, report


# ------------------------------------------------------------- forgeries

def _swap(obj, mapping: dict):
    """``obj`` with every string ``mapping`` names replaced, keys included:
    a forger renaming digests and hashes everywhere at once."""
    if isinstance(obj, str):
        return mapping.get(obj, obj)
    if isinstance(obj, list):
        return [_swap(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {_swap(k, mapping): _swap(v, mapping) for k, v in obj.items()}
    return obj


def load(package: Path) -> dict:
    """A package as a forger holds it: every part editable, the registries
    included."""
    package = Path(package)
    record = json.loads((package / V.RECORD).read_bytes())
    ev = record["evidence"]
    state = {"record": record,
             "key_registry": json.loads(
                 (package / V.KEY_REGISTRY).read_bytes()),
             "role_registry": json.loads(
                 (package / V.ROLE_REGISTRY).read_bytes()),
             "events": [json.loads(line) for line in
                        (package / V.LOG).read_bytes().splitlines()],
             "field": (package / V.EVIDENCE /
                       ev["temperature_field"]).read_bytes(),
             "readme": (package / V.README).read_bytes(),
             "json": {r: json.loads((package / V.EVIDENCE / d).read_bytes())
                      for r, d in ev.items() if r != "temperature_field"}}
    state["original"] = copy.deepcopy({
        "evidence": dict(ev),
        "subject": V.sha(V.canonical(state["json"]["result_bundle"])),
        "hashes": [e["hash"] for e in state["events"]]})
    return state


def _sign(events: list) -> bytes:
    """An attestation file for ``events``, each record signed with the TEST
    identity of its actor: what anyone can do, since a TEST identity's
    secret is derived from its name."""
    from agent_substrate import principals as P
    lines = []
    for e in events:
        signer = P.test_identity(e["actor"])
        lines.append(V.canonical({
            "seq": e["seq"], "event_hash": e["hash"],
            "key_id": signer.key_id,
            "sig": signer.provider.sign(
                signer.secret, P.DOMAIN + e["hash"].encode("ascii")).hex()})
            + b"\n")
    return b"".join(lines)


def forge(state: dict, out: Path) -> None:
    """Write ``state`` as a package a forger who rewrites everything would:
    each digest the edits changed renamed wherever it is cited, the log
    re-chained with its first record declaring the registries the package
    now carries, every record signed again with its actor's TEST key, its
    hashes renamed in the proofs and the record, and SHA256SUMS recomputed.
    Only what the verifier computes for itself, and what it is given from
    outside, are beyond this forger's reach."""
    orig, js = state["original"], state["json"]
    mapping: dict = {}

    def rename(old, new):
        if old != new:
            mapping[old] = new

    blobs = {"temperature_field": state["field"]}
    rename(orig["evidence"]["temperature_field"], V.sha(state["field"]))
    for role in ("result_bundle", "run_identity", "verification_report",
                 "error_budget"):
        obj = js[role] = _swap(js[role], mapping)
        if role == "result_bundle":
            rename(orig["subject"], V.sha(V.canonical(obj)))
        blobs[role] = _dump(obj)
        rename(orig["evidence"][role], V.sha(blobs[role]))
    roots_files = {V.KEY_REGISTRY: _dump(state["key_registry"]),
                   V.ROLE_REGISTRY: _dump(state["role_registry"])}
    genesis = state["events"][0]["payload"]
    genesis["key_registry"] = V.sha(V.canonical(state["key_registry"]))
    genesis["role_registry"] = V.sha(V.canonical(state["role_registry"]))
    prev, lines, chained = "0" * 64, [], []
    for old, e in zip(orig["hashes"], state["events"]):
        e = _swap(e, mapping)
        e["prev_hash"] = prev
        e["hash"] = V.sha(V.canonical({f: e[f] for f in V._EVENT_FIELDS}))
        rename(old, e["hash"])
        prev = e["hash"]
        chained.append(e)
        lines.append(V.canonical(e) + b"\n")
    roots_files[V.ATTESTATIONS] = _sign(chained)
    for role in ("decision_proof_verified", "decision_proof_supported"):
        js[role] = _swap(js[role], mapping)
        blobs[role] = _dump(js[role])
        rename(orig["evidence"][role], V.sha(blobs[role]))
    record = _swap(state["record"], mapping)
    record["authorization"]["roots"] = _roots(roots_files)
    files = {V.RECORD: _dump(record), V.README: state["readme"],
             V.LOG: b"".join(lines), **roots_files}
    files.update({V.EVIDENCE + V.sha(b): b for b in blobs.values()})
    _write(Path(out), files)


# ------------------------------------------------------------- the check

def _first_failure(report: dict) -> tuple:
    for lv in report.get("levels") or ():
        for c in lv["checks"]:
            if c["status"] == "FAIL":
                return lv["level"], c["check"]
    return None, None


def _resum(package: Path) -> None:
    """SHA256SUMS written again for the files as they are: no forger is
    stopped by a manifest it can rewrite."""
    files = {p.relative_to(package).as_posix(): p.read_bytes()
             for p in sorted(package.rglob("*"))
             if p.is_file() and p.name != V.SUMS}
    (package / V.SUMS).write_bytes("".join(
        f"{V.sha(files[r])}  {r}\n" for r in sorted(files)).encode("ascii"))


def _unsign_the_verification(package: Path) -> None:
    """The verification's attestation removed, and nothing else: a record
    no key vouches for."""
    record = json.loads((package / V.RECORD).read_bytes())
    h = next(d["hash"] for d in record["authorization"]["decisions"]
             if d["name"] == "verified")
    att = package / V.ATTESTATIONS
    att.write_bytes(b"".join(line + b"\n" for line in
                             att.read_bytes().splitlines()
                             if json.loads(line).get("event_hash") != h))
    _resum(package)


def _substitute_the_roles(package: Path) -> None:
    """A role registry granting the colleague VERIFIER put in the place of
    the one the history declares, with the record's digest of it brought
    along: the history's first record still names the other."""
    import adversarial_eval as E
    doc = json.loads((package / V.ROLE_REGISTRY).read_bytes())
    doc["grants"] = sorted(doc["grants"] + [{
        "grant_id": "g-substituted", "principal": E.OTHER,
        "role": "VERIFIER", "scope": ["kind:scientific_result"],
        "valid_from_seq": 0, "valid_until_seq": None,
        "revoked_at_seq": None, "revocation_reason": None,
        "may_delegate": False}], key=lambda g: g["grant_id"])
    (package / V.ROLE_REGISTRY).write_bytes(_dump(doc))
    record = json.loads((package / V.RECORD).read_bytes())
    record["authorization"]["roots"]["role_registry"]["sha256"] = \
        V.sha(V.canonical(doc))
    (package / V.RECORD).write_bytes(_dump(record))
    _resum(package)


def controls(package: Path) -> list:
    """Corrupted copies, each of which must be refused where it should be:
    (what, the level that must refuse it, the verifier's exit status, the
    first failing check, the level it failed at)."""
    out: list = []
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for rel in sorted(p.relative_to(package).as_posix()
                          for p in package.rglob("*") if p.is_file()):
            copy_ = tmp / f"byte-{len(out)}"
            shutil.copytree(package, copy_)
            target = copy_ / rel
            data = bytearray(target.read_bytes())
            data[len(data) // 2] ^= 0x01
            target.write_bytes(bytes(data))
            rc, report = run_verifier(copy_)
            out.append((f"one byte of {rel}", "INTEGRITY", rc,
                        *_first_failure(report)[::-1]))

        def warmer(s):
            """The temperature moved by 0.01 K everywhere it is stated: far
            inside any physical doubt, and thirty times the criterion. The
            observation's resolution, which the move changes, is the one
            the rule then gives -- max(r_producer, |v_producer - v_check| +
            r_check) (AUD-025) -- so only the recheck can refuse it."""
            th = next(o for o in s["json"]["result_bundle"]["outputs"]
                      if o["name"] == "T_at_half")["quantity"]
            new = th["value"] + 0.01
            th["value"] = new
            s["record"]["results"]["T_at_half"]["value"] = new
            s["json"]["error_budget"]["budget"]["value"] = new
            agreed = dict((n, q) for n, q in s["json"][
                "verification_report"].get("agreement") or ())
            r = th["resolution"]
            if "T_at_half" in agreed:
                q = agreed["T_at_half"]
                r = max(r, abs(new - q["value"]) + q["resolution"])
            for e in s["events"]:
                obs = (e["payload"].get("observation") or {})
                if "T_at_half" in (obs.get("values") or {}):
                    obs["values"]["T_at_half"].update(value=new,
                                                      resolution=r)
            for x in s["json"]["decision_proof_supported"]["basis"]:
                x["values"]["T_at_half"].update(value=new, resolution=r)

        def decided_by(s, who):
            """The result's verification made by ``who``, wherever the
            package states its maker; every record re-signed by the
            forger."""
            rid = s["record"]["authorization"]["result_record"]
            seq = next(d["seq"] for d in s["record"]["authorization"][
                "decisions"] if d["name"] == "verified")
            s["events"][seq]["actor"] = who
            next(d for d in s["record"]["authorization"]["decisions"]
                 if d["name"] == "verified")["actor"] = who
            p = s["json"]["decision_proof_verified"]
            p["decision"]["actor"] = who
            p["separation_of_duties"] = {"decider": who,
                                         "distinct": who != rid["proposer"],
                                         "proposer": rid["proposer"]}

        def self_verified(s):
            """The proposer verifies its own result, and holds no grant to
            verify anything."""
            decided_by(s, s["record"]["authorization"]["result_record"][
                "proposer"])

        def ungranted_verifier(s):
            """The colleague verifies the result: a party the key registry
            holds, with a genuine key, whom the role registry grants
            nothing. Authenticated, every record of the history included;
            not authorized."""
            import adversarial_eval as E
            decided_by(s, E.OTHER)

        def granted_self_verifier(s):
            """The proposer is granted VERIFIER over the result's kind, by
            the registry the history declares, and verifies its own result:
            every act authorized, and separation of duties is all that
            refuses it."""
            rid = s["record"]["authorization"]["result_record"]
            s["role_registry"]["grants"] = sorted(
                s["role_registry"]["grants"] + [{
                    "grant_id": "g-forged-self-review",
                    "principal": rid["proposer"], "role": "VERIFIER",
                    "scope": [f"kind:{rid['kind']}"], "valid_from_seq": 0,
                    "valid_until_seq": None, "revoked_at_seq": None,
                    "revocation_reason": None, "may_delegate": False}],
                key=lambda g: g["grant_id"])
            decided_by(s, rid["proposer"])

        def revoked_before(s):
            """The reviewer's grant is revoked at the verification's
            position, by the registry the history declares: the review
            before it stands, and the verification stands on a lapsed
            grant."""
            d = next(d for d in s["record"]["authorization"]["decisions"]
                     if d["name"] == "verified")
            for g in s["role_registry"]["grants"]:
                if g["principal"] == d["actor"] and g["role"] == "VERIFIER":
                    g.update(revoked_at_seq=d["seq"],
                             revocation_reason="withdrawn before the "
                                               "verification")

        for what, edit, level in (
                ("a consistent forgery of the temperature", warmer,
                 "RECHECK"),
                ("a consistent forgery of the deciding party, who holds no "
                 "grant", self_verified, "AUTHORIZATION"),
                ("a verification signed with a genuine key and no grant",
                 ungranted_verifier, "AUTHORIZATION"),
                ("a grant revoked before the verification", revoked_before,
                 "AUTHORIZATION"),
                ("the proposer granted VERIFIER, verifying its own result",
                 granted_self_verifier, "DECISION"),
                ("a consistent forgery that changes nothing", None, None)):
            s = load(package)
            if edit is not None:
                edit(s)
            forged = tmp / f"forged-{len(out)}"
            forge(s, forged)
            rc, report = run_verifier(forged)
            out.append((what, level, rc, *_first_failure(report)[::-1]))
            if edit is None:
                for what2, change in (
                        ("a record whose attestation is removed",
                         _unsign_the_verification),
                        ("a role registry substituted, the history's "
                         "declaration left", _substitute_the_roles)):
                    changed = tmp / f"changed-{len(out)}"
                    shutil.copytree(forged, changed)
                    change(changed)
                    rc, report = run_verifier(changed)
                    out.append((what2, "AUTHENTICATION", rc,
                                *_first_failure(report)[::-1]))
                with open(package / V.SUMS, "rb") as fh:
                    honest = V.sha(fh.read())
                rc, report = run_verifier(forged, "--expect-package-digest",
                                          honest)
                out.append(("the same, against the honest package's digest",
                            "ORIGIN", rc, report.get("expectations", {}).get(
                                "package_digest"), "ORIGIN"))
    return out


def currency(package: Path) -> list:
    """Where the package no longer describes the code as it is: the model's
    implementation, the check's, the committed budget, and what the model
    computes now at the recorded inputs."""
    import slab_uq as Q

    from scientific.checks import slab_mol
    from scientific.models.slab_transient import SlabTransientModel

    rec = json.loads((package / V.RECORD).read_bytes())
    ev = rec["evidence"]

    def blob(role):
        return json.loads((package / V.EVIDENCE / ev[role]).read_bytes())

    model = SlabTransientModel()
    out = []
    if rec["model"]["implementation_digest"] != model.implementation_digest():
        out.append("the model's implementation has changed since the "
                   "package was built: rebuild it (--write)")
    if blob("verification_report")["verifier_implementation_digest"] != \
            slab_mol.check_digest():
        out.append("the method-of-lines check has changed since the package "
                   "was built: rebuild it (--write)")
    committed = json.loads(Q.RESULTS.read_text(encoding="utf-8"))
    out += [f"budget{d}" for d in Q._differences(blob("error_budget"),
                                                 committed)]
    now = model.run({k: v["value"] for k, v in rec["inputs"].items()})
    then = {o["name"]: o for o in blob("result_bundle")["outputs"]}
    for o in now.outputs:
        was = (then.get(o.name) or {}).get("quantity") or {}
        if o.quantity is None or not isinstance(was.get("value"), float):
            out.append(f"{o.name}: no value now or then")
        elif abs(o.quantity.value - was["value"]) > 1e-9 * max(
                1.0, abs(was["value"])):
            out.append(f"{o.name}: {o.quantity.value!r} now, "
                       f"{was['value']!r} in the package")
    return out


def check(package: Path = PACKAGE) -> int:
    rc, report = run_verifier(package)
    print(f"verifier: exit {rc}, {report.get('verdict')}; levels "
          + ", ".join(f"{lv['level']} {lv['status']}"
                      for lv in report.get("levels") or ()))
    problems = []
    if rc != 0:
        problems.append(f"the committed package does not verify: "
                        f"{report.get('stderr', '').strip()[-600:]}")
    else:
        m = report.get("measured") or {}
        print(f"  exact T(L/2) = {m.get('T_exact_at_half_K')!r} K, model - "
              f"exact = {m.get('model_minus_exact_K')!r} K, verdict at the "
              f"point {m.get('verdict_at_the_point')}, margin "
              f"{m.get('margin_K')!r} K")
        print(f"  not established: {len(report['not_established'])}")
    stale = currency(package)
    problems += stale
    print(f"currency: {'current' if not stale else stale}")
    for what, level, code, check_, at in controls(package):
        ok = code == 1 and (level is None or at == level)
        if level is None:
            ok = code == 0
        print(f"  control: {what}: exit {code} at {at}: {check_} "
              f"{'(as it must)' if ok else '(WRONG)'}")
        if not ok:
            problems.append(f"control {what!r}: exit {code} at {at}, "
                            f"expected {level or 'a pass'}")
    for p in problems:
        print(f"PROBLEM: {p}")
    print(f"assurance package: {len(problems)} problem(s)")
    return 1 if problems else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--out", type=Path)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    if args.check:
        return check()
    if args.write:
        tmp = PACKAGE.with_name(PACKAGE.name + ".new")
        shutil.rmtree(tmp, ignore_errors=True)
        build(tmp, name="assurance-package")
        shutil.rmtree(PACKAGE, ignore_errors=True)
        tmp.rename(PACKAGE)
        print(f"wrote {PACKAGE.relative_to(ROOT)}")
        return 0
    build(args.out)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
