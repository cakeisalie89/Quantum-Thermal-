"""Small CSV/JSON export helpers for the multiphysics layer."""
from __future__ import annotations
import csv
import json
import math


class CsvSchemaError(ValueError):
    """A row set cannot be written without silently losing governed fields."""


def _union_fieldnames(rows):
    """Deterministic union of every key across every row, in first-seen order.

    Deriving the header from ``rows[0]`` alone silently drops any field that
    only appears in a later row. That is data loss in a governed artifact, not
    a formatting detail: it blanked the total radiative load reaching 10 mK in
    ``radiation_paths_3d_budget.csv``. Order is first-seen (not sorted) so
    re-deriving an artifact from unchanged inputs is byte-stable.
    """
    names = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                names.append(k)
    return names


def write_rows_csv(path, rows, fieldnames=None):
    """Write ``rows`` as CSV, failing closed rather than dropping fields.

    ``fieldnames`` is a declared contract when supplied: a row carrying a key
    outside it raises instead of being silently truncated. When it is omitted
    the header is the union of all row keys, so a heterogeneous summary row
    keeps its values.
    """
    rows = list(rows)
    if not rows:
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(fieldnames or ["empty"])
        return
    union = _union_fieldnames(rows)
    if fieldnames is None:
        fieldnames = union
    else:
        fieldnames = list(fieldnames)
        extra = [k for k in union if k not in set(fieldnames)]
        if extra:
            raise CsvSchemaError(
                f"{path}: rows carry fields absent from the declared header "
                f"{extra}; writing would discard governed data")
    for i, r in enumerate(rows):
        for k, v in r.items():
            if isinstance(v, (dict, list, tuple, set)):
                raise CsvSchemaError(
                    f"{path}: row {i} field {k!r} is a {type(v).__name__}; "
                    "nested containers have no faithful CSV representation")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def write_profile_csv(path, header, columns):
    """columns: list of equal-length sequences; header: list of names."""
    n = len(columns[0])
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for i in range(n):
            w.writerow([f"{columns[c][i]:.9e}" if isinstance(columns[c][i], float)
                        else columns[c][i] for c in range(len(columns))])


def _sanitize(o):
    if isinstance(o, dict):
        return {k: _sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_sanitize(v) for v in o]
    if isinstance(o, float):
        if math.isinf(o):
            return "inf" if o > 0 else "-inf"
        if math.isnan(o):
            return "nan"
    return o


def write_json(path, obj):
    with open(path, "w") as f:
        json.dump(_sanitize(obj), f, indent=2)
