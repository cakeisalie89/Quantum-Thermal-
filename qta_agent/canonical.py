"""Canonical serialization and digests for authority-bearing records.

Every integrity claim in this package reduces to one question: *are these the
same bytes?* That question is only answerable if a record has exactly one
byte representation, so serialization is defined here once and used
everywhere. Two records that are semantically equal must hash equally, and
two that differ must not.

Design rules, each chosen because the alternative silently breaks integrity:

``sort_keys=True``
    Dict iteration order is insertion order in Python. A record rebuilt from
    a different code path would otherwise hash differently despite being
    identical.

``ensure_ascii=True``
    Pins the encoding of non-ASCII text so a digest cannot depend on the
    writer's locale or terminal.

``separators`` without spaces
    Removes the only remaining formatting freedom in JSON.

``allow_nan=False``
    ``NaN`` and ``Infinity`` are not JSON. Python emits them anyway, and they
    do not round-trip through conformant parsers. A record carrying one is
    rejected rather than silently written in a form another reader cannot
    read back -- and NaN != NaN would break equality comparison regardless.

Floats are permitted but are a known hazard: ``repr`` round-trips in CPython,
yet a value computed on another platform may differ in its last bits and
therefore in its digest. Authority-bearing numeric fields should be carried as
strings or integers; :func:`assert_digest_stable` exists to catch violations
in tests rather than in production.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

#: Digest of the empty chain predecessor. The genesis record's ``prev_hash``.
#: Not a real digest of anything -- a sentinel that cannot collide with one,
#: since SHA-256 of any input is overwhelmingly unlikely to be all zeros.
ZERO_DIGEST = "0" * 64

#: Serialization contract version. Any change to the rules above changes every
#: digest in the system, so it is versioned and recorded alongside them.
CANONICAL_FORM_VERSION = 1


class CanonicalizationError(ValueError):
    """A value cannot be represented canonically, so it cannot be hashed."""


def canonical_bytes(obj: Any) -> bytes:
    """Return the single canonical byte representation of ``obj``.

    Raises :class:`CanonicalizationError` rather than emitting bytes that
    would hash inconsistently or fail to round-trip.
    """
    try:
        text = json.dumps(
            obj,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except ValueError as exc:
        # allow_nan=False raises ValueError for NaN/Inf.
        raise CanonicalizationError(
            f"value is not canonically serializable: {exc}") from exc
    except TypeError as exc:
        raise CanonicalizationError(
            f"value contains a non-JSON type: {exc}") from exc
    return text.encode("utf-8")


def digest(obj: Any) -> str:
    """SHA-256 of the canonical form, as 64 lowercase hex characters."""
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def digest_bytes(raw: bytes) -> str:
    """SHA-256 of raw bytes. For files and payloads already serialized."""
    return hashlib.sha256(raw).hexdigest()


def is_digest(value: object) -> bool:
    """True if ``value`` is syntactically a lowercase SHA-256 hex digest.

    Uppercase is deliberately rejected: accepting both would let one logical
    digest have two spellings, and a set of digests would then contain
    duplicates that compare unequal.
    """
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(c in "0123456789abcdef" for c in value)


def assert_digest_stable(obj: Any, *, rounds: int = 3) -> str:
    """Hash ``obj`` repeatedly and require agreement.

    Cheap insurance against a record whose digest depends on iteration order
    or on a mutable default. Used in tests and at record-construction
    boundaries, not in hot paths.
    """
    first = digest(obj)
    for _ in range(rounds - 1):
        again = digest(obj)
        if again != first:
            raise CanonicalizationError(
                f"digest is not stable across repeated serialization: "
                f"{first} then {again}")
    return first
