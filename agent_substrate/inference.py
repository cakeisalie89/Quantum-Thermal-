"""The inference gateway: a model is called, bounded, paid for and recorded,
and nothing it says is authority (Tranche H, Gate B).

WHAT A CALL IS

A request names a budget, the agent asking, a model, the exact prompt by
digest, and the context manifest the prompt was rendered from. Its id is the
digest of all of that, so the same question asked twice is one request. Four
records carry it through the log:

``inference.budget``
    Spending authority. Issued only by the log's root issuer, naming the
    provider, the models and the callers it may be spent on, the totals
    (calls, cost, tokens, calls per window) and the bounds on any one request.
    Withdrawn by whoever issued it, after which nothing is reserved against
    it.
``inference.reserve``
    One attempt's WORST-CASE cost, decided under the writer lock against
    every reservation and settlement before it. Two processes cannot spend
    more than the budget between them, because neither decides anywhere but
    under the lock, against the head it writes onto. A refusal is recorded
    too: an exhausted budget is a fact an investigation needs.
``inference.dispatch``
    Written BEFORE the request leaves. A gateway that crashes after it may
    have been charged; one that crashes before it was not. The other order
    would let a crash between the call and the record release a reservation
    the provider had already spent.
``inference.result``
    Exactly one per attempt: the outcome, the usage the provider CLAIMS, the
    cost settled, and the digest of the response held in the transcript store.

WHAT IS BOUNDED, AND HOW

* The reservation's input bound is one token per prompt byte plus
  :data:`FRAMING_TOKENS`: no byte-level tokenizer emits more tokens than
  bytes. Its output bound is the request's ``max_output_tokens``, which the
  provider is told. Cost is priced from those bounds.
* Settlement is conservative. A successful call is charged at the usage the
  provider reports, and a report above the reservation is flagged as an
  overrun rather than trimmed to fit. Any other dispatched attempt is charged
  its full reservation, because what the provider did is unknown. An attempt
  never dispatched costs nothing.
* A reservation holds a lease: the call's timeout plus
  :data:`LEASE_GRACE_S`. A dispatch after the lease is refused. Recovery
  settles an attempt only once its lease has run out: NOT_SENT if it was never
  dispatched, LOST if it was. A result arriving after recovery settled the
  attempt is refused, and its response is not used.
* Attempts are bounded per request, and only one attempt of a request is open
  at a time. A second process asking the same question finds the first
  attempt outstanding and does not call the provider.
* An idempotency key names one question. Take two processes resuming one
  step, each having built its own context: the first request granted under
  the step's key is the step's. The second is refused with
  :class:`KeyHeld`, nothing is recorded, and nothing is sent (D-2026-184).

WHAT A RESPONSE IS

Input. The bytes are kept in a TRANSCRIPT store, which is a content-addressed
store at its own root and never the evidence store: a model's output whose
digest resolved as evidence could be cited by an authority transition, and
its resolving would read as a check passed. The provider and model a reply
names are recorded as CLAIMED. A response becomes a proposal only through
:func:`envelope_from_inference`, and an ingress receives that proposal only
if :meth:`InferenceGateway.binding_problems` finds the proposal is what the
recorded response said.

WHAT THE MODEL SAW

:func:`render_prompt` turns a built :class:`~agent_substrate.context.Context`
into canonical bytes that name the manifest and carry each shown item.
:func:`prompt_problems` checks those bytes against the recorded manifest, item
by item and digest by digest. The reservation refuses a request whose
manifest was never recorded, or was built under a policy no longer in force
at the head the reservation is written onto (CONTEXT_STALE). A model answering
under rules that have since changed is answering a different question.

WHAT THIS MODULE DOES NOT DO

It holds no credential and opens no network connection. A live provider runs
behind :class:`SubprocessProvider`, in a separate process that reads its own
credential from a file provider root outside the repository. That process's
live mode refuses until the owner chooses a provider (OD-1), so no live call
can happen by accident.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import proposals as P
from .canonical import canonical_bytes, digest, digest_bytes, is_digest
from .context import ACT_CONTEXT_BUILD, Context, ContextError, \
    manifest_from_record
from .events import DecidedWriter, judged_at, projection_load
from .policy import PolicyError

ACT_BUDGET = "inference.budget"
ACT_RESERVE = "inference.reserve"
ACT_DISPATCH = "inference.dispatch"
ACT_RESULT = "inference.result"
ACTIONS = (ACT_BUDGET, ACT_RESERVE, ACT_DISPATCH, ACT_RESULT)
_ACT_ROOT = "capability.root"

BUDGET_SCHEMA = "inference-budget/1"
REQUEST_SCHEMA = "inference-request/1"
PROMPT_SCHEMA = "inference-prompt/1"
RESPONSE_SCHEMA = "proposal-response/1"
GATEWAY_ID = "inference-gateway"
ADAPTER = "inference-gateway"

OK = "OK"
SCHEMA_REFUSED = "SCHEMA_REFUSED"
OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
PROVIDER_ERROR = "PROVIDER_ERROR"
TIMEOUT = "TIMEOUT"
RATE_LIMITED = "RATE_LIMITED"
CANCELLED = "CANCELLED"
NOT_SENT = "NOT_SENT"
LOST = "LOST"
OUTCOMES = (OK, SCHEMA_REFUSED, OUTPUT_TOO_LARGE, PROVIDER_ERROR, TIMEOUT,
            RATE_LIMITED, CANCELLED, NOT_SENT, LOST)
#: Worth another attempt, budget and attempt bound permitting.
RETRYABLE = frozenset({SCHEMA_REFUSED, OUTPUT_TOO_LARGE, PROVIDER_ERROR,
                       TIMEOUT, RATE_LIMITED, LOST})
#: Written only by recovery, for an attempt whose gateway is gone.
RECOVERY_OUTCOMES = frozenset({NOT_SENT, LOST})
#: The outcomes that need a dispatch record before them, and the ones that
#: may not have one.
NEEDS_DISPATCH = frozenset({OK, SCHEMA_REFUSED, OUTPUT_TOO_LARGE,
                            PROVIDER_ERROR, TIMEOUT, RATE_LIMITED, LOST})
NEVER_DISPATCHED = frozenset({NOT_SENT})

REFUSALS = ("BUDGET_UNKNOWN", "BUDGET_WITHDRAWN", "CALLER_NOT_ALLOWED",
            "MODEL_NOT_ALLOWED", "OVER_REQUEST_LIMIT", "BUDGET_EXHAUSTED",
            "RATE_LIMITED", "ATTEMPTS_EXHAUSTED", "CONTEXT_UNKNOWN",
            "CONTEXT_STALE")

#: Tokens a provider's framing may add to a prompt, on top of one per byte.
FRAMING_TOKENS = 256
#: How long past its timeout a reservation is held before recovery may
#: settle it. Generous, because settling a live attempt as LOST double-counts
#: nothing but does refuse the result it was about to write.
LEASE_GRACE_S = 30.0
#: The longest a gateway sleeps between attempts.
MAX_BACKOFF_S = 30.0
MAX_ERROR_CHARS = 500

_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._@/:-]{0,127}\Z")
_REQUEST_ID = re.compile(r"\Ainfer-[0-9a-f]{64}\Z")

_BUDGET_KEYS = {"schema", "budget_id", "provider", "models", "callers",
                "limits", "per_request", "prices"}
_LIMIT_KEYS = {"max_calls", "max_cost_micro", "max_input_tokens",
               "max_output_tokens", "window_s", "max_calls_per_window"}
_PER_REQUEST_KEYS = {"max_input_bytes", "max_output_tokens",
                     "max_output_bytes", "timeout_s", "max_attempts"}
_REQUEST_KEYS = {"schema", "budget_id", "caller", "purpose", "model",
                 "prompt_sha256", "prompt_bytes", "context_manifest_digest",
                 "max_output_tokens", "max_output_bytes", "timeout_s",
                 "max_attempts", "idempotency_key", "response_schema"}
_USAGE_KEYS = {"input_tokens", "output_tokens"}
_CLAIM_KEYS = {"provider", "model"}


class InferenceError(Exception):
    """A call, a record or a history the gateway refuses. Fail closed."""


class InferenceRefused(InferenceError):
    """A reservation the budget did not permit. The refusal is recorded."""

    def __init__(self, code: str, reason: str, seq: int | None = None):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.seq = seq


class AttemptOutstanding(InferenceError):
    """Another gateway holds this request's open attempt. Nothing is
    recorded and nothing is sent: the question is already being asked."""


class KeyHeld(InferenceError):
    """Another request was granted under this request's idempotency key.
    The key names one question (a step's), and it has been asked: a second
    question under it would be a second answer to one step. Nothing is
    recorded and nothing is sent."""


def _count(value, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or \
            value < minimum:
        raise InferenceError(f"{name} must be an int >= {minimum}, "
                             f"got {value!r}")
    return value


def _name(value, what: str) -> str:
    if not isinstance(value, str) or not _NAME.match(value):
        raise InferenceError(f"{what} is not a name: {value!r}")
    return value


def _exact(obj, keys, what: str) -> dict:
    if not isinstance(obj, dict) or set(obj) != set(keys):
        got = sorted(obj) if isinstance(obj, dict) else type(obj).__name__
        raise InferenceError(f"{what} must have exactly {sorted(keys)}, "
                             f"got {got}")
    return obj


def cost_micro(prices: dict, model: str, input_tokens: int,
               output_tokens: int) -> int:
    """Cost in micro-units, prices being micro-units per million tokens.
    Rounded up: a budget is never charged less than it spent."""
    p = prices[model]
    total = input_tokens * p["input"] + output_tokens * p["output"]
    return -(-total // 1_000_000)


def input_bound(prompt_bytes: int) -> int:
    """The most input tokens a prompt of this many bytes can cost."""
    return prompt_bytes + FRAMING_TOKENS


# --------------------------------------------------------------- budgets

@dataclass(frozen=True)
class Budget:
    budget_id: str
    provider: str
    models: tuple
    callers: tuple
    max_calls: int
    max_cost_micro: int
    max_input_tokens: int
    max_output_tokens: int
    window_s: int
    max_calls_per_window: int
    max_input_bytes: int
    max_request_output_tokens: int
    max_output_bytes: int
    max_timeout_s: int
    max_attempts: int
    #: model -> {"input": micro-units per Mtok, "output": micro-units per Mtok}
    prices: dict

    def body(self) -> dict:
        return {"schema": BUDGET_SCHEMA, "budget_id": self.budget_id,
                "provider": self.provider, "models": sorted(self.models),
                "callers": sorted(self.callers),
                "limits": {"max_calls": self.max_calls,
                           "max_cost_micro": self.max_cost_micro,
                           "max_input_tokens": self.max_input_tokens,
                           "max_output_tokens": self.max_output_tokens,
                           "window_s": self.window_s,
                           "max_calls_per_window": self.max_calls_per_window},
                "per_request": {
                    "max_input_bytes": self.max_input_bytes,
                    "max_output_tokens": self.max_request_output_tokens,
                    "max_output_bytes": self.max_output_bytes,
                    "timeout_s": self.max_timeout_s,
                    "max_attempts": self.max_attempts},
                "prices": {m: dict(self.prices[m])
                           for m in sorted(self.prices)}}

    def digest(self) -> str:
        return digest(self.body())

    @classmethod
    def from_body(cls, body) -> "Budget":
        _exact(body, _BUDGET_KEYS, "budget")
        if body["schema"] != BUDGET_SCHEMA:
            raise InferenceError(f"budget schema {body['schema']!r} is not "
                                 f"{BUDGET_SCHEMA}")
        lim = _exact(body["limits"], _LIMIT_KEYS, "budget.limits")
        per = _exact(body["per_request"], _PER_REQUEST_KEYS,
                     "budget.per_request")
        models, callers = body["models"], body["callers"]
        for what, seq in (("models", models), ("callers", callers)):
            if not isinstance(seq, list) or not seq or \
                    len(set(seq)) != len(seq):
                raise InferenceError(f"budget.{what}: a non-empty list "
                                     "without repeats")
            for v in seq:
                _name(v, f"budget.{what}[]")
        prices = body["prices"]
        if not isinstance(prices, dict) or set(prices) != set(models):
            raise InferenceError("budget.prices must price exactly the "
                                 "models the budget names")
        for m, pr in prices.items():
            _exact(pr, {"input", "output"}, f"budget.prices.{m}")
            _count(pr["input"], f"prices.{m}.input")
            _count(pr["output"], f"prices.{m}.output")
        b = cls(budget_id=_name(body["budget_id"], "budget_id"),
                provider=_name(body["provider"], "budget.provider"),
                models=tuple(sorted(models)), callers=tuple(sorted(callers)),
                max_calls=_count(lim["max_calls"], "max_calls", minimum=1),
                max_cost_micro=_count(lim["max_cost_micro"], "max_cost_micro"),
                max_input_tokens=_count(lim["max_input_tokens"],
                                        "max_input_tokens"),
                max_output_tokens=_count(lim["max_output_tokens"],
                                         "max_output_tokens"),
                window_s=_count(lim["window_s"], "window_s", minimum=1),
                max_calls_per_window=_count(lim["max_calls_per_window"],
                                            "max_calls_per_window",
                                            minimum=1),
                max_input_bytes=_count(per["max_input_bytes"],
                                       "max_input_bytes", minimum=1),
                max_request_output_tokens=_count(per["max_output_tokens"],
                                                 "max_output_tokens",
                                                 minimum=1),
                max_output_bytes=_count(per["max_output_bytes"],
                                        "max_output_bytes", minimum=1),
                max_timeout_s=_count(per["timeout_s"], "timeout_s",
                                     minimum=1),
                max_attempts=_count(per["max_attempts"], "max_attempts",
                                    minimum=1),
                prices={m: {"input": pr["input"], "output": pr["output"]}
                        for m, pr in prices.items()})
        if b.body() != body:
            raise InferenceError("budget is not in canonical form (sorted "
                                 "models, callers and prices)")
        return b


# --------------------------------------------------------------- requests

@dataclass(frozen=True)
class InferenceRequest:
    budget_id: str
    caller: str
    purpose: str
    model: str
    prompt_sha256: str
    prompt_bytes: int
    context_manifest_digest: str
    max_output_tokens: int
    max_output_bytes: int
    timeout_s: int
    max_attempts: int
    idempotency_key: str

    def body(self) -> dict:
        return {"schema": REQUEST_SCHEMA, "budget_id": self.budget_id,
                "caller": self.caller, "purpose": self.purpose,
                "model": self.model, "prompt_sha256": self.prompt_sha256,
                "prompt_bytes": self.prompt_bytes,
                "context_manifest_digest": self.context_manifest_digest,
                "max_output_tokens": self.max_output_tokens,
                "max_output_bytes": self.max_output_bytes,
                "timeout_s": self.timeout_s, "max_attempts": self.max_attempts,
                "idempotency_key": self.idempotency_key,
                "response_schema": RESPONSE_SCHEMA}

    @property
    def request_id(self) -> str:
        return "infer-" + digest(self.body())

    @classmethod
    def for_prompt(cls, prompt: bytes, *, context: Context, budget_id: str,
                   caller: str, model: str, idempotency_key: str,
                   max_output_tokens: int, max_output_bytes: int,
                   timeout_s: int, max_attempts: int) -> "InferenceRequest":
        """The request for ``prompt``, rendered from ``context``."""
        return cls.from_body({
            "schema": REQUEST_SCHEMA, "budget_id": budget_id,
            "caller": caller, "purpose": context.manifest.purpose,
            "model": model, "prompt_sha256": digest_bytes(prompt),
            "prompt_bytes": len(prompt),
            "context_manifest_digest": context.manifest.digest(),
            "max_output_tokens": max_output_tokens,
            "max_output_bytes": max_output_bytes, "timeout_s": timeout_s,
            "max_attempts": max_attempts, "idempotency_key": idempotency_key,
            "response_schema": RESPONSE_SCHEMA})

    @classmethod
    def from_body(cls, body) -> "InferenceRequest":
        _exact(body, _REQUEST_KEYS, "request")
        if body["schema"] != REQUEST_SCHEMA:
            raise InferenceError(f"request schema {body['schema']!r} is not "
                                 f"{REQUEST_SCHEMA}")
        if body["response_schema"] != RESPONSE_SCHEMA:
            raise InferenceError("a request asks for a proposal response "
                                 f"({RESPONSE_SCHEMA}) and nothing else")
        for k in ("prompt_sha256", "context_manifest_digest"):
            if not is_digest(body[k]):
                raise InferenceError(f"request.{k} is not a digest")
        if not isinstance(body["purpose"], str) or \
                not 0 < len(body["purpose"]) <= 200:
            raise InferenceError("request.purpose: 1..200 characters")
        return cls(budget_id=_name(body["budget_id"], "request.budget_id"),
                   caller=_name(body["caller"], "request.caller"),
                   purpose=body["purpose"],
                   model=_name(body["model"], "request.model"),
                   prompt_sha256=body["prompt_sha256"],
                   prompt_bytes=_count(body["prompt_bytes"], "prompt_bytes",
                                       minimum=1),
                   context_manifest_digest=body["context_manifest_digest"],
                   max_output_tokens=_count(body["max_output_tokens"],
                                            "max_output_tokens", minimum=1),
                   max_output_bytes=_count(body["max_output_bytes"],
                                           "max_output_bytes", minimum=1),
                   timeout_s=_count(body["timeout_s"], "timeout_s",
                                    minimum=1),
                   max_attempts=_count(body["max_attempts"], "max_attempts",
                                       minimum=1),
                   idempotency_key=_name(body["idempotency_key"],
                                         "request.idempotency_key"))


# ------------------------------------------------------------------ prompts

def render_prompt(context: Context) -> bytes:
    """What the model is shown: the manifest's digest and every shown item,
    in the manifest's order, as canonical bytes."""
    m = context.manifest
    if len(m.items) != len(context.parts):
        raise ContextError("a context whose parts do not match its manifest")
    return canonical_bytes({
        "schema": PROMPT_SCHEMA, "manifest_digest": m.digest(),
        "response_schema": RESPONSE_SCHEMA,
        "items": [{"item_id": i.item_id, "tier": i.tier.value, "text": t}
                  for i, t in zip(m.items, context.parts)]})


def prompt_problems(prompt: bytes, manifest) -> list:
    """Does ``prompt`` show exactly what ``manifest`` records as shown?"""
    try:
        doc = json.loads(prompt.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return [f"the prompt is not JSON: {exc}"]
    if not isinstance(doc, dict) or set(doc) != {
            "schema", "manifest_digest", "response_schema", "items"}:
        return ["the prompt is not an inference prompt"]
    out = []
    if canonical_bytes(doc) != prompt:
        out.append("the prompt is not in canonical form")
    if doc["schema"] != PROMPT_SCHEMA:
        out.append(f"prompt schema {doc['schema']!r}")
    if doc["manifest_digest"] != manifest.digest():
        out.append("the prompt names another manifest")
    items = doc["items"] if isinstance(doc["items"], list) else []
    if len(items) != len(manifest.items):
        out.append(f"the prompt shows {len(items)} items and the manifest "
                   f"records {len(manifest.items)}")
    for shown, rec in zip(items, manifest.items):
        if not isinstance(shown, dict) or not isinstance(shown.get("text"),
                                                         str):
            out.append("a prompt item without text")
            continue
        if shown.get("item_id") != rec.item_id or \
                shown.get("tier") != rec.tier.value:
            out.append(f"prompt item {shown.get('item_id')!r} is not "
                       f"manifest item {rec.item_id!r}")
        if digest_bytes(shown["text"].encode("utf-8")) != rec.content_digest:
            out.append(f"item {rec.item_id!r}: the text shown is not the "
                       "text the manifest records")
    return out


# ---------------------------------------------------------------- providers

@dataclass(frozen=True)
class ProviderReply:
    """What came back. Every field is the provider's claim."""

    status: str
    body: bytes = b""
    usage: dict | None = None
    claimed: dict | None = None
    error: str = ""


class FixtureProvider:
    """Scripted replies: deterministic, offline, no credential.

    ``script`` is called with the request body, the prompt and the attempt
    number and returns a :class:`ProviderReply`, or raises. ``delay_s`` makes
    a call slow (for timeouts). Exactly as untrusted as a live provider.
    """

    def __init__(self, name: str, script: Callable, *, delay_s: float = 0.0):
        self.name = name
        self._script = script
        self.delay_s = delay_s
        self.calls: list = []

    def call(self, *, request: dict, prompt: bytes, attempt: int,
             timeout_s: float, max_output_bytes: int,
             cancel: threading.Event) -> ProviderReply:
        self.calls.append((request, attempt))
        if self.delay_s:
            cancel.wait(self.delay_s)
        return self._script(request, prompt, attempt)


WORKER = Path(__file__).with_name("_inference_worker.py")


class SubprocessProvider:
    """The provider behind a process boundary.

    The worker (``_inference_worker.py``) is a standalone script run under
    ``python -I`` with an empty environment. It reads the credential itself,
    from a file provider root that the caller names and that lies outside the
    repository. The credential never enters this process, any log, any
    transcript, or any tool's environment. This process bounds the call: a
    wall clock, an output cap, and a kill of the worker's whole process group
    on timeout or cancellation.

    ``mode="live"`` refuses until the owner chooses a provider (OD-1).
    ``mode="fixture"`` replays a reply file and requires the credential to be
    readable, so a test proves the worker, and only the worker, holds it.
    """

    def __init__(self, name: str, *, mode: str, secret_root: str | None = None,
                 secret_id: str | None = None, fixture: str | None = None,
                 python: str = sys.executable):
        if mode not in ("fixture", "live"):
            raise InferenceError(f"unknown provider mode {mode!r}")
        self.name = name
        self.mode = mode
        self.secret_root = secret_root
        self.secret_id = secret_id
        self.fixture = fixture
        self.python = python

    def _argv(self) -> list:
        argv = [self.python, "-I", str(WORKER), "--mode", self.mode]
        if self.secret_root is not None:
            argv += ["--secret-root", str(self.secret_root)]
        if self.secret_id is not None:
            argv += ["--secret-id", self.secret_id]
        if self.fixture is not None:
            argv += ["--fixture", str(self.fixture)]
        return argv

    def call(self, *, request: dict, prompt: bytes, attempt: int,
             timeout_s: float, max_output_bytes: int,
             cancel: threading.Event) -> ProviderReply:
        cap = 2 * max_output_bytes + 4096       # base64 and the envelope
        msg = canonical_bytes({"request": request, "attempt": attempt,
                               "prompt_b64": base64.b64encode(prompt)
                               .decode("ascii")})
        proc = subprocess.Popen(
            self._argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True, env={},
            cwd="/")
        got: dict = {}

        def pump():
            if proc.stdin is None or proc.stdout is None:
                got["err"] = "the worker has no pipes"
                return
            try:
                proc.stdin.write(msg)
                proc.stdin.close()
                got["out"] = proc.stdout.read(cap + 1)
            except OSError as exc:
                got["err"] = str(exc)

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        deadline = time.monotonic() + timeout_s
        while t.is_alive():
            if cancel.is_set() or time.monotonic() >= deadline:
                _kill_group(proc)
                t.join(5)
                return ProviderReply(CANCELLED if cancel.is_set()
                                     else TIMEOUT)
            t.join(0.05)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
        out = got.get("out", b"")
        if len(out) > cap:
            return ProviderReply(OUTPUT_TOO_LARGE,
                                 error="the worker wrote past its bound")
        try:
            rep = json.loads(out.decode("utf-8"))
            body = base64.b64decode(rep.get("body_b64", ""), validate=True)
        except (UnicodeDecodeError, ValueError) as exc:
            return ProviderReply("ERROR", error=f"the worker's reply is not "
                                 f"a reply ({exc}); exit {proc.returncode}")
        if not isinstance(rep, dict):
            return ProviderReply("ERROR", error="the worker's reply is not "
                                 "an object")
        return ProviderReply(str(rep.get("status", "ERROR")), body,
                             rep.get("usage"), rep.get("claimed"),
                             str(rep.get("error", ""))[:MAX_ERROR_CHARS])


def _kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:          # pragma: no cover
        pass


# ------------------------------------------------------------- projection

@dataclass(frozen=True)
class _Draft:
    """The event a writer is about to append, as its reducer will see it.

    Every writer runs the reducer's own rule on its draft, with ``commit``
    off, under the writer lock. A writer that checked a copy of the rule
    would append a record its own replay then refuses wherever the copy and
    the rule disagree -- which deterministic simulation found it doing for a
    second dispatch of one attempt (D-2026-165)."""

    seq: int
    actor: str
    target: str
    wall_time: float


@dataclass
class _Attempt:
    request_id: str
    attempt: int
    actor: str
    reserve_seq: int
    reserved_in: int
    reserved_out: int
    reserved_cost: int
    lease_until: float
    timeout_s: int
    dispatch_seq: int | None = None
    result: dict | None = None
    result_seq: int | None = None


@dataclass
class _BudgetState:
    budget: Budget
    issued_by: str
    seq: int
    withdrawn_seq: int | None = None
    calls: int = 0
    #: cost and tokens held: settled where settled, reserved where open
    held_cost: int = 0
    held_in: int = 0
    held_out: int = 0
    reserve_times: list = field(default_factory=list)


@dataclass(frozen=True)
class Outcome:
    """How a request ended, as the log records it."""

    request_id: str
    attempt: int
    outcome: str
    seq: int
    response: dict | None = None
    response_sha256: str | None = None
    usage: dict | None = None
    cost_micro: int = 0
    claimed: dict | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == OK


def settlement(att: _Attempt, outcome: str, usage: dict | None,
               prices: dict, model: str) -> tuple:
    """(cost, tokens_in, tokens_out, overrun) for one settled attempt.

    The one rule, used by the gateway that writes a result and by the replay
    that checks it."""
    if att.dispatch_seq is None:
        return 0, 0, 0, False
    if outcome == OK and usage is not None:
        tin, tout = usage["input_tokens"], usage["output_tokens"]
        cost = cost_micro(prices, model, tin, tout)
        over = (cost > att.reserved_cost or tin > att.reserved_in
                or tout > att.reserved_out)
        return cost, tin, tout, over
    return att.reserved_cost, att.reserved_in, att.reserved_out, False


def _usage(usage) -> dict | None:
    if not isinstance(usage, dict) or set(usage) != _USAGE_KEYS:
        return None
    for k in _USAGE_KEYS:
        v = usage[k]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return None
    return {k: usage[k] for k in sorted(_USAGE_KEYS)}


def _claimed(claimed) -> dict | None:
    if not isinstance(claimed, dict) or set(claimed) != _CLAIM_KEYS:
        return None
    if not all(v is None or (isinstance(v, str) and _NAME.match(v))
               for v in claimed.values()):
        return None
    return {k: claimed[k] for k in sorted(_CLAIM_KEYS)}


def _scrub(text: str) -> str:
    """An error message, bounded, with anything credential-shaped removed."""
    return P._CREDENTIAL.sub("[credential removed]", str(text))[
        :MAX_ERROR_CHARS]


def parse_response(body: bytes) -> dict:
    """A provider body as a proposal response, or :class:`ProposalRefused`.

    Strict: one JSON object with exactly the response keys, nothing
    credential-shaped, nothing a proposal may not carry."""
    try:
        resp = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise P.ProposalRefused(f"the response is not JSON: {exc}") from exc
    if not isinstance(resp, dict) or set(resp) != P.RESPONSE_KEYS:
        raise P.ProposalRefused(
            f"a response has exactly {sorted(P.RESPONSE_KEYS)}")
    P._walk(resp, "response")
    return resp


class InferenceGateway(DecidedWriter):
    """Reserves, dispatches, settles and recovers model calls.

    ``transcripts`` is a content-addressed store for prompts and responses,
    at a root that is not the evidence store's. ``policies`` is the policy
    store whose head decides whether a context is still current. ``provider``
    is the one provider this gateway dispatches to.
    """

    def __init__(self, log, *, transcripts, policies, provider,
                 actor: str = GATEWAY_ID, clock: Callable = time.time,
                 sleep: Callable = time.sleep, backoff_s: float = 1.0):
        self.log = log
        self.transcripts = transcripts
        self.policies = policies
        self.provider = provider
        self.actor = _name(actor, "gateway actor")
        self.clock = clock
        self.sleep = sleep
        self.backoff_s = backoff_s
        self._fold_refused: BaseException | None = None
        self._clear()

    def _clear(self) -> None:
        self._root: str | None = None
        self._budgets: dict = {}
        self._requests: dict = {}
        self._attempts: dict = {}
        #: idempotency key -> the request first granted under it
        self._keys: dict = {}
        self._contexts: dict = {}
        #: the latest wall time folded: what the context policy's expiry is
        #: judged against (events.judged_at, D-2026-168)
        self._latest_wall: float | None = None
        self._reset_fold()

    # ---- projection ----------------------------------------------------
    @projection_load
    def load(self) -> "InferenceGateway":
        report, events = self.log.read_verified()
        report.raise_if_bad()
        self._clear()
        for ev in events:
            self.apply(ev)
        self._set_fold_anchor()
        return self

    def apply(self, ev) -> bool:
        """Fold one event. True when it was an inference record."""
        self._folded(ev.seq)
        self._latest_wall = judged_at(self._latest_wall, ev.wall_time)
        p = ev.payload if isinstance(ev.payload, dict) else {}
        a = ev.action
        if a == _ACT_ROOT:
            if self._root is None and p.get("issuer") == ev.actor:
                self._root = ev.actor
            return False
        if a == ACT_CONTEXT_BUILD:
            self._apply_context(ev, p)
            return False
        if a == ACT_BUDGET:
            self._apply_budget(ev, p)
        elif a == ACT_RESERVE:
            self._apply_reserve(ev, p)
        elif a == ACT_DISPATCH:
            self._apply_dispatch(ev, p)
        elif a == ACT_RESULT:
            self._apply_result(ev, p)
        else:
            return False
        return True

    def _apply_context(self, ev, p: dict) -> None:
        rec = p.get("manifest")
        if not isinstance(rec, dict):
            return
        try:
            m = manifest_from_record(rec)
        except ContextError:
            return
        if p.get("manifest_digest") == m.digest():
            self._contexts.setdefault(m.digest(), (ev.seq, m))

    def _apply_budget(self, ev, p: dict, commit: bool = True) -> None:
        if p.get("withdraw"):
            bid = p.get("budget_id")
            st = self._budgets.get(bid)
            if st is None:
                raise InferenceError(f"seq {ev.seq}: withdraws {bid!r}, "
                                     "which was never issued")
            if ev.actor != st.issued_by:
                raise InferenceError(
                    f"seq {ev.seq}: {ev.actor!r} withdraws budget {bid!r}, "
                    f"issued by {st.issued_by!r}; spending authority is "
                    "withdrawn by whoever granted it")
            if commit and st.withdrawn_seq is None:
                st.withdrawn_seq = ev.seq
            return
        b = Budget.from_body(p.get("budget"))
        if p.get("budget_digest") != b.digest():
            raise InferenceError(f"seq {ev.seq}: budget digest mismatch")
        if self._root is None or ev.actor != self._root:
            raise InferenceError(
                f"seq {ev.seq}: {ev.actor!r} issued budget {b.budget_id!r}, "
                f"and the root issuer on this log is {self._root!r}. "
                "Spending authority answers to the root, as minting does")
        prior = self._budgets.get(b.budget_id)
        if prior is not None:
            if prior.budget.digest() == b.digest():
                return
            raise InferenceError(f"seq {ev.seq}: budget {b.budget_id!r} "
                                 "re-issued with different terms")
        if commit:
            self._budgets[b.budget_id] = _BudgetState(b, ev.actor, ev.seq)

    def grant_problem(self, req: InferenceRequest, attempt: int,
                      now: float, at_seq: int) -> tuple | None:
        """(code, reason) the budget refuses this attempt for, or None.

        ``at_seq`` is the last position before the reservation: the head it
        is decided against, and on replay the record before it. Replay asks
        this again of every granted reservation, so a reservation the gate
        would have refused, appended around it, is refused on every load."""
        st = self._budgets.get(req.budget_id)
        if st is None:
            return "BUDGET_UNKNOWN", f"no budget {req.budget_id!r}"
        if st.withdrawn_seq is not None:
            return ("BUDGET_WITHDRAWN", f"budget {req.budget_id!r} was "
                    f"withdrawn at seq {st.withdrawn_seq}")
        b = st.budget
        if req.caller not in b.callers:
            return ("CALLER_NOT_ALLOWED", f"{req.caller!r} may not spend "
                    f"budget {b.budget_id!r}")
        if req.model not in b.models:
            return ("MODEL_NOT_ALLOWED", f"budget {b.budget_id!r} does not "
                    f"cover model {req.model!r}")
        for have, cap, what in (
                (req.prompt_bytes, b.max_input_bytes, "prompt bytes"),
                (req.max_output_tokens, b.max_request_output_tokens,
                 "output tokens"),
                (req.max_output_bytes, b.max_output_bytes, "output bytes"),
                (req.timeout_s, b.max_timeout_s, "timeout seconds"),
                (req.max_attempts, b.max_attempts, "attempts")):
            if have > cap:
                return ("OVER_REQUEST_LIMIT", f"{have} {what} exceeds the "
                        f"per-request bound {cap}")
        if req.context_manifest_digest not in self._contexts:
            return ("CONTEXT_UNKNOWN", "no context.build record has this "
                    "manifest; a call whose context was never recorded "
                    "cannot be reconstructed")
        stale = self._policy_problem(req.context_manifest_digest, now, at_seq)
        if stale is not None:
            return "CONTEXT_STALE", stale
        if attempt > req.max_attempts:
            return ("ATTEMPTS_EXHAUSTED", f"attempt {attempt} of "
                    f"{req.max_attempts}")
        tin, tout = input_bound(req.prompt_bytes), req.max_output_tokens
        cost = cost_micro(b.prices, req.model, tin, tout)
        for held, add, cap, what in (
                (st.calls, 1, b.max_calls, "calls"),
                (st.held_cost, cost, b.max_cost_micro, "cost"),
                (st.held_in, tin, b.max_input_tokens, "input tokens"),
                (st.held_out, tout, b.max_output_tokens, "output tokens")):
            if held + add > cap:
                return ("BUDGET_EXHAUSTED", f"{what}: {held} held and {add} "
                        f"more exceeds {cap}")
        recent = [t for t in st.reserve_times if t > now - b.window_s]
        if len(recent) >= b.max_calls_per_window:
            return ("RATE_LIMITED", f"{len(recent)} calls in the last "
                    f"{b.window_s}s; the window allows "
                    f"{b.max_calls_per_window}")
        return None

    def _next_attempt(self, rid: str) -> tuple:
        """(next attempt number, the open attempt or None, an OK one)."""
        atts = self._requests.get(rid, {})
        open_ = next((a for a in atts.values() if a.result is None), None)
        done = next((a for a in atts.values()
                     if a.result is not None and a.result["outcome"] == OK),
                    None)
        return max(atts, default=0) + 1, open_, done

    def _apply_reserve(self, ev, p: dict, commit: bool = True) -> None:
        req = InferenceRequest.from_body(p.get("request"))
        rid = p.get("request_id")
        if rid != req.request_id or ev.target != rid:
            raise InferenceError(f"seq {ev.seq}: a reservation filed under "
                                 "an id that is not its request's digest")
        if ev.actor == req.caller:
            raise InferenceError(f"seq {ev.seq}: {req.caller!r} reserved its "
                                 "own spending; a caller asks, the gateway "
                                 "decides")
        nxt, open_, done = self._next_attempt(rid)
        n = p.get("attempt")
        if isinstance(n, bool) or n != nxt:
            raise InferenceError(f"seq {ev.seq}: attempt {p.get('attempt')!r}"
                                 f" where the next is {nxt}")
        if not p.get("granted"):
            if p.get("refusal") not in REFUSALS:
                raise InferenceError(f"seq {ev.seq}: refusal "
                                     f"{p.get('refusal')!r} is not one of "
                                     f"{REFUSALS}")
            return
        if open_ is not None or done is not None:
            raise InferenceError(f"seq {ev.seq}: {rid} reserved while "
                                 + ("an attempt is open" if open_ else
                                    "it already has an answer"))
        held = self._keys.get(req.idempotency_key)
        if held not in (None, rid):
            raise InferenceError(f"seq {ev.seq}: {rid} granted under "
                                 f"{req.idempotency_key!r}, which {held} "
                                 "holds: one key, one question")
        why = self.grant_problem(req, nxt, ev.wall_time, ev.seq - 1)
        if why is not None:
            raise InferenceError(f"seq {ev.seq}: a reservation the budget "
                                 f"refuses was recorded as granted: "
                                 f"{why[0]}: {why[1]}")
        b = self._budgets[req.budget_id].budget
        tin, tout = input_bound(req.prompt_bytes), req.max_output_tokens
        cost = cost_micro(b.prices, req.model, tin, tout)
        if p.get("reserved") != {"input_tokens": tin, "output_tokens": tout,
                                 "cost_micro": cost}:
            raise InferenceError(f"seq {ev.seq}: the reservation's amounts "
                                 "are not the request's worst case")
        lease = ev.wall_time + req.timeout_s + LEASE_GRACE_S
        if p.get("lease_until") != lease:
            raise InferenceError(f"seq {ev.seq}: lease_until is not the "
                                 "timeout and grace past the reservation")
        if not commit:
            return
        att = _Attempt(rid, nxt, ev.actor, ev.seq, tin, tout, cost, lease,
                       req.timeout_s)
        self._requests.setdefault(rid, {})[nxt] = att
        self._attempts[(rid, nxt)] = (att, req)
        self._keys.setdefault(req.idempotency_key, rid)
        st = self._budgets[req.budget_id]
        st.calls += 1
        st.held_cost += cost
        st.held_in += tin
        st.held_out += tout
        st.reserve_times.append(ev.wall_time)

    def _held(self, ev, p: dict) -> tuple:
        n = p.get("attempt")
        if isinstance(n, bool) or not isinstance(n, int):
            raise InferenceError(f"seq {ev.seq}: attempt {n!r} is not a "
                                 "number")
        key = (p.get("request_id"), n)
        got = self._attempts.get(key)
        if got is None:
            raise InferenceError(f"seq {ev.seq}: no granted reservation for "
                                 f"{key[0]!r} attempt {key[1]!r}")
        att, req = got
        if ev.actor != att.actor:
            raise InferenceError(f"seq {ev.seq}: {ev.actor!r} acts on an "
                                 f"attempt {att.actor!r} reserved; the "
                                 "gateway that reserved it dispatches and "
                                 "settles it")
        return att, req

    def _apply_dispatch(self, ev, p: dict, commit: bool = True) -> None:
        att, _ = self._held(ev, p)
        if att.dispatch_seq is not None or att.result is not None:
            raise InferenceError(f"seq {ev.seq}: attempt {att.attempt} of "
                                 f"{att.request_id} dispatched twice or "
                                 "after its result")
        if ev.wall_time + att.timeout_s > att.lease_until:
            raise InferenceError(f"seq {ev.seq}: dispatched too late for its "
                                 "lease; recovery may settle it meanwhile")
        if commit:
            att.dispatch_seq = ev.seq

    def _apply_result(self, ev, p: dict, commit: bool = True) -> None:
        att, req = self._held(ev, p)
        if att.result is not None:
            raise InferenceError(f"seq {ev.seq}: attempt {att.attempt} of "
                                 f"{att.request_id} settled twice")
        outcome = p.get("outcome")
        if outcome not in OUTCOMES:
            raise InferenceError(f"seq {ev.seq}: outcome {outcome!r}")
        dispatched = att.dispatch_seq is not None
        if p.get("dispatched") is not dispatched:
            raise InferenceError(f"seq {ev.seq}: the result says dispatched="
                                 f"{p.get('dispatched')!r} and the log says "
                                 f"{dispatched}")
        if outcome in NEEDS_DISPATCH and not dispatched:
            raise InferenceError(f"seq {ev.seq}: {outcome} for an attempt "
                                 "that was never dispatched")
        if outcome in NEVER_DISPATCHED and dispatched:
            raise InferenceError(f"seq {ev.seq}: NOT_SENT for an attempt "
                                 "that was dispatched")
        if outcome in RECOVERY_OUTCOMES and ev.wall_time <= att.lease_until:
            raise InferenceError(f"seq {ev.seq}: {outcome} written while the "
                                 "attempt's lease still held")
        usage = _usage(p.get("usage"))
        if outcome == OK:
            if usage is None or not is_digest(p.get("response_sha256")):
                raise InferenceError(f"seq {ev.seq}: OK without usage or a "
                                     "response")
        b = self._budgets[req.budget_id].budget
        cost, tin, tout, over = settlement(att, outcome, usage, b.prices,
                                           req.model)
        if p.get("cost_micro") != cost or p.get("overrun") is not over:
            raise InferenceError(f"seq {ev.seq}: settled at "
                                 f"{p.get('cost_micro')!r} (overrun "
                                 f"{p.get('overrun')!r}); the rule gives "
                                 f"{cost} ({over})")
        if not commit:
            return
        st = self._budgets[req.budget_id]
        st.held_cost += cost - att.reserved_cost
        st.held_in += tin - att.reserved_in
        st.held_out += tout - att.reserved_out
        att.result = dict(p)
        att.result_seq = ev.seq

    # ---- reads ---------------------------------------------------------
    def root_issuer(self) -> str | None:
        return self._root

    def budget_usage(self, budget_id: str) -> dict:
        st = self._budgets[budget_id]
        return {"calls": st.calls, "cost_micro": st.held_cost,
                "input_tokens": st.held_in, "output_tokens": st.held_out,
                "withdrawn_seq": st.withdrawn_seq}

    def attempts(self, request_id: str) -> tuple:
        """Every attempt of a request: (attempt, outcome or None)."""
        return tuple((n, a.result["outcome"] if a.result else None)
                     for n, a in sorted(self._requests.get(request_id,
                                                           {}).items()))

    def context_recorded(self, manifest_digest: str) -> bool:
        """Is there a context.build record whose manifest has this digest?"""
        self._fold_new()
        return manifest_digest in self._contexts

    def request_for(self, idempotency_key: str) -> InferenceRequest | None:
        """The granted request filed under ``idempotency_key``, if any: what
        a restarted caller resumes instead of asking a new question."""
        self._fold_new()
        for (_rid, _n), (_att, req) in sorted(self._attempts.items()):
            if req.idempotency_key == idempotency_key:
                return req
        return None

    def attempts_under(self, prefix: str) -> int:
        """Granted attempts of every request whose idempotency key starts
        with ``prefix``: what an objective has spent in model calls."""
        self._fold_new()
        return sum(1 for (_a, req) in self._attempts.values()
                   if req.idempotency_key.startswith(prefix))

    def outcome(self, request_id: str) -> Outcome | None:
        """The OK answer to a request, if it has one."""
        self._fold_new()
        _, _, done = self._next_attempt(request_id)
        return None if done is None else self._outcome_of(done)

    def _outcome_of(self, att: _Attempt) -> Outcome:
        r = att.result or {}
        resp = None
        if r.get("outcome") == OK:
            resp = parse_response(self.transcripts.get(r["response_sha256"]))
        if att.result_seq is None:
            raise InferenceError(f"{att.request_id} attempt {att.attempt} "
                                 "has no result yet")
        return Outcome(att.request_id, att.attempt, r.get("outcome", ""),
                       att.result_seq, resp, r.get("response_sha256"),
                       r.get("usage"), r.get("cost_micro", 0),
                       r.get("claimed"), r.get("error") or "")

    # ---- writes --------------------------------------------------------
    def publish_budget(self, budget: Budget, *, actor: str) -> Budget:
        """Grant spending authority. The root issuer only."""
        body = budget.body()

        def decide(head_seq: int) -> dict:
            payload = {"budget": body, "budget_digest": budget.digest()}
            self._apply_budget(_Draft(head_seq + 1, actor, budget.budget_id,
                                      self.clock()), payload, commit=False)
            return {"actor": actor, "action": ACT_BUDGET,
                    "target": budget.budget_id, "payload": payload}

        self._append_decided(decide)
        return budget

    def withdraw_budget(self, budget_id: str, *, actor: str,
                        reason: str) -> None:
        def decide(head_seq: int) -> dict:
            st = self._budgets.get(budget_id)
            if st is not None and st.withdrawn_seq is not None:
                raise InferenceError(f"budget {budget_id!r} is already "
                                     "withdrawn")
            payload = {"budget_id": budget_id, "withdraw": True,
                       "reason": str(reason)[:MAX_ERROR_CHARS]}
            self._apply_budget(_Draft(head_seq + 1, actor, budget_id,
                                      self.clock()), payload, commit=False)
            return {"actor": actor, "action": ACT_BUDGET, "target": budget_id,
                    "payload": payload}

        self._append_decided(decide)

    def _policy_problem(self, manifest_digest: str, now: float,
                        at_seq: int) -> str | None:
        """Why the context's policy is not the one in force at ``at_seq``, or
        None. Asked of the policy store folded to the head, so the answer is
        about the log as it is: under the writer lock when deciding, and
        about the position before the record when replaying it."""
        _, m = self._contexts[manifest_digest]
        identity = m.policy_identity
        pid, sep, _ = identity.rpartition("@")
        if not sep or not pid:
            return (f"the context names policy {identity!r}, which is not "
                    "an identity; its freshness cannot be checked")
        try:
            doc = self.policies.at_head().in_force_at(
                pid, at_seq, at_time=judged_at(self._latest_wall, now))
        except PolicyError as exc:
            return f"the context's policy is not in force: {exc}"
        if doc.identity != identity:
            return (f"the context was built under {identity} and "
                    f"{doc.identity} is in force now")
        return None

    def _reserve(self, req: InferenceRequest) -> _Attempt:
        made: dict = {}

        def decide(head_seq: int) -> dict:
            rid = req.request_id
            held = self._keys.get(req.idempotency_key)
            if held not in (None, rid):
                # two callers resuming one step each built a question; the
                # first granted is the step's, and the second is not asked
                raise KeyHeld(f"{req.idempotency_key!r} is held by {held}: "
                              "the question it names was asked by another "
                              "request")
            nxt, open_, done = self._next_attempt(rid)
            if done is not None:
                made["done"] = done
                raise _Answered()
            if open_ is not None:
                raise AttemptOutstanding(
                    f"{rid} attempt {open_.attempt} is open (reserved by "
                    f"{open_.actor!r} at seq {open_.reserve_seq})")
            now = self.clock()
            why = self.grant_problem(req, nxt, now, head_seq)
            payload: dict = {"request_id": rid, "request": req.body(),
                             "attempt": nxt}
            if why is not None:
                made["refused"] = why
                payload.update(granted=False, refusal=why[0],
                               reason=why[1][:MAX_ERROR_CHARS],
                               reserved=None, lease_until=None)
            else:
                b = self._budgets[req.budget_id].budget
                tin = input_bound(req.prompt_bytes)
                tout = req.max_output_tokens
                payload.update(
                    granted=True, refusal=None, reason="",
                    reserved={"input_tokens": tin, "output_tokens": tout,
                              "cost_micro": cost_micro(b.prices, req.model,
                                                       tin, tout)},
                    lease_until=now + req.timeout_s + LEASE_GRACE_S)
            self._apply_reserve(_Draft(head_seq + 1, self.actor, rid, now),
                                payload, commit=False)
            return {"actor": self.actor, "action": ACT_RESERVE, "target": rid,
                    "payload": payload, "wall_time": now}

        ev = self._append_decided(decide)
        if "refused" in made:
            code, reason = made["refused"]
            raise InferenceRefused(code, reason, ev.seq)
        return self._attempts[(req.request_id, ev.payload["attempt"])][0]

    def _dispatch(self, att: _Attempt) -> None:
        def decide(head_seq: int) -> dict:
            if att.result is not None:
                raise _Settled()
            now = self.clock()
            payload = {"request_id": att.request_id, "attempt": att.attempt,
                       "reserve_seq": att.reserve_seq}
            self._apply_dispatch(_Draft(head_seq + 1, self.actor,
                                        att.request_id, now), payload,
                                 commit=False)
            return {"actor": self.actor, "action": ACT_DISPATCH,
                    "target": att.request_id, "payload": payload,
                    "wall_time": now}

        self._append_decided(decide)

    def _settle(self, att: _Attempt, req: InferenceRequest, outcome: str, *,
                usage=None, response_sha256=None, response_bytes=None,
                claimed=None, error: str = "", latency_ms=None,
                recovery: bool = False) -> int:
        def decide(head_seq: int) -> dict:
            if att.result is not None:
                raise _Settled()
            now = self.clock()
            b = self._budgets[req.budget_id].budget
            cost, _, _, over = settlement(att, outcome, usage, b.prices,
                                          req.model)
            payload = {
                "request_id": att.request_id, "attempt": att.attempt,
                "outcome": outcome,
                "dispatched": att.dispatch_seq is not None,
                "usage": usage, "cost_micro": cost, "overrun": over,
                "response_sha256": response_sha256,
                "response_bytes": response_bytes,
                "claimed": claimed, "error": _scrub(error),
                "latency_ms": latency_ms,
                "settled_by": "recovery" if recovery else "gateway"}
            self._apply_result(_Draft(head_seq + 1, self.actor,
                                      att.request_id, now), payload,
                               commit=False)
            return {"actor": self.actor, "action": ACT_RESULT,
                    "target": att.request_id, "payload": payload,
                    "wall_time": now}

        return self._append_decided(decide).seq

    def _call(self, att: _Attempt, req: InferenceRequest, prompt: bytes,
              cancel: threading.Event) -> tuple:
        """(reply, latency_ms). The provider is bounded by a wall clock here
        whatever it does; an in-process provider that overruns is abandoned
        to its thread, which is why a live one runs in a subprocess.

        The provider is handed its own stop signal, set on the caller's
        cancellation or on the deadline. Setting the CALLER's would cancel
        every later attempt as well as this one."""
        box: dict = {}
        stop = threading.Event()

        def run():
            try:
                box["reply"] = self.provider.call(
                    request=req.body(), prompt=prompt, attempt=att.attempt,
                    timeout_s=req.timeout_s,
                    max_output_bytes=req.max_output_bytes, cancel=stop)
            except Exception as exc:              # noqa: BLE001 - a reply
                box["reply"] = ProviderReply("ERROR",
                                             error=f"{type(exc).__name__}: "
                                                   f"{exc}")

        start = time.monotonic()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        deadline = start + req.timeout_s
        while t.is_alive():
            if cancel.is_set():
                stop.set()
                return ProviderReply(CANCELLED), _ms(start)
            if time.monotonic() >= deadline:
                stop.set()
                return ProviderReply(TIMEOUT), _ms(start)
            t.join(0.02)
        return box["reply"], _ms(start)

    def _classify(self, reply: ProviderReply, req: InferenceRequest) -> dict:
        """The result fields for one reply: outcome, usage, response."""
        claimed = _claimed(reply.claimed)
        out: dict = {"claimed": claimed, "error": reply.error}
        status = reply.status
        if status in (TIMEOUT, CANCELLED, RATE_LIMITED, OUTPUT_TOO_LARGE):
            out["outcome"] = status
            return out
        if status != "OK":
            out["outcome"] = PROVIDER_ERROR
            return out
        body = bytes(reply.body)
        if len(body) > req.max_output_bytes:
            out.update(outcome=OUTPUT_TOO_LARGE,
                       error=f"{len(body)} bytes over the "
                             f"{req.max_output_bytes}-byte bound")
            return out
        usage = _usage(reply.usage)
        try:
            parse_response(body)
        except P.ProposalRefused as exc:
            out.update(outcome=SCHEMA_REFUSED, error=str(exc))
            if not P._CREDENTIAL.search(body.decode("utf-8", "replace")):
                out["response_sha256"] = self.transcripts.put(
                    body, media_type="application/json")
                out["response_bytes"] = len(body)
            return out
        if usage is None:
            out.update(outcome=PROVIDER_ERROR, error="the provider reported "
                       "no usable usage; charged the reservation")
            return out
        out.update(outcome=OK, usage=usage, response_bytes=len(body),
                   response_sha256=self.transcripts.put(
                       body, media_type="application/json"))
        return out

    def infer(self, req: InferenceRequest, prompt: bytes, *,
              cancel: threading.Event | None = None) -> Outcome:
        """Ask, within the budget, until answered or out of attempts.

        Returns the recorded :class:`Outcome` of the last attempt. Raises
        :class:`InferenceRefused` when the budget refuses (and records it),
        :class:`AttemptOutstanding` when another gateway is asking, and
        :class:`KeyHeld` when another request holds the idempotency key."""
        if self.provider.name != self._budget_provider(req.budget_id):
            raise InferenceError(
                f"this gateway dispatches to {self.provider.name!r} and "
                f"budget {req.budget_id!r} is for another provider")
        if digest_bytes(prompt) != req.prompt_sha256 or \
                len(prompt) != req.prompt_bytes:
            raise InferenceError("the prompt is not the one the request "
                                 "names")
        self.transcripts.put(prompt, media_type="application/json")
        cancel = cancel or threading.Event()
        while True:
            try:
                att = self._reserve(req)
            except _Answered:
                done = self.outcome(req.request_id)
                if done is None:
                    raise InferenceError(f"{req.request_id} was answered "
                                         "and its answer cannot be read")
                return done
            if cancel.is_set():
                seq = self._settle(att, req, CANCELLED,
                                   error="cancelled before dispatch")
                return Outcome(req.request_id, att.attempt, CANCELLED, seq)
            try:
                self._dispatch(att)
            except _Settled:
                return self._outcome_of(att)
            reply, ms = self._call(att, req, prompt, cancel)
            fields = self._classify(reply, req)
            try:
                self._settle(att, req, fields["outcome"],
                             usage=fields.get("usage"),
                             response_sha256=fields.get("response_sha256"),
                             response_bytes=fields.get("response_bytes"),
                             claimed=fields.get("claimed"),
                             error=fields.get("error", ""), latency_ms=ms)
            except _Settled:
                pass
            got = self._outcome_of(att)
            if got.outcome not in RETRYABLE or \
                    att.attempt >= req.max_attempts:
                return got
            self.sleep(min(self.backoff_s * 2 ** (att.attempt - 1),
                           MAX_BACKOFF_S))

    def _budget_provider(self, budget_id: str) -> str | None:
        self._fold_new()
        st = self._budgets.get(budget_id)
        return None if st is None else st.budget.provider

    def recover(self) -> list:
        """Settle this gateway's attempts whose leases have run out.

        NOT_SENT where nothing was dispatched, so nothing was spent; LOST
        where something was, charged at the reservation, because what the
        provider did is unknown. Returns the outcomes written."""
        self._fold_new()
        now = self.clock()
        out = []
        for (rid, n), (att, req) in sorted(self._attempts.items()):
            if att.result is not None or att.actor != self.actor or \
                    now <= att.lease_until:
                continue
            outcome = LOST if att.dispatch_seq is not None else NOT_SENT
            try:
                self._settle(att, req, outcome, recovery=True,
                             error="settled by recovery: the gateway that "
                                   "reserved this attempt did not finish it")
            except _Settled:
                continue
            out.append(self._outcome_of(att))
        return out

    # ---- proposals ----------------------------------------------------
    def binding_problems(self, env) -> list:
        """Is a /2 proposal exactly what the recorded response said?

        The inference record must exist and be OK, the response bytes must
        verify, and the proposal's kind, request, rationale and claimed
        source must be the response's. The proposer must be the agent that
        asked."""
        rec = env.record
        inf = rec.get("inference")
        if not isinstance(inf, dict):
            return ["the proposal names no inference record"]
        self._fold_new()
        n = inf.get("attempt")
        if isinstance(n, bool) or not isinstance(n, int):
            return ["the proposal names no attempt"]
        got = self._attempts.get((inf.get("request_id"), n))
        if got is None:
            return ["no granted reservation for the inference it names"]
        att, req = got
        r = att.result
        if r is None or r.get("outcome") != OK:
            return ["the inference it names did not end OK"]
        out = []
        if r.get("response_sha256") != inf.get("response_sha256"):
            out.append("the proposal names a response the log does not")
        if req.context_manifest_digest != inf.get("context_manifest_digest"):
            out.append("the proposal names another context")
        if req.caller != env.agent_id:
            out.append(f"the proposal is {env.agent_id!r}'s and the call "
                       f"was {req.caller!r}'s")
        try:
            resp = parse_response(self.transcripts.get(r["response_sha256"]))
        except Exception as exc:                  # noqa: BLE001 - a finding
            return out + [f"the response does not verify: {exc}"]
        if resp["kind"] != rec["kind"] or resp["request"] != rec["request"]:
            out.append("the proposal asks for something the response did not")
        if digest(resp["rationale"]) != rec["rationale_sha256"]:
            out.append("the proposal's rationale is not the response's")
        src = rec["source"]
        if src["provider"] != resp["provider"] or \
                src["model"] != resp["model"]:
            out.append("the proposal claims another source than the "
                       "response did")
        return out


class _Answered(Exception):
    """The request already has an OK answer: nothing is reserved."""


class _Settled(Exception):
    """The attempt was settled by someone else first."""


def _ms(start: float) -> int:
    return int(math.ceil((time.monotonic() - start) * 1000))


def envelope_from_inference(outcome: Outcome, *, context: dict,
                            agent_id: str, manifest_digest: str,
                            parent_proposal_id: str | None = None,
                            iteration: int = 0) -> dict:
    """A sealed proposal-envelope/2 from an OK inference.

    ``context`` is the retrieval context (:func:`proposals.assemble_context`)
    the proposal cites, as in /1. ``manifest_digest`` is the context manifest
    the prompt was rendered from, which the inference request names; an
    ingress checks that it does."""
    if not outcome.ok or outcome.response is None:
        raise P.ProposalRefused("only an OK inference becomes a proposal")
    rec = P.envelope_record(outcome.response, context=context,
                            agent_id=agent_id, adapter_name=ADAPTER,
                            adapter_version="1",
                            parent_proposal_id=parent_proposal_id,
                            iteration=iteration)
    rec["schema"] = P.SCHEMA_V2
    rec["inference"] = {"request_id": outcome.request_id,
                        "attempt": outcome.attempt,
                        "response_sha256": outcome.response_sha256,
                        "context_manifest_digest": manifest_digest}
    sealed = P.seal(rec)
    P.ProposalEnvelope.parse(sealed)
    return sealed
