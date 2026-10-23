----------------------------- MODULE Invalidation -----------------------------
(***************************************************************************)
(* Dependency invalidation (Assurance B, protocol 5).                      *)
(*                                                                         *)
(* agent_substrate.governed_model: a governed task's verdict is withdrawn  *)
(* (the task moves to INVALIDATED -- one durable append), then what rested *)
(* on it is made STALE, transitively, by agent_substrate.invalidation:     *)
(* the plan is computed in full, then applied one record at a time, each a *)
(* separate append. A process can die between any two of them; its plan   *)
(* was in memory. ``settle()`` follows every INVALIDATED task again,       *)
(* citing itself, so a cascade cut short is finished by whoever settles    *)
(* next. A verifier may re-verify a STALE record meanwhile (nothing in the *)
(* store asks about its origin), and the next settle makes it STALE again. *)
(*                                                                         *)
(* The traversal passes THROUGH records that cannot go stale (a SUPERSEDED *)
(* record's dependents may still stand) and marks only VERIFIED and        *)
(* PROMOTED records, the states with an edge to STALE.                     *)
(*                                                                         *)
(* NOT MODELLED: evidence, how a root is found (a record citing an        *)
(* artefact the task captured), and record-origin invalidation, which has *)
(* no settle of its own (the audit reports a half-applied one).            *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Records, Deps, Roots, Initial, MaxCrash, MaxReverify, Bug

ASSUME Bug \in {"none", "direct_only", "stop_at_skipped", "no_settle",
                "overeager"}

Standing == {"VERIFIED", "PROMOTED"}

\* planned and due: what the last plan held, and what it owed -- everything
\* resting on the origin that was standing when it was made.
VARIABLES origin, st, pending, planned, due, crashes, reverified, last

vars == <<origin, st, pending, planned, due, crashes, reverified, last>>

Dependents(S) == {r \in Records : Deps[r] \cap S # {}}

\* Everything resting on the origin, transitively, through any record.
RECURSIVE Closure(_)
Closure(S) == IF Dependents(S) \subseteq S THEN S
              ELSE Closure(S \cup Dependents(S))
Reach == Closure(Roots)

\* The broken traversals: one level only, and one that stops at a record
\* that cannot go stale itself.
RECURSIVE ClosureThroughStanding(_)
ClosureThroughStanding(S) ==
  LET more == {r \in Dependents(S) : \E d \in Deps[r] \cap S :
                 d \in Roots \/ st[d] \in Standing \/ st[d] = "STALE"}
  IN IF more \subseteq S THEN S ELSE ClosureThroughStanding(S \cup more)

Planned ==
  CASE Bug = "direct_only"     -> Roots \cup Dependents(Roots)
    [] Bug = "stop_at_skipped" -> ClosureThroughStanding(Roots)
    [] Bug = "overeager"       -> Records
    [] OTHER                   -> Reach

Plan == {r \in Planned : st[r] \in Standing}
Due == {r \in Reach : st[r] \in Standing}

Init ==
  /\ origin = "VALID"
  /\ st = Initial
  /\ pending = {}
  /\ planned = {} /\ due = {}
  /\ crashes = 0
  /\ reverified = 0
  /\ last = <<"Init">>

InvalidateOrigin ==
  /\ origin = "VALID"
  /\ origin' = "INVALIDATED"
  /\ pending' = Plan /\ planned' = Plan /\ due' = Due
  /\ last' = <<"InvalidateOrigin">>
  /\ UNCHANGED <<st, crashes, reverified>>

Mark(r) ==
  /\ r \in pending
  /\ st' = IF st[r] \in Standing THEN [st EXCEPT ![r] = "STALE"] ELSE st
  /\ pending' = pending \ {r}
  /\ last' = <<"Mark", r>>
  /\ UNCHANGED <<origin, planned, due, crashes, reverified>>

\* The follower dies; its plan was in memory.
Crash ==
  /\ pending # {} /\ crashes < MaxCrash
  /\ pending' = {}
  /\ crashes' = crashes + 1
  /\ last' = <<"Crash">>
  /\ UNCHANGED <<origin, st, planned, due, reverified>>

Settle ==
  /\ Bug # "no_settle"
  /\ origin = "INVALIDATED" /\ pending = {} /\ Plan # {}
  /\ pending' = Plan /\ planned' = Plan /\ due' = Due
  /\ last' = <<"Settle">>
  /\ UNCHANGED <<origin, st, crashes, reverified>>

Reverify(r) ==
  /\ st[r] = "STALE" /\ reverified < MaxReverify
  /\ st' = [st EXCEPT ![r] = "VERIFIED"]
  /\ reverified' = reverified + 1
  /\ last' = <<"Reverify", r>>
  /\ UNCHANGED <<origin, pending, planned, due, crashes>>

Next ==
  \/ InvalidateOrigin \/ Crash \/ Settle
  \/ \E r \in Records : Mark(r) \/ Reverify(r)
  \/ (pending = {} /\ ~ENABLED Settle /\ UNCHANGED vars)

\* Fairness: a follower that has a plan carries it out, and settling runs.
\* Neither the crash nor the re-verification is fair: they are adversaries.
Spec == Init /\ [][Next]_vars
        /\ \A r \in Records : WF_vars(Mark(r))
        /\ WF_vars(Settle)

\* Nothing stands on a withdrawn origin.
Consistent ==
  origin = "INVALIDATED" => \A r \in Reach : st[r] \notin Standing

\* Safety: every plan reaches everything resting on the withdrawn origin
\* that is still standing -- through records that cannot go stale too.
PlanIsComplete == due \subseteq planned

\* Liveness: once the origin is withdrawn, consistency is reached and kept.
EventuallyConsistent == <>[]Consistent

\* Safety: only what rests on the withdrawn origin is ever made STALE, and
\* only from a state with an edge to STALE.
JustifiedStaleness ==
  [][\A r \in Records :
       (st'[r] = "STALE" /\ st[r] # "STALE") =>
         (r \in Reach /\ origin' = "INVALIDATED" /\ st[r] \in Standing)]_vars
=============================================================================
