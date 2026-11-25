"""The VerificationResult: what a check establishes, and what it does not.

A check has a subject (a ResultBundle, by digest), a type, a verdict, the
value it measured with its unit and resolution, the criterion and where the
criterion comes from, the evidence it rests on, who ran it and with which
code, and its limitations. Three rules make "verified" mean something:

* **Independence is a field, not a word.** A check that claims to be an
  independent implementation must be run by code whose implementation
  digest differs from the producer's, must say what kind of independence it
  has (a different discretization is not a different physical model), and
  must list what it shares with the producer -- material models, source
  terms -- as limitations. "Independent" with the producer's own digest is
  refused.
* **What a check establishes is bounded by what it compared.** A convergence
  study, an invariant, or agreement between two simulations establishes
  NUMERICAL consistency. EXPERIMENTAL_VALIDATION can be claimed only by a
  check that cites at least one measured observation (RAW or PROCESSED);
  numerically converged is not experimentally validated.
* **A verdict needs a measurement.** PASS or FAIL without a measured
  Quantity and a criterion is refused: a check that measured nothing did not
  pass, it did not run (NOT_RUN) or could not decide (INCONCLUSIVE).

A VerificationResult is evidence. It is not an authority decision; the
authority layer decides, citing it by digest.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .identity import IdentityError, digest, require_digest
from .observation import MEASURED, ObservationKind
from .quantity import Quantity


class CheckType(str, Enum):
    INVARIANT = "INVARIANT"
    CONVERGENCE = "CONVERGENCE"
    ANALYTIC_REFERENCE = "ANALYTIC_REFERENCE"
    INDEPENDENT_IMPLEMENTATION = "INDEPENDENT_IMPLEMENTATION"
    INTEGRITY = "INTEGRITY"
    COMPARISON_WITH_MEASUREMENT = "COMPARISON_WITH_MEASUREMENT"


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_RUN = "NOT_RUN"


class Independence(str, Enum):
    NONE = "NONE"
    #: same governing equations, a different discretization / solver code.
    DIFFERENT_DISCRETIZATION = "DIFFERENT_DISCRETIZATION"
    #: a separately written implementation of the same model.
    DIFFERENT_IMPLEMENTATION = "DIFFERENT_IMPLEMENTATION"
    #: a different physical model of the same quantity.
    DIFFERENT_MODEL = "DIFFERENT_MODEL"


class Establishes(str, Enum):
    NUMERICAL_CONSISTENCY = "NUMERICAL_CONSISTENCY"
    INDEPENDENT_NUMERICAL_AGREEMENT = "INDEPENDENT_NUMERICAL_AGREEMENT"
    EXPERIMENTAL_VALIDATION = "EXPERIMENTAL_VALIDATION"


class VerificationError(ValueError):
    pass


def _agreement(result: "VerificationResult") -> tuple:
    """The agreement, validated and in output order (AUD-025).

    Each entry names one output and carries the check's own value as a
    Quantity with a stated resolution: a value with no stated error bounds
    nothing. A check that compared nothing states nothing, so NOT_RUN and
    INCONCLUSIVE carry no agreement.
    """
    out = []
    for entry in result.agreement:
        try:
            name, q = entry
        except (TypeError, ValueError):
            raise VerificationError(
                f"{result.check_id}: an agreement entry is (output, "
                f"Quantity), not {entry!r}") from None
        if not isinstance(name, str) or not name.strip():
            raise VerificationError(f"{result.check_id}: an agreement entry "
                                    "names no output")
        if not isinstance(q, Quantity) or q.resolution is None:
            raise VerificationError(
                f"{result.check_id}: the agreement on {name!r} carries no "
                "stated resolution; a check value with no error bounds "
                "nothing")
        out.append((name, q))
    names = [n for n, _ in out]
    if len(set(names)) != len(names):
        raise VerificationError(f"{result.check_id}: an output is named "
                                "twice in the agreement")
    if out and result.status not in (Status.PASS, Status.FAIL):
        raise VerificationError(
            f"{result.check_id}: a {result.status.value} compared nothing, "
            "so it states no agreement")
    return tuple(sorted(out, key=lambda e: e[0]))


@dataclass(frozen=True)
class VerificationResult:
    check_id: str
    check_type: CheckType
    subject_digest: str
    subject_model: str
    status: Status
    measured: Quantity | None
    criterion: str
    threshold: Quantity | None
    criterion_derivation: str
    verifier_id: str
    verifier_implementation_digest: str
    producer_implementation_digest: str
    independence: Independence
    #: what a PASS of this check establishes; a FAIL, NOT_RUN or
    #: INCONCLUSIVE establishes nothing (``passed`` is False).
    establishes: Establishes
    evidence: tuple = ()
    shared_components: tuple = ()
    limitations: tuple = ()
    #: ((observation_id, kind), ...) of measurements compared against.
    observations: tuple = ()
    #: ((output_name, Quantity), ...): for each output the check compared,
    #: the check's own value and the error it states for that value
    #: (AUD-025). A PASS establishes agreement within the sum of both
    #: sides' errors; this is what lets a reader bound the producer's value
    #: by what the check supports, |v_producer - v_check| + r_check, rather
    #: than by the producer's own statement alone.
    agreement: tuple = ()

    def __post_init__(self):
        for f, enum in (("check_type", CheckType), ("status", Status),
                        ("independence", Independence),
                        ("establishes", Establishes)):
            object.__setattr__(self, f, enum(getattr(self, f)))
        try:
            for f in ("subject_digest", "verifier_implementation_digest",
                      "producer_implementation_digest"):
                require_digest(f, getattr(self, f))
            for e in self.evidence:
                require_digest("evidence", e)
        except IdentityError as exc:
            raise VerificationError(str(exc)) from exc
        if not self.verifier_id.strip():
            raise VerificationError("say who ran the check")
        obs = tuple((str(i), ObservationKind(k)) for i, k in self.observations)
        object.__setattr__(self, "observations", obs)
        object.__setattr__(self, "agreement", _agreement(self))

        if self.status in (Status.PASS, Status.FAIL):
            if not isinstance(self.measured, Quantity):
                raise VerificationError(
                    f"{self.check_id}: a {self.status.value} with nothing "
                    "measured; that is NOT_RUN or INCONCLUSIVE")
            if not self.criterion.strip() or \
                    not self.criterion_derivation.strip():
                raise VerificationError(
                    f"{self.check_id}: a verdict needs its criterion and "
                    "the criterion's source")
            if self.threshold is not None and \
                    self.threshold.unit != self.measured.unit:
                raise VerificationError(
                    f"{self.check_id}: threshold in {self.threshold.unit}, "
                    f"measurement in {self.measured.unit}")

        independent = self.independence is not Independence.NONE
        if self.check_type is CheckType.INDEPENDENT_IMPLEMENTATION \
                and not independent:
            raise VerificationError(
                f"{self.check_id}: an independent-implementation check must "
                "say what kind of independence it has")
        if independent:
            if self.verifier_implementation_digest == \
                    self.producer_implementation_digest:
                raise VerificationError(
                    f"{self.check_id}: claims {self.independence.value} but "
                    "runs the producer's own code (same implementation "
                    "digest); a second call into the first implementation "
                    "is not independent")
            if self.shared_components and not self.limitations:
                raise VerificationError(
                    f"{self.check_id}: shares {list(self.shared_components)}"
                    " with the producer and states no limitation")

        if self.establishes is Establishes.EXPERIMENTAL_VALIDATION:
            if not any(k in MEASURED for _, k in obs):
                raise VerificationError(
                    f"{self.check_id}: EXPERIMENTAL_VALIDATION with no "
                    "measured observation; numerically converged is not "
                    "experimentally validated")
            if self.check_type is not CheckType.COMPARISON_WITH_MEASUREMENT:
                raise VerificationError(
                    f"{self.check_id}: only a comparison with measurement "
                    "can establish experimental validation")
        elif self.establishes is \
                Establishes.INDEPENDENT_NUMERICAL_AGREEMENT \
                and not independent:
            raise VerificationError(
                f"{self.check_id}: independent agreement claimed by a check "
                "that is not independent")

    @property
    def passed(self) -> bool:
        return self.status is Status.PASS

    def to_record(self) -> dict:
        return {
            "check_id": self.check_id,
            "check_type": self.check_type.value,
            "subject_digest": self.subject_digest,
            "subject_model": self.subject_model,
            "status": self.status.value,
            "measured": self.measured.to_record() if self.measured else None,
            "criterion": self.criterion,
            "threshold": (self.threshold.to_record()
                          if self.threshold else None),
            "criterion_derivation": self.criterion_derivation,
            "verifier_id": self.verifier_id,
            "verifier_implementation_digest":
                self.verifier_implementation_digest,
            "producer_implementation_digest":
                self.producer_implementation_digest,
            "independence": self.independence.value,
            "establishes": self.establishes.value,
            "evidence": list(self.evidence),
            "shared_components": list(self.shared_components),
            "limitations": list(self.limitations),
            "observations": [[i, k.value] for i, k in self.observations],
            "agreement": [[name, q.to_record()]
                          for name, q in self.agreement],
        }

    def digest(self) -> str:
        return digest(self.to_record())

    @classmethod
    def from_record(cls, rec: dict) -> "VerificationResult":
        keys = {"check_id", "check_type", "subject_digest", "subject_model",
                "status", "measured", "criterion", "threshold",
                "criterion_derivation", "verifier_id",
                "verifier_implementation_digest",
                "producer_implementation_digest", "independence",
                "establishes", "evidence", "shared_components",
                "limitations", "observations"}
        # A report written before AUD-025 has no agreement; it is read as
        # stating none, which the admission policy it was decided under did
        # not ask for.
        if not isinstance(rec, dict) or set(rec) not in (
                keys, keys | {"agreement"}):
            raise VerificationError("verification record keys differ")
        m, t = rec["measured"], rec["threshold"]
        agreement = rec.get("agreement", [])
        if not isinstance(agreement, list) or not all(
                isinstance(a, list) and len(a) == 2 for a in agreement):
            raise VerificationError("agreement must be [[output, quantity], "
                                    "...]")
        return cls(**{**rec,
                      "measured": Quantity.from_record(m) if m else None,
                      "threshold": Quantity.from_record(t) if t else None,
                      "evidence": tuple(rec["evidence"]),
                      "shared_components": tuple(rec["shared_components"]),
                      "limitations": tuple(rec["limitations"]),
                      "observations": tuple(tuple(o)
                                            for o in rec["observations"]),
                      "agreement": tuple(
                          (name, Quantity.from_record(q))
                          for name, q in agreement)})
