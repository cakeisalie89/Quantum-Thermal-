"""Independent checks: code that verifies a ResultBundle without calling
the implementation that produced it."""
from __future__ import annotations


def quantity(output):
    """The Quantity of an output a check has decided is usable, or a
    refusal: a value is never read from a FAILED or UNDEFINED output."""
    q = output.quantity
    if q is None:
        raise ValueError(f"output {output.name} carries no value "
                         f"({output.status.value}); nothing to check")
    return q


def resolution(q) -> float:
    """A quantity's stated resolution, or a refusal: a check cannot hold a
    value to a bound nobody stated."""
    if q.resolution is None:
        raise ValueError("a quantity with no stated resolution cannot be "
                         "held to one")
    return float(q.resolution)
