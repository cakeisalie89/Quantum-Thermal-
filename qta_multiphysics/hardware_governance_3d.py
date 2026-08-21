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


def validate_review_record(rev, record=None) -> tuple:
    """Validate a human review, and BIND it to the exact record reviewed.

    record_sha256 used to be checked only for being non-empty, so a review
    could be paired with a modified record carrying the same measurement_id
    and the dossier would accept it and copy the stale hash straight through.
    When ``record`` is supplied the hash is now recomputed from the record's
    canonical form and must match exactly.
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
                          run_ids=None, manifest_refs=None):
    """Only complete HARDWARE_REVIEWED records with a valid
    ACCEPT_AS_EVIDENCE human review enter (correction #3). Completeness =
    zero deficiencies except the standing unresolved repetition
    requirement, which forces readiness INCOMPLETE (correction #1)."""
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
        rok, rwhy = validate_review_record(rev, record=rec) if rev else \
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


def validate_matrix_update_request(doc) -> tuple:
    """Validate + cross-check a HUMAN-authored matrix update request.
    Tooling may archive it (append_audit); tooling never applies it,
    never changes a gate, never mutates a parameter, never creates PASS,
    and never treats the requester as the reviewer."""
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
    if not doc["review_ids"]:
        reasons.append("at least one review_id is required")
    elif doc["requester"] in doc["review_ids"]:
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
    """Deterministic default-execution record: no hardware data exists."""
    return {"schema_version": SCHEMA_VERSION,
            "data_classes": list(DATA_CLASSES),
            "default_execution": "NO_HARDWARE_DATA (deterministic; "
                                 "hardware paths are operator-invoked only)",
            "repetition_requirements": REPS_UNKNOWN,
            "review_authoring": "human-only; tools may verify, never author "
                                "or promote",
            "custody_caveat": CUSTODY_CAVEAT,
            "raw_data_policy": "references + sizes + SHA-256 only; raw "
                               "acquisition files stay outside the source",
            "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
            "measured_in_this_system": False, "can_PASS_now": "NO",
            "label": LABEL}
