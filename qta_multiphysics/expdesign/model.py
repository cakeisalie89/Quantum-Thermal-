"""Candidate-experiment model for Bayesian experimental design.

Parses the human-readable candidates from ``FIRST_VALIDATION_EXPERIMENTS.md`` and
reconciles them with the gate table, ``kill_gate_ranking.csv`` (gate criticality,
difficulty, dependency counts), and the genuinely-unknown parameters in
``assumed_parameters.json`` (tau_c, C_contr, ...).

Each candidate carries a parameter model (prior + Gaussian measurement
likelihood in a documented transformed space). Priors are WIDE where the repo
marks the parameter UNKNOWN - they encode genuine uncertainty, not a favourable
guess. Cost/duration/risk are forecast estimates derived transparently from the
kill-gate difficulty class; nothing here is a measurement. Forecast-only.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]

_GATE_RE = re.compile(r"\b([A-Z]{1,2}-?\d+[A-Za-z0-9_]*)")


@dataclass
class ParameterModel:
    """Prior (uniform in a transform) + Gaussian measurement likelihood."""
    name: str
    transform: str            # 'linear' or 'log10'
    prior_lo: float           # transformed-space prior lower bound
    prior_hi: float           # transformed-space prior upper bound
    meas_sigma: float         # measurement std in transformed space
    unit: str
    status: str               # UNKNOWN / ASSUMED
    falsify_below_transformed: Optional[float] = None  # kill threshold (transformed)

    def prior_width(self) -> float:
        return self.prior_hi - self.prior_lo

    def summary(self) -> str:
        return (f"uniform[{self.prior_lo:g},{self.prior_hi:g}] in {self.transform}; "
                f"meas_sigma={self.meas_sigma:g}")


@dataclass
class ExperimentCandidate:
    experiment_id: str
    label: str
    observable: str
    parameter: ParameterModel
    required_hardware: str
    required_mode: str
    gates_affected: list
    predecessors: list
    cost: float
    duration: float
    risk: float
    falsification_value: float
    criticality: float
    safety_note: str
    purpose: str
    kill_condition: str
    source_record: str

    def to_row(self) -> dict:
        d = asdict(self)
        d["parameter"] = self.parameter.name
        d["prior"] = self.parameter.summary()
        d["gates_affected"] = ";".join(self.gates_affected)
        d["predecessors"] = ";".join(self.predecessors)
        return d


# --- parameter models (priors reflect genuine repo uncertainty) ---------------
# tau_c, C_contr_10mK are value:null/UNKNOWN in assumed_parameters.json -> wide priors.
_PARAM_MODELS = {
    "C_contr_10mK": ParameterModel("C_contr_10mK", "linear", 0.0, 0.30, 0.01,
                                   "dimensionless", "UNKNOWN", falsify_below_transformed=0.02),
    "he3_he4_contrast": ParameterModel("he3_he4_contrast", "linear", 0.0, 1.0, 0.05,
                                       "fractional_decoherence_contrast", "UNKNOWN",
                                       falsify_below_transformed=0.05),
    "tau_c": ParameterModel("tau_c", "log10", -6.0, -2.0, 0.15, "s", "UNKNOWN",
                            falsify_below_transformed=-5.0),  # <10 us falsifies
    "h2_coverage": ParameterModel("h2_coverage", "log10", -3.0, 0.0, 0.20, "monolayer",
                                  "ASSUMED", falsify_below_transformed=None),
    "thermal_recovery_time": ParameterModel("thermal_recovery_time", "log10", -8.0, -5.0,
                                            0.15, "s", "ASSUMED", falsify_below_transformed=None),
    "mode_isolation_leak": ParameterModel("mode_isolation_leak", "log10", -6.0, -1.0, 0.30,
                                          "fraction", "ASSUMED", falsify_below_transformed=None),
}

# experiment letter -> (parameter key, predecessors, hardware, mode, safety)
_EXP_META = {
    "A": ("C_contr_10mK", [], "10 mK ODMR: laser + MW + optical collection", "D",
          "cryogenic laser illumination; charge-state stability"),
    "B": ("he3_he4_contrast", ["A", "C"], "He-3/He-4 dosing + NV decoherence readout", "D",
          "He isotope dosing; surface contamination control"),
    "C": ("tau_c", ["A"], "NV decoherence sequence on 3He/diamond at 10 mK", "D",
          "He-3 handling; cryogenic operation"),
    "D": ("h2_coverage", [], "calibrated RGA (Faraday-cup corrected) after conditioning", "C",
          "bakeout / NEG conditioning"),
    "E": ("thermal_recovery_time", [], "pump-probe / fast thermometry after fs pulse", "D",
          "fs-laser exposure"),
    "F": ("mode_isolation_leak", [], "witness coupon AFM/Raman/XPS + RGA", "C",
          "cross-contamination handling"),
}


def _load_kill_ranking(root: Path) -> dict:
    """gate_id -> {rank, difficulty, dep_count}. Handles combined ids like D10a-D10b."""
    out = {}
    with open(root / "kill_gate_ranking.csv", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            entry = {"rank": int(r["rank"]),
                     "difficulty": r["difficulty_class"].strip().upper(),
                     "dep_count": int(r["dependency_count"]) if r["dependency_count"].strip().isdigit() else 0}
            for g in _GATE_RE.findall(r["gate_id"]):
                out[g] = entry
    return out


def _difficulty_to_cost(diff: str) -> tuple:
    return {"HIGH": (3.0, 3.0, 3.0), "MEDIUM": (2.0, 2.0, 2.0)}.get(diff, (1.0, 1.0, 1.0))


def _parse_md(root: Path) -> dict:
    text = (root / "FIRST_VALIDATION_EXPERIMENTS.md").read_text(encoding="utf-8")
    sections = {}
    cur = None
    buf = []
    for line in text.splitlines():
        m = re.match(r"^##\s+([A-F])\.\s+(.*)", line)
        if m:
            if cur:
                sections[cur[0]] = (cur[1], "\n".join(buf))
            cur = (m.group(1), m.group(2).strip())
            buf = []
        elif cur and not line.startswith("## "):
            buf.append(line)
    if cur:
        sections[cur[0]] = (cur[1], "\n".join(buf))
    return sections


def _field(body: str, name: str) -> str:
    m = re.search(rf"\*\*{re.escape(name)}\*\*:\s*(.+?)(?:\n-\s\*\*|\Z)", body, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def load_candidates(repo_root: Optional[Path] = None) -> list:
    root = Path(repo_root) if repo_root else _REPO_ROOT
    sections = _parse_md(root)
    kill = _load_kill_ranking(root)
    n_kill = max((e["rank"] for e in kill.values()), default=1)

    cands = []
    for letter in sorted(_EXP_META):
        if letter not in sections:
            continue
        title, body = sections[letter]
        pkey, preds, hw, mode, safety = _EXP_META[letter]
        gates = sorted(set(_GATE_RE.findall(_field(body, "Gate affected"))))
        kill_text = _field(body, "Kill / redesign result")
        purpose = _field(body, "Purpose")

        # criticality: best (lowest) kill-rank among affected gates -> [~0,1]
        ranks = [kill[g]["rank"] for g in gates if g in kill]
        crit = (n_kill + 1 - min(ranks)) / n_kill if ranks else 0.15
        # cost/duration/risk from worst difficulty among affected kill gates
        diffs = [kill[g]["difficulty"] for g in gates if g in kill]
        worst = "HIGH" if "HIGH" in diffs else ("MEDIUM" if "MEDIUM" in diffs else "LOW")
        cost, dur, risk = _difficulty_to_cost(worst)
        # falsification value: an experiment that measures a core unknown carrying
        # an explicit kill threshold (tau_c, C_contr, He-contrast) can directly
        # falsify the sensing premise -> high. Peripheral experiments are scaled by
        # their gate criticality. (kill_condition text retained for transparency.)
        pm = _PARAM_MODELS[pkey]
        if pm.falsify_below_transformed is not None:
            fval = 1.0
        else:
            fval = round(0.40 + 0.40 * crit, 3)

        cands.append(ExperimentCandidate(
            experiment_id=f"EXP-{letter}", label=title, observable=pkey,
            parameter=_PARAM_MODELS[pkey], required_hardware=hw, required_mode=mode,
            gates_affected=gates, predecessors=[f"EXP-{p}" for p in preds],
            cost=cost, duration=dur, risk=risk, falsification_value=round(fval, 3),
            criticality=round(crit, 3), safety_note=safety, purpose=purpose,
            kill_condition=kill_text, source_record=f"FIRST_VALIDATION_EXPERIMENTS.md:{letter}",
        ))
    return cands


def enables_count(cands: list) -> dict:
    """How many other candidates list each candidate as a predecessor."""
    cnt = {c.experiment_id: 0 for c in cands}
    for c in cands:
        for p in c.predecessors:
            if p in cnt:
                cnt[p] += 1
    return cnt
