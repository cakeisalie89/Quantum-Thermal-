#!/usr/bin/env python3
"""Consumer release verifier (Stage 9). Fail-closed.

TWO MODES, AND THEY DIFFER IN KIND -- not merely in thoroughness.

OFFLINE (default) is STRUCTURAL ONLY. It recomputes the release zip digest,
every SHA256SUMS entry against the archive's own contents, SBOM against the
uv.lock inside the zip, provenance subjects, policy schema, a secret and
absolute-path scan, and the claim boundaries (scientific PASS must be zero).
It anchors nothing: with no local canonical policy it reports UNANCHORED, and
it never decides that a signer is authorized.

ONLINE (--online) is an AUTHORIZATION decision, and it REQUIRES AN EXTERNAL
TRUST ROOT. Pass either:

    --trusted-policy PATH            an owner-authorized policy obtained
                                     independently of this release
    --trusted-policy-sha256 DIGEST   the sha256 of that policy; the bundle's
                                     candidate copy is promoted only once its
                                     canonical bytes hash to this value

Without one, --online fails closed. There is no fallback to the working
directory, the bundle, or the archive. A release cannot authorize itself: a
malicious artifact can always carry a policy naming its own signer, so the
policies shipped with a release are CANDIDATES checked against the root.

PHASES, and no arrow points backward:

    1 trust root     external policy -> expected identity and issuer
    2 candidate      parse untrusted bundle/archive. Every malformation the
                     adversarial suite exercises -- absent, unreadable or
                     non-archive zip paths, corrupt or ambiguous archives,
                     unparseable, wrongly typed, incomplete, duplicated or
                     oversized metadata records, and decompression bombs --
                     yields a NAMED refusal rather than a traceback. That is a
                     claim about the reproduced surface, not a proof of
                     totality. NOTHING here has authority yet
    3 authenticate   Sigstore verifies the zip against the EXTERNAL identity
    4 authenticated  zip policy == root; bundle policy == root; read the
                     signed release_binding.json; recompute the payload digest
    5 auxiliary      cross-check provenance -- UNTRUSTED_AUXILIARY_METADATA,
                     never authority for repo/workflow/ref/builder/revision
    6 claims         recompute scientific PASS from the signed gate table

release_index.json is UNTRUSTED_ENVELOPE_METADATA: it is mutated after signing,
so it supplies routing and consistency assertions only.

MODEL-ONLY / FORECAST-ONLY: a signature proves origin and integrity of
software. It is never physics, hardware, or experimental validation.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import release_trust

SECRET_PATTERNS = [
    r"AKIA[0-9A-Z]{16}", r"-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----",
    r"ghp_[A-Za-z0-9]{36}", r"xox[baprs]-[A-Za-z0-9-]{10,}",
    r"(?i)aws_secret_access_key\s*=",
]
ABS_PATHS = ["/home/", "/tmp/", "C:\\\\"]
CLAIM_VIOLATIONS = ["experiment performed", "campaign performed",
                    "hardware-validated", "PLAYBOOK_EXECUTED",
                    "readiness advanced"]
SCI_PINS = {"numpy": "2.4.4", "scipy": "1.17.1", "qutip": "5.2.1",
            "h5py": "3.16.0"}


def sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def fail_list(problems: list, msg: str) -> None:
    problems.append(msg)
    print(f"  [FAIL] {msg}")


def ok(msg: str) -> None:
    print(f"  [ok] {msg}")



def _verify_sigstore(problems, zip_path: Path, bundle: Path, idx: dict,
                     sig: list, trusted_pol: dict) -> None:
    """Cryptographically authenticate the zip against the EXTERNAL trust root.

    The expected identity and issuer come from ``trusted_pol`` -- the policy the
    consumer supplied independently -- never from the bundle or the archive.

    On the certificate: Sigstore's ``Identity`` policy is what checks the
    presented certificate's SAN and issuer, and it is given the exact expected
    values from the trusted policy. An earlier version parsed the expected
    identity out of the policy and reported the result as an observed
    "certificate" value under a three-way agreement heading. That was wrong:
    before ``verify_artifact`` runs there is no observed certificate at all,
    only an expectation. The certificate check is exactly this call, and it is
    named for what it is.
    """
    identity = str(trusted_pol["signer_identity"])
    issuer = str(trusted_pol["oidc_issuer"])

    try:
        from sigstore.verify import Verifier
        from sigstore.verify.policy import Identity
        from sigstore.models import Bundle
    except ImportError as e:
        fail_list(problems,
                  f"sigstore verification requested but the library is not "
                  f"importable ({e.name}); absence of tooling is never "
                  "success -- install 'sigstore' in the verifying environment")
        return

    verifier = Verifier.production()
    pol = Identity(identity=identity, issuer=issuer)
    checked = 0
    seen_records: set = set()
    for entry in sig:
        if not isinstance(entry, dict) or set(entry) != {"name", "bundle"}:
            fail_list(problems,
                      f"malformed signature_bundles record: {entry!r}; "
                      "expected exactly {'name', 'bundle'}")
            continue
        key = (str(entry["name"]), str(entry["bundle"]))
        if key in seen_records:
            fail_list(problems, f"duplicate signature_bundles record: {key}")
            continue
        seen_records.add(key)

        # Exact subject name, not a suffix match. The finalizer writes the
        # exact basename; accepting anything ending in it would let routing
        # metadata point the verifier at a differently-named subject.
        name = str(entry["name"])
        if name != zip_path.name:
            fail_list(problems,
                      f"signature subject {name!r} is not the release zip "
                      f"{zip_path.name!r}; alternate subject names are "
                      "refused")
            continue
        bundle_path, why = _bundle_relative(bundle, str(entry["bundle"]))
        if bundle_path is None:
            fail_list(problems, f"refusing signature bundle path: {why}")
            continue
        if not bundle_path.exists():
            fail_list(problems,
                      f"signature bundle missing: {bundle_path.name}")
            continue
        try:
            b = Bundle.from_json(bundle_path.read_bytes())
            with open(zip_path, "rb") as fh:
                verifier.verify_artifact(fh.read(), b, pol)
            checked += 1
        except Exception as e:                        # noqa: BLE001
            fail_list(problems,
                      f"Sigstore verification FAILED for {name}: "
                      f"{type(e).__name__}: {e}")
    if checked and not problems:
        ok(f"Sigstore verified {checked} signature(s): the certificate "
           f"presented by the signer matched the expected identity "
           f"{identity!r} and issuer {issuer!r} from the EXTERNAL trusted "
           "policy")
    elif not checked and not problems:
        fail_list(problems, "no signature bundle could be verified")


def _bundle_relative(bundle: Path, name: str):
    """Resolve a bundle-relative name, refusing anything that escapes.

    signature_bundles is routing metadata and is mutable after signing, so it
    must never be able to make the verifier read an arbitrary path. Absolute
    paths, parent traversal and symlink escapes are all refused.
    """
    if not isinstance(name, str) or not name.strip():
        return None, "empty or non-string bundle name"
    if name != name.strip():
        return None, f"bundle name has surrounding whitespace: {name!r}"
    if PurePosixPath(name).is_absolute() or Path(name).is_absolute():
        return None, f"absolute path in signature_bundles: {name!r}"
    if ".." in PurePosixPath(name).parts:
        return None, f"parent traversal in signature_bundles: {name!r}"
    if "\\" in name or "\x00" in name:
        return None, f"illegal characters in signature_bundles: {name!r}"
    base = bundle.resolve()
    target = (base / name)
    try:
        resolved = target.resolve()
    except OSError as e:
        return None, f"unresolvable bundle path {name!r}: {e}"
    if resolved != base and base not in resolved.parents:
        return None, (f"signature bundle escapes the bundle directory: "
                      f"{name!r} -> {resolved}")
    return resolved, None


def _observed_repo_and_workflow(identity: str):
    """Split a GitHub Actions SAN into (repo_url, workflow_path, ref).

    Exact structural parsing, not substring matching: a SAN merely *containing*
    'github.com' or the workflow's basename proves nothing about which
    repository, workflow or ref actually signed.
    """
    m = re.match(
        r"\Ahttps://github\.com/([^/]+)/([^/]+)/"
        r"(\.github/workflows/[^@]+)@(.+)\Z", identity or "")
    if not m:
        return None
    owner, repo, wf, ref = m.groups()
    return (f"https://github.com/{owner}/{repo}", wf, ref)


# ---------------------------------------------------------------------------
# candidate-structure parsing: deterministic, classified, no tracebacks
# ---------------------------------------------------------------------------
#
# Phase 2 processes UNTRUSTED data by design -- the bundle and archive are
# attacker-controlled until phase 3 authenticates them. Direct reads therefore
# fail closed only accidentally, through a Python traceback: exit non-zero, but
# with no classification, no remaining diagnostics, and no way for a caller to
# tell a hostile artifact from a broken tool. A crash is not a decision.
#
# Each code below is a DECISION the verifier reached about a specific input.
# tests/test_hostile_input_no_traceback.py drives the real entry point with
# every shape that was reproduced as an uncaught exception and asserts a named
# code comes back; tests/test_candidate_structure.py pins each validator.
# Deleting any one guard is caught by the test that names its property --
# verified by mutation, not by inspection.

CANDIDATE_FAILURES = (
    "MISSING_RELEASE_INDEX",
    "INVALID_RELEASE_INDEX",
    "MISSING_SHA256SUMS",
    "INVALID_SHA256SUMS",
    "MISSING_SBOM",
    "INVALID_SBOM",
    "MISSING_PROVENANCE",
    "INVALID_PROVENANCE",
    "MISSING_RELEASE_ZIP",
    "UNREADABLE_RELEASE_ZIP",
    "INVALID_RELEASE_ZIP",
    "EMPTY_ZIP",
    "INVALID_ZIP_STRUCTURE",
    "MISSING_REQUIRED_ZIP_MEMBER",
    "MISSING_TRUST_POLICY",
    "UNREADABLE_TRUST_POLICY",
    "INVALID_TRUST_POLICY",
)

#: Failures of the EXTERNAL trust root. Kept separate from the candidate codes
#: above on purpose: a bad candidate is a bad release, but a bad trust root is
#: a bad question -- the consumer supplied material that cannot authorize
#: anything, and no release should be judged against it.
TRUST_ROOT_FAILURES = (
    "MISSING_TRUST_ROOT",
    "UNREADABLE_TRUST_ROOT",
    "INVALID_TRUST_ROOT",
)

# Structural resource bounds. These are NOT scientific thresholds and no
# scientific result depends on them; they exist so that resource exhaustion on
# hostile input is a classified refusal rather than a MemoryError or a
# RecursionError. Both are set far above any plausible legitimate release --
# the bundle's metadata files are kilobytes and the source archive is a few
# megabytes -- so a healthy release can never reach them.
MAX_METADATA_BYTES = 64 * 1024 * 1024
MAX_DECLARED_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024

#: Members every release archive must carry exactly once, relative to the root.
REQUIRED_ZIP_MEMBERS = (
    "uv.lock",
    "results_gate_table.csv",
    str(release_trust.CANONICAL_POLICY_PATH),
)


# --------------------------------------------------------------------------
# Shape validation for UNTRUSTED nested structures.
#
# Type-checking only the outer container ("files is a list") and then reading
# `e["name"]` inside a later comprehension makes that comprehension the first
# place a malformed record is discovered -- and a comprehension has no
# vocabulary for refusal, only KeyError and TypeError. These helpers state the
# record shape up front so every consumer downstream reads values that are
# already known to exist and to be of the right type.


def _is_text(v) -> bool:
    """A usable name: a non-empty string that is not just whitespace."""
    return isinstance(v, str) and bool(v.strip())


def _is_sha256(v) -> bool:
    return isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) is not None


def _is_size(v) -> bool:
    # bool is an int subclass; a size of True is a malformed record, not a 1.
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _records(problems, seq, where: str, code: str, fields: dict):
    """Validate a list of records against {field: predicate}.

    Returns the list of records, or None. `fields` maps a dotted path
    ("digest.sha256") to a predicate and a human description.
    """
    out = []
    for i, rec in enumerate(seq):
        if not isinstance(rec, dict):
            fail_list(problems,
                      f"{code}: {where}[{i}] is {type(rec).__name__}, "
                      "not an object")
            return None
        for path, (pred, desc) in fields.items():
            cur = rec
            for k in path.split("."):
                if not isinstance(cur, dict) or k not in cur:
                    fail_list(problems,
                              f"{code}: {where}[{i}] has no {path!r}")
                    return None
                cur = cur[k]
            if not pred(cur):
                fail_list(problems,
                          f"{code}: {where}[{i}].{path} is not {desc} "
                          f"({type(cur).__name__})")
                return None
        out.append(rec)
    return out


def _unique(problems, pairs, where: str, code: str):
    """Reject duplicate keys before a set comprehension silently absorbs them.

    A set built from records collapses duplicates, so a list carrying the same
    name twice with two different digests would compare equal to a set that
    never contained the contradiction. That must be a refusal.
    """
    seen: set = set()
    for name, _ in pairs:
        if name in seen:
            fail_list(problems, f"{code}: {where} lists {name!r} twice")
            return None
        seen.add(name)
    return frozenset(pairs)


@dataclass(frozen=True)
class CandidateBundle:
    """Untrusted bundle metadata that has passed structural validation.

    Construction is the ONLY way the rest of the verifier obtains these
    values, and construction validates. The derived collections below are
    built during validation rather than at the point of use, so no later
    expression can be the first place a malformed record is discovered --
    there is no later expression that indexes a raw record at all.

    Validated does not mean trusted. Every field here is still
    attacker-controlled; phase 3 decides authenticity. This object only
    guarantees that a refusal, not a traceback, is what a hostile shape
    produces.
    """
    index: dict
    sums: dict
    sbom: dict
    provenance: dict
    index_doc: JsonDocument
    sbom_doc: JsonDocument
    provenance_doc: JsonDocument
    policy_doc: PolicyDocument
    artifact_name: str
    artifact_size: int
    artifact_sha256: str
    index_files: frozenset      # {(name, sha256)}
    sbom_packages: frozenset    # {(name, version)}
    subjects: frozenset         # {(name, sha256)}

    @property
    def sums_set(self) -> frozenset:
        return frozenset(self.sums.items())

    @property
    def policy(self) -> dict:
        """The candidate policy object. Structurally valid, NOT trusted."""
        return self.policy_doc.policy

    @property
    def policy_bytes(self) -> bytes:
        """The exact bytes that were validated -- never re-read from disk."""
        return self.policy_doc.raw

    @property
    def scanned_text(self) -> str:
        """Concatenated text of every validated metadata document.

        The secret / absolute-path / claim-boundary scan reads THIS, not the
        filesystem. Re-reading after validation would let a file replaced
        mid-run be validated in one form and scanned in another -- and it
        forced errors="replace" on bytes whose strict decoding had already
        succeeded, which can only mask content.
        """
        return (self.index_doc.text + self.sbom_doc.text +
                self.provenance_doc.text + self.policy_doc.text)


def _validate_index(problems, idx: dict):
    """Full shape of release_index.json. Returns derived values, or None."""
    code = "INVALID_RELEASE_INDEX"
    for key, typ in (("release_artifact", dict), ("files", list),
                     ("claims", dict), ("provenance", dict)):
        if key not in idx or not isinstance(idx[key], typ):
            fail_list(problems,
                      f"{code}: {key!r} missing or not {typ.__name__}")
            return None

    art = idx["release_artifact"]
    for field, pred, desc in (("name", _is_text, "a non-empty string"),
                              ("sha256", _is_sha256, "64 lowercase hex"),
                              ("size", _is_size, "a non-negative integer")):
        if field not in art:
            fail_list(problems, f"{code}: release_artifact has no {field!r}")
            return None
        if not pred(art[field]):
            fail_list(problems,
                      f"{code}: release_artifact.{field} is not {desc} "
                      f"({type(art[field]).__name__})")
            return None

    files = _records(problems, idx["files"], "files", code,
                     {"name": (_is_text, "a non-empty string"),
                      "sha256": (_is_sha256, "64 lowercase hex")})
    if files is None:
        return None
    pairs = _unique(problems, [(f["name"], f["sha256"]) for f in files],
                    "files", code)
    if pairs is None:
        return None

    lvl = idx["provenance"].get("slsa_level_claimed")
    if lvl is not None and not isinstance(lvl, str):
        fail_list(problems,
                  f"{code}: provenance.slsa_level_claimed is "
                  f"{type(lvl).__name__}, not a string")
        return None

    # Written by the post-sign finalizer; still untrusted, still typed.
    sig = idx.get("signature_bundles", [])
    if not isinstance(sig, list):
        fail_list(problems,
                  f"{code}: 'signature_bundles' is "
                  f"{type(sig).__name__}, not a list")
        return None
    return art["name"], art["size"], art["sha256"], pairs


def _validate_sbom(problems, sbom: dict):
    code = "INVALID_SBOM"
    if not isinstance(sbom.get("components"), list):
        fail_list(problems, f"{code}: components is missing or not a list")
        return None
    comps = _records(problems, sbom["components"], "components", code,
                     {"name": (_is_text, "a non-empty string"),
                      "version": (_is_text, "a non-empty string")})
    if comps is None:
        return None
    # Duplicate (name, version) pairs are legitimate in a component list; only
    # the pair set is compared against uv.lock, so uniqueness is not required.
    return frozenset((c["name"], c["version"]) for c in comps)


def _validate_provenance(problems, prov: dict):
    code = "INVALID_PROVENANCE"
    if not isinstance(prov.get("subject"), list) or \
            not isinstance(prov.get("predicate"), dict):
        fail_list(problems,
                  f"{code}: subject/predicate missing or wrong type")
        return None
    subs = _records(problems, prov["subject"], "subject", code,
                    {"name": (_is_text, "a non-empty string"),
                     "digest": (lambda v: isinstance(v, dict), "an object"),
                     "digest.sha256": (_is_sha256, "64 lowercase hex")})
    if subs is None:
        return None
    lvl = prov["predicate"].get("slsa_level_claimed")
    if lvl is not None and not isinstance(lvl, str):
        fail_list(problems,
                  f"{code}: predicate.slsa_level_claimed is "
                  f"{type(lvl).__name__}, not a string")
        return None
    return _unique(problems,
                   [(s["name"], s["digest"]["sha256"]) for s in subs],
                   "subject", code)


@dataclass(frozen=True)
class PolicyDocument:
    """One policy file, read once, decoded once, parsed once, validated once.

    Holding the bytes alongside the object is the point. Every later consumer
    -- the metadata scan, the offline consistency check, the digest
    comparison, the post-authentication binding -- reads from this object, so
    the file is never re-opened. A second read is not merely wasteful: it is a
    TOCTOU window in which the bytes that were validated and the bytes that
    are used stop being the same bytes.

    Structurally valid is NOT trusted. This type says the document parsed and
    matched the schema; only TrustedPolicyRoot says anyone authorized it.
    """
    raw: bytes
    text: str
    policy: dict


def load_policy_document(problems, path: Path, *, label: str,
                         require_resolved: bool,
                         missing: str, unreadable: str, invalid: str):
    """THE parsing path from raw policy bytes to a validated document.

    There is exactly one of these on purpose. The same file used to be opened
    in five places with four different exception lists, so whether a hostile
    policy crashed the verifier depended on which caller reached it first.
    """
    if not path.exists():
        # exists() follows symlinks, so a broken or looping link lands here
        # too; that is "cannot be resolved", not "not there".
        if os.path.lexists(path):
            fail_list(problems,
                      f"{unreadable}: {label} at {path} exists but does not "
                      "resolve to a readable file")
        else:
            fail_list(problems, f"{missing}: {label} at {path}")
        return None
    if not path.is_file():
        fail_list(problems,
                  f"{unreadable}: {label} at {path} is not a regular file")
        return None
    try:
        size = path.stat().st_size
    except OSError as e:
        fail_list(problems,
                  f"{unreadable}: {label} at {path}: {type(e).__name__}")
        return None
    if size > MAX_METADATA_BYTES:
        fail_list(problems,
                  f"{invalid}: {label} is {size} bytes, above the "
                  f"{MAX_METADATA_BYTES}-byte structural bound")
        return None
    try:
        raw = path.read_bytes()
    except OSError as e:
        fail_list(problems,
                  f"{unreadable}: {label} at {path}: {type(e).__name__}: "
                  f"{e.strerror or e}")
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        fail_list(problems,
                  f"{invalid}: {label} is not valid UTF-8 (byte {e.start})")
        return None
    try:
        doc = json.loads(text)
    # RecursionError is not a ValueError. A policy nested tens of thousands of
    # levels deep crashed every caller that named only JSONDecodeError.
    except (ValueError, RecursionError) as e:
        fail_list(problems,
                  f"{invalid}: {label} is not parseable JSON: "
                  f"{type(e).__name__}: {str(e)[:100]}")
        return None
    if not isinstance(doc, dict):
        fail_list(problems,
                  f"{invalid}: {label} is a JSON {type(doc).__name__}, not an "
                  "object")
        return None
    try:
        release_trust.validate_policy(doc, require_resolved=require_resolved)
    except release_trust.PolicyError as e:
        fail_list(problems, f"{invalid}: {label}: {e}")
        return None
    return PolicyDocument(raw=raw, text=text, policy=doc)


@dataclass(frozen=True)
class JsonDocument:
    """One candidate metadata file: exact bytes, strict text, parsed object."""
    raw: bytes
    text: str
    doc: dict


def _load_json_member(problems, path: Path, missing: str, invalid: str):
    """Read one candidate JSON file, classifying both failure modes.

    Returns a JsonDocument carrying the exact bytes, the strictly decoded
    text, and the parsed object -- never just the object. Handing back only
    the object forced later consumers to re-read the file, which is a TOCTOU
    window: the document that was validated and the document that is used stop
    being the same document.
    """
    if not path.exists():
        fail_list(problems, f"{missing}: {path}")
        return None
    try:
        size = path.stat().st_size
    except OSError as e:
        fail_list(problems, f"{missing}: {path} unreadable: {e}")
        return None
    if size > MAX_METADATA_BYTES:
        fail_list(problems,
                  f"{invalid}: {path} is {size} bytes, above the "
                  f"{MAX_METADATA_BYTES}-byte structural bound for release "
                  "metadata")
        return None
    try:
        raw = path.read_bytes()
    except OSError as e:
        fail_list(problems, f"{missing}: {path} unreadable: {e}")
        return None
    if not raw.strip():
        fail_list(problems, f"{invalid}: {path} is empty")
        return None
    try:
        text = raw.decode("utf-8")
        doc = json.loads(text)
    # RecursionError is not a ValueError. A document nested tens of thousands
    # of levels deep -- trivially cheap to author -- crashed the decoder and
    # escaped this handler entirely, producing exactly the traceback this
    # function exists to prevent.
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        fail_list(problems, f"{invalid}: {path}: {type(e).__name__}: "
                            f"{str(e)[:120]}")
        return None
    if not isinstance(doc, dict):
        fail_list(problems,
                  f"{invalid}: {path} is not a JSON object "
                  f"({type(doc).__name__})")
        return None
    # The decoded text is RETAINED and handed back with the object. Every
    # later consumer -- notably the secret/path/claim scan -- reads it instead
    # of re-opening the file, so what is scanned is exactly what was parsed.
    return JsonDocument(raw=raw, text=text, doc=doc)


def parse_sha256sums(problems, path: Path):
    """Parse SHA256SUMS, refusing malformed lines rather than crashing."""
    if not path.exists():
        fail_list(problems, f"MISSING_SHA256SUMS: {path}")
        return None
    sums: dict = {}
    try:
        if path.stat().st_size > MAX_METADATA_BYTES:
            fail_list(problems,
                      f"INVALID_SHA256SUMS: {path} exceeds the "
                      f"{MAX_METADATA_BYTES}-byte structural bound")
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        fail_list(problems, f"INVALID_SHA256SUMS: {path} unreadable: {e}")
        return None
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            fail_list(problems,
                      f"INVALID_SHA256SUMS: line {n} is not "
                      f"'<digest>  <name>': {line[:60]!r}")
            return None
        digest, name = parts[0].strip(), parts[1].strip()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            fail_list(problems,
                      f"INVALID_SHA256SUMS: line {n} digest is not 64-hex: "
                      f"{digest[:20]!r}")
            return None
        if not name:
            fail_list(problems, f"INVALID_SHA256SUMS: line {n} has no name")
            return None
        if name in sums:
            fail_list(problems,
                      f"INVALID_SHA256SUMS: duplicate entry {name!r}")
            return None
        sums[name] = digest
    if not sums:
        fail_list(problems, f"INVALID_SHA256SUMS: {path} lists nothing")
        return None
    return sums


def validate_zip_structure(problems, names: list):
    """Structural validation of an UNTRUSTED archive.

    Returns the archive's top-level directory name (a str), or None. This is
    the ARCHIVE root -- a ZIP member prefix. It is not, and must never be
    confused with, the TrustedPolicyRoot returned by load_trusted_policy.

    Nothing is extracted; this only decides which member names may be read.
    Ambiguity matters more than usual here because payload-digest
    recomputation maps members into a dict keyed by relative path, so two
    members normalizing to one key would silently collapse into a single
    entry and change the digest's meaning.
    """
    if not names:
        fail_list(problems, "EMPTY_ZIP: the archive contains no members")
        return None

    seen: set = set()
    normalized: dict = {}
    roots: set = set()
    for raw in names:
        if raw in seen:
            fail_list(problems,
                      f"INVALID_ZIP_STRUCTURE: duplicate member {raw!r}")
            return None
        seen.add(raw)
        if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
            fail_list(problems,
                      f"INVALID_ZIP_STRUCTURE: absolute member {raw!r}")
            return None
        if "\\" in raw:
            fail_list(problems,
                      f"INVALID_ZIP_STRUCTURE: backslash in member {raw!r}; "
                      "path separator is ambiguous")
            return None
        parts = PurePosixPath(raw).parts
        if ".." in parts:
            fail_list(problems,
                      f"INVALID_ZIP_STRUCTURE: parent traversal in {raw!r}")
            return None
        if not parts:
            fail_list(problems,
                      f"INVALID_ZIP_STRUCTURE: empty member name {raw!r}")
            return None
        # A directory entry and a file of the same name are contradictory.
        key = raw.rstrip("/")
        kind = "dir" if raw.endswith("/") else "file"
        if key in normalized and normalized[key] != kind:
            fail_list(problems,
                      f"INVALID_ZIP_STRUCTURE: {key!r} appears as both a file "
                      "and a directory")
            return None
        if key in normalized and kind == "file":
            fail_list(problems,
                      f"INVALID_ZIP_STRUCTURE: member {key!r} occurs twice "
                      "after normalization")
            return None
        normalized[key] = kind
        roots.add(parts[0])

    if len(roots) != 1:
        fail_list(problems,
                  f"INVALID_ZIP_STRUCTURE: expected exactly one top-level "
                  f"release root, found {sorted(roots)[:5]}")
        return None
    archive_root = roots.pop()

    files = {k for k, v in normalized.items() if v == "file"}
    for required in REQUIRED_ZIP_MEMBERS:
        want = f"{archive_root}/{required}"
        if want not in files:
            fail_list(problems,
                      f"MISSING_REQUIRED_ZIP_MEMBER: {required!r} is not in "
                      "the archive")
            return None
    return archive_root


def parse_candidate_bundle(problems, bundle: Path,
                           policy_doc: PolicyDocument):
    """Read and validate the untrusted bundle. Returns a CandidateBundle.

    ``policy_doc`` is the candidate trust policy, already read and validated
    by ``load_policy_document`` in phase 0. It is passed in rather than opened
    here so that the file is read exactly once for the whole run.

    Every structure the verifier later reads is validated HERE, including the
    nested records. Returning the typed object rather than a bare dict is what
    makes that ordering structural: there is no accessor that yields an
    unvalidated record.
    """
    idx_doc = _load_json_member(problems, bundle / "release_index.json",
                                "MISSING_RELEASE_INDEX",
                                "INVALID_RELEASE_INDEX")
    if idx_doc is None:
        return None
    idx = idx_doc.doc
    got = _validate_index(problems, idx)
    if got is None:
        return None
    art_name, art_size, art_sha, idx_files = got

    sums = parse_sha256sums(problems, bundle / "SHA256SUMS")
    if sums is None:
        return None

    sbom_doc = _load_json_member(problems, bundle / "sbom.cdx.json",
                                 "MISSING_SBOM", "INVALID_SBOM")
    if sbom_doc is None:
        return None
    sbom = sbom_doc.doc
    sbom_pkgs = _validate_sbom(problems, sbom)
    if sbom_pkgs is None:
        return None

    prov_doc = _load_json_member(problems, bundle / "provenance.intoto.json",
                                 "MISSING_PROVENANCE", "INVALID_PROVENANCE")
    if prov_doc is None:
        return None
    prov = prov_doc.doc
    subjects = _validate_provenance(problems, prov)
    if subjects is None:
        return None

    return CandidateBundle(
        index=idx, sums=sums, sbom=sbom, provenance=prov,
        index_doc=idx_doc, sbom_doc=sbom_doc, provenance_doc=prov_doc,
        policy_doc=policy_doc,
        artifact_name=art_name, artifact_size=art_size,
        artifact_sha256=art_sha, index_files=idx_files,
        sbom_packages=sbom_pkgs, subjects=subjects)


def read_release_zip(problems, zip_path: Path):
    """Classify the release archive PATH before any byte of it is read.

    A missing, unreadable or non-archive file is an ordinary hostile input --
    a consumer verifying an interrupted download hits it routinely -- so it
    gets a named refusal like every other malformation. Previously the first
    contact with the path was a bare ``zip_path.read_bytes()``, and a missing
    file produced a FileNotFoundError traceback: non-zero exit, but no
    classification, and indistinguishable from a broken verifier.
    """
    if not zip_path.exists():
        # exists() follows symlinks, so a broken or looping link also lands
        # here. That is not the same fact as "no such path", and reporting it
        # as MISSING would misdescribe the artifact a consumer actually holds.
        if os.path.lexists(zip_path):
            fail_list(problems,
                      f"UNREADABLE_RELEASE_ZIP: {zip_path} exists but does "
                      "not resolve to a readable file")
        else:
            fail_list(problems, f"MISSING_RELEASE_ZIP: {zip_path}")
        return None
    if not zip_path.is_file():
        fail_list(problems,
                  f"UNREADABLE_RELEASE_ZIP: {zip_path} is not a regular file")
        return None
    try:
        return zip_path.read_bytes()
    except OSError as e:
        fail_list(problems,
                  f"UNREADABLE_RELEASE_ZIP: {zip_path}: "
                  f"{type(e).__name__}: {e.strerror or e}")
        return None


def open_release_zip(problems, zb: bytes):
    """Open the archive from bytes, classifying corruption.

    Returns ``(zf, names)``, or ``(None, None)``.

    ``testzip`` is run here so a CRC failure is refused before any member is
    consumed, rather than surfacing halfway through digest checking.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zb))
        names = zf.namelist()
        # The central directory declares uncompressed sizes, so a
        # decompression bomb is refused before a single member is expanded.
        declared = sum(max(i.file_size, 0) for i in zf.infolist())
        if declared > MAX_DECLARED_UNCOMPRESSED_BYTES:
            fail_list(problems,
                      f"INVALID_RELEASE_ZIP: declares {declared} "
                      f"uncompressed bytes, above the "
                      f"{MAX_DECLARED_UNCOMPRESSED_BYTES}-byte structural "
                      "bound")
            return None, None
        bad = zf.testzip()
        if bad is not None:
            fail_list(problems,
                      f"INVALID_RELEASE_ZIP: CRC failure at member {bad!r}")
            return None, None
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError,
            ValueError, RuntimeError, EOFError) as e:
        fail_list(problems,
                  f"INVALID_RELEASE_ZIP: not a readable archive: "
                  f"{type(e).__name__}")
        return None, None
    return zf, names


def load_trusted_policy(problems, trusted_policy, trusted_sha256,
                        candidate: PolicyDocument | None = None):
    """THE external trust root. Returns a TrustedPolicyRoot, or None.

    A release cannot authorize itself. A malicious artifact can always carry a
    policy naming its own signer, so the authorization must come from OUTSIDE
    the artifact -- supplied by the consumer through a channel the release does
    not control.

    Two modes, both ending in a fully resolved, authorized policy object:

    FILE   --trusted-policy PATH
           read, parse, validate resolved, canonicalize, digest. If
           --trusted-policy-sha256 is also given, the two must agree.

    DIGEST --trusted-policy-sha256 DIGEST
           the digest IS the root. It authenticates the exact bytes of a
           candidate policy, so the bundle's copy may be *promoted* to the
           trusted policy once its canonical bytes hash to the supplied value.
           That is not self-authorization: the consumer supplied the hash
           independently, and bytes that match it are the owner's bytes.

           The bundle copy is used, never the archive copy: the archive cannot
           be trusted before its signature is verified, and its signature
           cannot be checked without the identity this root supplies.

    An earlier version returned ``(None, None)`` for a "valid" digest-only
    root, so `_verify_sigstore` had no policy to read an identity from and
    digest-only verification could not work at all -- while a loader-level test
    still passed. Hence the explicit object.
    """
    if not trusted_policy and not trusted_sha256:
        fail_list(problems,
                  "--online requires an externally supplied trust root: pass "
                  "--trusted-policy PATH (an owner-authorized policy obtained "
                  "independently of this release) or --trusted-policy-sha256. "
                  "The policy inside the bundle or the zip is a CANDIDATE, "
                  "never the root: an artifact cannot authorize itself.")
        return None

    want_digest = None
    if trusted_sha256:
        try:
            want_digest = release_trust.normalize_digest(trusted_sha256)
        except release_trust.PolicyError as e:
            fail_list(problems, f"--trusted-policy-sha256 is malformed: {e}")
            return None

    # ---- FILE mode --------------------------------------------------------
    if trusted_policy:
        # Owner-supplied, but still parsed through the one safe path: a
        # malformed trust root is an INVALID TRUST ROOT, never a crash. The
        # question being asked is bad, not the release being asked about.
        doc = load_policy_document(
            problems, Path(trusted_policy),
            label="the externally supplied trusted policy",
            require_resolved=True,
            missing="MISSING_TRUST_ROOT", unreadable="UNREADABLE_TRUST_ROOT",
            invalid="INVALID_TRUST_ROOT")
        if doc is None:
            return None
        pol = doc.policy
        try:
            canon = release_trust.canonical_bytes(pol)
        except (TypeError, ValueError) as e:
            fail_list(problems,
                      f"INVALID_TRUST_ROOT: the supplied trusted policy "
                      f"cannot be canonicalized: {e}")
            return None
        got = release_trust.policy_digest(canon)
        if want_digest and got != want_digest:
            fail_list(problems,
                      f"INVALID_TRUST_ROOT: --trusted-policy digest {got} != "
                      f"--trusted-policy-sha256 {want_digest}")
            return None
        try:
            trust_root = release_trust.TrustedPolicyRoot(
                pol, canon, got, "file")
        except release_trust.PolicyError as e:
            fail_list(problems, f"trust root rejected: {e}")
            return None
        ok("external trust root loaded from file and authorized")
        return trust_root

    # ---- DIGEST-ONLY mode -------------------------------------------------
    # The candidate document was already read and structurally validated in
    # phase 0. Nothing here re-opens the file: the bytes that get hashed are
    # the bytes that were validated, with no window in between.
    if candidate is None:
        fail_list(problems,
                  "digest-only trust root needs the bundle's candidate policy "
                  "to authenticate, but no candidate policy was supplied")
        return None
    pol = candidate.policy

    # Canonicalize BEFORE comparing, so formatting cannot change the digest,
    # and compare BEFORE trusting any value inside the document.
    try:
        canon = release_trust.canonical_bytes(pol)
    except (TypeError, ValueError) as e:
        fail_list(problems,
                  f"digest-only trust root: candidate policy cannot be "
                  f"canonicalized: {e}")
        return None
    got = release_trust.policy_digest(canon)
    if got != want_digest:
        fail_list(problems,
                  f"digest-only trust root: candidate policy digest {got} does "
                  f"not match the supplied {want_digest}. Nothing in the "
                  "candidate has been trusted.")
        return None

    # Only now are these bytes the owner's bytes, and only now may their
    # values be read.
    try:
        release_trust.validate_policy(pol, require_resolved=True)
        trust_root = release_trust.TrustedPolicyRoot(
            pol, canon, got, "digest")
    except release_trust.PolicyError as e:
        fail_list(problems,
                  f"digest-authenticated policy is not authorized for a "
                  f"signed release: {e}")
        return None
    ok("external trust root established by digest: the bundle's candidate "
       "policy hashes to the independently supplied value and is authorized")
    return trust_root


def bind_candidate_policy(problems, label: str, raw: bytes,
                          trusted_canon: bytes, trusted_sha256):
    """A candidate policy copy must equal the external trust root exactly."""
    if trusted_canon is not None:
        if raw != trusted_canon:
            fail_list(problems,
                      f"{label} policy differs from the external trusted "
                      "policy; a release may only ship the authorized policy")
            return False
    elif trusted_sha256:
        got = release_trust.policy_digest(raw)
        if got != str(trusted_sha256).strip().lower():
            fail_list(problems,
                      f"{label} policy digest {got} != trusted "
                      f"{trusted_sha256}")
            return False
    else:
        fail_list(problems, f"no trust root to bind {label} policy against")
        return False
    ok(f"{label} policy is byte-identical to the external trust root")
    return True


def enforce_trust_policy(problems, candidate: "CandidateBundle", idx: dict,
                         prov: dict,
                         *, canonical: Path | None = None) -> dict | None:
    """Full policy enforcement. Returns the validated policy, or None.

    Runs for offline verification too: a bundle whose policy is malformed or
    whose bundled copy diverges from the canonical one is defective regardless
    of whether a signature exists.
    """
    # 1. Shape was established in phase 0 by load_policy_document, which is
    #    the ONLY path from raw policy bytes to a validated document. This
    #    function used to re-open and re-parse the file with a narrower
    #    exception list, so a policy that was a directory, or nested deeply
    #    enough to exhaust the stack, crashed here after passing everywhere
    #    else. It now consumes what phase 0 validated and never touches the
    #    filesystem.
    pol = candidate.policy
    bundled_raw = candidate.policy_bytes
    ok("bundled trust policy passes the strict schema")

    # 2. The bundled policy must be the canonical policy, byte for byte.
    #    This is what makes a single source of truth enforceable rather than
    #    merely intended.
    # OFFLINE ONLY. This is a repository-local consistency check, not a trust
    # root: it says "this bundle matches the policy in this checkout". If the
    # checkout is absent -- the independent-consumer case -- there is nothing
    # to anchor to, and that is REPORTED rather than silently skipped, which
    # is what the previous implementation did. Trusted verification never
    # relies on this path; it requires --trusted-policy (see
    # load_trusted_policy) and fails closed without one.
    canon = canonical if canonical is not None else \
        release_trust.CANONICAL_POLICY_PATH
    if Path(canon).is_file():
        try:
            want = release_trust.canonical_bytes(
                json.loads(Path(canon).read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, ValueError,
                RecursionError) as e:
            fail_list(problems,
                      f"the repository-local canonical policy at {canon} is "
                      f"unreadable: {type(e).__name__}; local consistency "
                      "cannot be checked")
            return None
        if bundled_raw != want:
            fail_list(problems,
                      "bundled trust policy differs from the canonical "
                      f"policy at {canon}; a release may only ship the "
                      "reviewed policy")
        else:
            ok("bundled policy matches this checkout's canonical policy "
               "(local consistency only; NOT a trust root)")
    else:
        ok(f"no local canonical policy at {canon}: bundled policy is "
           "UNANCHORED here. Offline verification checks structure only; "
           "use --online --trusted-policy for an authorization decision.")
    return pol


def enforce_authenticated_binding(problems, pol: dict, binding: dict,
                                  zip_policy_ok: bool) -> None:
    """Compare the SIGNED release binding against the external trusted policy.

    Called only after the Sigstore signature over the zip has verified, so the
    binding's bytes are authenticated. Order matters and is not negotiable:

        trusted policy -> expected identity -> verify signature ->
        NOW read binding -> compare binding to policy

    Never binding -> decide who to trust -> verify against that. That would be
    artifact self-authorization.

    The values here are genuinely observed release facts. The repository,
    workflow and ref are ALSO proven by the certificate, because Sigstore
    checked the SAN against the exact expected identity derived from the
    trusted policy -- so agreement between the binding and the policy is a
    real cross-check against an independent encoding, not a restatement.
    """
    try:
        release_trust.validate_binding(binding)
    except release_trust.PolicyError as e:
        fail_list(problems, f"signed release binding is invalid: {e}")
        return
    ok("signed release binding passes its schema")

    for field, policy_key in (("source_repository", "source_repository"),
                              ("workflow_path", "workflow_path"),
                              ("authorized_ref", "authorized_ref"),
                              ("reviewed_payload_sha256",
                               "reviewed_payload_sha256")):
        got, want = str(binding[field]), str(pol[policy_key])
        if got != want:
            fail_list(problems,
                      f"signed binding {field}={got!r} != authorized "
                      f"{want!r}")
    if binding["reviewed_revision"] != pol["pinned_revision"]:
        fail_list(problems,
                  f"signed binding reviewed_revision "
                  f"{binding['reviewed_revision']} != authorized "
                  f"pinned_revision {pol['pinned_revision']}")

    # The stable builder id is DERIVED from authenticated repository/workflow/
    # ref rather than taken from unsigned provenance, then compared against the
    # authorized list. See RELEASE_TRUST_ENFORCEMENT.md on what this does and
    # does not add over signer_identity.
    derived = release_trust.derive_stable_builder_id(
        binding["source_repository"], binding["workflow_path"],
        binding["authorized_ref"])
    if binding["stable_builder_id"] != derived:
        fail_list(problems,
                  f"signed binding stable_builder_id "
                  f"{binding['stable_builder_id']!r} is not the value derived "
                  f"from its own repository/workflow/ref ({derived!r})")
    elif derived not in list(pol["trusted_builders"]):
        fail_list(problems,
                  f"builder {derived!r} is not in trusted_builders "
                  f"{list(pol['trusted_builders'])}. Exact equality only.")
    elif derived == release_trust.LOCAL_BUILDER_ID:
        fail_list(problems,
                  "the local unsigned builder can never satisfy a hosted "
                  "signed release")
    else:
        ok(f"builder derived from authenticated content and authorized: "
           f"{derived}")

    if not zip_policy_ok:
        fail_list(problems,
                  "the policy inside the signed zip was not bound to the "
                  "external trust root")


def _dig(doc, *keys, default=None):
    """Walk nested mapping keys, yielding `default` the moment one is not one.

    Used for UNSIGNED auxiliary structures, where every level is
    attacker-controlled and a missing level and a wrongly-typed level are the
    same thing: no value. Returns {} by default so callers can `.get` safely.
    """
    cur = doc
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return {} if default is None else default
        cur = cur[k]
    return cur


def check_auxiliary_provenance(problems, pol: dict, binding: dict,
                               prov: dict) -> None:
    """Cross-check UNSIGNED provenance. It is never authority.

    provenance.intoto.json is generated OUTSIDE the source zip and is not
    itself signed, so it is UNTRUSTED_AUXILIARY_METADATA. It was previously
    consulted for repository, workflow, ref, builder and source revision --
    every one an authorization decision resting on unauthenticated bytes.
    Those now come from the signed binding. What remains here is a consistency
    report: a disagreement is worth surfacing, but agreement grants nothing.
    """
    # Provenance is UNSIGNED. It sits beside the archive, so anyone can edit
    # it while the signature over the zip stays valid -- which means these
    # nested reads are attacker-controlled even on a genuinely authenticated
    # release. `predicate` is typed at parse time, but nothing below it was:
    # a `buildDefinition` of "nope", an `externalParameters` list, a
    # `resolvedDependencies` string, or a null dependency each raised
    # AttributeError here. Reached only after the signature verifies, which is
    # exactly why no refusal test found it.
    ext = _dig(prov, "predicate", "buildDefinition", "externalParameters")
    deps = _dig(prov, "predicate", "buildDefinition", "resolvedDependencies",
                default=[])
    rev = ""
    if isinstance(deps, list):
        for dep in deps:
            digest = dep.get("digest") if isinstance(dep, dict) else None
            if isinstance(digest, dict) and "gitCommit" in digest:
                rev = str(digest["gitCommit"])
                break
    mismatches = []
    if not isinstance(ext, dict):
        ext = {}
    for k in ("source_repository", "workflow_path", "authorized_ref"):
        if str(ext.get(k, "")) != str(binding[k]):
            mismatches.append(k)
    if rev and rev != binding["release_revision"]:
        mismatches.append("release_revision")
    if mismatches:
        fail_list(problems,
                  f"auxiliary provenance disagrees with the signed binding "
                  f"on {mismatches}; provenance is not authority, but a "
                  "disagreement means the bundle is inconsistent")
    else:
        ok("auxiliary provenance is consistent with the signed binding "
           "(cross-check only; provenance is not authenticated)")


def decode_member(problems, zf, name: str):
    """Read one archive member as UTF-8 text, classifying both failures.

    Silently substituting replacement characters would be worse than the
    traceback it replaces: the gate table is where the PASS count is
    RECOMPUTED, so a member that cannot be decoded must never be counted. Zero
    PASS rows found in undecodable bytes is not evidence of zero PASS rows --
    it is absence of evidence, and reporting it as "PASS count = 0" would be a
    claim the verifier cannot support.
    """
    try:
        raw = zf.read(name)
    except (KeyError, OSError, zipfile.BadZipFile, RuntimeError,
            EOFError, ValueError) as e:
        fail_list(problems,
                  f"INVALID_RELEASE_ZIP: cannot read {name!r}: "
                  f"{type(e).__name__}")
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        fail_list(problems,
                  f"INVALID_RELEASE_ZIP: {name!r} is not valid UTF-8 "
                  f"(byte {e.start}); its content cannot be established")
        return None


def read_release_binding(problems, zf, archive_root: str):
    """Parse the release binding from the AUTHENTICATED archive.

    This input is not pre-authentication hostile input: the signature over the
    archive already verified, so these bytes are the signer's bytes. That does
    not make them well formed. A malformed signed release is a real outcome --
    a broken build, a truncated write -- and it must produce a refusal with a
    reason rather than a traceback, so this is deterministic signed-artifact
    validation, not a security boundary. Claiming otherwise would overstate
    what it does.

    RecursionError and UnicodeDecodeError are named explicitly because neither
    is a JSONDecodeError, which is all the earlier version caught.
    """
    name = f"{archive_root}/{release_trust.RELEASE_BINDING_NAME}"
    try:
        raw = zf.read(name)
        doc = json.loads(raw.decode("utf-8"))
    except (KeyError, OSError, zipfile.BadZipFile, RuntimeError, EOFError,
            UnicodeDecodeError, ValueError, RecursionError) as e:
        fail_list(problems,
                  f"the signed zip carries no usable "
                  f"{release_trust.RELEASE_BINDING_NAME}: "
                  f"{type(e).__name__}: {str(e)[:100]}")
        return None
    if not isinstance(doc, dict):
        fail_list(problems,
                  f"the signed {release_trust.RELEASE_BINDING_NAME} is a JSON "
                  f"{type(doc).__name__}, not an object")
        return None
    return doc


def verify(zip_path: Path, bundle: Path, online: bool,
           trusted_policy=None, trusted_sha256=None) -> int:
    problems: list = []

    # ---- PHASE 0: candidate trust-root MATERIAL -------------------------
    # The bundled policy is attacker-controlled bytes. It is read, decoded,
    # parsed and schema-checked exactly once, here, before anything else looks
    # at it -- and NOTHING in it is believed yet. Structural validity is not
    # authorization; it only means the document can be handled safely.
    #
    # This runs before phase 1 because the digest-only trust root
    # authenticates these very bytes. Doing it here means one parser and one
    # validated byte sequence, instead of a second policy reader living inside
    # the digest path.
    candidate_policy = load_policy_document(
        problems, bundle / "release_trust_policy.json",
        label="the bundled candidate trust policy",
        require_resolved=False,
        missing="MISSING_TRUST_POLICY",
        unreadable="UNREADABLE_TRUST_POLICY",
        invalid="INVALID_TRUST_POLICY")
    if candidate_policy is None:
        print(f"\nRESULT: {len(problems)} FAILURES")
        print("note: a signature proves origin and integrity only; "
              "never physics or hardware validation")
        return 1

    # ---- PHASE 1: trust root -------------------------------------------
    # Nothing from the candidate artifact has any authority until phase 3.
    # `trust_root` and `archive_root` are DIFFERENT KINDS OF THING and were
    # once both called `root`. Phase 1 bound the TrustedPolicyRoot; phase 2
    # rebound the same name to the archive's top-level directory string; and
    # phase 4 then evaluated `root.sha256` on a str. Every refusal test passed,
    # because no test had ever executed the successful online path all the way
    # into phase 4. The names are now distinct and annotated so the two can
    # never be confused again.
    trust_root: release_trust.TrustedPolicyRoot | None = None
    trusted_pol, trusted_canon = (None, None)
    if online:
        trust_root = load_trusted_policy(problems, trusted_policy,
                                         trusted_sha256, candidate_policy)
        if trust_root is not None:
            # One object, both modes. Everything downstream reads the
            # AUTHENTICATED canonical bytes held here, never a re-read of the
            # candidate file, so bytes changed after the digest check cannot
            # re-enter the decision.
            trusted_pol = trust_root.policy
            trusted_canon = trust_root.canonical_bytes
        if problems:
            print(f"\nRESULT: {len(problems)} FAILURES")
            print("note: a signature proves origin and integrity only; "
                  "never physics or hardware validation")
            return 1

    # ---- PHASE 2: candidate artifact structure --------------------------
    # Untrusted by definition. Every expected malformation is a classified
    # refusal, not a traceback.
    candidate = parse_candidate_bundle(problems, bundle, candidate_policy)
    if candidate is None:
        print(f"\nRESULT: {len(problems)} FAILURES")
        print("note: a signature proves origin and integrity only; "
              "never physics or hardware validation")
        return 1
    idx = candidate.index
    sums = candidate.sums

    zb = read_release_zip(problems, zip_path)
    if zb is None:
        print(f"\nRESULT: {len(problems)} FAILURES")
        print("note: a signature proves origin and integrity only; "
              "never physics or hardware validation")
        return 1
    zh = sha_bytes(zb)
    if candidate.artifact_name != zip_path.name or \
            candidate.artifact_sha256 != zh or \
            candidate.artifact_size != len(zb):
        fail_list(problems, "release zip name/size/digest mismatch vs "
                             "release_index")
    else:
        ok(f"release zip digest matches index ({zh[:16]}...)")
    if sums.get(zip_path.name) != zh:
        fail_list(problems, "release zip digest mismatch vs SHA256SUMS")

    zf, names = open_release_zip(problems, zb)
    if zf is None:
        print(f"\nRESULT: {len(problems)} FAILURES")
        print("note: a signature proves origin and integrity only; "
              "never physics or hardware validation")
        return 1
    archive_root: str | None = validate_zip_structure(problems, names)
    if archive_root is None:
        print(f"\nRESULT: {len(problems)} FAILURES")
        print("note: a signature proves origin and integrity only; "
              "never physics or hardware validation")
        return 1
    seen: set = set()
    for name, h in sums.items():
        if name == zip_path.name:
            continue
        inner = f"{archive_root}/{name}"
        if inner in seen:
            fail_list(problems, f"duplicate artifact {name}")
        seen.add(inner)
        try:
            data = zf.read(inner)
        except KeyError:
            fail_list(problems, f"missing artifact in zip: {name}")
            continue
        except Exception as e:
            fail_list(problems, f"unreadable artifact {name}: "
                                 f"{type(e).__name__}")
            continue
        if sha_bytes(data) != h:
            fail_list(problems, f"digest mismatch inside zip: {name}")
    if not problems:
        ok(f"all {len(sums) - 1} SHA256SUMS entries verified inside zip")
    # These collections were built during validation, not here: a set
    # comprehension over raw records has no way to refuse a malformed one.
    idx_files = candidate.index_files
    sums_set = candidate.sums_set
    if idx_files != sums_set:
        fail_list(problems, "release_index files != SHA256SUMS set")
    lock_txt = decode_member(problems, zf, f"{archive_root}/uv.lock")
    sbom_pkgs = candidate.sbom_packages
    if lock_txt is None:
        # A comparison that could not run is not a comparison that passed.
        # Every branch below must be reachable only when lock_txt exists,
        # including -- especially -- the success branch.
        fail_list(problems,
                  "SBOM cannot be checked against uv.lock: the archived lock "
                  "file is unreadable")
    else:
        lock_pkgs = set(re.findall(
            r'name = "([^"]+)"\nversion = "([^"]+)"', lock_txt))
        if lock_pkgs != sbom_pkgs:
            fail_list(problems, "SBOM/uv.lock drift: only-in-lock "
                                f"{sorted(lock_pkgs - sbom_pkgs)[:2]} "
                                "only-in-sbom "
                                f"{sorted(sbom_pkgs - lock_pkgs)[:2]}")
        else:
            ok(f"SBOM matches uv.lock ({len(sbom_pkgs)} packages)")
    for n, v in SCI_PINS.items():
        if (n, v) not in sbom_pkgs:
            fail_list(problems, f"scientific pin drift: {n}!={v}")
    prov = candidate.provenance
    subj = candidate.subjects
    if (zip_path.name, zh) not in subj:
        fail_list(problems, "provenance subject missing/mismatching the "
                             "release zip digest")
    else:
        ok("provenance subject binds the release zip digest")
    if subj != sums_set:
        fail_list(problems, "provenance subjects != SHA256SUMS set")
    # SLSA. The old guard was `"hosted" not in builder`, a substring test --
    # and the CI builder id was the literal placeholder
    # "PENDING-hosted-runner", which contains "hosted" and therefore SATISFIED
    # it. An unresolved value passed a trust check. Any non-NONE claim now
    # requires the exact authorized builder and a fully resolved policy, so no
    # mutable string can promote the level.
    lvl = idx["provenance"].get("slsa_level_claimed")
    prov_lvl = prov["predicate"].get("slsa_level_claimed", lvl)
    # Both containers, and both values, were typed in parse_candidate_bundle.
    if lvl not in (None, "NONE") or prov_lvl not in (None, "NONE"):
        fail_list(problems,
                  f"SLSA level claimed ({lvl!r}/{prov_lvl!r}) but no SLSA "
                  "level is authorized in this repository; admission criteria "
                  "for a level do not exist yet")
    else:
        ok("SLSA level claimed = NONE")

    pol = enforce_trust_policy(problems, candidate, idx, prov)
    if pol is None:
        pol = {}
    else:
        # The structural wildcard scan lives in release_trust.validate_policy
        # and covers every leaf, including nested list entries.
        ok("trust policy contains no wildcards")
    # NOTHING is re-read here. Every document was decoded strictly and parsed
    # during validation, and its text was retained; the scan examines exactly
    # those documents. A file replaced after validation cannot change what
    # this run scans.
    blob = candidate.scanned_text
    for pat in SECRET_PATTERNS:
        if re.search(pat, blob):
            fail_list(problems, f"secret-pattern hit: {pat[:24]}...")
    for pat in ABS_PATHS:
        if pat in blob:
            fail_list(problems, "absolute path in release metadata: "
                                 f"{pat}")
    for pat in CLAIM_VIOLATIONS:
        if pat in blob:
            fail_list(problems, f"claim-boundary violation: '{pat}'")
    # claims[] is index text and therefore mutable; the gate table inside the
    # signed zip is the authority. Recompute from it, and additionally require
    # the index to AGREE -- an index that understates the PASS count is a
    # lying envelope, not merely a redundant one.
    gate_csv = decode_member(
        problems, zf, f"{archive_root}/results_gate_table.csv")
    claimed = idx.get("claims", {}).get("scientific_gate_PASS_count")
    if gate_csv is None:
        # Fail closed on the claim itself. An unreadable gate table does not
        # mean zero PASS rows; it means the count is UNKNOWN, and an unknown
        # count can never satisfy "PASS must be zero".
        fail_list(problems,
                  "scientific PASS count cannot be recomputed: the gate table "
                  "inside the zip is unreadable; an unestablished count is "
                  "never treated as zero")
        n_pass = None
    else:
        n_pass = sum(1 for line in gate_csv.splitlines()[1:]
                     if re.search(r",PASS(,|$)", line))
        if n_pass:
            fail_list(problems, f"gate table inside zip has {n_pass} PASS")
        else:
            ok("gate table inside zip: scientific PASS count = 0")
    if n_pass is not None and claimed != n_pass:
        fail_list(problems,
                  f"release_index claims scientific_gate_PASS_count="
                  f"{claimed!r} but the gate table inside the zip has "
                  f"{n_pass}; the zip is authoritative")
    if claimed != 0:
        fail_list(problems, "scientific PASS count nonzero in release")
    # release_index.json is UNTRUSTED_ENVELOPE_METADATA: it is mutated after
    # signing by finalize_release_signing.py, so nothing here may confer
    # authority merely because the index says it. signing_status is treated as
    # consistency metadata -- the signed state is derived from facts (a
    # declared bundle that exists, parses and cryptographically verifies
    # against an authorized identity), and the string must AGREE with those
    # facts or the bundle is internally inconsistent.
    sig = idx.get("signature_bundles", [])
    status = idx.get("signing_status")
    declared = bool(sig)
    present = False
    if isinstance(sig, list):
        for entry in sig:
            if isinstance(entry, dict) and isinstance(entry.get("bundle"), str):
                bp, _why = _bundle_relative(bundle, entry["bundle"])
                if bp is not None and bp.exists() and bp.stat().st_size > 0:
                    present = True
    if status == "PENDING" and present:
        fail_list(problems,
                  "inconsistent bundle: signing_status is PENDING but a "
                  "signature bundle exists on disk")
    if status == "SIGNED" and not declared:
        fail_list(problems, "claims SIGNED with no bundles")
    if status == "SIGNED" and declared and not present:
        fail_list(problems,
                  "claims SIGNED and declares a bundle, but no declared "
                  "bundle exists on disk")
    if status not in ("PENDING", "SIGNED"):
        fail_list(problems, f"unknown signing_status {status!r}")

    if online:
        if not (status == "SIGNED" and declared and present):
            fail_list(problems, "online verification requested but no "
                                 "real signature exists (status="
                                 f"{status}, declared={declared}, "
                                 f"present={present}); absence is never "
                                 "success")
        else:
            # ---- PHASE 3: cryptographic authentication ------------------
            before = len(problems)
            _verify_sigstore(problems, zip_path, bundle, idx, sig,
                             trusted_pol)
            authenticated = len(problems) == before

            # ---- PHASE 4: authenticated-content checks ------------------
            # Only now are the archive's bytes evidence.
            if authenticated:
                zip_pol_ok = False
                try:
                    zip_pol_raw = zf.read(
                        f"{archive_root}/"
                        f"{release_trust.CANONICAL_POLICY_PATH}")
                except KeyError:
                    fail_list(problems,
                              "the signed zip contains no canonical trust "
                              "policy")
                    zip_pol_raw = None
                if zip_pol_raw is not None:
                    zip_pol_ok = bind_candidate_policy(
                        problems, "signed-zip", zip_pol_raw, trusted_canon,
                        trust_root.sha256 if trust_root else None)

                # The retained phase-0 bytes, not a fresh read: the copy
                # bound to the trust root must be the copy that was validated.
                bind_candidate_policy(problems, "bundle",
                                      candidate.policy_bytes, trusted_canon,
                                      trust_root.sha256 if trust_root
                                      else None)

                binding = read_release_binding(problems, zf, archive_root)
                if binding is not None and trusted_pol is not None:
                    enforce_authenticated_binding(
                        problems, trusted_pol, binding, zip_pol_ok)

                    # Recompute the reviewed payload digest from the
                    # AUTHENTICATED archive -- an offline consumer can do this
                    # with no Git checkout at all.
                    payload = {}
                    for n in zf.namelist():
                        if (not n.startswith(archive_root + "/")
                                or n.endswith("/")):
                            continue
                        rel = n[len(archive_root) + 1:]
                        if rel == release_trust.RELEASE_BINDING_NAME:
                            continue
                        payload[rel] = zf.read(n)
                    got = release_trust.payload_digest(payload)
                    want = str(trusted_pol["reviewed_payload_sha256"])
                    if got != want:
                        fail_list(problems,
                                  f"reviewed payload digest {got} != "
                                  f"authorized {want}; the signed content is "
                                  "not the reviewed content")
                    else:
                        ok("reviewed payload digest matches the authorized "
                           "value, recomputed from the signed archive")

                    # ---- PHASE 5: auxiliary metadata --------------------
                    check_auxiliary_provenance(problems, trusted_pol,
                                               binding, prov)
    elif status == "PENDING" and not problems:
        ok("signing status honestly PENDING (blockers recorded; no "
           "simulated signatures)")
    res = ("VERIFIED (offline)" if not problems
           else f"{len(problems)} FAILURES")
    print(f"\nRESULT: {res}")
    print("note: a signature proves origin and integrity only; never "
          "physics or hardware validation; software verification only")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--trusted-policy", default=None,
                    help="PATH to an owner-authorized trust policy obtained "
                         "INDEPENDENTLY of this release. Required for "
                         "--online: the policy shipped with a release is a "
                         "candidate, never the trust root.")
    ap.add_argument("--trusted-policy-sha256", default=None,
                    help="sha256 of the owner-authorized policy, as an "
                         "alternative trust root when the file itself is not "
                         "at hand")
    a = ap.parse_args()
    return verify(Path(a.zip), Path(a.bundle), a.online,
                  a.trusted_policy, a.trusted_policy_sha256)


if __name__ == "__main__":
    sys.exit(main())
