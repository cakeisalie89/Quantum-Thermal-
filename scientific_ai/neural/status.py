"""The current status of the Scientific-AI harness, derived from evidence.

A reader who opens this repository should not have to reconstruct what has
been done from a gate table that belongs to something else. The legacy QTA
hardware forecast's ``PASS_count = 0`` is a fact about that forecast -- no
gate could pass because nothing was measured -- and it is reported here in
its own section, labelled LEGACY_QTA_ONLY, and NOWHERE ELSE: every other
section is computed from the learned-model substrate's own documents, and
:func:`build` takes the legacy gate statuses as a separate argument that no
other section reads (``tests/test_claim_hygiene.py`` changes it and requires
everything else to stay the same).

Pure: it reads nothing and imports no framework. ``tools/neural.py status``
gathers the committed documents, calls :func:`build`, and writes the JSON
and :func:`render`'s Markdown; ``tools/neural.py verify`` recomputes both.

What is NOT derived is said to be declared: the programme verdict of the
NF-1T closure is the closure report's judgement, cited, not computed.
"""
from __future__ import annotations

from . import claims as C

SCHEMA = "scientific-ai-current-status/1"
LEGACY_LABEL = "LEGACY_QTA_ONLY"


def _held(table: dict, scope: str) -> list:
    return sorted(c for c, v in table.items()
                  if v["holds"] and v["scope"] == scope)


def _parallel_plan_only(report: dict) -> list:
    """Parallel axes the distributed report never EXECUTED."""
    return [ax for ax in ("tensor", "pipeline")
            if not any(str(c.get("check", "")).startswith(ax)
                       and c.get("executed", True) is not False
                       for c in report["checks"])]


def _flagship(man: dict, subject: dict, meta_report: dict) -> dict:
    t = subject["claims"]
    pc = man["parameter_accounting"]
    moe_layers = man["moe_layers"]
    experts = man["experts_per_moe_layer"]
    k = man["experts_selected_per_token"]
    profiles = {name: {"top_k": p["top_k"],
                       "active_parameters_per_token":
                           p["active_parameters_per_token"]}
                for name, p in sorted(man["routing_profiles"].items())}
    trained = t["DEVELOPMENT_MODEL_TRAINED"]["holds"] and \
        t["DEVELOPMENT_MODEL_TRAINED"]["scope"] == "SUBJECT"
    return {
        "variant": man["architecture_variant"],
        "config_digest": man["configuration_digest"],
        "status": subject["status"],
        "claims_held": _held(t, "SUBJECT"),
        "claims_held_by_a_family_member_only": _held(t, "FAMILY_MEMBER"),
        "claims_held_by_the_family": _held(t, "FAMILY"),
        "layers": man["num_layers"], "hidden_size": man["hidden_size"],
        "attention_heads": man["num_attention_heads"],
        "kv_heads": man["num_kv_heads"],
        "head_dimension": man["head_dimension"],
        "moe_layers": moe_layers, "experts_per_moe_layer": experts,
        "expert_modules_total": moe_layers * experts,
        "experts_selected_per_token_per_moe_layer": k,
        "expert_selections_per_token_across_depth": moe_layers * k,
        "expert_hidden_size": man["expert_hidden_size"],
        "parameters_per_expert": man["expert_parameters_per_expert"],
        "trainable_parameters": man["trainable_parameters"],
        "non_trainable_parameters": man["non_trainable_parameters"],
        "total_stored_values": man["total_parameters"],
        "shared_parameters": man["shared_parameters"],
        "expert_parameters": man["expert_parameters"],
        "active_parameters_per_token_standard":
            man["estimated_active_parameters_per_token"],
        "routing_profiles": profiles,
        # False only on evidence: the meta report refused a real
        # allocation and no training run of the subject exists.
        "real_weights_allocated": False if (
            not trained and meta_report["real_allocation_refused"] is True
            and meta_report["allocation_mode"] == "meta") else None,
        "large_model_trained": t["LARGE_MODEL_TRAINED"]["holds"],
        "scientific_performance": "ESTABLISHED" if t[
            "SCIENTIFIC_PERFORMANCE_ESTABLISHED"]["holds"]
        else "UNESTABLISHED",
        "estimates": {
            "bfloat16_parameter_bytes":
                man["precision_memory_estimates"]["parameter_storage"][
                    "bfloat16_bytes"],
            "training_state_bytes":
                man["precision_memory_estimates"]["training_state"][
                    "total_bytes"],
            "fp8": "ESTIMATION_ONLY",
            "status": "ESTIMATE, not a measurement"},
        "categories_checked": sorted(pc["by_category"]),
    }


def _development(man, subject, training, dataset, evaluation, checkpoint):
    t = subject["claims"]
    pt = evaluation["per_target"]
    cons = evaluation["constraints"]
    ood = evaluation["ood"]

    def violations(split):
        return {name: [c["violations"], c["evaluated"]]
                for name, c in sorted(cons[split]["constraints"].items())
                if c["violations"]}

    return {
        "variant": man["architecture_variant"],
        "config_digest": man["configuration_digest"],
        "status": subject["status"],
        "claims_held": _held(t, "SUBJECT"),
        "trainable_parameters": man["trainable_parameters"],
        "active_parameters_per_token":
            man["estimated_active_parameters_per_token"],
        "layers": man["num_layers"], "hidden_size": man["hidden_size"],
        "experts_per_moe_layer": man["experts_per_moe_layer"],
        "experts_selected_per_token": man["experts_selected_per_token"],
        "expert_hidden_size": man["expert_hidden_size"],
        "training": {
            "steps": training["steps"]["stop"],
            "completion_state": training["completion_state"],
            "world_size": training["world_size"],
            "reproducibility_claimed": training["reproducibility"]["claimed"],
            "reproducibility_scope": training["reproducibility"]["scope"],
            "source_commit": training["source_commit"]},
        "dataset": {"dataset_id": dataset["dataset_id"],
                    "dataset_digest": dataset["dataset_digest"],
                    "samples": dataset["sample_count"],
                    "splits": dict(sorted(dataset["splits"].items()))},
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "reload_outputs_equal": evaluation["reload"]["outputs_equal"],
        "test_median_relative_error": {
            k: v["median_relative_error"]
            for k, v in sorted(pt["test"].items())},
        "test_interval_coverage_90": {
            k: v["interval_coverage"]["0.9"]
            for k, v in sorted(pt["test"].items())},
        "ood_interval_coverage_90": {
            k: v["interval_coverage"]["0.9"]
            for k, v in sorted(pt["ood"].items())},
        "ood_rmse_ratio_over_test": dict(sorted(
            ood["rmse_ratio_ood_over_test"].items())),
        "invariant_violations_test": violations("test"),
        "invariant_violations_ood": violations("ood"),
        "epistemic_uncertainty": "NOT_ASSESSED" if str(
            evaluation["calibration"]["epistemic"]).startswith(
            "NOT_ASSESSED") else evaluation["calibration"]["epistemic"],
        "limitations": evaluation["limitations"],
        "prediction_semantics": evaluation["semantics"],
    }


def _distributed(report: dict) -> dict:
    prof = report["execution_profile"]
    hardware = prof.get("kind") not in C.SIMULATED_PROFILES and \
        prof.get("hardware_executed") is True
    return {
        "execution_profile": prof.get("kind"),
        "hardware_executed": prof.get("hardware_executed") is True,
        "devices": len(report.get("devices") or ()),
        "checks": [{"check": c["check"], "passed": c["passed"],
                    "executed": c.get("executed", True)}
                   for c in report["checks"]],
        "software": (f"{prof.get('kind')} PATHS VALIDATED"
                     if report["result"] == "PASS" and not hardware
                     else report["result"]),
        "hardware": "VALIDATED" if hardware and report["result"] == "PASS"
        else "NOT_VALIDATED",
        "plan_only": _parallel_plan_only(report),
    }


def _reproduction(witness_profile: dict, equivalence: str) -> dict:
    ws = witness_profile["witnesses"]
    return {
        "stored_witness": [{
            "context": "the witnessed numerical backend, by digest: the "
                       "byte-reproduction witness profile, not a hosted "
                       "runner",
            "backend_digest": w["backend_digest"],
            "compared": w["observed"]["compared"],
            "byte_identical": w["observed"]["byte_identical"],
            "observed_at": w["observed"]["head"],
            "on": w["observed"]["on"]} for w in ws],
        "generic_hosted_runner": (
            "may resolve a DIFFERENT numerical backend; such a run reports "
            "REPRODUCTION_STATUS="
            "DIFFERENT_RESOLVED_BACKEND and at best CROSS_ENV_STATUS="
            "DECISION_STABLE_WITH_NUMERIC_DRIFT -- decision stability, not "
            "byte identity; each run's result belongs to its own commit"),
        "scientific_equivalence": equivalence,
    }


def _legacy(gate_statuses: dict, pass_audit: dict, mode_audit: dict) -> dict:
    return {
        "classification": LEGACY_LABEL,
        "what": "the legacy QTA hardware-forecast gate table "
                "(results_gate_table.csv)",
        "gates": sum(gate_statuses.values()),
        "statuses": dict(sorted(gate_statuses.items())),
        "PASS_count": gate_statuses.get("PASS", 0),
        "meaning": "a historical fact about the legacy hardware forecast: "
                   "no gate could pass because nothing was measured. It is "
                   "not a success metric of the Scientific-AI harness, the "
                   "learned-model substrate or the agent substrate, and no "
                   "other section of this status reads it",
        "pass_semantics_audit": {
            "current_ai_semantic_leaks":
                pass_audit["current_ai_semantic_leaks"],
            "unclassified": pass_audit["unclassified"]},
        "hardware_era_mode_audit": {
            "active_neural_semantic_leaks":
                mode_audit["active_neural_semantic_leaks"],
            "unclassified": mode_audit["unclassified"]},
    }


def build(*, flagship_manifest, flagship_meta, dev_manifest, claims_doc,
          training, dataset,
          evaluation, checkpoint, distributed, family, witness_profile,
          equivalence, pass_audit, mode_audit, legacy_gate_statuses,
          sources) -> dict:
    """The status document. ``legacy_gate_statuses`` reaches ONLY the
    legacy section."""
    subj = claims_doc["subjects"]
    return {
        "schema": SCHEMA,
        "derived_from": sources,
        "programme": {
            "NF-1T": "COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT",
            "basis": "DECLARED by the NF-1T closure report "
                     "(ARCHITECTURE_CONVERGENCE_PLAN.md, section 18) for "
                     "the scope authorised and executed; not derived here",
        },
        "flagship": _flagship(flagship_manifest, subj["flagship"],
                              flagship_meta),
        "development": _development(dev_manifest, subj["development"],
                                    training, dataset, evaluation,
                                    checkpoint),
        "scale_ladder": [{"rung": r["rung"],
                          "trainable_parameters": r["trainable_parameters"],
                          "active_parameters_per_token":
                              r["active_parameters_per_token"],
                          "meta_validation": r["meta_validation"],
                          "trained": r["trained"]}
                         for r in family["rungs"]],
        "distributed": _distributed(distributed),
        "learned_output_authority": {
            "semantics": claims_doc["semantics"],
            "refused_states": claims_doc["learned_refused_states"],
            "acceptance_attempted": claims_doc["acceptance_attempt"]["dst"],
            "acceptance_refused": claims_doc["acceptance_attempt"]["refused"],
        },
        "reproduction": _reproduction(witness_profile, equivalence),
        "legacy_qta_hardware_forecast": _legacy(
            legacy_gate_statuses, pass_audit, mode_audit),
    }


def _where(di: dict) -> str:
    """What the devices were, from the profile -- never assumed."""
    return ("simulated CPU devices in one process"
            if di["execution_profile"] in C.SIMULATED_PROFILES
            else f"devices of the {di['execution_profile']} profile")


def _n(x) -> str:
    return f"{x:,}" if isinstance(x, int) else str(x)


def render(st: dict) -> str:
    """The status as Markdown, every number from ``st``."""
    f, d = st["flagship"], st["development"]
    di, la = st["distributed"], st["learned_output_authority"]
    rp, lg = st["reproduction"], st["legacy_qta_hardware_forecast"]
    prof = f["routing_profiles"]
    lines = [
        "# Scientific-AI harness: current status",
        "",
        "Generated by `tools/neural.py status` from the committed evidence "
        "(`docs/neural/current_status.json`, which lists every source by "
        "digest); `tools/neural.py verify` fails if this file differs from "
        "what the evidence gives. Learned outputs are "
        f"{', '.join(la['semantics'])}.",
        "",
        "| | |",
        "|---|---|",
        f"| NF-1T | {st['programme']['NF-1T']} -- "
        f"{st['programme']['basis']} |",
        f"| ~1T flagship (`{f['variant']}`) | {f['status']}; held: "
        f"{', '.join(f['claims_held'])} |",
        f"| flagship trainable parameters | "
        f"{_n(f['trainable_parameters'])} (exact) |",
        f"| flagship active parameters per token | "
        f"{_n(f['active_parameters_per_token_standard'])} standard "
        f"(top_k {f['experts_selected_per_token_per_moe_layer']}) |",
        "| flagship real-weight allocation | "
        + ("NOT EXECUTED" if f["real_weights_allocated"] is False
           else "see evidence") + " |",
        "| flagship training | " + ("NOT EXECUTED" if not
                                     f["large_model_trained"]
                                     else "EXECUTED") + " |",
        f"| flagship scientific performance | {f['scientific_performance']} "
        "|",
        f"| development member | {d['status']}; held: "
        f"{', '.join(d['claims_held'])} |",
        f"| distributed software | {di['software']} "
        f"({di['devices']} {_where(di)}) |",
        f"| distributed hardware | {di['hardware']} |",
        "| learned output authority | " + ", ".join(la["semantics"])
        + "; " + "/".join(la["refused_states"]) + " REFUSED |",
        f"| epistemic uncertainty | {d['epistemic_uncertainty']} |",
        f"| scientific equivalence across backends | "
        f"{rp['scientific_equivalence']} |",
        f"| legacy QTA hardware forecast | historical PASS_count "
        f"{lg['PASS_count']} of {lg['gates']} gates -- {lg['classification']}"
        ", not a current Scientific-AI success metric |",
        "",
        "## The flagship (`" + f["variant"] + "`)",
        "",
        f"{f['layers']} layers, all of them mixture-of-experts; hidden size "
        f"{_n(f['hidden_size'])}; {f['attention_heads']} attention heads of "
        f"dimension {f['head_dimension']} ({f['kv_heads']} key/value heads); "
        f"{f['experts_per_moe_layer']} routed experts per layer of width "
        f"{_n(f['expert_hidden_size'])} -- {_n(f['expert_modules_total'])} "
        f"expert modules in the network, {_n(f['parameters_per_expert'])} "
        "parameters each. Standard routing selects "
        f"{f['experts_selected_per_token_per_moe_layer']} experts per token "
        f"in each MoE layer: {f['expert_selections_per_token_across_depth']}"
        " expert applications per token across depth "
        f"({f['experts_selected_per_token_per_moe_layer']} of "
        f"{f['experts_per_moe_layer']} at each layer, not that many "
        "distinct modules).",
        "",
        "| quantity | value |",
        "|---|---|",
        f"| trainable parameters | {_n(f['trainable_parameters'])} |",
        f"| non-trainable stored values | "
        f"{_n(f['non_trainable_parameters'])} |",
        f"| total stored values | {_n(f['total_stored_values'])} |",
        f"| shared (non-routed) parameters | "
        f"{_n(f['shared_parameters'])} |",
        f"| routed expert parameters | {_n(f['expert_parameters'])} |",
    ] + [
        f"| active per token, {name} (top_k {p['top_k']}) | "
        f"{_n(p['active_parameters_per_token'])} |"
        for name, p in prof.items()
    ] + [
        f"| bfloat16 parameter storage (estimate) | "
        f"{_n(f['estimates']['bfloat16_parameter_bytes'])} bytes |",
        f"| training state (estimate, before activations) | "
        f"{_n(f['estimates']['training_state_bytes'])} bytes |",
        "",
        "Claims held only through a family member: "
        + (", ".join(f["claims_held_by_a_family_member_only"]) or "none")
        + "." + (" The flagship's own real weights have never been "
                 "allocated: its meta validation refused the allocation."
                 if f["real_weights_allocated"] is False else ""),
        "",
        "## The development member (`" + d["variant"] + "`)",
        "",
        f"{_n(d['trainable_parameters'])} trainable parameters, "
        f"{_n(d['active_parameters_per_token'])} active per token; "
        f"{d['layers']} layers, {d['experts_per_moe_layer']} experts per "
        f"layer, top_k {d['experts_selected_per_token']}. Trained "
        f"{_n(d['training']['steps'])} steps "
        f"({d['training']['completion_state']}, world size "
        f"{d['training']['world_size']}) on `{d['dataset']['dataset_id']}` "
        f"({_n(d['dataset']['samples'])} samples: "
        + ", ".join(f"{k} {_n(v)}" for k, v in d["dataset"]["splits"].items())
        + f"); checkpoint `{d['checkpoint_digest'][:16]}...` reloaded to "
        + ("identical outputs" if d["reload_outputs_equal"] else
           "DIFFERENT outputs")
        + ". Reproducibility claimed: "
        + ", ".join(d["training"]["reproducibility_claimed"])
        + f" -- scope: {d['training']['reproducibility_scope']}.",
        "",
        "| target | test median relative error | test 90 % coverage | "
        "OOD 90 % coverage | OOD/test RMSE |",
        "|---|---|---|---|---|",
    ] + [
        f"| {t} | {d['test_median_relative_error'][t]:.4%} | "
        f"{d['test_interval_coverage_90'][t]:.2%} | "
        f"{d['ood_interval_coverage_90'][t]:.2%} | "
        f"{d['ood_rmse_ratio_over_test'][t]:.2f} |"
        for t in d["test_median_relative_error"]
    ] + [
        "",
        "Declared invariants violated by the predictions (reported, never "
        "clipped): test "
        + ", ".join(f"{k} {v[0]}/{v[1]}"
                    for k, v in d["invariant_violations_test"].items())
        + "; OOD "
        + ", ".join(f"{k} {v[0]}/{v[1]}"
                    for k, v in d["invariant_violations_ood"].items())
        + f". Epistemic uncertainty: {d['epistemic_uncertainty']}.",
        "",
        "## Scale ladder",
        "",
        "| rung | trainable | active per token | meta validation | trained |",
        "|---|---|---|---|---|",
    ] + [
        f"| {r['rung']} | {_n(r['trainable_parameters'])} | "
        f"{_n(r['active_parameters_per_token'])} | {r['meta_validation']} | "
        f"{'yes' if r['trained'] else 'no'} |"
        for r in st["scale_ladder"]
    ] + [
        "",
        "## Distributed execution",
        "",
        f"Executed: {di['execution_profile']} on {di['devices']} "
        f"{_where(di)} (hardware_executed: "
        f"{str(di['hardware_executed']).lower()}). Checks: "
        + ", ".join(f"{c['check']} "
                    + ("passed" if c["passed"] else "FAILED")
                    + ("" if c["executed"] else " (not executed: a plan)")
                    for c in di["checks"])
        + ". Plan only, never executed: "
        + (", ".join(f"{a} parallelism" for a in di["plan_only"]) or "none")
        + f". Distributed hardware: {di['hardware']}.",
        "",
        "## Byte reproduction",
        "",
    ] + [
        f"* Stored witness ({w['context']}): {w['byte_identical']}/"
        f"{w['compared']} canonical files byte-identical on backend "
        f"`{w['backend_digest'][:16]}...`, observed {w['on']}."
        for w in rp["stored_witness"]
    ] + [
        f"* Generic hosted runners: {rp['generic_hosted_runner']}.",
        f"* Scientific equivalence across backends: "
        f"{rp['scientific_equivalence']}.",
        "",
        "## Legacy QTA hardware forecast (" + lg["classification"] + ")",
        "",
        f"The legacy gate table has {lg['gates']} gates ("
        + ", ".join(f"{k} {v}" for k, v in lg["statuses"].items())
        + f"); its historical PASS_count is {lg['PASS_count']}: "
        + lg["meaning"] + ". Audits: PASS semantics "
        f"{lg['pass_semantics_audit']['current_ai_semantic_leaks']} "
        "current-AI leaks; hardware-era modes "
        f"{lg['hardware_era_mode_audit']['active_neural_semantic_leaks']} "
        "leaks into the learned-model substrate.",
        "",
    ]
    return "\n".join(lines)
