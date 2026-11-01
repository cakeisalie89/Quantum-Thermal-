#!/usr/bin/env python3
"""Verify a scientific assurance package from the package alone.

    python3 -I verify_assurance.py PACKAGE [--expect-package-digest HEX]
                                           [--expect-head SEQ:HASH]

PORTABLE BY CONSTRUCTION. This file imports the Python standard library and
nothing else. Copy it by itself to a machine with no checkout of the
repository and no network, and run it there. Obtain it from somewhere you
trust, never from the package: a package that brought its own verifier
would be checking itself, so this program refuses to run from inside the
package it is asked about.

WHAT IT TRUSTS: its own code and the Python standard library. Nothing the
package says is taken as true until a check below has held it to something
else in the package, or to a computation of the verifier's own. The
producing agent, its explanations, its CI logs, its workspace and its
cached projections are not consulted: none of them is in the package.

WHAT IT ESTABLISHES, IN ORDER. Five levels, each needing the one before it
and reported separately; a level after a failed one is NOT_REACHED, never
passed.

  INTEGRITY     every file is exactly the one SHA256SUMS lists, nothing is
                missing or added, nothing is a link, and each evidence file
                is named by its own digest;
  CONSISTENCY   the record and the evidence say the same things: the model,
                its inputs and their units, its outputs, the check's subject,
                the error budget's point and value. The record states the
                problem this verifier can recheck, claims no kind of
                establishment the package cannot carry, and names every gap
                this verifier knows of;
  HISTORY       the event log is a hash chain from its genesis, each record
                in the canonical encoding, ending at the head the record
                states;
  DECISION      what the record reports is in that history as reported: the
                question the owner registered, the hypothesis, the result
                proposed by one party and verified by another citing exactly
                this package's evidence and not undone since, the hypothesis
                decided on an observation of this result, and the producer's
                decision proofs about these same records;
  RECHECK       the verifier's own computation. It solves the stated
                equations in closed form (an eigenfunction series, its
                coefficients integrated exactly, its eigenvalues found by
                bracketed Newton iteration, all written here) and holds the
                package to it: every temperature the model reported, the
                model's whole final profile, the budget's measured
                discretization, the parametric spread (the same declared
                draws, propagated through the exact solution instead of the
                model), the budget's arithmetic, and the hypothesis's
                verdict.

WHAT IT CANNOT ESTABLISH is listed in NOT_ESTABLISHED and reported on every
run. Two of the items an operator can supply: a package digest and a history
head, each obtained from a channel independent of the package
(--expect-package-digest, --expect-head). Without them, a package anyone
assembled consistently passes every level: consistency is not origin.

EXIT STATUS
  0  every level holds, and every expectation supplied matched
  1  REFUSED: a check failed, or an expectation did not match
  2  UNREADABLE: there is no package at that path
  3  MISUSE: the verifier is inside the package, or an argument is malformed
  anything else, including 4 for an error inside the verifier, is not a pass
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import stat
import struct
import sys
from pathlib import Path
from typing import Any

VERSION = 1
RECORD_SCHEMA = "scientific-assurance-record/1"
REPORT_SCHEMA = "assurance-verification/1"
#: the one problem this verifier can recheck, by name; a package about any
#: other is refused rather than rechecked against the wrong equations
PROBLEM = "slab-1d-insulated-convective/1"

SUMS = "SHA256SUMS"
RECORD = "assurance_record.json"
LOG = "history/log.jsonl"
README = "README.md"
EVIDENCE = "evidence/"
MAX_FILES = 256
MAX_DEPTH = 4
MAX_FILE_BYTES = 8 << 20
MAX_TOTAL_BYTES = 32 << 20
MAX_LINE_BYTES = 1 << 20
_PATH = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")
_HEX = re.compile(r"[0-9a-f]{64}")
_SUM_LINE = re.compile(r"([0-9a-f]{64})  (\S+)")
_EPS = sys.float_info.epsilon

RECORD_KEYS = frozenset({
    "schema", "package_id", "verifier", "question", "hypothesis",
    "mathematical_statement", "assumptions", "model", "inputs", "toolchain",
    "results", "error_budget", "independent_checks", "formal", "evidence",
    "authorization", "history", "establishment", "authority", "trust_roots",
    "not_established", "reproduction"})
EVIDENCE_ROLES = frozenset({
    "result_bundle", "run_identity", "temperature_field",
    "verification_report", "error_budget", "decision_proof_verified",
    "decision_proof_supported"})
#: the units the closed-form solution below is written in; a record that
#: states another unit for an input is not asking about these equations
UNITS = {"L_m": "m", "T0_K": "K", "T_inf_K": "K", "h_W_m2_K": "W m^-2 K^-1",
         "k_W_m_K": "W m^-1 K^-1", "q_W_m3": "W m^-3",
         "rho_c_J_m3_K": "J m^-3 K^-1", "t_end_s": "s", "n_cells": "1",
         "n_steps": "1"}
#: where the model reports a temperature, as a fraction of L
SAMPLES = {"T_at_0": 0.0, "T_at_quarter": 0.25, "T_at_half": 0.5,
           "T_at_three_quarter": 0.75, "T_at_L": 1.0}
#: Roache's factor of safety on a two-level discretization estimate: the
#: criterion the producer's own analytic check applies, restated here
GCI_SAFETY = 3.0
#: what a package may say of the kinds of establishment it cannot carry
UNCLAIMED = ("NOT_RUN", "NOT_ESTABLISHED")
CARRIED = ("NUMERICALLY_VERIFIED",)
NOT_CARRIED = ("FORMALLY_PROVED", "INDEPENDENTLY_REPRODUCED",
               "EXPERIMENTALLY_VALIDATED", "MODEL_ASSESSED")
#: decisions a record may report as the result's standing state
STANDING = ("VERIFIED", "PROMOTED")
_EVENT_FIELDS = ("seq", "event_id", "wall_time", "actor", "action", "target",
                 "payload", "prev_hash", "canonical_form_version")

#: What this verifier cannot establish from a package, whatever the package
#: says. A record must name every one of them: a record that leaves one out
#: claims more than any offline check can give it.
NOT_ESTABLISHED = (
    ("PACKAGE_ORIGIN",
     "that this is the package that was published: a package anyone "
     "assembled consistently passes every level. Supply "
     "--expect-package-digest, obtained independently of the package."),
    ("HISTORY_ANCHORED",
     "that the history is the producer's whole history and not a consistent "
     "rewrite: the chain binds its records to one another, not to anything "
     "outside. Supply --expect-head from an independent witness."),
    ("PRODUCER_EXECUTION",
     "that the producer's processes did what the log records: the log is "
     "the producer's statement, chained and unsigned."),
    ("MODEL_IMPLEMENTATION",
     "that the code at the stated implementation digest computed the "
     "bundle: re-running it needs the producer's source."),
    ("ROLE_ENTITLEMENT",
     "that the deciding parties were entitled to decide: the producing run "
     "carried no role registry, and none is provisioned for production."),
    ("FORMAL_PROOF", "any machine-checked proof: the package carries none."),
    ("PHYSICAL_VALIDITY",
     "that the equations describe any real slab: no temperature of one was "
     "observed."),
    ("INPUT_DISTRIBUTIONS",
     "that the declared input spreads are right: they are declared, not "
     "measured."),
    ("PRODUCER_TOOLCHAIN",
     "the producer's interpreter, libraries and machine: the bundle states "
     "them, and nothing here can see them."),
    ("WALL_TIMES",
     "when anything happened: the times in the log are the producer's "
     "clock."),
    ("HISTORY_SEMANTICS",
     "what the history's other records mean: the verifier reads the "
     "objective, the hypothesis, the result's creation and transitions, the "
     "captured evidence, the observation and the hypothesis's statuses. "
     "Every other record is chained and not interpreted."),
)
LEVELS = ("INTEGRITY", "CONSISTENCY", "HISTORY", "DECISION", "RECHECK")


class Misuse(Exception):
    """The verifier was asked to do something it must not."""


class Unreadable(Exception):
    """There is no package to read."""


class Refused(Exception):
    """A check that could not even be evaluated: (check, detail)."""


# ------------------------------------------------------------ encodings

def canonical(obj) -> bytes:
    """The one byte form of a record: sorted keys, ASCII, no whitespace, no
    NaN. Restated from the producer's contract, not imported."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _no_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"the key {k!r} is given twice")
        out[k] = v
    return out


def _no_constant(name):
    raise ValueError(f"{name} is not JSON")


def _finite(text: str) -> float:
    """A JSON number as a float, refused if it does not fit one: 1e400 is
    valid JSON and parses to infinity without passing through
    ``parse_constant``."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is not a finite number")
    return value


def strict_json(raw: bytes, where: str):
    """JSON as one reading of it: UTF-8, no key given twice (whose first and
    last values different readers would show), no NaN, Infinity or number
    too large for a float."""
    try:
        return json.loads(raw.decode("utf-8"),
                          object_pairs_hook=_no_duplicates,
                          parse_constant=_no_constant, parse_float=_finite)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise Refused(f"{where} is one reading of JSON",
                      f"{type(exc).__name__}: {exc}") from exc


# ------------------------------------------------------------ the files

def _walk(root: Path) -> dict:
    """relative path -> size, for every regular file under ``root``; a link,
    a device or anything else that is not a file or a directory is refused,
    and so is a tree past the bounds."""
    out: dict = {}
    total = 0

    def visit(d: Path, rel: str, depth: int):
        nonlocal total
        if depth > MAX_DEPTH:
            raise Refused("the package is no deeper than the bound",
                          f"{rel or '.'} is {depth} levels down")
        with os.scandir(d) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            r = f"{rel}{e.name}"
            st = e.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                visit(Path(e.path), r + "/", depth + 1)
            elif stat.S_ISREG(st.st_mode):
                out[r] = st.st_size
                total += st.st_size
                if len(out) > MAX_FILES or total > MAX_TOTAL_BYTES:
                    raise Refused("the package is within its bounds",
                                  f"more than {MAX_FILES} files or "
                                  f"{MAX_TOTAL_BYTES} bytes")
            else:
                raise Refused("every entry is a file or a directory",
                              f"{r} is a link or a special file")
    visit(root, "", 0)
    return out


def _read(root: Path, rel: str) -> bytes:
    """A file's bytes, opened without following a link, bounded."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(root / rel, flags)
    except OSError as exc:
        raise Refused(f"{rel} can be read", f"{type(exc).__name__}: {exc}") \
            from exc
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
            raise Refused(f"{rel} is a file within the bound",
                          f"mode {st.st_mode:o}, {st.st_size} bytes")
        data = fh.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise Refused(f"{rel} is a file within the bound", "it grew")
    return data


def _parse_sums(raw: bytes) -> dict:
    """SHA256SUMS: one ``<digest>  <path>`` line per file, sorted, each path
    once, every path inside the package, the file ending in a newline."""
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise Refused("SHA256SUMS is ASCII", str(exc)) from exc
    if not text.endswith("\n"):
        raise Refused("SHA256SUMS ends with a newline", "it does not")
    out: dict = {}
    last = None
    for n, line in enumerate(text[:-1].split("\n"), 1):
        m = _SUM_LINE.fullmatch(line)
        if m is None:
            raise Refused("every SHA256SUMS line is <sha256>  <path>",
                          f"line {n}: {line[:120]!r}")
        digest_, path = m.groups()
        segs = path.split("/")
        if not _PATH.fullmatch(path) or any(s in (".", "..") for s in segs):
            raise Refused("every listed path is inside the package",
                          f"line {n}: {path!r}")
        if path == SUMS:
            raise Refused("SHA256SUMS does not list itself", f"line {n}")
        if last is not None and path <= last:
            raise Refused("SHA256SUMS lists each path once, in order",
                          f"line {n}: {path!r} after {last!r}")
        out[path], last = digest_, path
    return out


# ------------------------------------------------------- the exact solution

def _eigenvalue(n: int, bi: float) -> float:
    """The n-th root z of z sin z = Bi cos z, which lies in
    ((n - 1) pi, (n - 1/2) pi): Newton's method kept inside a shrinking
    bracket, falling back to bisection whenever a step would leave it."""
    a, b = (n - 1) * math.pi, (n - 0.5) * math.pi

    def f(z):
        return z * math.sin(z) - bi * math.cos(z)

    fa = f(a)
    z = 0.5 * (a + b)
    for _ in range(200):
        fz = f(z)
        if fz == 0.0:
            return z
        if (fz < 0.0) == (fa < 0.0):
            a, fa = z, fz
        else:
            b = z
        d = (1.0 + bi) * math.sin(z) + z * math.cos(z)
        nxt = z - fz / d if d != 0.0 else 0.5 * (a + b)
        if not a < nxt < b:
            nxt = 0.5 * (a + b)
        if abs(nxt - z) <= 4.0 * _EPS * abs(nxt):
            return nxt
        z = nxt
    return z


def exact_temperature(p: dict, frac: float, *, max_terms: int = 400) -> float:
    """T(frac L, t_end) for rho c T_t = k T_xx + q on 0 < x < L, insulated
    at x = 0, convective to T_inf at x = L, uniform T0 at t = 0.

    T = T_s + sum_n c_n exp(-k t z_n^2 / (rho c L^2)) cos(z_n x / L), with
    T_s = T_inf + q L / h + q (L^2 - x^2) / (2 k), z_n tan z_n = h L / k,
    and c_n the projection of T0 - T_s = A + B x^2 on cos(z_n x / L),
    integrated in closed form. Terms are summed until the next decay factor
    underflows."""
    L, k, rc = p["L_m"], p["k_W_m_K"], p["rho_c_J_m3_K"]
    q, h, tinf = p["q_W_m3"], p["h_W_m2_K"], p["T_inf_K"]
    t0, t = p["T0_K"], p["t_end_s"]
    if not (L > 0 and k > 0 and rc > 0 and h > 0 and t > 0 and q >= 0):
        raise Refused("the inputs are inside the equations' domain",
                      f"L, k, rho c, h, t > 0 and q >= 0: {p}")
    x = frac * L
    bi = h * L / k
    fo = k * t / (rc * L * L)
    a = t0 - tinf - q * L / h - q * L * L / (2.0 * k)
    b = q / (2.0 * k)
    total = tinf + q * L / h + q * (L * L - x * x) / (2.0 * k)
    for n in range(1, max_terms + 1):
        z = _eigenvalue(n, bi)
        decay = math.exp(-fo * z * z)
        if decay == 0.0:
            return total
        lam = z / L
        s, c = math.sin(z), math.cos(z)
        i0 = s / lam
        i2 = L * L * s / lam + 2.0 * L * c / lam ** 2 - 2.0 * s / lam ** 3
        norm = 0.5 * L + math.sin(2.0 * z) / (4.0 * lam)
        total += (a * i0 + b * i2) / norm * decay * math.cos(lam * x)
    raise Refused("the series converges at these inputs",
                  f"{max_terms} terms did not reach underflow: outside what "
                  "this verifier rechecks")


def _admissible(p: dict) -> bool:
    """The physical domain the declared draws must fall in."""
    return all(p[k] > 0 for k in ("k_W_m_K", "rho_c_J_m3_K", "h_W_m2_K",
                                  "L_m", "T_inf_K", "T0_K")) \
        and p["q_W_m3"] >= 0


def _propagate(base: dict, spreads: dict, *, rel_step: Any, samples: Any,
               seed: Any) -> dict:
    """The declared spreads through the exact solution: central differences
    at ``base``, and the same seeded draws the producer declares, in the
    same order (each sample's parameters in sorted order)."""
    if not (isinstance(samples, int) and not isinstance(samples, bool)
            and 30 <= samples <= 20000 and isinstance(seed, int)
            and isinstance(rel_step, float) and 0.0 < rel_step <= 0.1
            and set(spreads) <= set(UNITS) - {"n_cells", "n_steps"}
            and all(isinstance(s, (int, float)) and math.isfinite(s)
                    and s > 0 for s in spreads.values())):
        raise Refused("the propagation is one this verifier can repeat",
                      f"{samples!r} samples, seed {seed!r}, step "
                      f"{rel_step!r}, spreads {spreads}")
    var = 0.0
    for name in sorted(spreads):
        p0 = float(base[name])
        step = rel_step * (abs(p0) if p0 != 0 else 1.0)
        up = exact_temperature({**base, name: p0 + step}, 0.5)
        dn = exact_temperature({**base, name: p0 - step}, 0.5)
        var += ((up - dn) / (2.0 * step) * spreads[name]) ** 2
    rng = random.Random(seed)
    ys: list = []
    redrawn = 0
    while len(ys) < samples:
        p = dict(base)
        for name in sorted(spreads):
            p[name] = rng.gauss(float(base[name]), spreads[name])
        if not _admissible(p):
            redrawn += 1
            if redrawn > 10 * samples:
                raise Refused("the declared draws fall in the domain",
                              "almost every draw is outside it")
            continue
        ys.append(exact_temperature(p, 0.5))
    ys.sort()
    n = len(ys)
    mean = sum(ys) / n
    std = math.sqrt(sum((y - mean) ** 2 for y in ys) / (n - 1))

    def quantile(f):
        i = f * (n - 1)
        lo = int(math.floor(i))
        hi = min(lo + 1, n - 1)
        return ys[lo] + (ys[hi] - ys[lo]) * (i - lo)

    return {"std_linear": math.sqrt(var), "mean": mean, "std": std,
            "p2_5": quantile(0.025), "p97_5": quantile(0.975),
            "redrawn": redrawn}


def _field(raw: bytes) -> tuple:
    """(x, T) from the model's field artifact: ``QTSL1``, a JSON header
    line, then x [m] and T [K] at the cell centres as little-endian
    float64."""
    if not raw.startswith(b"QTSL1\n"):
        raise Refused("the field artifact is in its stated format",
                      "no QTSL1 magic")
    end = raw.index(b"\n", 6)
    head = strict_json(raw[6:end], "the field header")
    n = head.get("n")
    if head.get("dtype") != "<f8" or head.get("columns") != ["x", "T"] or \
            head.get("units") != {"T": "K", "x": "m"} or \
            not isinstance(n, int) or n < 2 or len(raw) - end - 1 != 16 * n:
        raise Refused("the field artifact is in its stated format",
                      f"header {head}, {len(raw) - end - 1} bytes of data")
    body = raw[end + 1:]
    return (struct.unpack(f"<{n}d", body[:8 * n]),
            struct.unpack(f"<{n}d", body[8 * n:]))


# ------------------------------------------------ authentication, restated
#
# WHO WROTE EACH RECORD, checked from the RFC and the documents alone. The
# producer authenticates its history with agent_substrate.principals and
# agent_substrate.ed25519; nothing here imports either, and the separate
# process (tools/independent_verify.py, which may not import them) reads a
# governed history with this code instead. Each rule below restates the
# producer's, finding kind for finding kind, so the two can be held to the
# same histories and made to disagree when either changes.

#: RFC 8032 section 5.1: the field prime, the group order, the curve
#: constant d, and a square root of -1.
_ED_P = 2 ** 255 - 19
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493
_ED_D = -121665 * pow(121666, _ED_P - 2, _ED_P) % _ED_P
_ED_I = pow(2, (_ED_P - 1) // 4, _ED_P)
#: what an attestation signs: the domain, then the event's hash in ASCII
ATTESTATION_DOMAIN = b"qta-agent/event-attestation/v1\x00"
KEY_SCHEMA_V1 = "qta-agent/key-registry/v1"
KEY_SCHEMA_V2 = "qta-agent/key-registry/v2"
_KEY_FIELDS_V1 = frozenset({"key_id", "principal", "scheme", "public"})
_KEY_FIELDS_V2 = _KEY_FIELDS_V1 | {
    "status", "valid_from_seq", "valid_until_seq", "revoked_at_seq",
    "revocation_reason", "compromised_from_seq", "replacement_key"}
_MAX_KEYS = 4096
_MAX_NAME = 256
_LOWER = re.compile(r"[0-9a-f]*")


def _ed_x(y: int, sign: int):
    """The x of the point with ``y``, or None: RFC 8032 5.1.3."""
    if y >= _ED_P:
        return None
    u, v = (y * y - 1) % _ED_P, (_ED_D * y * y + 1) % _ED_P
    x2 = u * pow(v, _ED_P - 2, _ED_P) % _ED_P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_ED_P + 3) // 8, _ED_P)
    if (x * x - x2) % _ED_P:
        x = x * _ED_I % _ED_P
    if (x * x - x2) % _ED_P:
        return None
    return _ED_P - x if (x & 1) != sign else x


def _ed_point(raw: bytes):
    """Extended coordinates of an encoded point, or None."""
    if len(raw) != 32:
        return None
    n = int.from_bytes(raw, "little")
    y, sign = n & ((1 << 255) - 1), n >> 255
    x = _ed_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _ED_P)


def _ed_add(a, b):
    """RFC 8032 5.1.4, the unified addition in extended coordinates."""
    (x1, y1, z1, t1), (x2, y2, z2, t2) = a, b
    aa = (y1 - x1) * (y2 - x2) % _ED_P
    bb = (y1 + x1) * (y2 + x2) % _ED_P
    cc = 2 * _ED_D * t1 * t2 % _ED_P
    dd = 2 * z1 * z2 % _ED_P
    e, f, g, h = bb - aa, dd - cc, dd + cc, bb + aa
    return (e * f % _ED_P, g * h % _ED_P, f * g % _ED_P, e * h % _ED_P)


def _ed_times(k: int, pt):
    out = (0, 1, 1, 0)
    while k:
        if k & 1:
            out = _ed_add(out, pt)
        pt, k = _ed_add(pt, pt), k >> 1
    return out


#: the base point B: y = 4/5, x even
_ED_BY = 4 * pow(5, _ED_P - 2, _ED_P) % _ED_P
_ED_BX = _ed_x(_ED_BY, 0)
_ED_B = (_ED_BX, _ED_BY, 1, _ED_BX * _ED_BY % _ED_P)


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 5.1.7, with the equation [S]B = R + [k]A that the RFC
    allows in place of the cofactored one; the producer checks the same
    equation, so the two cannot disagree on a small-order component. Bytes
    an adversary wrote: anything malformed is False, never an exception."""
    if not (isinstance(public, (bytes, bytearray)) and len(public) == 32
            and isinstance(signature, (bytes, bytearray))
            and len(signature) == 64):
        return False
    a, r = _ed_point(bytes(public)), _ed_point(bytes(signature[:32]))
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= _ED_L:
        return False
    k = int.from_bytes(hashlib.sha512(
        bytes(signature[:32]) + bytes(public) + message).digest(),
        "little") % _ED_L
    lhs, rhs = _ed_times(s, _ED_B), _ed_add(r, _ed_times(k, a))
    return ((lhs[0] * rhs[2] - rhs[0] * lhs[2]) % _ED_P == 0
            and (lhs[1] * rhs[2] - rhs[1] * lhs[2]) % _ED_P == 0)


def _hex_of(value, length: int) -> bool:
    """Exactly ``length`` lowercase hex digits: the one encoding a signer
    writes, so one signature has one record."""
    return (isinstance(value, str) and len(value) == length
            and _LOWER.fullmatch(value) is not None)


def _seq_or_none(value, what: str):
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise Refused("every key's lifecycle is in log positions",
                      f"{what} is {value!r}")
    return value


def _key_name(value, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_NAME:
        raise Refused("every key names itself and its principal",
                      f"{what} is {value!r}")
    return value


def derived_key_id(public: bytes, *, test: bool) -> str:
    """A key's id is derived from the key, never chosen."""
    kid = f"ed25519:{hashlib.sha256(public).hexdigest()[:32]}"
    return "test:" + kid if test else kid


def read_key_registry(doc, *, allow_test_keys: bool) -> dict:
    """``key_id -> key``, from a key-registry document (v1 or v2). Every
    rule the producer's registry keeps is restated; a document either
    reading would refuse is refused."""
    if not isinstance(doc, dict) or doc.get("schema") not in (
            KEY_SCHEMA_V1, KEY_SCHEMA_V2):
        shown = doc.get("schema") if isinstance(doc, dict) else doc
        raise Refused("the key registry is a key-registry document",
                      f"schema {shown!r}")
    v2 = doc["schema"] == KEY_SCHEMA_V2
    if v2 and (set(doc) != {"schema", "registry_version", "keys"}
               or not isinstance(doc.get("registry_version"), int)
               or isinstance(doc.get("registry_version"), bool)):
        raise Refused("the key registry's header is readable", sorted(doc))
    entries = doc.get("keys")
    if not isinstance(entries, list) or len(entries) > _MAX_KEYS:
        raise Refused("the key registry lists its keys", type(entries))
    keys: dict = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != (
                _KEY_FIELDS_V2 if v2 else _KEY_FIELDS_V1):
            raise Refused("every key entry has exactly its fields",
                          sorted(entry) if isinstance(entry, dict)
                          else entry)
        kid = _key_name(entry["key_id"], "key_id")
        if entry["scheme"] != "ed25519" or not _hex_of(entry["public"], 64):
            raise Refused("every key is an Ed25519 public key in lowercase "
                          "hex", kid)
        public = bytes.fromhex(entry["public"])
        test = kid.startswith("test:")
        if kid != derived_key_id(public, test=test):
            raise Refused("every key id is derived from its key", kid)
        if test and not allow_test_keys:
            raise Refused("no TEST key is read where test keys are not "
                          "admitted", kid)
        if kid in keys:
            raise Refused("no key is registered twice", kid)
        key: dict[str, Any] = {
               "key_id": kid,
               "principal": _key_name(entry["principal"], "principal"),
               "public": public, "valid_from_seq": 0,
               "valid_until_seq": None, "revoked_at_seq": None,
               "revocation_reason": None, "compromised_from_seq": None,
               "replacement_key": None}
        if v2:
            key.update(
                valid_from_seq=_seq_or_none(entry["valid_from_seq"],
                                            "valid_from_seq") or 0,
                valid_until_seq=_seq_or_none(entry["valid_until_seq"],
                                             "valid_until_seq"),
                revoked_at_seq=_seq_or_none(entry["revoked_at_seq"],
                                            "revoked_at_seq"),
                revocation_reason=None if entry["revocation_reason"] is None
                else _key_name(entry["revocation_reason"],
                               "revocation_reason"),
                compromised_from_seq=_seq_or_none(
                    entry["compromised_from_seq"], "compromised_from_seq"),
                replacement_key=None if entry["replacement_key"] is None
                else _key_name(entry["replacement_key"], "replacement_key"))
            status = ("REVOKED" if key["revoked_at_seq"] is not None else
                      "ROTATED" if key["valid_until_seq"] is not None else
                      "ACTIVE")
            if entry["status"] != status:
                raise Refused("every key's status is what its lifecycle "
                              "says", f"{kid}: {entry['status']!r}, the "
                              f"lifecycle says {status}")
        if (key["valid_until_seq"] is not None
                and key["valid_until_seq"] < key["valid_from_seq"]):
            raise Refused("no key expires before it begins", kid)
        if (key["revoked_at_seq"] is None) != (
                key["revocation_reason"] is None):
            raise Refused("a revocation names both where and why", kid)
        keys[kid] = key
    for key in keys.values():
        nxt = keys.get(key["replacement_key"]) \
            if key["replacement_key"] is not None else None
        if key["replacement_key"] is not None and (
                nxt is None or nxt["principal"] != key["principal"]):
            raise Refused("a key's replacement is a key of the same "
                          "principal", key["key_id"])
    return keys


def key_refusal_at(key: dict, seq: int):
    """``(kind, detail)`` when ``key`` may not sign the event at ``seq``:
    the producer's lifecycle rule, in its order."""
    kid = key["key_id"]
    if key["compromised_from_seq"] is not None and \
            seq >= key["compromised_from_seq"]:
        return ("KEY_COMPROMISED", f"{kid!r} is declared compromised from "
                f"seq {key['compromised_from_seq']}")
    if key["revoked_at_seq"] is not None and seq >= key["revoked_at_seq"]:
        return ("KEY_REVOKED", f"{kid!r} was revoked at seq "
                f"{key['revoked_at_seq']} ({key['revocation_reason']})")
    if seq < key["valid_from_seq"]:
        return ("KEY_NOT_YET_VALID",
                f"{kid!r} speaks from seq {key['valid_from_seq']}")
    if key["valid_until_seq"] is not None and seq > key["valid_until_seq"]:
        return ("KEY_EXPIRED", f"{kid!r} spoke until seq "
                f"{key['valid_until_seq']}")
    return None


def read_attestation_file(raw: bytes) -> dict:
    """The attestation file: each line an attestation or an abort record.
    A last line with no newline is a prepare a crash tore, and is skipped,
    as the producer skips it. A line in any other shape is MALFORMED."""
    out: dict = {"by_hash": {}, "malformed": [], "aborted": set(),
                 "torn_tail": False}
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    elif lines:
        out["torn_tail"] = True
        lines.pop()
    for n, line in enumerate(lines, 1):
        try:
            rec = strict_json(line, f"attestation line {n}")
        except Refused:
            out["malformed"].append(n)
            continue
        seq = rec.get("seq") if isinstance(rec, dict) else None
        good_seq = isinstance(seq, int) and not isinstance(seq, bool) \
            and seq >= 0
        if isinstance(rec, dict) and set(rec) == {"abort", "seq"} and \
                good_seq and _hex_of(rec["abort"], 64):
            out["aborted"].add(rec["abort"])
        elif (isinstance(rec, dict)
              and set(rec) == {"seq", "event_hash", "key_id", "sig"}
              and good_seq and _hex_of(rec["event_hash"], 64)
              and isinstance(rec["key_id"], str)
              and 0 < len(rec["key_id"]) <= _MAX_NAME
              and _hex_of(rec["sig"], 128)):
            out["by_hash"].setdefault(rec["event_hash"], []).append(rec)
        else:
            out["malformed"].append(n)
    return out


def _field_of(ev, name: str):
    return ev[name] if isinstance(ev, dict) else getattr(ev, name)


def authenticate_events(events, attested: dict, keys: dict, *,
                        complete: bool = True, require=None) -> dict:
    """Every event whose actor must authenticate (every actor when
    ``require`` is None) carries an attestation by a key the registry holds
    for that actor, valid at the event's position, whose signature verifies
    over the domain and the event's hash. ``findings`` are ``(seq, kind,
    detail)``, seq None for a finding about the file; ``refused`` the
    positions of the events that did not authenticate."""
    events = list(events)
    by_hash, aborted = attested["by_hash"], attested["aborted"]
    findings: list = [(None, "MALFORMED",
                       f"attestation line {n} is not an attestation")
                      for n in attested["malformed"]]
    committed = {_field_of(ev, "hash") for ev in events}
    findings += [(None, "ABORTED_COMMITTED",
                  f"an abort names committed event {h[:16]}...; no writer "
                  "aborts what it committed")
                 for h in sorted(aborted & committed)]
    principals = {k["principal"] for k in keys.values()}
    refused: set = set()
    seen: set = set()
    authenticated = 0
    for ev in events:
        seq, actor, h = (_field_of(ev, "seq"), _field_of(ev, "actor"),
                         _field_of(ev, "hash"))
        if require is not None and actor not in require:
            continue
        seen.add(h)
        atts = by_hash.get(h, [])
        if actor not in principals:
            findings.append((seq, "NO_KEY",
                             f"no key is registered for {actor!r}"))
            refused.add(seq)
            continue
        if not atts:
            findings.append((seq, "MISSING",
                             f"no attestation of this event by {actor!r}"))
            refused.add(seq)
            continue
        why: list = []
        for att in atts:
            key = keys.get(att["key_id"])
            if key is None:
                why.append(("UNKNOWN_KEY", f"{att['key_id']!r} is not a "
                            "registered key"))
            elif key["principal"] != actor:
                why.append(("WRONG_PRINCIPAL", f"signed by a key of "
                            f"{key['principal']!r}, and the event names "
                            f"{actor!r}"))
            elif key_refusal_at(key, seq) is not None:
                why.append(key_refusal_at(key, seq))
            elif att["seq"] != seq or not ed25519_verify(
                    key["public"], ATTESTATION_DOMAIN + h.encode("ascii"),
                    bytes.fromhex(att["sig"])):
                why.append(("BAD_SIGNATURE",
                            f"{key['key_id']!r} did not sign this event"))
            else:
                why = []
                break
        if why:
            findings += [(seq, k, d) for k, d in why]
            refused.add(seq)
        else:
            authenticated += 1
    head = _field_of(events[-1], "seq") if events else -1
    pending = {h for h, atts in by_hash.items()
               if h not in seen and h not in aborted
               and all(a["seq"] == head + 1 for a in atts)}
    if len(pending) > 1:
        pending = set()
    for h in sorted(set(by_hash) - seen - aborted - pending) \
            if complete else ():
        if require is None or any(
                keys.get(a["key_id"]) is not None
                and keys[a["key_id"]]["principal"] in require
                for a in by_hash[h]):
            findings.append((None, "DANGLING",
                             f"an attestation names event hash {h[:16]}..., "
                             "which is in no event of this history"))
    return {"findings": findings, "refused": refused,
            "authenticated": authenticated}


class _Held:
    """A document a reader holds and compares by its digest."""

    def __init__(self, doc):
        self._doc = json.loads(json.dumps(doc))

    def to_document(self):
        return json.loads(json.dumps(self._doc))


class _AuthenticationReport:
    def __init__(self, result: dict):
        self.findings = result["findings"]
        self.refused = result["refused"]
        self.authenticated = result["authenticated"]

    @property
    def ok(self) -> bool:
        return not self.findings

    def lines(self) -> list:
        return [f"seq {s}: {k}: {d}" if s is not None else f"{k}: {d}"
                for s, k, d in self.findings]


class RestatedAuthenticator:
    """What a reader of the event log is given to authenticate a history,
    built from this file's code alone: the attestation file's path and the
    key-registry document. Shaped as the log expects an authenticator to
    be -- called with the events, ``require``, and a ``registry`` whose
    document the log compares with what a GOVERNED history declares."""

    def __init__(self, attestations, registry_doc, *,
                 allow_test_keys: bool = False, require=None):
        self.require = None if require is None else frozenset(require)
        self.attestations_path = Path(attestations)
        self.keys = read_key_registry(registry_doc,
                                      allow_test_keys=allow_test_keys)
        self.registry = _Held(registry_doc)

    def __call__(self, events, *, complete: bool = True):
        try:
            raw = self.attestations_path.read_bytes()
        except FileNotFoundError:
            raw = b""
        return _AuthenticationReport(authenticate_events(
            events, read_attestation_file(raw), self.keys,
            complete=complete, require=self.require))


def held_document(doc):
    """A role registry, or any other document a reader is given to compare
    by digest and read as a document: nothing is imported to hold it."""
    return _Held(doc)


# ------------------------------------------------------------ the levels

def _level_integrity(ctx, check):
    root = ctx["root"]
    present = _walk(root)
    if SUMS not in present:
        raise Refused("the package has a SHA256SUMS", "it has none")
    sums = _read(root, SUMS)
    listed = _parse_sums(sums)
    ctx["package_digest"] = sha(sums)
    missing = sorted(set(listed) - set(present))
    extra = sorted(set(present) - set(listed) - {SUMS})
    check("every listed file is present", not missing, f"missing {missing}")
    check("every file present is listed", not extra, f"unlisted {extra}")
    for rel in (RECORD, LOG, README):
        check(f"{rel} is in the package", rel in listed and rel in present,
              "it is not")
    for rel in sorted(listed):
        check(f"{rel} is a file the package's layout has",
              rel in (RECORD, README, LOG) or (
                  rel.startswith(EVIDENCE) and "/" not in rel[len(EVIDENCE):]),
              "the layout is the record, the guide, the history and "
              "evidence/<sha256>")
    for rel in sorted(set(listed) & set(present)):
        data = _read(root, rel)
        d = sha(data)
        check(f"{rel} is the bytes SHA256SUMS lists", d == listed[rel],
              f"sha256 {d}, listed {listed[rel]}")
        if rel.startswith(EVIDENCE):
            check(f"{rel} is named by its own digest",
                  rel == EVIDENCE + d, f"its sha256 is {d}")
        ctx["files"][rel] = data


def _q(o: dict) -> dict:
    return o.get("quantity") or {}


def _level_consistency(ctx, check):
    files = ctx["files"]
    rec = strict_json(files[RECORD], RECORD)
    if not isinstance(rec, dict):
        raise Refused("the record is an object", type(rec).__name__)
    ctx["record"] = rec
    check("the record has exactly the record's fields",
          set(rec) == RECORD_KEYS,
          f"extra {sorted(set(rec) - RECORD_KEYS)}, missing "
          f"{sorted(RECORD_KEYS - set(rec))}")
    check("the record is a scientific assurance record",
          rec.get("schema") == RECORD_SCHEMA, f"schema {rec.get('schema')!r}")
    check("the record was made for this verifier's version",
          (rec.get("verifier") or {}).get("version") == VERSION,
          f"version {(rec.get('verifier') or {}).get('version')!r}")
    stmt = rec.get("mathematical_statement") or {}
    check("the record asks about the problem this verifier rechecks",
          stmt.get("problem") == PROBLEM, f"problem {stmt.get('problem')!r}")

    ev = rec.get("evidence") or {}
    check("the record names each kind of evidence once",
          set(ev) == EVIDENCE_ROLES,
          f"extra {sorted(set(ev) - EVIDENCE_ROLES)}, missing "
          f"{sorted(EVIDENCE_ROLES - set(ev))}")
    held = {r[len(EVIDENCE):] for r in files if r.startswith(EVIDENCE)}
    named = {d for d in ev.values() if isinstance(d, str)}
    check("every evidence file is one the record names, and each it names "
          "is here", held == named,
          f"unnamed {sorted(held - named)}, absent {sorted(named - held)}")
    check("each kind of evidence is its own file", len(named) == len(ev),
          "two kinds name one file")
    if not EVIDENCE_ROLES <= set(ev) or not named <= held:
        raise Refused("the evidence can be read", "it is not all here")

    def load(role):
        return strict_json(files[EVIDENCE + ev[role]], role)

    bundle, ident, ver = (load("result_bundle"), load("run_identity"),
                          load("verification_report"))
    budget, pv, ps = (load("error_budget"), load("decision_proof_verified"),
                      load("decision_proof_supported"))
    xs, ts = _field(files[EVIDENCE + ev["temperature_field"]])
    ctx.update(bundle=bundle, verification=ver, budget=budget,
               proof_verified=pv, proof_supported=ps, field=(xs, ts),
               evidence=ev)

    model = rec.get("model") or {}
    for k in ("model_id", "model_version", "implementation_digest"):
        check(f"the bundle's {k} is the record's",
              bundle.get(k) == model.get(k),
              f"{bundle.get(k)!r}, record {model.get(k)!r}")
    params = bundle.get("parameters") or {}
    inputs = rec.get("inputs") or {}
    check("the record's inputs are the bundle's parameters",
          {k: (v or {}).get("value") for k, v in inputs.items()} == params,
          f"record {inputs}, bundle {params}")
    units = {k: (v or {}).get("unit") for k, v in inputs.items()}
    check("every input is in the unit the equations are written in",
          units == UNITS, f"record units {units}")
    check("the bundle's parameter digest is its parameters'",
          bundle.get("parameter_digest") == sha(canonical(params)),
          "it is not")
    check("the bundle is a simulation result, not a measurement",
          bundle.get("observation_kind") == "SIMULATION_RESULT",
          f"{bundle.get('observation_kind')!r}")
    outs = {o.get("name"): o for o in bundle.get("outputs") or ()}
    th = outs.get("T_at_half") or {}
    stated = (rec.get("results") or {}).get("T_at_half") or {}
    check("the record's T_at_half is the bundle's",
          th.get("status") == "OK" and all(
              _q(th).get(k) == stated.get(k)
              for k in ("value", "unit", "resolution", "resolution_basis")),
          f"bundle {_q(th)}, record {stated}")
    arts = bundle.get("artifacts") or []
    check("the bundle's one artifact is the field the record names",
          len(arts) == 1 and arts[0].get("digest") == ev["temperature_field"]
          and arts[0].get("size_bytes") ==
          len(files[EVIDENCE + ev["temperature_field"]]),
          f"artifacts {arts}")
    for k in ("model_id", "model_version", "implementation_digest",
              "parameter_digest", "environment_digest"):
        check(f"the run identity's {k} is the bundle's",
              ident.get(k) == bundle.get(k), f"{ident.get(k)!r}")
    env = ((bundle.get("provenance") or {}).get("environment") or {})
    tool = rec.get("toolchain") or {}
    check("the record's toolchain is what the bundle states",
          tool.get("environment_digest") == bundle.get("environment_digest")
          and tool.get("python") == env.get("python")
          and tool.get("distributions") == env.get("distributions")
          and tool.get("stated_by") == "producer",
          f"record {tool}")

    check("the check's subject is this bundle",
          ver.get("subject_digest") == sha(canonical(bundle)),
          f"subject {ver.get('subject_digest')}, bundle "
          f"{sha(canonical(bundle))}")
    check("the check names the bundle's producer, and is not its code",
          ver.get("producer_implementation_digest") ==
          bundle.get("implementation_digest") and
          ver.get("verifier_implementation_digest") not in
          (None, bundle.get("implementation_digest")),
          f"producer {ver.get('producer_implementation_digest')}, verifier "
          f"{ver.get('verifier_implementation_digest')}")
    mv = (ver.get("measured") or {}).get("value")
    tv = (ver.get("threshold") or {}).get("value")
    check("the check passed on what it measured",
          ver.get("status") == "PASS" and isinstance(mv, (int, float))
          and isinstance(tv, (int, float)) and mv <= tv,
          f"status {ver.get('status')!r}, measured {mv!r}, threshold {tv!r}")
    check("the check establishes numerical agreement and no more",
          ver.get("establishes") == "INDEPENDENT_NUMERICAL_AGREEMENT",
          f"{ver.get('establishes')!r}")
    checks = rec.get("independent_checks") or []
    first = checks[0] if checks else {}
    check("the record's check is the one in the evidence",
          first.get("evidence") == "verification_report"
          and first.get("check_id") == ver.get("check_id")
          and first.get("status") == ver.get("status"),
          f"record {first}, evidence {ver.get('check_id')!r}")

    b = budget.get("budget") or {}
    check("the budget is the slab's, at the bundle's point",
          budget.get("schema") == "slab-error-budget/1"
          and budget.get("point") == params, f"point {budget.get('point')}")
    check("the budget is of this model's implementation",
          (budget.get("model") or {}).get("implementation_digest") ==
          bundle.get("implementation_digest"),
          f"{(budget.get('model') or {}).get('implementation_digest')!r}")
    check("the budget is of the bundle's T_at_half",
          b.get("output") == "T_at_half" and b.get("unit") == "K"
          and b.get("value") == _q(th).get("value"),
          f"{b.get('output')!r} = {b.get('value')!r} {b.get('unit')!r}")
    eb = rec.get("error_budget") or {}
    check("the record states the budget's total and statement",
          eb.get("evidence") == "error_budget"
          and eb.get("total_half_width") == (b.get("total") or {}).get(
              "half_width") and eb.get("statement") == b.get("statement"),
          f"record {eb}")
    check("the budget and the package authorize nothing",
          budget.get("authority") == b.get("authority") ==
          rec.get("authority") == "NON_AUTHORITATIVE",
          f"{budget.get('authority')!r}, {rec.get('authority')!r}")

    est = rec.get("establishment") or {}
    check("the record says something of every kind of establishment",
          set(est) == set(CARRIED + NOT_CARRIED), f"kinds {sorted(est)}")
    over = [k for k in NOT_CARRIED
            if not str(est.get(k, "")).startswith(UNCLAIMED)]
    check("the record claims no establishment the package cannot carry",
          not over, f"claimed {over}: nothing here could check them")
    check("the record claims no formal proof",
          (rec.get("formal") or {}).get("status") in UNCLAIMED,
          f"{rec.get('formal')}")
    gaps = {g.get("id") for g in rec.get("not_established") or ()
            if isinstance(g, dict)}
    hidden = [i for i, _ in NOT_ESTABLISHED if i not in gaps]
    check("the record names every gap this verifier knows of", not hidden,
          f"not named: {hidden}")


def _parse_log(raw: bytes) -> list:
    """The records of the log, each its canonical bytes and one newline,
    every field present and typed, its hash its body's digest, each linked
    to the one before from a genesis."""
    if not raw.endswith(b"\n"):
        raise Refused("the log ends with a newline", "its last line is torn")
    events = []
    prev = "0" * 64
    for n, line in enumerate(raw[:-1].split(b"\n")):
        where = f"log line {n + 1}"
        if len(line) > MAX_LINE_BYTES:
            raise Refused("every log line is within the bound", where)
        rec = strict_json(line, where)
        if not isinstance(rec, dict) or set(rec) != set(_EVENT_FIELDS) | {
                "hash"}:
            shown = sorted(rec) if isinstance(rec, dict) else rec
            raise Refused("every log record has exactly the event's fields",
                          f"{where}: {shown!r}")
        seq = rec["seq"]
        typed = (isinstance(seq, int) and not isinstance(seq, bool)
                 and isinstance(rec["payload"], dict)
                 and all(isinstance(rec[f], str) for f in (
                     "event_id", "actor", "action", "target", "prev_hash",
                     "hash"))
                 and isinstance(rec["wall_time"], (int, float))
                 and not isinstance(rec["wall_time"], bool)
                 and rec["canonical_form_version"] == 1
                 and not isinstance(rec["canonical_form_version"], bool))
        if not typed:
            raise Refused("every log record's fields have their types",
                          where)
        if canonical(rec) != line:
            raise Refused("every log line is its record's canonical bytes",
                          f"{where}: the hash covers the parsed record, so "
                          "other bytes may say what no check read")
        body = {f: rec[f] for f in _EVENT_FIELDS}
        if rec["seq"] != n:
            raise Refused("the log's positions run from 0 without a gap",
                          f"{where} is seq {rec['seq']}")
        if rec["prev_hash"] != prev:
            raise Refused("every record links to the one before it",
                          f"seq {n}: prev_hash {rec['prev_hash'][:16]}, the "
                          f"record before is {prev[:16]}")
        if sha(canonical(body)) != rec["hash"] or not _HEX.fullmatch(
                rec["hash"]):
            raise Refused("every record's hash is its body's digest",
                          f"seq {n}")
        prev = rec["hash"]
        events.append(rec)
    return events


def _level_history(ctx, check):
    events = _parse_log(ctx["files"][LOG])
    ctx["events"] = events
    hist = ctx["record"].get("history") or {}
    head = hist.get("head") or {}
    check("the history ends at the head the record states",
          head == {"seq": events[-1]["seq"], "hash": events[-1]["hash"]},
          f"record {head}, log ends at seq {events[-1]['seq']} "
          f"{events[-1]['hash']}")
    check("the record counts the history's records",
          hist.get("events") == len(events) and hist.get("log") == LOG,
          f"record {hist.get('events')!r} at {hist.get('log')!r}, log "
          f"{len(events)}")


def _at(events: list, seq) -> dict:
    if not isinstance(seq, int) or isinstance(seq, bool) or \
            not 0 <= seq < len(events):
        raise Refused("every position the record names is in the history",
                      f"seq {seq!r}")
    return events[seq]


def _level_decision(ctx, check):
    rec, events, ev = ctx["record"], ctx["events"], ctx["evidence"]
    bundle = ctx["bundle"]
    q, hyp = rec.get("question") or {}, rec.get("hypothesis") or {}
    auth = rec.get("authorization") or {}
    rr = auth.get("result_record") or {}
    decisions = {d.get("name"): d for d in auth.get("decisions") or ()
                 if isinstance(d, dict)}
    if set(decisions) != {"verified", "supported"}:
        raise Refused("the record reports the result's verification and "
                      "the hypothesis's verdict",
                      f"{sorted(map(str, decisions))}")
    dv, ds = decisions["verified"], decisions["supported"]

    obj = _at(events, q.get("registered_seq"))
    o = (obj.get("payload") or {}).get("objective") or {}
    check("the question is the one the owner registered",
          obj["action"] == "research.objective"
          and obj["target"] == q.get("objective_id")
          and obj["actor"] == q.get("asked_by")
          and o.get("statement") == q.get("statement"),
          f"seq {obj['seq']}: {obj['action']} {obj['target']} by "
          f"{obj['actor']}")
    check("the run answered the point the question asked about",
          (o.get("question") or {}).get("parameters") ==
          bundle.get("parameters"),
          f"asked {(o.get('question') or {}).get('parameters')}")
    check("the hypothesis predicts what the question asked",
          (o.get("question") or {}).get("prediction") ==
          hyp.get("prediction"), f"{hyp.get('prediction')}")
    hev = [e for e in events if e["action"] == "research.hypothesis"
           and e["target"] == hyp.get("hypothesis_id")]
    h = ((hev[0]["payload"] or {}).get("hypothesis") or {}) if hev else {}
    check("the hypothesis is in the history once, as the record states it",
          len(hev) == 1 and h.get("prediction") == hyp.get("prediction")
          and h.get("proposed_by") == hyp.get("proposed_by")
          and h.get("objective_id") == q.get("objective_id"),
          f"{len(hev)} records; {h}")

    create = _at(events, rr.get("created_seq"))
    cp = create.get("payload") or {}
    check("the result was proposed as the record states, citing this "
          "package's bundle and run identity",
          create["action"] == "record.create"
          and create["target"] == rr.get("record_id")
          and cp.get("kind") == rr.get("kind") == "scientific_result"
          and cp.get("proposer") == rr.get("proposer")
          and (cp.get("evidence") or {}) == {
              "result_bundle": ev["result_bundle"],
              "run_identity": ev["run_identity"]},
          f"seq {create['seq']}: {create['action']} {cp}")
    captured = set()
    for e in events:
        if e["action"] == "task.evidence":
            captured |= set(((e.get("payload") or {}).get("artifacts")
                             or {}).values())
    check("a governed task captured the bundle, its field and the check's "
          "report", {ev["result_bundle"], ev["temperature_field"],
                     ev["verification_report"]} <= captured,
          "not every one is in a task's evidence")

    d = _at(events, dv.get("seq"))
    dp = d.get("payload") or {}
    check("the verification is in the history as the record reports it",
          d["hash"] == dv.get("hash") and d["action"] == dv.get("action")
          == "record.transition" and d["actor"] == dv.get("actor")
          and dp.get("record_id") == rr.get("record_id")
          and dp.get("dst") == dv.get("outcome") == "VERIFIED"
          and dp.get("role") == dv.get("role") == "VERIFIER"
          and (dp.get("evidence") or {}) == {
              "verification_report": ev["verification_report"]},
          f"seq {d['seq']}: {d['action']} by {d['actor']}: {dp}")
    check("the party that verified the result is not the one that proposed "
          "it", d["actor"] != cp.get("proposer"),
          f"both {d['actor']!r}")
    moves = [e for e in events if e["action"] == "record.transition"
             and (e.get("payload") or {}).get("record_id") ==
             rr.get("record_id")]
    final = (moves[-1].get("payload") or {}).get("dst") if moves else None
    check("the result still stands at the head of the history",
          final == auth.get("final_state") and final in STANDING,
          f"its last transition is to {final!r}; the record says "
          f"{auth.get('final_state')!r}")

    s = _at(events, ds.get("seq"))
    sp = s.get("payload") or {}
    check("the verdict is in the history as the record reports it",
          s["hash"] == ds.get("hash") and s["action"] == ds.get("action")
          == "research.status" and s["actor"] == ds.get("actor")
          and s["target"] == hyp.get("hypothesis_id")
          and sp.get("status") == ds.get("outcome") == "SUPPORTED",
          f"seq {s['seq']}: {s['action']} {s['target']} by {s['actor']}: "
          f"{sp.get('status')!r}")
    basis = set(sp.get("basis") or ())
    th = _q(next((o for o in bundle.get("outputs") or ()
                  if o.get("name") == "T_at_half"), {}))
    seen = [e for e in events if e["action"] == "research.observation"
            and e["target"] in basis and e["seq"] < s["seq"]]
    good = [e for e in seen if ((e.get("payload") or {}).get("observation")
                                or {}).get("record_id") == rr.get("record_id")
            and ((e.get("payload") or {}).get("observation") or {}).get(
                "result_bundle") == ev["result_bundle"]
            and (((e.get("payload") or {}).get("observation") or {}).get(
                "values") or {}).get("T_at_half") == {
                    "value": th.get("value"), "unit": th.get("unit"),
                    "resolution": th.get("resolution")}]
    check("the verdict rests on an observation of this result's T_at_half",
          bool(good) and len(good) == len(seen) == len(basis),
          f"basis {sorted(basis)}; {len(good)} of {len(seen)} observations "
          "cite this result as it is")
    latest = [e for e in events if e["action"] == "research.status"
              and e["target"] == hyp.get("hypothesis_id")]
    check("the verdict still stands at the head of the history",
          bool(latest) and latest[-1]["seq"] == s["seq"],
          f"the hypothesis's last status is at seq "
          f"{latest[-1]['seq'] if latest else None}")

    for name, proof, at in (("verified", ctx["proof_verified"], d),
                            ("supported", ctx["proof_supported"], s)):
        pd = proof.get("decision") or {}
        before = _at(events, at["seq"] - 1)
        check(f"the producer's proof of the {name} decision is about this "
              "history's record of it",
              proof.get("schema") == "decision-proof/1"
              and proof.get("accepted") is True
              and pd.get("seq") == at["seq"] and pd.get("hash") == at["hash"]
              and pd.get("actor") == at["actor"]
              and pd.get("action") == at["action"]
              and proof.get("head") == {"seq": before["seq"],
                                        "hash": before["hash"]},
              f"proof decision {pd}, head {proof.get('head')}")
    pr = ctx["proof_verified"].get("record") or {}
    sod = ctx["proof_verified"].get("separation_of_duties") or {}
    check("the proof of the verification cites this package's evidence and "
          "two parties",
          pr.get("record_id") == rr.get("record_id")
          and pr.get("proposer") == rr.get("proposer")
          and pr.get("evidence") == {
              "result_bundle": ev["result_bundle"],
              "run_identity": ev["run_identity"],
              "verification_report": ev["verification_report"]}
          and sod == {"proposer": rr.get("proposer"), "decider": d["actor"],
                      "distinct": True},
          f"record {pr}, separation {sod}")
    pb = ctx["proof_supported"].get("basis") or []
    check("the proof of the verdict rests on this result, verified",
          bool(pb) and all(x.get("result_bundle") == ev["result_bundle"]
                           and x.get("record_state") in STANDING
                           for x in pb)
          and (ctx["proof_supported"].get("hypothesis") or {}).get(
              "prediction") == hyp.get("prediction"),
          f"basis {pb}")


def _evaluate(op: str, lo: float, hi: float, value: float) -> str:
    """A prediction ``T op value`` against an interval [lo, hi] for T."""
    if op in ("<", "<="):
        holds, fails = (hi < value, lo >= value) if op == "<" else \
            (hi <= value, lo > value)
    elif op in (">", ">="):
        holds, fails = (lo > value, hi <= value) if op == ">" else \
            (lo >= value, hi < value)
    else:
        raise Refused("the prediction's comparison is one this verifier "
                      "evaluates", f"{op!r}")
    return "SUPPORTED" if holds else "REFUTED" if fails else "INCONCLUSIVE"


def _level_recheck(ctx, check):
    bundle, budget = ctx["bundle"], ctx["budget"]
    p = bundle["parameters"]
    outs = {o.get("name"): _q(o) for o in bundle.get("outputs") or ()
            if o.get("status") == "OK"}
    m = ctx["measured"]
    exact = {}
    for name, frac in SAMPLES.items():
        o = outs.get(name) or {}
        e = exact[name] = exact_temperature(p, frac)
        r: Any = o.get("resolution")
        v: Any = o.get("value")
        ok = isinstance(r, (int, float)) and isinstance(v, (int, float))
        tol = GCI_SAFETY * r + 64 * _EPS * abs(e) if ok else 0.0
        check(f"the model's {name} is the exact solution's, within 3 times "
              "its own discretization estimate",
              ok and abs(v - e) <= tol,
              f"model {v!r}, exact {e!r}, criterion {tol!r}")
    xs, ts = ctx["field"]
    L = p["L_m"]
    worst = max(abs(t - exact_temperature(p, x / L)) for x, t in zip(xs, ts))
    widest = max((outs.get(n) or {}).get("resolution") or 0.0
                 for n in SAMPLES)
    tol = GCI_SAFETY * widest + 64 * _EPS * max(abs(t) for t in ts)
    m["field_max_deviation_K"] = worst
    check("the model's whole final profile is the exact solution's, within "
          "3 times its widest estimate",
          len(xs) == p.get("n_cells") and worst <= tol,
          f"{len(xs)} cells, largest deviation {worst!r}, criterion {tol!r}")

    model_t = outs["T_at_half"]["value"]
    e = exact["T_at_half"]
    measured = abs(model_t - e)
    m.update(T_exact_at_half_K=e, model_minus_exact_K=model_t - e)
    ref = (budget.get("reference") or {}).get("value")
    check("the producer's reference is the exact solution",
          isinstance(ref, float) and abs(ref - e) <= 1e-9 * abs(e),
          f"recorded {ref!r}, exact {e!r}")
    b = budget.get("budget") or {}
    cs = {c.get("name"): c for c in b.get("contributions") or ()}
    meas = cs.get("discretization_against_the_series") or {}
    check("the budget's measured discretization is |model - exact|",
          meas.get("basis") == "MEASURED"
          and isinstance(meas.get("half_width"), float)
          and math.isclose(meas["half_width"], measured, rel_tol=1e-9,
                           abs_tol=1e-15),
          f"recorded {meas.get('half_width')!r}, recomputed {measured!r}")

    lin = (budget.get("propagation") or {}).get("linear") or {}
    mc = (budget.get("propagation") or {}).get("monte_carlo") or {}
    spreads = budget.get("spreads") or {}
    mine = _propagate(p, spreads, rel_step=lin.get("rel_step"),
                      samples=mc.get("samples"), seed=mc.get("seed"))
    m["propagation"] = mine
    tol = GCI_SAFETY * measured + 64 * _EPS * abs(e)
    m["propagation_tolerance_K"] = tol
    for k_rec, k_mine, src in (("std", "std_linear", lin),
                               ("mean", "mean", mc), ("std", "std", mc),
                               ("p2_5", "p2_5", mc), ("p97_5", "p97_5", mc)):
        v = src.get(k_rec)
        check(f"the {src.get('method', '?').lower()} {k_rec} is what the "
              "declared spreads give through the exact solution",
              isinstance(v, float) and abs(v - mine[k_mine]) <= tol,
              f"recorded {v!r}, recomputed {mine[k_mine]!r}, criterion "
              f"{tol!r}")
    check("the same draws needed the same redraws",
          mc.get("redrawn") == mine["redrawn"],
          f"recorded {mc.get('redrawn')!r}, recomputed {mine['redrawn']}")

    par = cs.get("parameters") or {}
    check("the parametric contribution is half the central 95% interval",
          par.get("basis") == "ASSUMED"
          and isinstance(par.get("half_width"), float)
          and par["half_width"] == 0.5 * (mc["p97_5"] - mc["p2_5"]),
          f"{par.get('half_width')!r}")
    rnd = cs.get("rounding") or {}
    check("the rounding contribution is 64 eps |T|",
          rnd.get("half_width") == 64 * _EPS * abs(model_t),
          f"{rnd.get('half_width')!r}")
    replaced = {c.get("supersedes") for c in cs.values()
                if c.get("supersedes")}
    for c in cs.values():
        if c.get("supersedes") is not None:
            check(f"{c.get('name')} replaces only an estimate, and is a "
                  "measurement", c.get("basis") == "MEASURED"
                  and (cs.get(c["supersedes"]) or {}).get("basis") ==
                  "ESTIMATED", f"{c}")
        check(f"{c.get('name')} carries a number exactly when it is "
              "established",
              (c.get("basis") == "NOT_ESTABLISHED") ==
              (c.get("half_width") is None)
              and c.get("basis") in ("MEASURED", "ESTIMATED", "ASSUMED",
                                     "NOT_ESTABLISHED"), f"{c}")
    used = [c for c in b.get("contributions") or ()
            if c.get("basis") != "NOT_ESTABLISHED"
            and c.get("name") not in replaced]
    total = b.get("total") or {}
    hw = sum(c["half_width"] for c in used)
    check("the budget's total is the sum of what it includes",
          total.get("includes") == [c["name"] for c in used]
          and total.get("half_width") == hw
          and total.get("excludes_not_established") == [
              c.get("name") for c in b.get("contributions") or ()
              if c.get("basis") == "NOT_ESTABLISHED"]
          and total.get("replaced_by_a_measurement") == sorted(replaced),
          f"recorded {total}, recomputed {hw!r}")
    check("the budget's statement says its own numbers",
          str(b.get("statement", "")).startswith(
              f"T_at_half = {model_t:.6g} +/- {hw:.3g} K:"),
          f"{b.get('statement')!r}")
    ratio = (budget.get("estimate_covers_measured") or {})
    est = _q(next((o for o in bundle.get("outputs") or ()
                   if o.get("name") == "T_at_half"), {})).get("resolution")
    check("the budget's verdict on the model's estimate is the one the "
          "numbers give",
          ratio.get("verdict") == ("ESTIMATE_COVERS_MEASURED"
                                   if measured <= est else
                                   "ESTIMATE_DOES_NOT_COVER")
          and ratio.get("estimate") == est,
          f"recorded {ratio.get('verdict')!r}")

    pred = (ctx["record"].get("hypothesis") or {}).get("prediction") or {}
    check("the prediction is of a temperature this verifier computes",
          pred.get("quantity") == "T_at_half" and pred.get("unit") == "K"
          and isinstance(pred.get("value"), (int, float)),
          f"{pred}")
    near = measured + 64 * _EPS * abs(e)
    verdict = _evaluate(pred["op"], e - near, e + near, pred["value"])
    spread = _evaluate(pred["op"], mine["p2_5"] - near, mine["p97_5"] + near,
                       pred["value"])
    m.update(verdict_at_the_point=verdict,
             verdict_under_declared_spreads=spread,
             margin_K=abs(pred["value"] - e) - near)
    decided = next(d.get("outcome") for d in
                   ctx["record"]["authorization"]["decisions"]
                   if d.get("name") == "supported")
    check("the recorded verdict is the one the exact solution gives",
          verdict == decided, f"recorded {decided!r}, exact gives "
          f"{verdict!r}")


# ------------------------------------------------------------ the run

def _expect_head(text: str) -> tuple:
    seq, sep, h = (text or "").partition(":")
    if not sep or not seq.isdigit() or not _HEX.fullmatch(h):
        raise Misuse(f"--expect-head is SEQ:SHA256, not {text!r}")
    return int(seq), h


def verify(package, *, expect_digest: str | None = None,
           expect_head: str | None = None) -> dict:
    """The verification report for the package at ``package``."""
    root = Path(package)
    if expect_digest is not None and not _HEX.fullmatch(expect_digest):
        raise Misuse("--expect-package-digest is a lowercase sha256")
    head = _expect_head(expect_head) if expect_head is not None else None
    try:
        st = os.lstat(root)
    except OSError as exc:
        raise Unreadable(f"no package at {root}: {exc}") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise Unreadable(f"{root} is not a directory")
    here = Path(__file__).resolve()
    if root.resolve() in here.parents:
        raise Misuse("this verifier is inside the package it is asked "
                     "about; obtain it independently of the package")
    ctx: dict = {"root": root, "files": {}, "measured": {}}
    report: dict = {"schema": REPORT_SCHEMA, "verifier_version": VERSION,
                    "package": str(root), "levels": []}
    reached = True
    for name, fn in zip(LEVELS, (_level_integrity, _level_consistency,
                                 _level_history, _level_decision,
                                 _level_recheck)):
        if not reached:
            report["levels"].append({"level": name, "status": "NOT_REACHED",
                                     "checks": []})
            continue
        checks: list = []

        def check(label, cond, detail="", _c=checks):
            _c.append({"check": label,
                       "status": "PASS" if cond else "FAIL",
                       "detail": "" if cond else str(detail)[:600]})

        try:
            fn(ctx, check)
        except Refused as exc:
            check(exc.args[0], False, exc.args[1])
        except Exception as exc:                    # noqa: BLE001
            # anything a level did not expect is a failure of that level,
            # never a pass
            check("the level ran to its end", False,
                  f"{type(exc).__name__}: {exc}")
        ok = bool(checks) and all(c["status"] == "PASS" for c in checks)
        report["levels"].append({"level": name,
                                 "status": "PASS" if ok else "FAIL",
                                 "checks": checks})
        reached = ok

    expectations = {}
    gaps = dict(NOT_ESTABLISHED)
    digest_ = ctx.get("package_digest")
    if expect_digest is not None:
        expectations["package_digest"] = (
            "MATCH" if digest_ == expect_digest else
            f"MISMATCH: the package's is {digest_}")
        if digest_ == expect_digest:
            gaps.pop("PACKAGE_ORIGIN")
    if head is not None:
        events = ctx.get("events") or []
        seq, h = head
        found = events[seq]["hash"] if seq < len(events) else None
        expectations["head"] = (
            "MATCH" if found == h else
            "MISMATCH: the history does not hold that head" if found else
            "MISMATCH: the history ends before that head")
        if found == h:
            gaps.pop("HISTORY_ANCHORED")
    report["package_digest"] = digest_
    report["expectations"] = expectations
    report["measured"] = ctx["measured"]
    report["not_established"] = [{"id": k, "why": v} for k, v in gaps.items()]
    passed = all(lv["status"] == "PASS" for lv in report["levels"]) and all(
        v == "MATCH" for v in expectations.values())
    report["verdict"] = "VERIFIED" if passed else "REFUSED"
    return report


def _first_failure(report: dict) -> str:
    for lv in report["levels"]:
        for c in lv["checks"]:
            if c["status"] == "FAIL":
                return f"{lv['level']}: {c['check']}: {c['detail']}"
    for k, v in report["expectations"].items():
        if v != "MATCH":
            return f"expected {k}: {v}"
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("package")
    ap.add_argument("--expect-package-digest", default=None,
                    help="sha256 of SHA256SUMS, obtained independently")
    ap.add_argument("--expect-head", default=None,
                    help="SEQ:SHA256 of a history head, obtained "
                         "independently")
    args = ap.parse_args(argv)
    try:
        report = verify(args.package, expect_digest=args.expect_package_digest,
                        expect_head=args.expect_head)
    except Misuse as exc:
        print(f"assurance package: MISUSE: {exc}", file=sys.stderr)
        return 3
    except Unreadable as exc:
        print(f"assurance package: UNREADABLE: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:                        # noqa: BLE001
        print(f"assurance package: VERIFIER ERROR, not a pass: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 4
    print(json.dumps(report, indent=1, sort_keys=True))
    if report["verdict"] == "VERIFIED":
        print(f"assurance package: VERIFIED at {len(LEVELS)} levels; "
              f"{len(report['not_established'])} things not established "
              "(listed)", file=sys.stderr)
        return 0
    print(f"assurance package: REFUSED: {_first_failure(report)}",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
