"""Hardware-validation governance layer (read-only; no hardware data yet).

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Scope and invariants (approved Stage 5, with corrections):
- Three data classes: SYNTHETIC (Stage 4, unchanged), HARDWARE_UNVERIFIED,
  HARDWARE_REVIEWED. Schema-valid HARDWARE_UNVERIFIED records enter
  QUARANTINE even when calibration, custody, controls, repetitions or
  uncertainty are incomplete -- every deficiency is recorded; such records
  NEVER enter evidence dossiers. Only fully complete HARDWARE_REVIEWED
  records with a valid human review record (decision ACCEPT_AS_EVIDENCE)
  may enter a gate-evidence dossier.
- No tool authors a review record or promotes evidence automatically.
- ``automatic_gate_effect`` is the constant "NONE" everywhere.
- Repetition requirements come ONLY from the plan authority
  (validation_matrix.csv). The matrix defines none today, so every
  repetition requirement is "UNKNOWN (not plan-specified)" and dossier
  completeness records it as UNRESOLVED_REQUIREMENT (readiness cannot be
  COMPLETE until the plan specifies it). No default count is invented.
- Custody validation checks ordering, required stages, timestamps and
  declared hashes. It is stated explicitly that a contiguous DOCUMENTED
  chain does not prove the absence of undocumented gaps.
- Forbidden-field rejection targets structured gate-status fields and
  controlled status values -- not arbitrary occurrences of words in free
  text.
- Default execution contains no hardware data and is deterministic; raw
  acquisition files live outside the canonical source (records carry
  references, sizes and SHA-256 values only).
- PASS remains zero; can_PASS_now=NO; canonical
  measured_in_this_system=false.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path

from .measurement_ingest_3d import QUANTITY_REGISTRY, validate_record

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"
SCHEMA_VERSION = "1.0"
AUTOMATIC_GATE_EFFECT = "NONE"
CUSTODY_CAVEAT = ("documented chain is ordered and stage-complete; this "
                  "does not prove the absence of undocumented custody gaps")
INTERPRETATION = ("evidence assembly for HUMAN gate review only; not "
                  "validation; automatic_gate_effect=NONE")
REPS_UNKNOWN = "UNKNOWN (not plan-specified)"

#: The declared reviewer roster. See :func:`load_reviewer_roster`.
REVIEWER_ROSTER_PATH = "hardware_reviewers.json"
#: The one ``registered_by`` value that terminates a chain of trust without
#: naming another roster entry. An installation that writes this into an
#: entry has registered a reviewer out of band, and has said so in a place
#: that can be read back.
REVIEWER_BOOTSTRAP = "OUT_OF_BAND_BOOTSTRAP"
REVIEWER_KINDS = ("HUMAN", "TOOL", "SERVICE")

DATA_CLASSES = ("SYNTHETIC", "HARDWARE_UNVERIFIED", "HARDWARE_REVIEWED")
CUSTODY_REQUIRED_STAGES = ("acquisition", "archive", "ingestion")

#: structured fields a measurement record may never carry, and the
#: controlled status vocabulary rejected when found in a *status-bearing*
#: field. (Free-text occurrences of words are NOT rejected.)
FORBIDDEN_STATUS_FIELDS = ("status", "gate_status", "result_status",
                           "gate_result", "pass")
CONTROLLED_STATUS_VALUES = ("PASS", "FAIL", "CONDITIONAL", "BLOCKED")

HW_REQUIRED_IDS = ("experiment_id", "sample_id", "operator_id",
                   "instrument_id", "calibration_id", "run_id")
HW_REQUIRED_BLOCKS = ("raw_data", "chain_of_custody", "calibration")

_MATRIX_CACHE = None


def plan_registry():
    """Plan authority: validation_matrix.csv items (verbatim)."""
    global _MATRIX_CACHE
    if _MATRIX_CACHE is None:
        rows = list(csv.DictReader(open("validation_matrix.csv")))
        _MATRIX_CACHE = {r["item"]: r for r in rows}
    return _MATRIX_CACHE


def required_repetitions(_item: str) -> str:
    """The plan defines no repetition counts today; never invent one."""
    return REPS_UNKNOWN


def _iso(ts) -> bool:
    try:
        datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return True
    except Exception:
        return False


def _forbidden_status_check(rec: dict) -> list:
    hits = []
    # name-based rule applies at RECORD level only (gate-status smuggling);
    # nested legitimate metadata like calibration.status is not a gate field
    for k in rec:
        if isinstance(k, str) and k.lower() in FORBIDDEN_STATUS_FIELDS:
            hits.append(f"forbidden record-level status field '{k}'")

    def walk(obj, path):
        if isinstance(obj, dict):
            for k, v in obj.items():
                kp = f"{path}.{k}" if path else k
                if isinstance(v, str) and v in CONTROLLED_STATUS_VALUES \
                        and isinstance(k, str) \
                        and k.lower().endswith(("status", "_state",
                                                 "disposition")):
                    hits.append(f"controlled status value '{v}' in "
                                f"status-bearing field '{kp}'")
                walk(v, kp)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]")
    walk(rec, "")
    return hits


def schema_validate_hardware(rec) -> tuple:
    """Structural gate for quarantine admission (correction #2):
    required fields present with basic types + Stage-4 quantity/units/
    alignment sanity + no forbidden status fields. Completeness of
    calibration/custody/controls/reps/uncertainty is NOT required here --
    those become recorded deficiencies."""
    reasons = []
    if not isinstance(rec, dict):
        return False, ["record is not an object"]
    if rec.get("data_class") not in ("HARDWARE_UNVERIFIED",
                                     "HARDWARE_REVIEWED"):
        return False, [f"data_class '{rec.get('data_class')}' is not a "
                       "hardware class"]
    for f in HW_REQUIRED_IDS:
        if not rec.get(f):
            reasons.append(f"missing identifier '{f}'")
    for b in HW_REQUIRED_BLOCKS:
        if not isinstance(rec.get(b), dict) and not (
                b == "chain_of_custody" and isinstance(rec.get(b), list)):
            reasons.append(f"missing block '{b}'")
    base = dict(rec)
    base["data_class"] = "SYNTHETIC"        # reuse Stage-4 structural checks
    ok, why = validate_record(base)
    reasons += [w for w in why
                if "data_class" not in w and "uncertainty" not in w]
    reasons += _forbidden_status_check(rec)
    if rec.get("experiment_id") and rec["experiment_id"] not in \
            plan_registry():
        reasons.append(f"experiment_id '{rec['experiment_id']}' not in the "
                       "plan registry (validation_matrix.csv)")
    return (len(reasons) == 0), reasons


def _calibration_deficiencies(rec) -> list:
    d = []
    cal = rec.get("calibration") or {}
    if cal.get("status") != "CALIBRATED":
        d.append(f"calibration status '{cal.get('status')}' (CALIBRATED "
                 "required for evidence)")
    for f in ("reference", "date", "valid_until"):
        if not cal.get(f):
            d.append(f"calibration missing '{f}'")
    if cal.get("date") and not _iso(cal["date"]):
        d.append("calibration date not ISO-8601")
    if cal.get("valid_until"):
        if not _iso(cal["valid_until"]):
            d.append("calibration valid_until not ISO-8601")
        elif _iso(cal.get("date", "")) and cal["valid_until"] < cal["date"]:
            d.append("calibration validity window ends before it begins")
        elif rec.get("timestamp") and _iso(rec["timestamp"]) and \
                rec["timestamp"] > cal["valid_until"]:
            d.append("calibration expired at acquisition time")
    return d


def _custody_deficiencies(rec) -> list:
    d = []
    chain = rec.get("chain_of_custody")
    if not isinstance(chain, list) or not chain:
        return ["chain_of_custody missing or empty"]
    stages = [e.get("action") for e in chain]
    for s in CUSTODY_REQUIRED_STAGES:
        if s not in stages:
            d.append(f"custody missing required stage '{s}'")
    ts = [e.get("timestamp") for e in chain]
    if not all(_iso(t) for t in ts):
        d.append("custody event timestamps not all ISO-8601")
    elif any(ts[i] > ts[i + 1] for i in range(len(ts) - 1)):
        d.append("custody events not in non-decreasing time order")
    for i, e in enumerate(chain):
        for f in ("actor", "action", "timestamp"):
            if not e.get(f):
                d.append(f"custody event [{i}] missing '{f}'")
    return d


def _raw_deficiencies(rec, raw_base_dir=None) -> list:
    d = []
    raw = rec.get("raw_data") or {}
    for f in ("filename", "byte_size", "sha256", "format",
              "acquisition_timestamp_utc"):
        if not raw.get(f):
            d.append(f"raw_data missing '{f}'")
    sha = raw.get("sha256", "")
    if sha and (len(sha) != 64 or any(c not in "0123456789abcdef"
                                      for c in sha.lower())):
        d.append("raw_data sha256 malformed")
    if raw_base_dir and raw.get("filename"):
        p = Path(raw_base_dir) / raw["filename"]
        if p.exists():
            actual = hashlib.sha256(p.read_bytes()).hexdigest()
            if actual != sha:
                d.append("raw-hash MISMATCH on re-verification")
        else:
            d.append("raw file not accessible for re-hash "
                     "(reference-only record)")
    else:
        d.append("raw file not accessible for re-hash "
                 "(reference-only record)")
    return d


def _controls_deficiencies(rec) -> list:
    ctl = rec.get("control_refs")
    if not isinstance(ctl, list) or not ctl:
        return ["no control/background measurement linked"]
    return []


def _uncertainty_deficiencies(rec) -> list:
    unc = rec.get("uncertainty") or {}
    d = []
    if unc.get("type") not in ("stddev", "interval"):
        d.append("hardware evidence requires an explicit uncertainty "
                 "(stddev or interval)")
    if not unc.get("method"):
        d.append("uncertainty missing 'method' "
                 "(statistical|systematic|combined)")
    return d


def full_deficiencies(rec, raw_base_dir=None) -> list:
    d = []
    d += _calibration_deficiencies(rec)
    d += _custody_deficiencies(rec)
    d += _raw_deficiencies(rec, raw_base_dir)
    d += _controls_deficiencies(rec)
    d += _uncertainty_deficiencies(rec)
    d.append(f"repetition requirement {REPS_UNKNOWN} -> "
             "UNRESOLVED_REQUIREMENT (plan authority defines no count; "
             "none invented)")
    return d


#: Keys excluded from the canonical form of a hardware record. The review's
#: own hash cannot be part of what it hashes, and transport/bookkeeping fields
#: must not make an otherwise-identical record hash differently.
RECORD_HASH_EXCLUDED_KEYS = ("record_sha256", "review", "review_record")


def canonical_record_bytes(rec) -> bytes:
    """Deterministic canonical form of a hardware/measurement record.

    Sorted keys, no insignificant whitespace, UTF-8, NFC-stable via
    ensure_ascii. Two records that differ in any governed field produce
    different bytes; the same record loaded twice produces identical bytes.
    """
    body = {k: v for k, v in rec.items() if k not in RECORD_HASH_EXCLUDED_KEYS}
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def compute_record_sha256(rec) -> str:
    return hashlib.sha256(canonical_record_bytes(rec)).hexdigest()


# ------------------------- who may author a review -------------------------
#
# WHAT THIS SECTION IS FOR, SAID BEFORE THE CODE
#
# This module has always claimed "human-only review authoring": it is in the
# module docstring, in governance_summary(), and in the authority registry.
# What enforced it was ``if rev.get("authored_by_tool")`` -- a refusal that
# fires only when the author confesses -- and a reviewer_id that was any
# non-empty string. A program that simply did not set the flag authored a
# review, and the review admitted a hardware record into a gate-evidence
# dossier (D-2026-42).
#
# WHY A SIBLING MODULE IS DESCRIBED HERE AND NEVER NAMED. The agent
# substrate is the other place in this repository that holds a HUMAN
# principal to a registration chain, and it is the right thing to point
# at. It is pointed at by description only: tests/test_agent_substrate_
# isolation.py scans the gate-computing tree for that package's NAME,
# with no exception for gate modules and none for comments, and it is
# right to be that blunt -- the cheapest guarantee that no gate depends
# on the substrate is that the string never appears here. It caught
# these very comments. authorities.json carries the cross-reference,
# where naming it costs nothing.
#
# WHAT IS AND IS NOT FIXED BELOW. A reviewer_id must now RESOLVE, against a
# declared roster, to an entry of kind HUMAN whose own registration traces to
# the out-of-band bootstrap. That is the same standard the agent substrate
# holds its HUMAN principals to (named in authorities.json; deliberately
# NOT named here -- see below), and it is worth being exact about what it
# buys, because that substrate already says it plainly: it cannot
# authenticate a person. The roster is a DECLARATION. Whoever can write it holds hardware
# review authority. What changes is that the authority is named, is a file
# that can be read and diffed, refuses to vouch for itself, and -- when it is
# absent or empty, as it is in this repository -- refuses EVERY review rather
# than accepting every one.


def load_reviewer_roster(path=None) -> dict:
    """Load the declared reviewer roster.

    Returns ``{"source", "present", "reviewers", "problems"}``. ``present``
    distinguishes "there is no roster" from "the roster names nobody"; both
    refuse every review, and a report that cannot tell them apart cannot say
    whether its scope was empty on purpose.

    A roster that cannot serve as a naming authority is returned with
    ``problems`` and no reviewers: duplicate ids, or two ids differing only
    in case. The second is not pedantry -- an identity comparison that treats
    ``A`` and ``a`` as the same subject is how the requester/reviewer
    separation used to be defeated, and one that treats them as different
    subjects while a human reads one name is how it would be defeated next.
    A roster containing both is refused instead of resolved either way.
    """
    p = Path(REVIEWER_ROSTER_PATH if path is None else path)
    out = {"source": str(p), "present": False, "reviewers": {},
           "problems": []}
    if not p.exists():
        out["problems"].append(f"no reviewer roster at {p}")
        return out
    out["present"] = True
    try:
        doc = json.loads(p.read_text())
    except Exception as exc:
        out["problems"].append(f"reviewer roster {p} is not JSON: {exc}")
        return out
    entries = doc.get("reviewers")
    if not isinstance(entries, list):
        out["problems"].append(f"reviewer roster {p} has no 'reviewers' list")
        return out
    seen, folded = {}, {}
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or not e.get("reviewer_id"):
            out["problems"].append(f"roster entry {i} has no reviewer_id")
            continue
        rid = e["reviewer_id"]
        if rid in seen:
            out["problems"].append(f"roster names {rid!r} twice")
            continue
        low = rid.lower()
        if low in folded:
            out["problems"].append(
                f"roster names both {folded[low]!r} and {rid!r}, which differ "
                "only in case; the roster cannot decide which subject a "
                "review means")
            continue
        seen[rid] = e
        folded[low] = rid
    if out["problems"]:
        return out
    out["reviewers"] = seen
    return out


def _registration_chain(rid, reviewers, seen=None) -> list:
    """Reasons why ``rid``'s registration does not trace to the bootstrap.

    Empty list means it does. The walk mirrors the agent substrate's HUMAN
    registry: an entry is usable only if a HUMAN who is themselves usable
    registered it, or the bootstrap did. Self-registration and cycles are refused by name, because
    a roster whose entries vouch for each other is a list of names, which is
    what this section exists to stop being enough.
    """
    seen = seen or []
    if rid in seen:
        return [f"registration chain for {rid!r} is a cycle "
                f"({' -> '.join(seen + [rid])})"]
    e = reviewers.get(rid)
    if e is None:
        return [f"registrar {rid!r} is not in the roster"]
    by = e.get("registered_by")
    if not by:
        return [f"{rid!r} declares no registered_by"]
    if by == REVIEWER_BOOTSTRAP:
        return []
    if by == rid:
        return [f"{rid!r} registered itself; only the out-of-band bootstrap "
                f"({REVIEWER_BOOTSTRAP}) may terminate a chain of trust"]
    reg = reviewers.get(by)
    if reg is None:
        return [f"{rid!r} was registered by {by!r}, which is not in the "
                "roster"]
    if reg.get("kind") != "HUMAN":
        return [f"{rid!r} was registered by {by!r}, whose kind is "
                f"{reg.get('kind')!r}; only a HUMAN may register a reviewer"]
    return _registration_chain(by, reviewers, seen + [rid])


def resolve_reviewer(reviewer_id, roster, at=None) -> tuple:
    """``(entry_or_None, reasons)`` for one reviewer identity.

    ``at`` is the ISO date the identity is being claimed for -- a review's
    own date. A reviewer retired before then did not write it.
    """
    if roster is None:
        roster = load_reviewer_roster()
    if roster.get("problems"):
        return None, list(roster["problems"])
    if not isinstance(reviewer_id, str) or not reviewer_id:
        return None, ["reviewer_id is missing or not a string"]
    reviewers = roster.get("reviewers") or {}
    if not reviewers:
        return None, [f"the reviewer roster at {roster.get('source')} "
                      "registers nobody, so no identity can be a reviewer"]
    e = reviewers.get(reviewer_id)
    if e is None:
        near = [k for k in reviewers if k.lower() == reviewer_id.lower()]
        if near:
            return None, [
                f"reviewer_id {reviewer_id!r} is not registered; the roster "
                f"has {near[0]!r}, which is a DIFFERENT identity (matching "
                "is exact and case-sensitive)"]
        return None, [f"reviewer_id {reviewer_id!r} is not registered in "
                      f"{roster.get('source')}"]
    reasons = []
    if e.get("kind") != "HUMAN":
        reasons.append(
            f"reviewer {reviewer_id!r} is registered as kind "
            f"{e.get('kind')!r}; only a HUMAN may author a review")
    ret = e.get("retired_on")
    if ret:
        if at is None or str(at) >= str(ret):
            reasons.append(f"reviewer {reviewer_id!r} was retired on {ret}")
    reasons += _registration_chain(reviewer_id, reviewers)
    return (None if reasons else e), reasons


def validate_review_record(rev, record=None, roster=None) -> tuple:
    """Validate a human review, BIND it to the record, and RESOLVE its author.

    Three separate things, and they used to be one and a half.

    * record_sha256 was once checked only for being non-empty, so a review
      could be paired with a modified record carrying the same
      measurement_id. When ``record`` is supplied the hash is recomputed
      from the record's canonical form and must match exactly.
    * reviewer_id was once any non-empty string, and "human-only" rested on
      ``authored_by_tool`` -- a refusal that fires only when the author
      confesses. It must now resolve through :func:`resolve_reviewer`.
      ``authored_by_tool`` is still refused, but it is no longer what is
      holding the rule up: it is a courtesy for an honest tool, kept because
      it costs nothing, and a review that omits it is now refused anyway.

    ``roster`` defaults to the declared roster on disk. Passing one
    explicitly is how a caller validates against a roster it already loaded;
    passing ``{"source": ..., "present": False, "reviewers": {}}`` is how a
    caller says "no authority", and every review is then refused.
    """
    reasons = []
    if not isinstance(rev, dict):
        return False, ["review_record is not an object"]
    for f in ("reviewer_id", "review_date", "checklist_version",
              "decision", "record_sha256"):
        if not rev.get(f):
            reasons.append(f"review_record missing '{f}'")
    if rev.get("decision") not in ("ACCEPT_AS_EVIDENCE", "REJECT"):
        reasons.append("review decision must be ACCEPT_AS_EVIDENCE|REJECT")
    if rev.get("review_date") and not _iso(rev["review_date"]):
        reasons.append("review_date not ISO-8601")
    if rev.get("authored_by_tool"):
        reasons.append("review records authored by tools are invalid by "
                       "governance rule")
    # THE LOAD-BEARING HUMAN CHECK. Not the flag above it.
    _, who = resolve_reviewer(rev.get("reviewer_id"), roster,
                              at=rev.get("review_date"))
    reasons += who

    claimed = rev.get("record_sha256") or ""
    if claimed:
        if len(claimed) != 64 or any(c not in "0123456789abcdef"
                                     for c in claimed.lower()):
            reasons.append("review record_sha256 malformed (expected 64 hex "
                           "characters)")
        elif record is not None:
            actual = compute_record_sha256(record)
            if actual.lower() != claimed.lower():
                reasons.append(
                    "review record_sha256 does not bind to this record "
                    f"(review claims {claimed[:16]}..., record canonicalizes "
                    f"to {actual[:16]}...); the reviewed record was modified "
                    "or replaced")
    elif record is not None:
        reasons.append("review has no record_sha256 to bind against")
    return (len(reasons) == 0), reasons


def build_quarantine_report(records, raw_base_dir=None) -> dict:
    """HARDWARE_UNVERIFIED intake (correction #2): schema-valid records
    are quarantined with every deficiency recorded; structural failures
    are rejected. Nothing here ever reaches a dossier."""
    quarantined, rejected = [], []
    for rec in records:
        ok, reasons = schema_validate_hardware(rec)
        if not ok:
            rejected.append({"measurement_id":
                             rec.get("measurement_id", "(missing)")
                             if isinstance(rec, dict) else "(not an object)",
                             "reasons": reasons,
                             "disposition": "REJECTED (fail-closed)"})
            continue
        quarantined.append({
            "measurement_id": rec["measurement_id"],
            "experiment_id": rec["experiment_id"],
            "quantity": rec["quantity"],
            "data_class": rec["data_class"],
            "deficiencies": full_deficiencies(rec, raw_base_dir),
            "custody_caveat": CUSTODY_CAVEAT,
            "standing": "unreviewed hardware claim -- no evidentiary "
                        "standing; never enters dossiers",
            "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
            "label": LABEL})
    return {"schema_version": SCHEMA_VERSION,
            "report": "hardware_quarantine",
            "n_quarantined": len(quarantined), "n_rejected": len(rejected),
            "quarantined": quarantined, "rejected": rejected,
            "measured_in_this_system": False, "can_PASS_now": "NO",
            "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
            "interpretation": INTERPRETATION, "label": LABEL}


def build_evidence_dossier(gate_id, records, reviews, raw_base_dir=None,
                          campaign_id=None, matrix_items=None,
                          run_ids=None, manifest_refs=None, roster=None):
    """Only complete HARDWARE_REVIEWED records with a valid
    ACCEPT_AS_EVIDENCE human review enter (correction #3). Completeness =
    zero deficiencies except the standing unresolved repetition
    requirement, which forces readiness INCOMPLETE (correction #1).

    The roster is loaded ONCE here and threaded into every review check, so
    one dossier is decided against one authority. Loading it per record
    would let the authority change halfway down the list, and the dossier
    would then report a verdict no single roster ever gave.

    ``reviewer_authority`` in the report says which roster decided and how
    many reviewers it could offer. A dossier with no entries because nobody
    is registered and a dossier with no entries because every record was
    deficient are different states, and a reader who cannot tell them apart
    cannot tell whether this check examined anything (cf. D-2026-39).
    """
    if roster is None:
        roster = load_reviewer_roster()
    entries, excluded = [], []
    for rec in records:
        rid = rec.get("measurement_id", "(missing)")
        ok, reasons = schema_validate_hardware(rec)
        if not ok or rec.get("data_class") != "HARDWARE_REVIEWED":
            excluded.append({"measurement_id": rid, "why":
                             reasons or ["not HARDWARE_REVIEWED"]})
            continue
        rev = reviews.get(rid)
        # Bind the review to THIS record, not merely to its measurement_id.
        rok, rwhy = validate_review_record(rev, record=rec,
                                           roster=roster) if rev else \
            (False, ["no review record"])
        if not rok or rev.get("decision") != "ACCEPT_AS_EVIDENCE":
            excluded.append({"measurement_id": rid,
                             "why": rwhy or ["review did not accept"]})
            continue
        defs = full_deficiencies(rec, raw_base_dir)
        hard = [x for x in defs if "UNRESOLVED_REQUIREMENT" not in x]
        if hard:
            excluded.append({"measurement_id": rid, "why": hard})
            continue
        entries.append({"measurement_id": rid,
                        "experiment_id": rec["experiment_id"],
                        "campaign_id": rec.get("campaign_id",
                                               campaign_id or "-"),
                        "run_id": rec.get("run_id", "-"),
                        "quantity": rec["quantity"],
                        "raw_data_ref": (rec.get("raw_data") or {})
                        .get("sha256", "-"),
                        "processed_data_refs":
                            rec.get("processed_data_refs", []),
                        "calibration_ref": rec.get("calibration_id", "-"),
                        "uncertainty_analysis_ref":
                            rec.get("uncertainty_ref",
                                    "(inline uncertainty block)"),
                        "deviations": rec.get("deviations", []),
                        "stop_events": rec.get("stop_events", []),
                        "review": {k: rev[k] for k in
                                   ("reviewer_id", "review_date",
                                    "decision", "record_sha256")},
                        "unresolved_requirements":
                            [x for x in defs if "UNRESOLVED" in x],
                        "custody_caveat": CUSTODY_CAVEAT})
    readiness = "INCOMPLETE"
    reasons = ["repetition requirement " + REPS_UNKNOWN] if entries else \
        ["no accepted reviewed evidence"]
    return {"schema_version": SCHEMA_VERSION,
            "report": "gate_evidence_dossier", "gate_id": gate_id,
            "campaign_id": campaign_id, "run_ids": run_ids or [],
            "matrix_items": matrix_items or [],
            "manifest_refs": manifest_refs or [],
            "reviewer_authority": {
                "roster": roster.get("source"),
                "roster_present": roster.get("present"),
                "n_registered_reviewers": len(roster.get("reviewers") or {}),
                "roster_problems": list(roster.get("problems") or []),
                "basis": "declared roster resolved to kind HUMAN with a "
                         "registration chain reaching "
                         + REVIEWER_BOOTSTRAP,
                "not": "authentication; this package cannot establish that "
                       "a roster entry is a person or that a review "
                       "attributed to one was written by them"},
            "permitted_claims": ["consistency/evidence context for human "
                                 "review only"],
            "forbidden_claims": FORBIDDEN_CLAIMS,
            "n_entries": len(entries), "entries": entries,
            "n_excluded": len(excluded), "excluded": excluded,
            "review_readiness": readiness,
            "readiness_reasons": reasons,
            "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
            "measured_in_this_system": False, "can_PASS_now": "NO",
            "interpretation": INTERPRETATION, "label": LABEL}


# ------------------------- hash-chained audit log -------------------------

def append_audit(path, event: dict) -> str:
    """Append-only JSONL; each line embeds the previous line's SHA-256."""
    p = Path(path)
    prev = "0" * 64
    if p.exists():
        lines = p.read_text().strip().splitlines()
        if lines:
            prev = hashlib.sha256(lines[-1].encode()).hexdigest()
    entry = {"prev_sha256": prev, "event": event,
             "automatic_gate_effect": AUTOMATIC_GATE_EFFECT}
    line = json.dumps(entry, sort_keys=True)
    with open(p, "a") as f:
        f.write(line + "\n")
    return hashlib.sha256(line.encode()).hexdigest()


def verify_audit_chain(path) -> tuple:
    p = Path(path)
    if not p.exists():
        return True, "empty (no log)"
    lines = p.read_text().strip().splitlines()
    prev = "0" * 64
    for i, line in enumerate(lines):
        try:
            obj = json.loads(line)
        except Exception:
            return False, f"line {i} not JSON"
        if obj.get("prev_sha256") != prev:
            return False, f"chain break at line {i}"
        prev = hashlib.sha256(line.encode()).hexdigest()
    return True, f"chain intact ({len(lines)} entries, head {prev[:16]}...)"


# ------------- Stage-6 integration: registry + dossier binding -------------

_REG_CACHE = None


def load_experiment_registry():
    """Stage-6 experiment registry (planning authority for experiment_id;
    supplements the validation-matrix item registry)."""
    global _REG_CACHE
    if _REG_CACHE is None:
        _REG_CACHE = json.loads(
            Path("experiment_registry.json").read_text())
    return {e["experiment_id"]: e for e in _REG_CACHE["experiments"]}


def validate_matrix_update_request(doc, roster=None) -> tuple:
    """Validate + cross-check a HUMAN-authored matrix update request.
    Tooling may archive it (append_audit); tooling never applies it,
    never changes a gate, never mutates a parameter, never creates PASS,
    and never treats the requester as the reviewer.

    "HUMAN-authored" is now checked rather than asserted. ``requester`` and
    every ``review_ids`` entry must resolve through
    :func:`resolve_reviewer`, which is also what closed the separation rule
    below: ``doc["requester"] in doc["review_ids"]`` compared two strings
    nobody had resolved, so respelling the requester -- a change of case was
    enough -- made one person into two subjects and the separation passed
    (D-2026-42). Identities that must resolve cannot be respelled into
    existence.

    Consequence, stated so it is not discovered as a surprise: in THIS
    repository the roster registers nobody, so no matrix update request can
    be valid today. That is the same state the agent substrate describes for
    escalations -- the mechanism exists, its input does not -- and it is
    the honest reading of a package where PASS is zero.
    """
    req = ("request_id", "item", "current_status", "proposed_status",
           "experiment_ids", "dossier_refs", "raw_data_refs",
           "calibration_refs", "uncertainty_refs", "review_ids",
           "requester", "request_date", "rationale",
           "claim_boundary_confirmation", "automatic_application",
           "schema_version")
    reasons = []
    if not isinstance(doc, dict):
        return False, ["request is not an object"]
    for f in req:
        if f not in doc:
            reasons.append(f"missing required field '{f}'")
    if reasons:
        return False, reasons
    if doc["automatic_application"] is not False:
        reasons.append("automatic_application must be false (requests "
                       "configured for automatic application are "
                       "invalid)")
    if roster is None:
        roster = load_reviewer_roster()
    _, why_req = resolve_reviewer(doc["requester"], roster,
                                  at=doc.get("request_date"))
    reasons += [f"requester: {w}" for w in why_req]
    if not doc["review_ids"]:
        reasons.append("at least one review_id is required")
    else:
        for rid in doc["review_ids"]:
            _, why_rev = resolve_reviewer(rid, roster,
                                          at=doc.get("request_date"))
            reasons += [f"review_id: {w}" for w in why_rev]
        # Decided on resolved identity, not on the spelling in the document.
        if doc["requester"] in doc["review_ids"]:
            reasons.append("requester may not be a reviewer")
    if not doc["experiment_ids"]:
        reasons.append("at least one experiment_id is required")
    else:
        known = load_experiment_registry()
        for e in doc["experiment_ids"]:
            if e not in known:
                reasons.append(f"unknown experiment_id '{e}' (not in "
                               "experiment_registry.json)")
    if not (doc["dossier_refs"] or doc["raw_data_refs"]):
        reasons.append("evidence references required (dossier and/or "
                       "raw-data refs)")
    if not _iso(doc["request_date"]):
        reasons.append("request_date not ISO-8601")
    if doc["item"] and Path("validation_matrix.csv").exists():
        import csv as _csv
        items = {r["item"] for r in
                 _csv.DictReader(open("validation_matrix.csv"))}
        if doc["item"] not in items:
            reasons.append(f"item '{doc['item']}' not in "
                           "validation_matrix.csv")
    return (len(reasons) == 0), reasons


FORBIDDEN_CLAIMS = [
    "hardware-validated", "experimentally demonstrated isotope contrast",
    "completed growth", "validated digital twin", "COMSOL-equivalent",
    "any PASS-status vocabulary"]


def governance_summary() -> dict:
    """Deterministic default-execution record: no hardware data exists.

    ``review_authority`` reports the roster this package would decide
    against and how many reviewers it holds. It is in the readiness artifact
    rather than only in this docstring because "human-only review authoring"
    was asserted in three places -- the module docstring, this summary and
    the authority registry -- while the code refused only a review that
    confessed to being tool-authored. A claim about who may act belongs
    where a reader can check it against a number.
    """
    roster = load_reviewer_roster()
    return {"schema_version": SCHEMA_VERSION,
            "data_classes": list(DATA_CLASSES),
            "default_execution": "NO_HARDWARE_DATA (deterministic; "
                                 "hardware paths are operator-invoked only)",
            "repetition_requirements": REPS_UNKNOWN,
            "review_authoring": "human-only; tools may verify, never author "
                                "or promote",
            "review_authority": {
                "roster": roster["source"],
                "roster_present": roster["present"],
                "n_registered_reviewers": len(roster["reviewers"]),
                "roster_problems": list(roster["problems"]),
                "enforced_by": "reviewer_id must resolve to a roster entry "
                               "of kind HUMAN whose registration chain "
                               "reaches " + REVIEWER_BOOTSTRAP,
                "not": "authentication; the roster is a declaration, and "
                       "whoever can write it holds this authority",
                "consequence_today": "no reviewer is registered, so no "
                                     "review record can be valid and no "
                                     "gate-evidence dossier can have "
                                     "entries"},
            "custody_caveat": CUSTODY_CAVEAT,
            "raw_data_policy": "references + sizes + SHA-256 only; raw "
                               "acquisition files stay outside the source",
            "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
            "measured_in_this_system": False, "can_PASS_now": "NO",
            "label": LABEL}
