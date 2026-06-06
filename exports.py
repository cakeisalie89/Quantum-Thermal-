"""Small CSV/JSON export helpers for the multiphysics layer."""
from __future__ import annotations
import csv
import json
import math


def write_rows_csv(path, rows, fieldnames=None):
    rows = list(rows)
    if not rows:
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(fieldnames or ["empty"])
        return
    fieldnames = fieldnames or list(rows[0].keys())
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
