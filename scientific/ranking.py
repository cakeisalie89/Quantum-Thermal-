"""Ordering quantities only as far as their resolution resolves them.

D-2026-99, made a policy. A table that listed "the sixteen hottest cells"
chose them by ``argsort`` of temperatures agreeing to ten significant
digits; which cells it named was decided in the last bits and moved with the
numerical backend. The defect class is general: any selection or ranking made
from differences smaller than what the method resolves is a statement about
roundoff presented as a statement about the science.

THE SEMANTICS THIS POLICY APPLIES

Resolution tiers, with every indistinguishable candidate reported at a
selection boundary, and a secondary order used for presentation only after
the tiers are fixed:

* each value carries a declared resolution -- the bound ``r`` with
  ``|computed - exact| <= r`` that its method states (a
  :class:`~scientific.quantity.Quantity` with a resolution class and basis,
  or a sampling estimator's confidence half-width). A value with no declared
  resolution is REFUSED, never ranked: ranking it would need a scale, and
  choosing one is the fabricated floor D-2026-66 refuses;
* two values are DISTINGUISHABLE when their intervals ``[v - r, v + r]``
  are disjoint -- only then is the order of the exact values known from the
  bounds. Overlapping intervals are indistinguishable, and the tiers are the
  connected components of that relation: every member of a higher tier is
  distinguishably above every member of a lower one, and nothing is claimed
  about order inside a tier;
* a top-k selection takes whole tiers while they fit. The tier that
  straddles the boundary is reported WHOLE as candidates with the number of
  slots left (``AMBIGUOUS_AT_RESOLUTION``): membership is never decided by
  mantissa noise, a stable sort, a coordinate preference or a rounding;
* inside a tier, members are listed by their key -- a PRESENTATION order,
  labelled as such, carrying no scientific meaning.

What this does NOT do: resolve the distinction. Sampling or refining until
the tiers separate is the only way to do that, and it is the caller's
choice. Nothing here moves a legacy QTA output; D-2026-99's own artefact
stays as it is and stays refused by the cross-environment comparator
wherever its selection moves, until its method declares a resolution and the
owner authorizes a migration.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .quantity import Quantity, QuantityError

SEMANTICS = "RESOLUTION_TIERS_REPORT_ALL_INDISTINGUISHABLE"
PRESENTATION_NOTE = ("within a tier members are listed by key; that order is "
                     "PRESENTATION_ORDER_ONLY and carries no scientific "
                     "meaning")

RESOLVED = "RESOLVED"
AMBIGUOUS = "AMBIGUOUS_AT_RESOLUTION"

IDENTICAL = "IDENTICAL_AT_RESOLUTION"
CONSISTENT = "CONSISTENT_AT_RESOLUTION"
DISAGREE = "METHODS_DISAGREE"


class RankingRefused(ValueError):
    pass


@dataclass(frozen=True)
class Ranked:
    """One value to order: its key, value and declared resolution."""
    key: str
    value: float
    resolution: float
    basis: str

    def __post_init__(self):
        if not isinstance(self.key, str) or not self.key:
            raise RankingRefused("a ranked value needs a key")
        for name in ("value", "resolution"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not math.isfinite(v):
                raise RankingRefused(f"{self.key}: {name} {v!r} is not a "
                                     "finite number")
        if self.resolution < 0:
            raise RankingRefused(f"{self.key}: a resolution is a bound, "
                                 "never negative")
        if not isinstance(self.basis, str) or not self.basis.strip():
            raise RankingRefused(f"{self.key}: a resolution must say where "
                                 "it came from")

    @classmethod
    def from_quantity(cls, key: str, q: Quantity) -> "Ranked":
        if q.exact:
            return cls(key, float(q.value), 0.0, "exact by construction")
        if q.resolution is None:
            raise RankingRefused(
                f"{key}: no declared resolution, so its order against "
                "anything is undecidable; ranking it would let roundoff "
                "decide (D-2026-99)")
        return cls(key, float(q.value), float(q.resolution),
                   f"{q.resolution_class.value}: {q.resolution_basis}")


@dataclass(frozen=True)
class Tier:
    index: int
    keys: tuple
    low: float
    high: float

    def to_record(self) -> dict:
        return {"tier": self.index, "members": list(self.keys),
                "interval": [self.low, self.high]}


def _check(items) -> list:
    items = list(items)
    if not items:
        raise RankingRefused("nothing to rank")
    if not all(isinstance(i, Ranked) for i in items):
        raise RankingRefused("rank Ranked values (from_quantity states the "
                             "resolution)")
    keys = [i.key for i in items]
    if len(keys) != len(set(keys)):
        raise RankingRefused("keys repeat")
    return items


def tiers(items, *, descending: bool = True) -> list:
    """The resolution tiers, highest first when ``descending``.

    Components of interval overlap: sorted by lower bound, an interval that
    starts at or below the running upper bound joins the current tier.
    Touching intervals (``low == high``) overlap -- the bound is inclusive.
    """
    items = _check(items)
    spans = sorted(((i.value - i.resolution, i.value + i.resolution, i.key)
                    for i in items), key=lambda s: (s[0], s[1], s[2]))
    groups: list = []
    for lo, hi, key in spans:
        if groups and lo <= groups[-1][1]:
            g = groups[-1]
            g[1] = max(g[1], hi)
            g[2].append(key)
        else:
            groups.append([lo, hi, [key]])
    if descending:
        groups.reverse()
    return [Tier(n, tuple(sorted(g[2])), g[0], g[1])
            for n, g in enumerate(groups)]


@dataclass(frozen=True)
class Selection:
    k: int
    selected: tuple
    candidates: tuple
    slots_left: int
    status: str

    def to_record(self) -> dict:
        return {"k": self.k, "selected": list(self.selected),
                "candidates": list(self.candidates),
                "slots_left_among_candidates": self.slots_left,
                "status": self.status, "semantics": SEMANTICS,
                "presentation": PRESENTATION_NOTE}


def select_top(items, k: int, *, descending: bool = True) -> Selection:
    """The top ``k`` as far as resolution decides it, and no further."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise RankingRefused(f"k must be a positive integer: {k!r}")
    selected: list = []
    for t in tiers(items, descending=descending):
        room = k - len(selected)
        if room <= 0:
            break
        if len(t.keys) <= room:
            selected.extend(t.keys)
            continue
        return Selection(k, tuple(selected), t.keys, room, AMBIGUOUS)
    return Selection(k, tuple(selected), (), 0, RESOLVED)


def _rank_of(tier_list) -> dict:
    return {key: t.index for t in tier_list for key in t.keys}


@dataclass(frozen=True)
class RankComparison:
    status: str
    method_a: str
    method_b: str
    discordant: tuple
    resolved_by_a_only: tuple
    resolved_by_b_only: tuple
    kendall_tau_b: float
    pairs: int

    def to_record(self) -> dict:
        return {"status": self.status, "methods": [self.method_a,
                                                   self.method_b],
                "discordant_pairs": [list(p) for p in self.discordant],
                "ordered_by_a_only": [list(p) for p in
                                      self.resolved_by_a_only],
                "ordered_by_b_only": [list(p) for p in
                                      self.resolved_by_b_only],
                "kendall_tau_b_over_tiers": self.kendall_tau_b,
                "pairs_compared": self.pairs, "semantics": SEMANTICS}


def compare(method_a: str, tiers_a, method_b: str, tiers_b
            ) -> RankComparison:
    """Two rankings of the same keys, compared only where each resolves.

    A pair is DISCORDANT when both methods order it, oppositely; that is
    METHODS_DISAGREE, reported and never resolved here -- which method is
    right is a scientific question for a person. A pair one method orders
    and the other leaves in one tier is not a disagreement; it is recorded.
    Kendall's tau-b is computed over tier ranks, ties as ties.
    """
    ra, rb = _rank_of(tiers_a), _rank_of(tiers_b)
    if set(ra) != set(rb):
        raise RankingRefused(
            f"the rankings cover different keys: only in {method_a} "
            f"{sorted(set(ra) - set(rb))}, only in {method_b} "
            f"{sorted(set(rb) - set(ra))}")
    keys = sorted(ra)
    if len(keys) < 2:
        raise RankingRefused("a comparison of fewer than two keys "
                             "examines no pair")
    disc, a_only, b_only = [], [], []
    conc = dis = ties_a = ties_b = 0
    for i, x in enumerate(keys):
        for y in keys[i + 1:]:
            da = ra[x] - ra[y]
            db = rb[x] - rb[y]
            if da and db:
                if (da > 0) != (db > 0):
                    disc.append((x, y))
                    dis += 1
                else:
                    conc += 1
            elif da:
                a_only.append((x, y))
                ties_b += 1
            elif db:
                b_only.append((x, y))
                ties_a += 1
            else:
                ties_a += 1
                ties_b += 1
    n0 = len(keys) * (len(keys) - 1) // 2
    denom = math.sqrt((n0 - ties_a) * (n0 - ties_b))
    tau = (conc - dis) / denom if denom else 0.0
    if disc:
        status = DISAGREE
    elif [t.keys for t in tiers_a] == [t.keys for t in tiers_b]:
        status = IDENTICAL
    else:
        status = CONSISTENT
    return RankComparison(status, method_a, method_b, tuple(disc),
                          tuple(a_only), tuple(b_only), tau, n0)


def ranked_quantities(named: dict) -> list:
    """``{key: Quantity}`` to Ranked values, refusing any without a
    declared resolution."""
    out = []
    for key in sorted(named):
        try:
            out.append(Ranked.from_quantity(key, named[key]))
        except QuantityError as exc:
            raise RankingRefused(f"{key}: {exc}") from exc
    return out
