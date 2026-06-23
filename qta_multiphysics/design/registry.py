"""Typed, serializable QTA design model (Qiskit-Metal-inspired pattern).

Deterministically derived from existing repository records only:
``BOM.csv``, ``interface_map.csv``, ``interlock_table.csv`` (and the canonical
mode semantics). No geometry, component, vendor, or relationship is invented;
every object carries the source record it came from. Forecast/pre-hardware.

Canonical mode semantics used here (not redefined, just referenced):
  A -> setup/idle, B -> C-13-methane material processing (LCVD),
  C -> isolation/purge/cryotrap/thermal recovery, D -> He-3/He-4 NV sensing.
Sequence is A -> B -> C -> D; processing (B) and sensing (D) never overlap.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
MODES = ("A", "B", "C", "D")
# Canonical cold-stage ordering (warm -> cold). Used by stage-ordering checks.
STAGE_ORDER = ["300K", "50K", "4K", "1K", "100mK", "10mK"]


# --- parsing helpers ----------------------------------------------------------

def parse_modes(cell: str) -> list:
    c = (cell or "").strip().upper()
    if not c or c in {"N/A", "NONE", "-"}:
        return []
    if "ALL" in c:
        return list(MODES)
    return sorted(set(re.findall(r"\b([ABCD])\b", c)))


def parse_gate_refs(cell: str) -> list:
    c = (cell or "").strip()
    if not c or c.upper() in {"N/A", "NONE", "-"}:
        return []
    return sorted(set(re.findall(r"\b([A-Z]\d+)\b", c)))


def parse_signals(condition: str) -> list:
    parts = re.split(r"\bAND\b|\bOR\b", condition or "")
    return [p.strip() for p in parts if p.strip()]


def _norm_stage(text: str) -> list:
    """Extract canonical stage tokens (e.g. '4K', '100mK', '10 mK') from text."""
    t = (text or "")
    found = []
    for m in re.findall(r"(\d+)\s*(mK|K)\b", t):
        tok = f"{m[0]}{m[1]}".replace(" ", "")
        if tok in STAGE_ORDER:
            found.append(tok)
    return found


# --- typed objects ------------------------------------------------------------

@dataclass
class DesignComponent:
    component_id: str
    name: str
    component_type: str          # role
    subsystem: str
    allowed_modes: list          # parsed from 'mode'
    status: str
    current_or_forecast: str
    provenance_source: str       # vendor_or_source
    part_number_or_spec: str
    parameters: dict             # {name: {"value":..., "unit":...}} (structured)
    parent: Optional[str]        # cold/warm anchor relationship (best-effort)
    warm_side_anchor: str
    cold_side_anchor: str
    thermal_intercepts: list
    interfaces: list             # interface_ids touching this subsystem
    associated_gates: list
    contamination_path: str
    can_PASS_now: str
    validation_state: str        # filled by validation engine
    source_record: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DesignInterface:
    interface_id: str
    name: str
    subsystems: str
    function: str
    allowed_modes: list
    status: str
    associated_gates: list
    possible_mismatch: str
    measurement_required: str
    can_PASS_now: str
    source_record: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Interlock:
    interlock_id: str
    condition: str
    interlock_type: str          # e.g. IMPOSSIBLE
    reason: str
    signals: list
    source_record: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DesignRegistry:
    components: dict             # id -> DesignComponent
    interfaces: dict            # id -> DesignInterface
    interlocks: dict            # id -> Interlock

    # ---- operational accessors (consumed by validation + simulation) --------
    def component_ids(self) -> list:
        return sorted(self.components)

    def components_in_mode(self, mode: str) -> list:
        return sorted(cid for cid, c in self.components.items() if mode in c.allowed_modes)

    def forbidden_simultaneous_states(self) -> list:
        """Return parsed forbidden concurrent signal-sets from the interlocks."""
        out = []
        for il in sorted(self.interlocks.values(), key=lambda x: x.interlock_id):
            if il.interlock_type.upper().startswith("IMPOSSIBLE"):
                out.append({"interlock_id": il.interlock_id, "signals": il.signals,
                            "reason": il.reason})
        return out

    def components_for_gate(self, gate_id: str) -> list:
        c = [cid for cid, comp in self.components.items() if gate_id in comp.associated_gates]
        i = [iid for iid, intf in self.interfaces.items() if gate_id in intf.associated_gates]
        return sorted(c) + sorted(i)

    def all_referenced_gates(self) -> list:
        g = set()
        for c in self.components.values():
            g.update(c.associated_gates)
        for i in self.interfaces.values():
            g.update(i.associated_gates)
        return sorted(g)


# --- loaders ------------------------------------------------------------------

def _read_csv(path: Path) -> list:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load_registry(repo_root: Optional[Path] = None) -> DesignRegistry:
    root = Path(repo_root) if repo_root else _REPO_ROOT
    bom = _read_csv(root / "BOM.csv")
    ifaces = _read_csv(root / "interface_map.csv")
    interlocks = _read_csv(root / "interlock_table.csv")

    # interfaces first (so components can reference them by subsystem overlap)
    iface_objs = {}
    for r in ifaces:
        iid = r["interface_id"].strip()
        iface_objs[iid] = DesignInterface(
            interface_id=iid, name=r.get("interface", "").strip(),
            subsystems=r.get("subsystems", "").strip(),
            function=r.get("intended_function", "").strip(),
            allowed_modes=parse_modes(r.get("mode", "")),
            status=r.get("status", "").strip(),
            associated_gates=parse_gate_refs(r.get("gate_impact", "")),
            possible_mismatch=r.get("possible_mismatch", "").strip(),
            measurement_required=r.get("measurement_required", "").strip(),
            can_PASS_now=r.get("can_PASS_now", "").strip(),
            source_record=f"interface_map.csv:{iid}",
        )

    comp_objs = {}
    for r in bom:
        cid = r["item_id"].strip()
        subsystem = r.get("subsystem", "").strip()
        ifaces_here = sorted(iid for iid, o in iface_objs.items()
                             if subsystem and subsystem.split()[0].lower() in o.subsystems.lower())
        comp_objs[cid] = DesignComponent(
            component_id=cid, name=r.get("item_name", "").strip(),
            component_type=r.get("role", "").strip(), subsystem=subsystem,
            allowed_modes=parse_modes(r.get("mode", "")),
            status=r.get("status", "").strip(),
            current_or_forecast=r.get("current_or_forecast", "").strip(),
            provenance_source=r.get("vendor_or_source", "").strip(),
            part_number_or_spec=r.get("part_number_or_spec", "").strip(),
            parameters={},  # BOM carries no structured numeric params; kept typed/empty
            parent=(r.get("warm_side_anchor", "").strip() or None),
            warm_side_anchor=r.get("warm_side_anchor", "").strip(),
            cold_side_anchor=r.get("cold_side_anchor", "").strip(),
            thermal_intercepts=_norm_stage(r.get("intermediate_thermal_intercepts", "")),
            interfaces=ifaces_here,
            associated_gates=parse_gate_refs(r.get("gate_dependency", "")),
            contamination_path=r.get("contamination_path", "").strip(),
            can_PASS_now=r.get("can_support_PASS_now", "").strip(),
            validation_state="UNVALIDATED",
            source_record=f"BOM.csv:{cid}",
        )

    il_objs = {}
    for r in interlocks:
        iid = r["id"].strip()
        il_objs[iid] = Interlock(
            interlock_id=iid, condition=r.get("condition", "").strip(),
            interlock_type=r.get("type", "").strip(), reason=r.get("reason", "").strip(),
            signals=parse_signals(r.get("condition", "")),
            source_record=f"interlock_table.csv:{iid}",
        )

    return DesignRegistry(components=comp_objs, interfaces=iface_objs, interlocks=il_objs)


# --- interface graph ----------------------------------------------------------

def build_interface_graph(reg: DesignRegistry) -> dict:
    """Deterministic node/edge graph: subsystem nodes, interface + thermal edges."""
    nodes = set()
    for c in reg.components.values():
        if c.subsystem:
            nodes.add(c.subsystem)
    edges = []
    for i in sorted(reg.interfaces.values(), key=lambda x: x.interface_id):
        # subsystems field like "Mode B optics / diamond"
        parts = [p.strip() for p in re.split(r"[/↔]", i.subsystems) if p.strip()]
        for n in parts:
            nodes.add(n)
        if len(parts) >= 2:
            edges.append({"edge_id": i.interface_id, "kind": "interface",
                          "source": parts[0], "target": parts[1],
                          "modes": i.allowed_modes, "gates": i.associated_gates})
    # thermal edges from warm/cold anchors
    for c in sorted(reg.components.values(), key=lambda x: x.component_id):
        if c.warm_side_anchor and c.cold_side_anchor:
            edges.append({"edge_id": f"TH-{c.component_id}", "kind": "thermal",
                          "source": c.warm_side_anchor, "target": c.cold_side_anchor,
                          "component": c.component_id, "modes": c.allowed_modes})
    return {
        "_note": "Derived interface/thermal graph (forecast). Nodes are subsystems/"
                 "anchors; edges are interfaces and warm->cold thermal paths.",
        "nodes": sorted(nodes),
        "edges": sorted(edges, key=lambda e: e["edge_id"]),
    }
