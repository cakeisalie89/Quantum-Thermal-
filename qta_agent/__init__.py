"""Agent authority substrate: durable state, provenance, fail-closed authority.

The scientific stack answers "what did the model compute?". This package
answers the questions that surround it and that a long-running agent cannot be
trusted to answer about itself:

  * what is canonical right now, and who made it so?
  * what evidence supports that, and has the evidence changed?
  * if a parameter moved, what silently stopped being true?
  * if the process died mid-promotion, what is the state?
  * can any of it be rebuilt without trusting the running agent?

Layering, lowest first. Each layer depends only on those above it in this
list, so the dependency graph is a line rather than a web:

  canonical      one byte representation, therefore one digest
  events         append-only hash-chained log; the authority history
  evidence       content-addressed store; what a cited digest resolves to
  authority      the transition table; what may become canonical
  store          live projection, transactional through the log
  invalidation   transitive consequence of a change
  reconstruct    a SECOND implementation, for differential verification

The log is the truth and everything else is derived from it. That inversion is
the design: a lost projection costs time, never authority.

This layer is infrastructure, not science. ``automatic_gate_effect = NONE``:
nothing here is imported by the solvers, by ``qta_full_sim.py``, or by
``metrics.py``, it cannot read or write any of the 83 gates, and PASS = 0 is
unaffected by anything it does. It has no production caller -- its only
consumers are its own tests. See ``AGENT_SUBSTRATE.md``.
"""
from __future__ import annotations

__all__ = [
    "canonical", "events", "evidence", "authority", "store", "invalidation",
    "reconstruct",
]

#: Substrate contract version. Bumping this signals that persisted logs or
#: digests from an earlier version are not directly comparable.
SUBSTRATE_VERSION = 1
