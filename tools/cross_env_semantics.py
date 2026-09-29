#!/usr/bin/env python3
"""Did a DECISION change, or only a digit -- and may this comparison say?

DIAGNOSTIC + GATE. Nothing here moves a threshold, authors a gate state or
writes into the canonical tree. MODEL-ONLY / FORECAST-ONLY. PASS remains 0.

WHAT QUESTION THIS ANSWERS

When a RESOLVED numerical backend that is demonstrably different from the one
that reproduced the canonical corpus regenerates it and the bytes differ,
did anything change that the package ASSERTS -- its structure, a status, a
verdict, a count, a label, a unit, a resolution class -- or only floating-
point digits, in quantities whose method says what it resolves?

That is cross-environment DECISION STABILITY. It is not byte reproduction
(the byte gate answers that, on the backend that reproduces), and it is not
scientific equivalence: two backends whose every decision agrees have still
computed different numbers, and nothing here says the difference is within
any model's error. ``scientific_equivalence`` in every report is
``NOT_ESTABLISHED``, always.

WHAT IT REFUSES ON -- every class below fails the comparison

``STRUCTURAL``    a declared artefact missing or foreign; a JSON key added or
                  removed, a list or table that changed length or shape, a
                  CSV header that changed, a value that changed TYPE (an int
                  becoming a float, a number becoming text, a structured cell
                  becoming plain).
``DECISION``      any non-numeric token that differs: a status, a verdict, a
                  boolean, null, a label, a unit, a resolution class, any
                  text. Text is compared as text, WHOLE. The instrument this
                  replaces deleted the digits from a string and compared what
                  was left, so ``model_v2`` -> ``model_v3`` was a precision
                  event; here it is a changed label.
``DISCRETE``      an integer that changed, or any value in a column declared
                  exact -- a count, a coordinate, a configured constant.
                  Integers are counts and indices; one that moves is a
                  discrete outcome that moved, not a last digit.
``NONFINITE``     NaN or infinity introduced, removed or changed sign.
``UNCLASSIFIED``  a differing artefact or leaf this comparator cannot classify
                  safely: a suffix it does not parse, bytes that do not
                  parse, a number whose text changed while its value did not.
                  An unknown difference is not a precision difference.
``ZERO_CROSSING_BARE`` / ``SIGN_FLIP_BARE``
                  exactly zero on one side and not on the other, or opposite
                  signs, in a quantity that is not BOUND to a resolution class
                  saying BELOW_RESOLUTION on BOTH sides (and, where a floor is
                  bound, with both values inside it).

WHAT IT PERMITS -- reported, never hidden

``PRECISION``            finite, nonzero, same sign, different digits.
``ZERO_CROSSING_BOUND`` / ``SIGN_FLIP_BOUND``
                  the quantity's own class says BELOW_RESOLUTION on both
                  sides: the same statement, "less than this method can
                  see", in two accumulation orders' digits.

QUANTITY-BOUND RESOLUTION, NOT FILE-LEVEL

A class counts only for the quantity it is BOUND to in
``docs/resolution_inventory.json`` -- a wide column's ``resolution_from`` in
the same row, a long-format table's row binding, a JSON leaf binding -- as
reconciled against the committed artefacts by ``tools/resolution_
inventory.py``. The instrument this replaces asked whether the FILE declared
a resolution anywhere, and so called ``.state.gasC_sample.CH4`` declared
because ``.metrics`` beside it was (D-2026-98).

EXACT SCOPE

The caller names the declared canonical set and the exemptions; every
declared, non-exempt artefact must be present on both sides and is compared;
an exemption must name a declared artefact; a scope of zero, or differing
files with zero leaves compared, is a refusal (``ScopeError``) -- "no
decision changed" is trivially true of nothing.

Usage:
    python3 tools/cross_env_semantics.py <other-dir> [<committed-dir>]
    python3 tools/cross_env_semantics.py outputs/ . --json report.json

The CLI reads the declared set and the exemptions from the committed tree's
``package_consistency_check.py`` (by parsing, not importing: that module
runs its whole check at import).
"""
from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import resolution_inventory as RI  # noqa: E402

INVENTORY = ROOT / "docs" / "resolution_inventory.json"

# ---- finding classes --------------------------------------------------------
STRUCTURAL = "STRUCTURAL"
DECISION = "DECISION"
DISCRETE = "DISCRETE"
NONFINITE = "NONFINITE"
UNCLASSIFIED = "UNCLASSIFIED"
ZERO_CROSSING_BARE = "ZERO_CROSSING_BARE"
ZERO_CROSSING_BOUND = "ZERO_CROSSING_BOUND"
SIGN_FLIP_BARE = "SIGN_FLIP_BARE"
SIGN_FLIP_BOUND = "SIGN_FLIP_BOUND"
PRECISION = "PRECISION"
CLASSES = (STRUCTURAL, DECISION, DISCRETE, NONFINITE, UNCLASSIFIED,
           ZERO_CROSSING_BARE, SIGN_FLIP_BARE, ZERO_CROSSING_BOUND,
           SIGN_FLIP_BOUND, PRECISION)
FAILING = frozenset({STRUCTURAL, DECISION, DISCRETE, NONFINITE, UNCLASSIFIED,
                     ZERO_CROSSING_BARE, SIGN_FLIP_BARE})

# ---- CROSS_ENV_STATUS -------------------------------------------------------
NOT_NEEDED_BYTE_IDENTICAL = "NOT_NEEDED_BYTE_IDENTICAL"
DECISION_STABLE = "DECISION_STABLE_WITH_NUMERIC_DRIFT"
DECISION_DRIFT = "DECISION_DRIFT"
STRUCTURAL_DRIFT = "STRUCTURAL_DRIFT"
RESOLUTION_AMBIGUITY = "RESOLUTION_AMBIGUITY"
UNCLASSIFIED_DIVERGENCE = "UNCLASSIFIED_DIVERGENCE"
NOT_CHECKED = "NOT_CHECKED"
#: What this comparator can never establish, whatever it finds.
SCIENTIFIC_EQUIVALENCE = "NOT_ESTABLISHED"

#: The only resolution class under which a crossing is representation drift.
BELOW_RESOLUTION = "BELOW_RESOLUTION"
#: Column bases whose values are exact: any change is a discrete change.
EXACT_BASES = frozenset({"EXACT_BY_CONSTRUCTION", "COORDINATE",
                         "INPUT_CONSTANT"})

#: What basis the headline is drawn from.
MEASURED = "MEASURED_ACROSS_DISPATCHES"
IDENTICAL = "IDENTICAL_TREES"

#: A whole token that is a number, as these artefacts serialise one. Anchored
#: at both ends: a number INSIDE text makes the text text.
_INTEGER = re.compile(r"[+-]?\d+\Z")
_FLOAT = re.compile(r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?\Z")
_NONFINITE = frozenset({"nan", "+nan", "-nan", "inf", "+inf", "-inf",
                        "infinity", "+infinity", "-infinity"})


class ScopeError(RuntimeError):
    """The comparison did not measure what it would report on.

    A raise, not an ``assert``: ``python -O`` deletes asserts."""


def number(token):
    """``("int", n)`` / ``("float", x)`` for a whole numeric token, else
    None. Python ints and floats pass through; bools never do."""
    if isinstance(token, bool) or token is None:
        return None
    if isinstance(token, int):
        return ("int", token)
    if isinstance(token, float):
        return ("float", token)
    if not isinstance(token, str):
        return None
    if _INTEGER.match(token):
        return ("int", int(token))
    if _FLOAT.match(token) or token.lower() in _NONFINITE:
        return ("float", float(token))
    return None


def structured(token):
    """A cell that carries a JSON or Python-literal container, parsed; else
    None. Parsed safely -- ``json.loads`` or ``ast.literal_eval``, never
    ``eval``."""
    if not isinstance(token, str) or token[:1] not in ("{", "[", "("):
        return None
    try:
        return json.loads(token)
    except ValueError:
        pass
    try:
        value = ast.literal_eval(token)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    return value if isinstance(value, (dict, list, tuple)) else None


def _kind(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "text"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, (list, tuple)):
        return "array"
    return type(v).__name__


class _Compare:
    """One differing artefact, both sides parsed, every leaf classified."""

    def __init__(self, src, inventory, findings):
        self.src, self.inv, self.findings = src, inventory, findings
        self.leaves = self.differing = 0

    def add(self, kind, where, a, b, rel=None, bound=None):
        self.findings.append({"file": self.src, "key": where, "class": kind,
                              "committed": _short(a), "other": _short(b),
                              "rel": rel, "bound": bound})

    # -- a number against a number --------------------------------------------
    def numbers(self, where, a, b, na, nb, *, exact=False, bound=None):
        (ka, va), (kb, vb) = na, nb
        if ka != kb:
            return self.add(STRUCTURAL, where, a, b)
        if ka == "int" or exact:
            if va != vb:
                return self.add(DISCRETE, where, a, b)
            if a != b:
                return self.add(UNCLASSIFIED, where, a, b)
            return None
        fa, fb = math.isfinite(va), math.isfinite(vb)
        if not (fa and fb):
            if math.isnan(va) and math.isnan(vb):
                return None
            if va == vb:
                return None
            return self.add(NONFINITE, where, a, b)
        if va == vb:
            # the same value in different text: nothing this comparator
            # knows produces that, so it does not guess
            return self.add(UNCLASSIFIED, where, a, b)
        if va == 0.0 or vb == 0.0:
            ok = _inside(bound, va, vb)
            return self.add(ZERO_CROSSING_BOUND if ok else ZERO_CROSSING_BARE,
                            where, a, b, bound=bound)
        if (va < 0) != (vb < 0):
            ok = _inside(bound, va, vb)
            return self.add(SIGN_FLIP_BOUND if ok else SIGN_FLIP_BARE, where,
                            a, b, bound=bound)
        rel = abs(va - vb) / max(abs(va), abs(vb))
        return self.add(PRECISION, where, a, b, rel=rel, bound=bound)

    # -- any two values -------------------------------------------------------
    def value(self, where, a, b, *, exact=False, bound=None, text=True):
        """``text``: the values came from text (a CSV cell, a JSON string)
        and a numeric token inside it is a number."""
        self.leaves += 1
        if a == b and type(a) is type(b):
            return
        if isinstance(a, float) and isinstance(b, float) and \
                math.isnan(a) and math.isnan(b):
            return
        self.differing += 1
        na = number(a) if (text or not isinstance(a, str)) else None
        nb = number(b) if (text or not isinstance(b, str)) else None
        if na is not None and nb is not None:
            return self.numbers(where, a, b, na, nb, exact=exact, bound=bound)
        if (na is None) != (nb is None):
            return self.add(STRUCTURAL, where, a, b)
        sa, sb = structured(a), structured(b)
        if sa is not None or sb is not None:
            if sa is None or sb is None:
                return self.add(STRUCTURAL, where, a, b)
            return self.tree(where, sa, sb, bindable=False)
        if _kind(a) != _kind(b):
            return self.add(STRUCTURAL, where, a, b)
        return self.add(DECISION, where, a, b)

    # -- JSON-shaped trees ----------------------------------------------------
    def tree(self, where, a, b, *, bindable=True, root_a=None, root_b=None):
        if root_a is None:
            root_a, root_b = a, b
        ka, kb = _kind(a), _kind(b)
        if ka != kb:
            self.leaves += 1
            self.differing += 1
            return self.add(STRUCTURAL, where, a, b)
        if ka == "object":
            for k in sorted(set(a) ^ set(b), key=str):
                self.add(STRUCTURAL, f"{where}.{k}", a.get(k), b.get(k))
            for k in sorted(set(a) & set(b), key=str):
                self.tree(f"{where}.{k}", a[k], b[k], bindable=bindable,
                          root_a=root_a, root_b=root_b)
            return None
        if ka == "array":
            if len(a) != len(b):
                return self.add(STRUCTURAL, f"{where}[#]", len(a), len(b))
            for i, (x, y) in enumerate(zip(a, b)):
                self.tree(f"{where}[{i}]", x, y, bindable=bindable,
                          root_a=root_a, root_b=root_b)
            return None
        bound = None
        if bindable and ka in ("number", "text") and a != b:
            carrier = RI.json_carrier(self.inv, self.src, where)
            if carrier is not None:
                bound = _classes(_json_get(root_a, carrier),
                                 _json_get(root_b, carrier))
        return self.value(where, a, b, bound=bound, text=(ka == "text"))

    # -- CSV ------------------------------------------------------------------
    def table(self, a_rows, b_rows):
        if not a_rows or not b_rows:
            return self.add(STRUCTURAL, "[header]", len(a_rows), len(b_rows))
        ha, hb = a_rows[0], b_rows[0]
        if ha != hb:
            return self.add(STRUCTURAL, "[header]", ",".join(ha),
                            ",".join(hb))
        if len(a_rows) != len(b_rows):
            return self.add(STRUCTURAL, "[rows]", len(a_rows) - 1,
                            len(b_rows) - 1)
        long_spec = self.inv.get("row_bindings", {}).get(self.src)
        a_keyed = b_keyed = None
        if long_spec and long_spec.get("key_column") in ha \
                and long_spec.get("value_column") in ha:
            ki = ha.index(long_spec["key_column"])
            vi = ha.index(long_spec["value_column"])
            a_keyed = {r[ki]: r[vi] for r in a_rows[1:] if len(r) > vi}
            b_keyed = {r[ki]: r[vi] for r in b_rows[1:] if len(r) > vi}
        for i, (ra, rb) in enumerate(zip(a_rows[1:], b_rows[1:]), 1):
            if len(ra) != len(ha) or len(rb) != len(ha):
                self.add(STRUCTURAL, f"r{i}", len(ra), len(rb))
                continue
            for j, col in enumerate(ha):
                where = f"r{i}.{col}"
                x, y = ra[j], rb[j]
                entry = RI.column_entry(self.inv, self.src, col) or {}
                exact = entry.get("basis") in EXACT_BASES
                bound = None
                if x != y and entry.get("basis") == "CARRIER":
                    bound = self._wide_bound(ha, ra, rb, entry)
                elif x != y and a_keyed is not None and \
                        j == ha.index(long_spec["value_column"]):
                    bound = self._row_bound(long_spec, ra[ki], a_keyed,
                                            b_keyed)
                    where = f"r{i}[{ra[ki]}]"
                self.value(where, x, y, exact=exact, bound=bound)
        return None

    def _wide_bound(self, header, ra, rb, entry):
        cls = entry.get("resolution_from")
        if cls not in header:
            return None
        ci = header.index(cls)
        fi = header.index(entry["floor_from"]) if entry.get("floor_from") \
            in header else None
        return _classes(ra[ci], rb[ci],
                        None if fi is None else ra[fi],
                        None if fi is None else rb[fi])

    def _row_bound(self, spec, key, a_keyed, b_keyed):
        found = RI.row_carrier(self.inv, self.src, key)
        if found is None:
            return None
        cls_row, floor_row = found
        return _classes(a_keyed.get(cls_row), b_keyed.get(cls_row),
                        a_keyed.get(floor_row) if floor_row else None,
                        b_keyed.get(floor_row) if floor_row else None)


def _short(v):
    s = v if isinstance(v, str) else json.dumps(v, sort_keys=True,
                                                 default=str)
    return s if len(s) <= 120 else s[:117] + "..."


def _json_get(doc, path):
    try:
        return RI.json_at(doc, path)
    except (KeyError, ValueError):
        return None


def _floor(v):
    n = number(v)
    if n is None or not math.isfinite(n[1]) or n[1] <= 0:
        return None
    return n[1]


def _classes(ca, cb, fa=None, fb=None) -> dict:
    return {"committed": ca, "other": cb, "floor_committed": _floor(fa),
            "floor_other": _floor(fb), "floor_declared": fa is not None}


def _inside(bound, va, vb) -> bool:
    """A crossing is representation drift only when the quantity's OWN
    class says BELOW_RESOLUTION on both sides -- and, where a floor is
    bound, both values lie inside it on their own side. A declared floor
    that is not a positive number authorises nothing."""
    if not bound:
        return False
    if bound["committed"] != BELOW_RESOLUTION or \
            bound["other"] != BELOW_RESOLUTION:
        return False
    if bound["floor_declared"]:
        fa, fb = bound["floor_committed"], bound["floor_other"]
        if fa is None or fb is None:
            return False
        if not (abs(va) < fa and abs(vb) < fb):
            return False
    return True


def _read_rows(data: bytes):
    return list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))


def _json(data: bytes):
    def pairs(items):
        seen = {}
        for k, v in items:
            if k in seen:
                raise ValueError(f"duplicate key {k!r}")
            seen[k] = v
        return seen
    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs)


def load_inventory(path: Path = INVENTORY) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def compare(other: Path, committed: Path, *, declared, exempt,
            inventory: dict | None = None) -> dict:
    """Compare every declared, non-exempt artefact of ``other`` with
    ``committed``. Returns the report; raises ScopeError on a comparison
    that cannot measure anything."""
    declared, exempt = frozenset(declared), frozenset(exempt)
    if not declared:
        raise ScopeError("the declared canonical set is empty; a comparison "
                         "over nothing reports agreement")
    if not exempt <= declared:
        raise ScopeError(f"exemption names undeclared artefact(s) "
                         f"{sorted(exempt - declared)}; an exemption is for "
                         "a declared artefact, by name")
    inventory = load_inventory() if inventory is None else inventory
    scope = sorted(declared - exempt)
    findings: list = []
    files: dict = {}
    leaves = leaves_differing = 0
    for name in scope:
        pa, pb = committed / name, other / name
        if not pa.is_file() or not pb.is_file():
            files[name] = ("MISSING_COMMITTED" if not pa.is_file()
                           else "MISSING_OTHER")
            findings.append({"file": name, "key": "[artefact]",
                             "class": STRUCTURAL, "committed": pa.is_file(),
                             "other": pb.is_file(), "rel": None,
                             "bound": None})
            continue
        try:
            da, db = pa.read_bytes(), pb.read_bytes()
        except OSError as exc:
            files[name] = "UNREADABLE"
            findings.append({"file": name, "key": "[artefact]",
                             "class": UNCLASSIFIED, "committed": str(exc),
                             "other": None, "rel": None, "bound": None})
            continue
        if da == db:
            files[name] = "IDENTICAL"
            continue
        files[name] = "DIFFERING"
        c = _Compare(name, inventory, findings)
        try:
            if name.endswith(".json"):
                c.tree("", _json(da), _json(db))
            elif name.endswith(".csv"):
                c.table(_read_rows(da), _read_rows(db))
            else:
                c.add(UNCLASSIFIED, "[artefact]",
                      f"{len(da)} bytes", f"{len(db)} bytes")
        except (ValueError, UnicodeDecodeError, RecursionError,
                csv.Error) as exc:
            c.add(UNCLASSIFIED, "[artefact]", f"unparseable: {exc}", None)
        leaves += c.leaves
        leaves_differing += c.differing
    foreign = sorted(p.name for p in other.iterdir()
                     if p.is_file() and p.name not in declared) \
        if other.is_dir() else []
    for name in foreign:
        files[name] = "FOREIGN"
        findings.append({"file": name, "key": "[artefact]",
                         "class": STRUCTURAL, "committed": None,
                         "other": "present", "rel": None, "bound": None})
    counts = {k: sum(1 for f in findings if f["class"] == k)
              for k in CLASSES}
    precisions = sorted((f for f in findings if f["class"] == PRECISION),
                        key=lambda f: -f["rel"])
    differing = sum(1 for v in files.values() if v == "DIFFERING")
    report = {
        "declared": len(declared), "exempted": sorted(exempt),
        "files_compared": sum(1 for v in files.values()
                              if v in ("IDENTICAL", "DIFFERING")),
        "files_differing": differing, "files": files,
        "leaves_compared": leaves, "leaves_differing": leaves_differing,
        "counts": counts,
        "max_precision_rel": precisions[0]["rel"] if precisions else 0.0,
        "largest_precision": precisions[:5],
        "basis": IDENTICAL if differing == 0 and not findings else MEASURED,
        "findings": findings,
        "scientific_equivalence": SCIENTIFIC_EQUIVALENCE,
    }
    report["status"] = status(report)
    return report


def status(report: dict) -> str:
    """CROSS_ENV_STATUS, most serious first."""
    c = report["counts"]
    if c[STRUCTURAL]:
        return STRUCTURAL_DRIFT
    if c[DECISION] or c[DISCRETE] or c[NONFINITE]:
        return DECISION_DRIFT
    if c[UNCLASSIFIED]:
        return UNCLASSIFIED_DIVERGENCE
    if c[ZERO_CROSSING_BARE] or c[SIGN_FLIP_BARE]:
        return RESOLUTION_AMBIGUITY
    if report["files_differing"] == 0:
        return NOT_NEEDED_BYTE_IDENTICAL
    return DECISION_STABLE


def check_scope(report: dict) -> None:
    """Refuse a verdict drawn from an empty comparison."""
    if report["files_compared"] == 0:
        raise ScopeError(
            "compared 0 files: no declared artefact was present on both "
            "sides; 'no decision changed' would be a statement about an "
            "empty set")
    if report["files_differing"] and report["leaves_compared"] == 0 \
            and not report["counts"][STRUCTURAL] \
            and not report["counts"][UNCLASSIFIED]:
        raise ScopeError(
            f"{report['files_differing']} file(s) differ but 0 leaves were "
            "compared; nothing was actually put side by side")


def summary_line(report: dict) -> str:
    """One machine-readable line; CI logs are read for it."""
    c = report["counts"]
    return (f"CROSS_ENV_STATUS={report['status']} "
            f"files_compared={report['files_compared']} "
            f"files_differing={report['files_differing']} "
            f"leaves_compared={report['leaves_compared']} "
            f"structural={c[STRUCTURAL]} decision={c[DECISION]} "
            f"discrete={c[DISCRETE]} nonfinite={c[NONFINITE]} "
            f"unclassified={c[UNCLASSIFIED]} "
            f"bare_zero_crossings={c[ZERO_CROSSING_BARE]} "
            f"bare_sign_flips={c[SIGN_FLIP_BARE]} "
            f"bound_zero_crossings={c[ZERO_CROSSING_BOUND]} "
            f"bound_sign_flips={c[SIGN_FLIP_BOUND]} "
            f"precision={c[PRECISION]} "
            "SCIENTIFIC_EQUIVALENCE_STATUS="
            f"{report['scientific_equivalence']}")


def print_report(report: dict, out=None) -> None:
    out = sys.stdout if out is None else out
    c = report["counts"]
    print(f"compared {report['files_compared']} declared file(s) "
          f"({report['declared']} declared, "
          f"{len(report['exempted'])} exempt: "
          f"{', '.join(report['exempted']) or 'none'}); "
          f"{report['files_differing']} differ byte-for-byte; "
          f"{report['leaves_compared']} leaves put side by side, "
          f"{report['leaves_differing']} differing", file=out)
    for k in CLASSES:
        tag = "REFUSES" if k in FAILING else "permitted"
        print(f"  {k:<20} {c[k]:6d}   {tag}", file=out)
    if c[PRECISION]:
        print(f"  largest relative difference "
              f"{report['max_precision_rel']:.3e}:", file=out)
        for f in report["largest_precision"]:
            print(f"    {f['file']} {f['key']}: {f['committed']} -> "
                  f"{f['other']} ({f['rel']:.3e})", file=out)
    failing = [f for f in report["findings"] if f["class"] in FAILING]
    for f in failing[:25]:
        print(f"  ! {f['class']}: {f['file']} {f['key']}: "
              f"{f['committed']!r} -> {f['other']!r}", file=out)
    if len(failing) > 25:
        print(f"  ! ... and {len(failing) - 25} more", file=out)
    print(summary_line(report), file=out)
    print("scientific equivalence: NOT ESTABLISHED -- decision stability "
          "under a different backend is not equivalence of the "
          "computations, and this comparison never says it is.", file=out)


def declared_scope(committed: Path):
    """The declared canonical set and the exemptions, read from the
    committed tree's own package_consistency_check.py by parsing."""
    src = (committed / "package_consistency_check.py").read_text(
        encoding="utf-8")
    declared = exempt = None
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Assign):
            continue
        names = [getattr(t, "id", None) for t in node.targets]
        if "CANONICAL_EXPECTED" in names:
            for k, v in zip(node.value.keys, node.value.values):
                if getattr(k, "value", None) == "canonical_outputs":
                    declared = [e.value for e in v.elts]
        if "_REGEN_EXEMPT" in names:
            exempt = ast.literal_eval(node.value.args[0])
    if declared is None or exempt is None:
        raise ScopeError("package_consistency_check.py does not declare the "
                         "canonical set and its exemptions where expected")
    return frozenset(declared), frozenset(exempt)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("other", type=Path,
                   help="tree regenerated in the other environment")
    p.add_argument("committed", type=Path, nargs="?", default=ROOT,
                   help="tree holding the committed canonical copies")
    p.add_argument("--json", type=Path, help="write the full report here")
    args = p.parse_args(argv)
    try:
        declared, exempt = declared_scope(args.committed)
        report = compare(args.other, args.committed, declared=declared,
                         exempt=exempt,
                         inventory=load_inventory(args.committed / "docs"
                                                  / "resolution_inventory"
                                                  ".json"))
        check_scope(report)
    except ScopeError as exc:
        print(f"SCOPE REFUSED: {exc}", file=sys.stderr)
        return 2
    print_report(report)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True))
        print(f"full report: {args.json}")
    return 0 if report["status"] in (NOT_NEEDED_BYTE_IDENTICAL,
                                      DECISION_STABLE) else 1


if __name__ == "__main__":                       # pragma: no cover
    raise SystemExit(main())
