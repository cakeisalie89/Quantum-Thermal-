"""Independent checks: code that verifies a ResultBundle without calling
the implementation that produced it."""
from __future__ import annotations

from ..quantity import Quantity


def quantity(output):
    """The Quantity of an output a check has decided is usable, or a
    refusal: a value is never read from a FAILED or UNDEFINED output."""
    q = output.quantity
    if q is None:
        raise ValueError(f"output {output.name} carries no value "
                         f"({output.status.value}); nothing to check")
    return q


def agreement(output, value, error, *, resolution_class,
              basis: str) -> tuple:
    """``(output name, Quantity)``: the check's own value for ``output``
    and the error it states for that value, in the output's unit -- one
    entry of a verification result's agreement (AUD-025). The error is the
    one the check's criterion credits it with, so a reader bounding the
    producer's value by ``|v_producer - v_check| + r_check`` uses no more
    precision than the check claimed."""
    return (output.name, Quantity(float(value), quantity(output).unit,
                                  resolution=float(error),
                                  resolution_class=resolution_class,
                                  resolution_basis=basis))


def resolution(q) -> float:
    """A quantity's stated resolution, or a refusal: a check cannot hold a
    value to a bound nobody stated."""
    if q.resolution is None:
        raise ValueError("a quantity with no stated resolution cannot be "
                         "held to one")
    return float(q.resolution)
