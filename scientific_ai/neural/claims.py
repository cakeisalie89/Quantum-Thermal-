"""What may be said about a learned model, computed from evidence.

THE CLAIMS ARE INDEPENDENT (directive s.51)

Each claim below holds only on evidence bound BY DIGEST to the subject it
is about. None implies another: a meta-validated architecture is not
allocated, an allocated one is not trained, a trained one is not
scientifically established, a development model's training is not the
flagship's.

===============================  ==========================================
claim                            holds when
===============================  ==========================================
ARCHITECTURE_DEFINED             a model manifest whose configuration
                                 re-derives the subject digest
ARCHITECTURE_PARAMETER_VERIFIED  the manifest's counts equal the exact
                                 count recomputed now AND a meta report for
                                 the subject found the abstract tensors
                                 count the same, category by category
ARCHITECTURE_META_VALIDATED      that meta report PASSED: forward traced,
                                 routing legal, nothing allocated, a real
                                 allocation of the subject refused
DEVELOPMENT_MODEL_TRAINED        a COMPLETED training manifest bound to the
                                 SUBJECT's digest
CHECKPOINT_RELOAD_VALIDATED      a checkpoint of the subject whose reload
                                 reproduced its outputs (evaluation report)
DISTRIBUTED_SOFTWARE_READY       a distributed report whose checks all
                                 passed, on any execution profile; on a
                                 simulated one (SIMULATED_PROFILES) this is
                                 SOFTWARE-PATH VALIDATION ONLY, and its
                                 reason says so and names what never ran
DISTRIBUTED_HARDWARE_VALIDATED   the same on an execution profile that is
                                 NOT simulated -- never in this tranche
LARGE_MODEL_TRAINED              a COMPLETED training manifest bound to the
                                 subject's digest, the subject having at
                                 least LARGE_MODEL_THRESHOLD parameters
SCIENTIFIC_PERFORMANCE_          an authority acceptance outside this
ESTABLISHED                      package; the store refuses one for every
                                 learned record in this tranche
===============================  ==========================================

A family member's evidence is reported for the flagship as FAMILY_MEMBER
scope with the member's digest -- "a development-scale member was trained",
never "the flagship was trained".

THE STATUS LADDER (directive s.49)

A subject's status moves along explicit edges only (:data:`TRANSITIONS`),
each requiring evidence; a history is a list of transitions and an old
status is never rewritten as if the new one had always held.
"""
from __future__ import annotations

from scientific.identity import digest

from . import accounting, manifests
from .config import ConfigError, ModelConfig

PREDICTION_SEMANTICS = manifests.PREDICTION_SEMANTICS

CLAIMS = ("ARCHITECTURE_DEFINED", "ARCHITECTURE_PARAMETER_VERIFIED",
          "ARCHITECTURE_META_VALIDATED", "DEVELOPMENT_MODEL_TRAINED",
          "CHECKPOINT_RELOAD_VALIDATED", "DISTRIBUTED_SOFTWARE_READY",
          "DISTRIBUTED_HARDWARE_VALIDATED", "LARGE_MODEL_TRAINED",
          "SCIENTIFIC_PERFORMANCE_ESTABLISHED")

#: Below this many trainable parameters a trained model is a development
#: model, whatever its variant is called.
LARGE_MODEL_THRESHOLD = 1_000_000_000

#: Execution profiles that are not hardware validation, whatever passed.
SIMULATED_PROFILES = ("CPU_DEVELOPMENT", "SIMULATED_MULTI_DEVICE")

STATUSES = ("ARCHITECTURE_DEFINED", "META_VALIDATED",
            "DEVELOPMENT_PIPELINE_VALIDATED", "TRAINING_CANDIDATE",
            "TRAINED", "EVALUATED", "ACCEPTED_FOR_LIMITED_USE", "REJECTED",
            "INVALIDATED")
#: status -> the statuses it may move to. REJECTED and INVALIDATED are
#: terminal; recovery is a NEW subject with new evidence.
TRANSITIONS = {
    None: ("ARCHITECTURE_DEFINED",),
    "ARCHITECTURE_DEFINED": ("META_VALIDATED",),
    "META_VALIDATED": ("DEVELOPMENT_PIPELINE_VALIDATED", "TRAINED"),
    "DEVELOPMENT_PIPELINE_VALIDATED": ("TRAINING_CANDIDATE",),
    "TRAINING_CANDIDATE": ("TRAINED",),
    "TRAINED": ("EVALUATED",),
    "EVALUATED": ("ACCEPTED_FOR_LIMITED_USE", "REJECTED"),
    "ACCEPTED_FOR_LIMITED_USE": ("INVALIDATED",),
    "REJECTED": (),
    "INVALIDATED": (),
}
#: The edges this package can never take: they need an authority decision
#: by a principal who is not the model and not its trainer. TRAINING_
#: CANDIDATE is a decision to SPEND compute, an owner's to make.
DECIDED_OUTSIDE = ("TRAINING_CANDIDATE", "ACCEPTED_FOR_LIMITED_USE",
                   "REJECTED", "INVALIDATED")


class ClaimError(ValueError):
    pass


def allowed(current, nxt) -> bool:
    return nxt in TRANSITIONS.get(current, ())


def manifest_identity(doc: dict) -> str:
    """A model manifest's identity: the digest of everything but its own
    ``claim_status`` (which cites the manifest, and so cannot be part of
    what it cites)."""
    return digest({k: v for k, v in doc.items() if k != "claim_status"})


def _by_schema(evidence) -> dict:
    out: dict = {}
    for doc in evidence:
        if manifests.problems(doc):
            continue        # a malformed document is evidence of nothing
        out.setdefault(doc["schema"], []).append(doc)
    return out


def _subject_config(doc) -> ModelConfig | None:
    try:
        return ModelConfig.from_dict(doc["configuration"])
    except (ConfigError, KeyError, TypeError):
        return None


def _manifest_counts_hold(doc, cfg: ModelConfig) -> bool:
    pc = accounting.count(cfg).to_dict()
    return (doc["trainable_parameters"] == pc["trainable_parameters"]
            and doc["total_parameters"] == pc["total_parameters"]
            and doc["expert_parameters"] == pc["expert_parameters"]
            and doc["shared_active_parameters"]
            == pc["shared_active_parameters"]
            and doc["expert_active_parameters"]
            == pc["expert_active_parameters"]
            and doc["estimated_active_parameters_per_token"]
            == pc["estimated_total_active_parameters_per_token"]
            and doc["parameter_accounting"] == pc)


def _meta_ok(rep, cfg: ModelConfig) -> tuple:
    pc = accounting.count(cfg)
    counts = (rep["counts_equal"] is True
              and rep["analytic_by_category"] == pc.by_category
              and rep["abstract_by_category"] == pc.by_category
              and rep["abstract_trainable"] == pc.trainable_parameters
              and rep["abstract_non_trainable"]
              == pc.non_trainable_parameters)
    guard = rep["allocation_guard"]
    over = isinstance(guard, dict) and isinstance(
        guard.get("would_be_parameter_bytes"), int) and isinstance(
        guard.get("real_allocation_limit_bytes"), int) and \
        guard["would_be_parameter_bytes"] > guard[
            "real_allocation_limit_bytes"]
    refusal_ok = rep["real_allocation_refused"] is True if over else (
        rep["real_allocation_refused"] is None and isinstance(guard, dict)
        and guard.get("real_allocation_limit_bytes") is not None)
    passed = (counts and rep["result"] == "PASS"
              and isinstance(guard, dict) and guard.get("passed") is True
              and refusal_ok
              and isinstance(rep["forward"], dict)
              and rep["forward"].get("traced") is True
              and isinstance(rep["routing"], dict)
              and rep["routing"].get("legal") is True)
    return counts, passed


def _trained(docs, subject: str) -> list:
    return [t for t in docs.get(manifests.TRAINING_MANIFEST, ())
            if t["model_configuration_digest"] == subject
            and t["completion_state"] == "COMPLETED"]


def _reload_ok(docs, subject: str) -> list:
    out = []
    for ev in docs.get(manifests.EVALUATION_REPORT, ()):
        r = ev["reload"]
        if ev["model_config_digest"] == subject and isinstance(r, dict) \
                and r.get("outputs_equal") is True \
                and r.get("digest_verified") is True:
            out.append(ev)
    return out


def evaluate(subject: str, evidence, *, family_members=()) -> dict:
    """Which claims hold for ``subject`` (a configuration digest).

    ``family_members``: configuration digests of OTHER members of the same
    family whose training and reload evidence may be reported, in
    FAMILY_MEMBER scope, as the family's development pipeline. Their
    evidence never satisfies a SUBJECT-scope claim."""
    docs = _by_schema(evidence)
    out = {c: {"holds": False, "scope": "SUBJECT", "evidence": [],
               "reason": "no evidence"} for c in CLAIMS}

    mans = [m for m in docs.get(manifests.MODEL_MANIFEST, ())
            if m["configuration_digest"] == subject]
    cfg = None
    for m in mans:
        c = _subject_config(m)
        if c is not None and c.digest() == subject:
            cfg = c
            out["ARCHITECTURE_DEFINED"] = {
                "holds": True, "scope": "SUBJECT",
                "evidence": [manifest_identity(m)],
                "reason": "the manifest's configuration re-derives the "
                          "subject digest"}
            if _manifest_counts_hold(m, c):
                manifest = m
                break
    else:
        manifest = None

    if cfg is not None:
        for rep in docs.get(manifests.META_REPORT, ()):
            if rep["config_digest"] != subject:
                continue
            counts, passed = _meta_ok(rep, cfg)
            if counts and manifest is not None:
                out["ARCHITECTURE_PARAMETER_VERIFIED"] = {
                    "holds": True, "scope": "SUBJECT",
                    "evidence": [manifest_identity(manifest),
                                 digest(rep)],
                    "reason": "manifest, recomputed count and abstract "
                              "tensors agree category by category"}
            if passed:
                out["ARCHITECTURE_META_VALIDATED"] = {
                    "holds": True, "scope": "SUBJECT",
                    "evidence": [digest(rep)],
                    "reason": "resolved by abstract evaluation with no "
                              "allocation; a real allocation was refused"}

    trained = _trained(docs, subject)
    if trained:
        out["DEVELOPMENT_MODEL_TRAINED"] = {
            "holds": True, "scope": "SUBJECT",
            "evidence": [digest(t) for t in trained],
            "reason": "a completed training run of this configuration"}
        params = trained[0]["parameter_count"]
        if cfg is not None:
            params = accounting.count(cfg).trainable_parameters
        if isinstance(params, int) and params >= LARGE_MODEL_THRESHOLD:
            out["LARGE_MODEL_TRAINED"] = {
                "holds": True, "scope": "SUBJECT",
                "evidence": [digest(t) for t in trained],
                "reason": f"a completed training run of this "
                          f"{params}-parameter configuration"}
    else:
        for member in family_members:
            if member == subject:
                continue
            mt = _trained(docs, member)
            if mt:
                out["DEVELOPMENT_MODEL_TRAINED"] = {
                    "holds": True, "scope": "FAMILY_MEMBER",
                    "member": member, "evidence": [digest(t) for t in mt],
                    "reason": "a development-scale MEMBER of the family "
                              "was trained; the subject was not"}
                break

    reloads = _reload_ok(docs, subject)
    if reloads:
        out["CHECKPOINT_RELOAD_VALIDATED"] = {
            "holds": True, "scope": "SUBJECT",
            "evidence": [digest(e) for e in reloads],
            "reason": "a checkpoint of this configuration reloaded to "
                      "identical outputs"}
    else:
        for member in family_members:
            if member == subject:
                continue
            mr = _reload_ok(docs, member)
            if mr:
                out["CHECKPOINT_RELOAD_VALIDATED"] = {
                    "holds": True, "scope": "FAMILY_MEMBER",
                    "member": member, "evidence": [digest(e) for e in mr],
                    "reason": "a family MEMBER's checkpoint reloaded; no "
                              "checkpoint of the subject exists"}
                break

    for rep in docs.get(manifests.DISTRIBUTED_REPORT, ()):
        prof = rep["execution_profile"]
        ok = rep["result"] == "PASS" and isinstance(rep["checks"], list) \
            and rep["checks"] and all(c.get("passed") is True
                                      for c in rep["checks"])
        if not ok or not isinstance(prof, dict):
            continue
        hardware = prof.get("kind") not in SIMULATED_PROFILES \
            and prof.get("hardware_executed") is True
        if hardware:
            reason = f"every parallel check passed on {prof.get('kind')}"
        else:
            # The identifier is kept for the documents that already carry
            # it; what it means here is said in full, so a simulated run is
            # never read as hardware validation.
            plan_only = [ax for ax in ("tensor", "pipeline")
                         if not any(str(c.get("check", "")).startswith(ax)
                                    and c.get("executed", True) is not False
                                    for c in rep["checks"])]
            reason = (f"{prof.get('kind')} SOFTWARE-PATH VALIDATION ONLY: "
                      f"every parallel check passed on "
                      f"{len(rep.get('devices') or ())} devices of that "
                      "profile; hardware_executed is not true, so this is "
                      "not distributed hardware validation")
            if plan_only:
                reason += (f"; {' and '.join(plan_only)} parallelism "
                           f"{'is' if len(plan_only) == 1 else 'are'} "
                           "PLAN_ONLY (validated as plans, never executed)")
        out["DISTRIBUTED_SOFTWARE_READY"] = {
            "holds": True, "scope": "FAMILY",
            "evidence": [digest(rep)], "reason": reason}
        if hardware:
            out["DISTRIBUTED_HARDWARE_VALIDATED"] = {
                "holds": True, "scope": "FAMILY",
                "evidence": [digest(rep)],
                "reason": f"executed on {prof.get('kind')}"}

    out["SCIENTIFIC_PERFORMANCE_ESTABLISHED"]["reason"] = (
        "established only by an authority acceptance outside this "
        "package; the store refuses one for learned records")
    for c, v in out.items():
        if not v["holds"] and v["reason"] == "no evidence" \
                and c == "DISTRIBUTED_HARDWARE_VALIDATED":
            v["reason"] = "no execution on distributed hardware"
    return out


def derive_status(claims: dict) -> list:
    """The status history the claims SUPPORT, as explicit transitions from
    nothing. Edges in :data:`DECIDED_OUTSIDE` are never derived."""
    history = []
    cur = None

    def step(nxt, why):
        nonlocal cur
        if not allowed(cur, nxt):
            raise ClaimError(f"{cur} -> {nxt} is not an edge")
        history.append({"from": cur, "to": nxt, "evidence": why})
        cur = nxt

    if not claims["ARCHITECTURE_DEFINED"]["holds"]:
        return history
    step("ARCHITECTURE_DEFINED", claims["ARCHITECTURE_DEFINED"]["evidence"])
    if not (claims["ARCHITECTURE_META_VALIDATED"]["holds"]
            and claims["ARCHITECTURE_PARAMETER_VERIFIED"]["holds"]):
        return history
    step("META_VALIDATED", claims["ARCHITECTURE_META_VALIDATED"]["evidence"])
    t = claims["DEVELOPMENT_MODEL_TRAINED"]
    r = claims["CHECKPOINT_RELOAD_VALIDATED"]
    if t["holds"] and t["scope"] == "SUBJECT":
        step("TRAINED", t["evidence"])
        if r["holds"] and r["scope"] == "SUBJECT":
            step("EVALUATED", r["evidence"])
    elif t["holds"] and r["holds"] and t["scope"] == "FAMILY_MEMBER" \
            and r["scope"] == "FAMILY_MEMBER":
        step("DEVELOPMENT_PIPELINE_VALIDATED", t["evidence"] + r["evidence"])
    return history


def current_status(claims: dict):
    h = derive_status(claims)
    return h[-1]["to"] if h else None
