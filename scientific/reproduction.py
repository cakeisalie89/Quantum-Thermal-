"""Which question a regenerated corpus answers: reproduction, or stability.

Four properties that one boolean used to carry:

1. PACKAGE INTEGRITY -- the declared outputs, complete, no foreign ones,
   readable, rooted. The caller's; this module takes it as given.
2. BYTE REPRODUCTION -- under a RESOLVED numerical backend that a witness
   profile has seen regenerate this exact corpus byte-for-byte, did the
   regeneration reproduce it? Any byte difference there is a FAILURE with
   no fallback: nothing about the computation was supposed to differ.
3. CROSS-ENVIRONMENT DECISION STABILITY -- under a RESOLVED backend that is
   demonstrably NOT a witness, the bytes may differ; does every structural
   and decision-bearing fact agree? (``tools/cross_env_semantics.py``.)
4. SCIENTIFIC EQUIVALENCE -- whether two different backends' numbers are
   equivalent under a model's own error criteria. Nothing here establishes
   it; every verdict that reaches 3 says ``NOT_ESTABLISHED``.

A backend that cannot be resolved answers none of them: unknown is neither
"the same" nor "different", and the verdict is a refusal.

THE WITNESS PROFILE

``{schema: byte-reproduction-witness/1, ...}`` says one thing: "this RESOLVED
runtime was observed to regenerate this exact canonical corpus,
byte-for-byte, from this generator closure and this locked environment." It
is not the historical provenance of the corpus -- the machine that first
produced it is not claimed -- and it is not evidence the numbers are right.
It binds the corpus (every non-exempt file by sha256), the exemption set,
the generator's measured input closure, the lock files, and each witnessed
backend's identity; change any of the first four and the profile no longer
applies to the tree, whatever backend is asking.

THE BACKEND IDENTITY

:func:`backend_identity` projects an environment record
(``scientific.run_identity.environment_record`` with the runtime probe) onto
what selects the arithmetic: interpreter, distributions and their native
bytes, the CPU's SIMD features and vendor (glibc's libm IFUNCs select on
them), the backend variables, what NumPy dispatched function by function,
each BLAS's kernel, build, threads and bytes, and the system libraries and
loader by their bytes. The CPU MODEL is not in it: two CPUs that offer the
same features and on which every dispatcher chose the same code run the
same instructions. Anything unresolved is None -- never a partial identity.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .identity import digest

SCHEMA = "byte-reproduction-witness/1"

POLICY_CI = "ci"
POLICY_STRICT = "strict-reproduction"
POLICIES = (POLICY_CI, POLICY_STRICT)

CONSISTENT, REFUSED = "CONSISTENT", "REFUSED"
RESOLVED, UNRESOLVED = "RESOLVED", "UNRESOLVED"

BYTE_IDENTICAL = "BYTE_IDENTICAL"
DIFFERENT_RESOLVED_BACKEND = "DIFFERENT_RESOLVED_BACKEND"
BYTE_DRIFT_COMPARABLE_BACKEND = "BYTE_DRIFT_COMPARABLE_BACKEND"
BACKEND_UNRESOLVED = "BACKEND_UNRESOLVED"
WITNESS_INVALID = "WITNESS_INVALID"
WITNESS_INAPPLICABLE = "WITNESS_INAPPLICABLE"
REFERENCE_ENVIRONMENT_REQUIRED = "CANONICAL_REPRODUCTION_ENVIRONMENT_REQUIRED"
NOTHING_COMPARED = "NOTHING_COMPARED"
NOT_CHECKED = "NOT_CHECKED"

NOT_NEEDED_BYTE_IDENTICAL = "NOT_NEEDED_BYTE_IDENTICAL"
DECISION_STABLE = "DECISION_STABLE_WITH_NUMERIC_DRIFT"

ESTABLISHED_BY_MODEL_POLICY = "ESTABLISHED_BY_MODEL_POLICY"
NOT_ESTABLISHED = "NOT_ESTABLISHED"
NOT_APPLICABLE = "NOT_APPLICABLE"

WHAT_THIS_IS = (
    "A byte-reproduction WITNESS: each witnessed runtime below was observed "
    "to regenerate exactly this canonical corpus, byte for byte, from this "
    "generator closure and this locked environment.")
DOES_NOT_MEAN = (
    "Not the historical provenance of the corpus: the machine that first "
    "produced it is not claimed. Not evidence that any number is correct, "
    "measured or validated. Not scientific equivalence between backends. "
    "The corpus is the legacy QTA hardware forecast's: MODEL-ONLY / "
    "FORECAST-ONLY, its historical PASS_count 0.")


def sha256_file(path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def manifest(root: Path, names) -> dict:
    """``{name: sha256}`` for each of ``names`` under ``root``; an unreadable
    or missing file maps to None, which no profile matches."""
    return {n: sha256_file(Path(root) / n) for n in sorted(names)}


def manifest_digest(files: dict) -> str:
    return digest({"files": dict(sorted(files.items()))})


# ---- the backend ------------------------------------------------------------
def backend_identity(record: dict | None) -> dict | None:
    """What selects the arithmetic, from an environment record -- or None
    when any of it is unresolved."""
    if not isinstance(record, dict) or \
            record.get("backend_status") != RESOLVED:
        return None
    runtime = record.get("runtime")
    cpu = record.get("cpu") or {}
    if not isinstance(runtime, dict) or runtime.get("status") != RESOLVED:
        return None
    if cpu.get("source") != "cpuinfo" or runtime.get("numpy") is None:
        return None
    core = cpu.get("core") or {}
    return {
        "python": record.get("python"),
        "implementation": record.get("implementation"),
        "machine": record.get("machine"),
        "distributions": record.get("distributions"),
        "native": {d: {k: n.get(k) for k in ("status", "native_files",
                                             "native_sha256",
                                             "installed_sha256")}
                   for d, n in sorted((record.get("native") or {}).items())},
        "cpu_simd": cpu.get("simd"),
        "cpu_vendor": core.get("vendor_id") or core.get("CPU implementer"),
        "variables": (record.get("backend") or {}).get("variables"),
        "numpy": runtime.get("numpy"),
        "blas": runtime.get("blas"),
        "system_libraries": runtime.get("system_libraries"),
        "interpreter": runtime.get("interpreter"),
    }


def unresolved_parts(record: dict | None) -> list:
    """Why :func:`backend_identity` is None, as far as the record says."""
    if not isinstance(record, dict):
        return ["no environment record for the process that regenerated"]
    parts = []
    runtime = record.get("runtime")
    if not isinstance(runtime, dict):
        parts.append("no runtime probe")
    else:
        parts += [f"runtime: {u}" for u in runtime.get("unresolved", [])]
        if runtime.get("numpy") is None:
            parts.append("no NumPy backend in the regenerating process")
    for d, n in sorted((record.get("native") or {}).items()):
        if n.get("status") not in (RESOLVED, "ABSENT"):
            parts.append(f"native {d}: {n.get('status')}")
    if (record.get("cpu") or {}).get("source") != "cpuinfo":
        parts.append("the CPU's features could not be read")
    return parts or [f"backend_status {record.get('backend_status')!r}"]


def identity_digest(identity: dict) -> str:
    return digest(identity)


def differences(a: dict, b: dict, prefix: str = "") -> list:
    """Dotted keys at which two identities differ, two levels deep."""
    out = []
    for k in sorted(set(a) | set(b)):
        x, y = a.get(k), b.get(k)
        if x == y:
            continue
        if isinstance(x, dict) and isinstance(y, dict) and not prefix:
            out += differences(x, y, f"{k}.")
        else:
            out.append(prefix + k)
    return out


# ---- the profile ------------------------------------------------------------
def build_profile(*, declared, exempt, corpus: dict, closure: dict,
                  lock: dict, entry: str, argv, record: dict,
                  observed: dict) -> dict:
    """A new profile with one witness. Refuses (ValueError) anything that
    would make it a claim no observation supports."""
    identity = backend_identity(record)
    if identity is None:
        raise ValueError("the regenerating backend is UNRESOLVED: "
                         + "; ".join(unresolved_parts(record)))
    declared, exempt = sorted(declared), sorted(exempt)
    if set(corpus) != set(declared) - set(exempt) or \
            any(v is None for v in corpus.values()):
        raise ValueError("the corpus must be every declared, non-exempt "
                         "file, each read")
    if not closure or any(v is None for v in closure.values()):
        raise ValueError("an empty or unreadable generator closure binds "
                         "nothing")
    return {
        "schema": SCHEMA, "what_this_is": WHAT_THIS_IS,
        "does_not_mean": DOES_NOT_MEAN,
        "corpus": {"declared": declared, "exempt": exempt,
                   "files": dict(sorted(corpus.items())),
                   "digest": manifest_digest(corpus)},
        "generator": {"entry": entry, "argv": list(argv),
                      "closure": dict(sorted(closure.items())),
                      "digest": manifest_digest(closure)},
        "environment": {"lock": dict(sorted(lock.items())),
                        "digest": manifest_digest(lock)},
        "witnesses": [_witness(identity, observed)],
    }


def _witness(identity: dict, observed: dict) -> dict:
    return {"backend": identity, "backend_digest": identity_digest(identity),
            "observed": dict(observed)}


def add_witness(profile: dict, record: dict, observed: dict) -> dict:
    """The same corpus, closure and lock, reproduced on another backend."""
    identity = backend_identity(record)
    if identity is None:
        raise ValueError("the regenerating backend is UNRESOLVED")
    d = identity_digest(identity)
    out = dict(profile)
    out["witnesses"] = [w for w in profile["witnesses"]
                        if w["backend_digest"] != d] + [
        _witness(identity, observed)]
    return out


def validate_profile(doc) -> list:
    """Problems with a profile as a document -- before asking whether it
    applies to anything. Every digest in it is recomputed, never read."""
    if not isinstance(doc, dict):
        return ["the profile is not a JSON object"]
    if doc.get("schema") != SCHEMA:
        return [f"schema is {doc.get('schema')!r}, not {SCHEMA!r}"]
    problems = []
    corpus, gen, env = (doc.get("corpus"), doc.get("generator"),
                        doc.get("environment"))
    for name, part in (("corpus", corpus), ("generator", gen),
                       ("environment", env)):
        if not isinstance(part, dict):
            problems.append(f"{name} is missing")
    if problems:
        return problems
    try:
        declared, exempt = set(corpus["declared"]), set(corpus["exempt"])
        files = corpus["files"]
        if not declared or not isinstance(files, dict) or not files:
            problems.append("the corpus declares nothing")
        if not exempt <= declared:
            problems.append("an exemption names an undeclared file")
        if set(files) != declared - exempt:
            problems.append("the corpus files are not exactly the declared, "
                            "non-exempt set")
        if manifest_digest(files) != corpus["digest"]:
            problems.append("the corpus digest does not match its files")
        closure = gen["closure"]
        if not isinstance(closure, dict) or not closure:
            problems.append("the generator closure is empty")
        elif manifest_digest(closure) != gen["digest"]:
            problems.append("the generator digest does not match its "
                            "closure")
        if manifest_digest(env["lock"]) != env["digest"]:
            problems.append("the environment digest does not match its "
                            "lock")
        witnesses = doc.get("witnesses")
        if not isinstance(witnesses, list) or not witnesses:
            problems.append("no witness")
            witnesses = []
        seen = set()
        for i, w in enumerate(witnesses):
            b = w.get("backend") if isinstance(w, dict) else None
            if not isinstance(b, dict) or identity_digest(b) != \
                    w.get("backend_digest"):
                problems.append(f"witness {i}: the backend digest does not "
                                "match the identity it claims")
                continue
            if w["backend_digest"] in seen:
                problems.append(f"witness {i} is a duplicate")
            seen.add(w["backend_digest"])
            obs = w.get("observed") or {}
            if obs.get("byte_identical") != len(files) or \
                    obs.get("compared") != len(files):
                problems.append(f"witness {i} did not observe every file "
                                "byte-identical")
    except (KeyError, TypeError, AttributeError) as exc:
        problems.append(f"malformed: {type(exc).__name__}: {exc}")
    return problems


def applicability(doc: dict, *, declared, exempt, corpus: dict,
                  closure: dict | None, lock: dict) -> list:
    """Why a VALID profile does not apply to this tree -- empty when it
    does. ``closure`` None means the generator was not run (the static
    check re-hashes the recorded closure instead)."""
    out = []
    c = doc["corpus"]
    if set(declared) != set(c["declared"]):
        out.append("DECLARED_SET_CHANGED")
    if set(exempt) != set(c["exempt"]):
        out.append("EXEMPTION_SET_CHANGED")
    if dict(sorted(corpus.items())) != c["files"]:
        changed = sorted(n for n in set(corpus) | set(c["files"])
                         if corpus.get(n) != c["files"].get(n))
        out.append(f"CORPUS_CHANGED ({len(changed)}: {changed[:5]})")
    if closure is not None and manifest_digest(closure) != \
            doc["generator"]["digest"]:
        rec = doc["generator"]["closure"]
        changed = sorted(n for n in set(closure) | set(rec)
                         if closure.get(n) != rec.get(n))
        out.append(f"GENERATOR_CHANGED ({len(changed)}: {changed[:5]})")
    if dict(sorted(lock.items())) != doc["environment"]["lock"]:
        out.append("ENVIRONMENT_LOCK_CHANGED")
    return out


def witness_for(doc: dict, identity: dict | None) -> dict | None:
    if identity is None:
        return None
    d = identity_digest(identity)
    for w in doc.get("witnesses", []):
        if w.get("backend_digest") == d:
            return w
    return None


# ---- the verdict ------------------------------------------------------------
def decide(policy: str, *, byte_identical: bool, files_compared: int,
           drift, record: dict | None, profile: dict | None,
           profile_problems, inapplicable, cross_env=None) -> dict:
    """The verdict on one regeneration.

    ``cross_env`` is a zero-argument callable returning a comparator
    report; it is called only when the question it answers is the one being
    asked -- a resolved, non-witness backend, a profile that applies, bytes
    that differ, under the ``ci`` policy."""
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}")
    drift = sorted(drift)
    v = {"policy": policy, "files_byte_compared": files_compared,
         "files_differing": len(drift), "drift": drift, "reasons": [],
         "CROSS_ENV_STATUS": NOT_CHECKED,
         "SCIENTIFIC_EQUIVALENCE_STATUS": NOT_APPLICABLE,
         "backend_digest": None, "witness": None, "cross_env": None}

    def done(package, backend, reproduction, *reasons):
        v.update(PACKAGE_STATUS=package, BACKEND_STATUS=backend,
                 REPRODUCTION_STATUS=reproduction)
        v["reasons"] += list(reasons)
        return v

    if files_compared <= 0:
        return done(REFUSED, UNRESOLVED if backend_identity(record) is None
                    else RESOLVED, NOTHING_COMPARED,
                    "no canonical file was byte-compared")
    identity = backend_identity(record)
    if identity is None:
        return done(REFUSED, UNRESOLVED, BACKEND_UNRESOLVED,
                    *unresolved_parts(record))
    v["backend_digest"] = identity_digest(identity)
    profile_ok = isinstance(profile, dict) and not profile_problems
    witness = witness_for(profile, identity) \
        if profile_ok and not inapplicable else None
    if witness is not None:
        v["witness"] = witness["backend_digest"]
    elif profile_ok:
        v["backend_differences"] = {
            w["backend_digest"][:16]: differences(w["backend"], identity)
            for w in profile.get("witnesses", [])}

    if policy == POLICY_STRICT:
        if witness is None:
            why = (["the witness profile is invalid: "
                    + "; ".join(profile_problems or ["missing"])]
                   if not profile_ok else
                   [f"the witness profile does not apply: {r}"
                    for r in inapplicable] or
                   ["this backend is not one the profile witnessed "
                    "reproducing the corpus"])
            return done(REFUSED, RESOLVED, REFERENCE_ENVIRONMENT_REQUIRED,
                        *why)
        if byte_identical:
            v["CROSS_ENV_STATUS"] = NOT_NEEDED_BYTE_IDENTICAL
            return done(CONSISTENT, RESOLVED, BYTE_IDENTICAL)
        return done(REFUSED, RESOLVED, BYTE_DRIFT_COMPARABLE_BACKEND,
                    f"{len(drift)} file(s) differ on a witnessed backend: "
                    f"{drift[:8]}")

    if byte_identical:
        v["CROSS_ENV_STATUS"] = NOT_NEEDED_BYTE_IDENTICAL
        return done(CONSISTENT, RESOLVED, BYTE_IDENTICAL)
    if not profile_ok:
        return done(REFUSED, RESOLVED, WITNESS_INVALID,
                    *(profile_problems or ["no witness profile"]))
    if inapplicable:
        return done(REFUSED, RESOLVED, WITNESS_INAPPLICABLE, *inapplicable)
    if witness is not None:
        return done(REFUSED, RESOLVED, BYTE_DRIFT_COMPARABLE_BACKEND,
                    f"{len(drift)} file(s) differ on a witnessed backend; "
                    f"there is no semantic fallback for that: {drift[:8]}")
    if cross_env is None:
        return done(REFUSED, RESOLVED, DIFFERENT_RESOLVED_BACKEND,
                    "no cross-environment comparator was supplied")
    report = cross_env()
    v["cross_env"] = report
    v["CROSS_ENV_STATUS"] = report["status"]
    v["SCIENTIFIC_EQUIVALENCE_STATUS"] = NOT_ESTABLISHED
    if report["status"] == DECISION_STABLE:
        return done(CONSISTENT, RESOLVED, DIFFERENT_RESOLVED_BACKEND)
    return done(REFUSED, RESOLVED, DIFFERENT_RESOLVED_BACKEND,
                f"cross-environment comparison: {report['status']}")


def verdict_line(v: dict) -> str:
    """The machine-readable line a CI log is read for."""
    return (f"REPRODUCTION_VERDICT policy={v['policy']} "
            f"PACKAGE_STATUS={v['PACKAGE_STATUS']} "
            f"BACKEND_STATUS={v['BACKEND_STATUS']} "
            f"REPRODUCTION_STATUS={v['REPRODUCTION_STATUS']} "
            f"CROSS_ENV_STATUS={v['CROSS_ENV_STATUS']} "
            f"SCIENTIFIC_EQUIVALENCE_STATUS="
            f"{v['SCIENTIFIC_EQUIVALENCE_STATUS']} "
            f"files_byte_compared={v['files_byte_compared']} "
            f"files_differing={v['files_differing']} "
            f"backend={(v['backend_digest'] or 'UNRESOLVED')[:16]} "
            f"witness={(v['witness'] or 'none')[:16]}")
