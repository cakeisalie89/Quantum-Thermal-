"""An error budget for one output of one model: every contribution says
whether it was measured, estimated, assumed, or not established.

WHAT A BUDGET IS FOR. A computed value with no stated error is not a
result. A stated error that mixes a bound the method guarantees, a number
computed against a reference, and a spread that follows from a guess about
the inputs is not much better: each kind means something different, and a
reader who cannot tell them apart will add them up. Each contribution here
carries its ``basis``:

- MEASURED: computed in this run against an independent reference, such as
  the analytic series for the slab;
- ESTIMATED: from the method's own theory (a Richardson estimate, a
  rounding bound). It is not a measurement, and it can be checked against
  one;
- ASSUMED: it follows from distributions the caller declared for the
  inputs. It is exactly as good as those declarations;
- NOT_ESTABLISHED: nothing here can say. Model form without observations,
  and measurement where nothing was measured.

NOT_ESTABLISHED is listed and is never summed. A total says which
contributions it excludes.

PROPAGATION, TWICE. Parameter uncertainty is propagated two ways that share
no step. The first is linearly, through central-difference sensitivity
coefficients. The second is by Monte Carlo over the declared distributions,
with a fixed seed. They are compared at the Monte Carlo estimate's own
standard error: agreement says the response is close to linear over the
declared spread, and disagreement is a finding about the model, never a
reason to keep whichever looks better.

NON_AUTHORITATIVE. A budget is a property of a model under stated
assumptions. It is not a measurement and not a validation, and it
authorizes nothing.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable

SCHEMA = "error-budget/1"
MEASURED = "MEASURED"
ESTIMATED = "ESTIMATED"
ASSUMED = "ASSUMED"
NOT_ESTABLISHED = "NOT_ESTABLISHED"
BASES = (MEASURED, ESTIMATED, ASSUMED, NOT_ESTABLISHED)
#: as in scientific.quantity.UncertaintyClass
CLASSES = ("NUMERICAL", "PARAMETRIC", "MODEL_FORM", "MEASUREMENT")
#: the Monte Carlo and linear standard deviations agree when they differ by
#: no more than this many Monte Carlo standard errors
AGREEMENT_SE = 3.0
Z95 = 1.959963984540054


class BudgetRefused(ValueError):
    pass


@dataclass(frozen=True)
class Contribution:
    """One part of an output's error, in the output's unit."""
    name: str
    uncertainty_class: str
    basis: str
    #: the half-width this contribution adds; None when NOT_ESTABLISHED
    half_width: float | None
    method: str
    note: str = ""
    #: a MEASURED contribution's name for the ESTIMATED one it checks and
    #: replaces in the total: where a reference measured the error, the
    #: measurement is what the total carries, and the estimate is kept and
    #: held to it
    supersedes: str | None = None

    def __post_init__(self):
        if self.basis not in BASES:
            raise BudgetRefused(f"{self.name}: basis {self.basis!r} is not "
                                f"one of {BASES}")
        if self.uncertainty_class not in CLASSES:
            raise BudgetRefused(f"{self.name}: class "
                                f"{self.uncertainty_class!r} is not one of "
                                f"{CLASSES}")
        if (self.basis == NOT_ESTABLISHED) != (self.half_width is None):
            raise BudgetRefused(
                f"{self.name}: a contribution has a half-width exactly when "
                "it is established; a number for what nothing here can say "
                "would be read as one")
        if self.half_width is not None and (
                not math.isfinite(self.half_width) or self.half_width < 0):
            raise BudgetRefused(f"{self.name}: a half-width is finite and "
                                f">= 0, not {self.half_width!r}")
        if self.supersedes is not None and self.basis != MEASURED:
            raise BudgetRefused(f"{self.name}: only a measurement replaces "
                                "an estimate")

    def to_record(self) -> dict:
        return {"name": self.name, "class": self.uncertainty_class,
                "basis": self.basis, "half_width": self.half_width,
                "method": self.method, "note": self.note,
                "supersedes": self.supersedes}


@dataclass(frozen=True)
class Distribution:
    """A declared input distribution: normal, with a standard deviation."""
    sigma: float
    #: what the declaration rests on, said once, so it travels with it
    source: str = "declared by the caller; not measured"

    def __post_init__(self):
        if not math.isfinite(self.sigma) or self.sigma <= 0:
            raise BudgetRefused(f"a standard deviation is > 0, not "
                                f"{self.sigma!r}")


def linear(evaluate: Callable[[dict], float], base: dict,
           inputs: dict, *, rel_step: float = 1e-3) -> dict:
    """First-order propagation: central differences at ``base``.

    ``inputs`` maps a parameter to its :class:`Distribution`. The variance is
    the sum of (dy/dp * sigma)^2, which assumes the inputs are independent,
    and the record says so."""
    y0 = evaluate(dict(base))
    rows, var = [], 0.0
    for k in sorted(inputs):
        p = float(base[k])
        step = rel_step * (abs(p) if p != 0 else 1.0)
        up = evaluate({**base, k: p + step})
        dn = evaluate({**base, k: p - step})
        c = (up - dn) / (2.0 * step)
        part = (c * inputs[k].sigma) ** 2
        var += part
        rows.append({"parameter": k, "sigma": inputs[k].sigma,
                     "coefficient": c, "variance_part": part})
    std = math.sqrt(var)
    for r in rows:
        r["share"] = r["variance_part"] / var if var > 0 else 0.0
    return {"method": "LINEAR_CENTRAL_DIFFERENCE", "rel_step": rel_step,
            "assumes": "independent inputs; a response linear over the "
                       "declared spread",
            "value_at_base": y0, "std": std, "rows": rows,
            "evaluations": 1 + 2 * len(inputs)}


def monte_carlo(evaluate: Callable[[dict], float], base: dict,
                inputs: dict, *, n: int, seed: int,
                admissible: Callable[[dict], bool] | None = None) -> dict:
    """Monte Carlo over independent normal inputs, with a fixed seed. A
    sample ``admissible`` refuses is drawn again, and counted."""
    if n < 30:
        raise BudgetRefused("fewer than 30 samples estimate no spread")
    rng = random.Random(seed)
    ys: list[float] = []
    redrawn = 0
    while len(ys) < n:
        p = dict(base)
        for k in sorted(inputs):
            p[k] = rng.gauss(float(base[k]), inputs[k].sigma)
        if admissible is not None and not admissible(p):
            redrawn += 1
            if redrawn > 10 * n:
                raise BudgetRefused("the declared distributions put almost "
                                    "every sample outside the model's domain")
            continue
        ys.append(evaluate(p))
    ys.sort()
    mean = sum(ys) / n
    std = math.sqrt(sum((y - mean) ** 2 for y in ys) / (n - 1))

    def q(f):
        i = f * (n - 1)
        lo = int(math.floor(i))
        hi = min(lo + 1, n - 1)
        return ys[lo] + (ys[hi] - ys[lo]) * (i - lo)

    return {"method": "MONTE_CARLO", "samples": n, "seed": seed,
            "generator": "python random.Random (Mersenne Twister), gauss",
            "redrawn": redrawn, "mean": mean, "std": std,
            "std_standard_error": std / math.sqrt(2.0 * (n - 1)),
            "p2_5": q(0.025), "p97_5": q(0.975)}


def compare(lin: dict, mc: dict) -> dict:
    """Do the two propagations agree, at the Monte Carlo standard error?"""
    gap = abs(mc["std"] - lin["std"])
    se = mc["std_standard_error"]
    agree = gap <= AGREEMENT_SE * se
    return {"std_linear": lin["std"], "std_monte_carlo": mc["std"],
            "gap": gap, "monte_carlo_standard_error": se,
            "verdict": "AGREE_WITHIN_SAMPLING" if agree else "DIFFER",
            "reading": ("the response is close to linear over the declared "
                        "spread" if agree else
                        "the response is not linear over the declared "
                        "spread, or the sample is too small; the Monte Carlo "
                        "interval is the one that does not assume linearity")}


@dataclass
class Budget:
    """The contributions to one output, and what may be said with them."""
    model: str
    output: str
    unit: str
    value: float
    contributions: list = field(default_factory=list)

    def add(self, c: Contribution) -> None:
        if any(x.name == c.name for x in self.contributions):
            raise BudgetRefused(f"{c.name} is in the budget already")
        if c.supersedes is not None and not any(
                x.name == c.supersedes and x.basis == ESTIMATED
                for x in self.contributions):
            raise BudgetRefused(f"{c.name} replaces {c.supersedes!r}, which "
                                "is no estimate in this budget")
        self.contributions.append(c)

    def total(self) -> dict:
        """The half-width the established contributions add up to, and what
        it leaves out.

        Contributions are added linearly, not in quadrature. Numerical
        errors here are systematic (a discretization's error has a sign and
        a size), not draws, and combining them as independent standard
        deviations would understate them. An estimate a measurement
        replaced is reported and not added."""
        replaced = {c.supersedes for c in self.contributions if c.supersedes}
        used = [c for c in self.contributions
                if c.basis != NOT_ESTABLISHED and c.name not in replaced]
        excluded = [c.name for c in self.contributions
                    if c.basis == NOT_ESTABLISHED]
        return {"half_width": sum(c.half_width for c in used),
                "combination": "linear sum of every established "
                               "contribution not replaced by a measurement",
                "includes": [c.name for c in used],
                "replaced_by_a_measurement": sorted(replaced),
                "excludes_not_established": excluded}

    def to_record(self) -> dict:
        t = self.total()
        return {"schema": SCHEMA, "model": self.model, "output": self.output,
                "unit": self.unit, "value": self.value,
                "contributions": [c.to_record() for c in self.contributions],
                "total": t,
                "statement": (
                    f"{self.output} = {self.value:.6g} +/- "
                    f"{t['half_width']:.3g} {self.unit}: the parametric "
                    "part at 95% under the declared input distributions, "
                    "the numerical part as measured or estimated, each said "
                    "which; "
                    + ("not established: " + ", ".join(
                        t["excludes_not_established"])
                       if t["excludes_not_established"] else
                       "nothing left out")),
                "authority": "NON_AUTHORITATIVE"}


def estimate_bounds(estimate: float, measured: float) -> dict:
    """Whether an ESTIMATED error covers what a reference MEASURED. If it
    does not, the estimate is wrong for this run, and the budget says so
    instead of quietly using the larger."""
    holds = measured <= estimate
    return {"estimate": estimate, "measured": measured,
            "verdict": "ESTIMATE_COVERS_MEASURED" if holds
            else "ESTIMATE_DOES_NOT_COVER",
            "ratio": measured / estimate if estimate > 0 else None}
