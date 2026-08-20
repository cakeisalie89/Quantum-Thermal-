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
from functools import lru_cache
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
MODES = ("A", "B", "C", "D")
# Canonical cold-stage ordering (warm -> cold). Used by stage-ordering checks.
STAGE_ORDER = ["300K", "50K", "4K", "1K", "100mK", "10mK"]


# --- parsing helpers ----------------------------------------------------------

class ModeSyntaxError(ValueError):
    """A mode cell used syntax this parser does not recognise.

    Raised rather than returning an empty list: silently mapping an unknown
    non-empty mode string to "no modes" is how a record loses its mode
    restriction without anyone noticing.
    """


_EMPTY_CELLS = {"N/A", "NONE", "-", ""}
# Compact runs (AB, BCD, ABCD), separated forms (A/B, "B, D"), and transition
# forms (A->B, "A\u2192B") all occur in the governed CSVs; all are legitimate.
_MODE_TOKEN = re.compile(r"[ABCD]")
_MODE_SEPARATORS = re.compile(
    r"[\s,/|+&]+|\bAND\b|\bOR\b|\bTO\b|->|\u2192|-")
# Annotation words that appear alongside a mode in governed cells and carry no
# mode meaning ("D (optional)", "D / shield", "A/D (both)"). Listed explicitly
# so an unrecognised word still fails closed instead of being ignored.
_MODE_ANNOTATIONS = ("BOTH", "OPTIONAL", "SHIELD")
_PARENTHETICAL = re.compile(r"\([^)]*\)")


def parse_modes(cell: str, strict: bool = True) -> list:
    """Canonical modes named by a governed mode cell.

    Accepts the compact run notation the repository actually uses (``AB``,
    ``BCD``, ``ABCD``) as well as separated forms (``A/B``, ``B, D``) and
    ``ALL``. Fails closed on unrecognised syntax when ``strict``.
    """
    c = (cell or "").strip().upper()
    if c in _EMPTY_CELLS:
        return []
    if "ALL" in c:
        return list(MODES)
    c = _PARENTHETICAL.sub(" ", c)          # "D (optional)" -> "D"
    for word in _MODE_ANNOTATIONS:          # "D / shield"   -> "D"
        c = c.replace(word, " ")
    residue = _MODE_SEPARATORS.sub("", c)
    found = sorted(set(_MODE_TOKEN.findall(residue)))
    # every character had to be a mode letter or a separator; anything else is
    # syntax this parser does not understand
    if strict and (not found or _MODE_TOKEN.sub("", residue)):
        raise ModeSyntaxError(
            f"unrecognised mode syntax {cell!r}; canonical modes are "
            f"{MODES}, compact runs (AB, BCD) and ALL are accepted")
    return found


@lru_cache(maxsize=1)
def canonical_gate_ids() -> frozenset:
    """Gate IDs from the gate table, which is their registered authority.

    Matching against the authoritative set rather than guessing a regex
    grammar is what makes this both complete and incapable of overmatching:
    the canonical IDs include ``D10a``, ``D12_G23``, ``Shield-RAD`` and
    ``THERMAL_1D_STABILITY_CHECK``, which no single simple pattern covers
    without also swallowing arbitrary prose.
    """
    path = _REPO_ROOT / "results_gate_table.csv"
    with open(path, encoding="utf-8", newline="") as fh:
        return frozenset(r["gate_id"] for r in csv.DictReader(fh)
                         if r.get("gate_id"))


# Tokens that look like a gate reference. Used only on the text left over
# after canonical IDs are matched, so a reference to a gate the table does not
# contain still surfaces instead of vanishing.
_GATE_LIKE = re.compile(r"\b[A-Z]\d+[a-z]?\b|\b[A-Z][A-Za-z0-9_]*-[A-Z]+\b")


def parse_gate_refs(cell: str) -> list:
    r"""Gate IDs referenced by a cell.

    Canonical IDs are matched longest-first against the gate table, which is
    their registered authority -- that is what makes ``D10a``, ``D12_G23`` and
    ``THERMAL_1D_STABILITY_CHECK`` resolve where a simple ``[A-Z]\d+`` pattern
    silently dropped them.

    Gate-like tokens in the *remaining* text are returned too, even though
    they match no canonical ID. They are real references to something, and the
    design validator's ``gate_referenced_but_absent`` rule needs to see them:
    the governed records reference a bare ``D12`` while the gate table calls it
    ``D12_G23``, and that mismatch must stay visible rather than be silently
    resolved away.
    """
    c = (cell or "").strip()
    if not c or c.upper() in _EMPTY_CELLS:
        return []
    found = set()
    residue = c
    for gid in sorted(canonical_gate_ids(), key=len, reverse=True):
        pattern = rf"(?<![A-Za-z0-9_-]){re.escape(gid)}(?![A-Za-z0-9_-])"
        if re.search(pattern, residue):
            found.add(gid)
            residue = re.sub(pattern, " ", residue)
    found.update(_GATE_LIKE.findall(residue))
    return sorted(found)


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
