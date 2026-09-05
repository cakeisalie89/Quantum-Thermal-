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
  actions        every durable action name, and which reducer owns it
  events         append-only hash-chained log; the authority history
  evidence       content-addressed store; what a cited digest resolves to
  capability     authority as a bounded object, not an ambient flag
  tools          tool contracts and a default-deny registry
  execution      kernel-bounded execution; a timeout is not a success
  checkpoint     a cached verification result, never a second truth
  authority      the transition table; what may become canonical
  policy         versioned rules, deny-overrides, decisions that outlive them
  secrets        references that travel, values that do not
  netauth        egress as a bounded grant; the default is no network
  store          live projection, transactional through the log
  invalidation   transitive consequence of a change
  reconstruct    a SECOND implementation, for differential verification
  tasks          durable work: state that survives the process that started it
  scheduler      the durable queue: readiness, leases, retry, cancellation
  memory         what the agent remembers, kept apart from what is evidence
  context        what the agent was shown, kept apart from what is true
  agents         who is participating, and which separations they cannot cross
  audit          turning the log into answers, and finding provenance holes

The log is the truth and everything else is derived from it. That inversion is
the design: a lost projection costs time, never authority.

This layer is infrastructure, not science. ``automatic_gate_effect = NONE``:
nothing here is imported by the solvers, by ``qta_full_sim.py``, or by
``metrics.py``, it cannot read or write any of the 83 gates, and PASS = 0 is
unaffected by anything it does.

It DOES have a production caller: ``governed_stage10`` runs a real Stage-10
workflow through the whole chain, and the Snakemake rule ``s10_governed``
fails the build unless that run reaches VERIFIED with no provenance gaps. The
subsystems above it in this list -- policy, scheduler, secrets, netauth,
memory, context, agents -- are implemented and tested but are NOT yet on that
path;
``docs/completion_matrix.json`` is the authority for which is which. See
``AGENT_SUBSTRATE.md``.
"""
from __future__ import annotations

__all__ = [
    "canonical", "actions", "events", "evidence", "capability", "tools",
    "execution", "checkpoint", "authority", "policy", "secrets", "netauth",
    "store",
    "invalidation", "reconstruct", "tasks", "scheduler", "memory", "context",
    "agents", "audit",
]

#: Substrate contract version. Bumping this signals that persisted logs or
#: digests from an earlier version are not directly comparable.
SUBSTRATE_VERSION = 1
