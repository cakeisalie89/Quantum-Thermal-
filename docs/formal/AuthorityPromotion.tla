-------------------------- MODULE AuthorityPromotion --------------------------
(***************************************************************************)
(* Authority promotion and separation of duties (Assurance B, protocol 1). *)
(*                                                                         *)
(* One record, any actor claiming any role, and a policy that its owner    *)
(* may withdraw. The record moves only along the edges of                  *)
(* agent_substrate.authority.EDGES, which the block between BEGIN EDGES    *)
(* and END EDGES restates and tests/test_formal_conformance.py compares    *)
(* with the Python table, edge for edge.                                   *)
(*                                                                         *)
(* NOT MODELLED: evidence (I6, the digest form of what an edge cites, is   *)
(* the unit tests'), the result-admission rules of a scientific record,    *)
(* and how an actor comes to hold a role. Any actor may claim any role     *)
(* here, so separation of duties is the only thing between a proposer and  *)
(* its own promotion -- the strongest adversary this model can state.      *)
(*                                                                         *)
(* Bug selects a deliberately broken variant, so each invariant is shown   *)
(* to be able to fail (tools/formal_check.py expects each to be violated). *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Actors, Bug

ASSUME Bug \in {"none", "self_verify", "review_promotes", "withdrawn_policy"}

Roles == {"PROPOSER", "VERIFIER", "PROMOTER", "SYSTEM"}
States == {"PROPOSED", "UNDER_REVIEW", "VERIFIED", "PROMOTED", "STALE",
           "SUPERSEDED", "REVOKED", "REJECTED"}
Terminal == {"REVOKED", "REJECTED"}

\* BEGIN EDGES
Edges == {
  [src |-> "PROPOSED",     dst |-> "UNDER_REVIEW", roles |-> {"VERIFIER"}, distinct |-> TRUE],
  [src |-> "PROPOSED",     dst |-> "REJECTED",     roles |-> {"VERIFIER"}, distinct |-> TRUE],
  [src |-> "UNDER_REVIEW", dst |-> "VERIFIED",     roles |-> {"VERIFIER"}, distinct |-> TRUE],
  [src |-> "UNDER_REVIEW", dst |-> "REJECTED",     roles |-> {"VERIFIER"}, distinct |-> TRUE],
  [src |-> "VERIFIED",     dst |-> "PROMOTED",     roles |-> {"PROMOTER"}, distinct |-> TRUE],
  [src |-> "VERIFIED",     dst |-> "STALE",        roles |-> {"SYSTEM"},   distinct |-> FALSE],
  [src |-> "PROMOTED",     dst |-> "STALE",        roles |-> {"SYSTEM"},   distinct |-> FALSE],
  [src |-> "PROMOTED",     dst |-> "SUPERSEDED",   roles |-> {"PROMOTER"}, distinct |-> FALSE],
  [src |-> "PROMOTED",     dst |-> "REVOKED",      roles |-> {"PROMOTER"}, distinct |-> FALSE],
  [src |-> "VERIFIED",     dst |-> "REVOKED",      roles |-> {"PROMOTER"}, distinct |-> FALSE],
  [src |-> "STALE",        dst |-> "UNDER_REVIEW", roles |-> {"VERIFIER"}, distinct |-> TRUE],
  [src |-> "STALE",        dst |-> "SUPERSEDED",   roles |-> {"PROMOTER"}, distinct |-> FALSE],
  [src |-> "STALE",        dst |-> "REVOKED",      roles |-> {"PROMOTER"}, distinct |-> FALSE],
  [src |-> "SUPERSEDED",   dst |-> "REVOKED",      roles |-> {"PROMOTER"}, distinct |-> FALSE]
}
\* END EDGES

\* The broken variants: a verifier who is also the proposer, an edge that
\* promotes straight from review, and a promotion that ignores the policy.
ModelEdges ==
  IF Bug = "review_promotes"
  THEN Edges \cup {[src |-> "UNDER_REVIEW", dst |-> "PROMOTED",
                    roles |-> {"VERIFIER"}, distinct |-> TRUE]}
  ELSE Edges

Distinct(e) ==
  IF Bug = "self_verify" /\ e.dst = "VERIFIED" THEN FALSE ELSE e.distinct

VARIABLES state, proposer, verifier, promoter, promotedFrom, promotedUnder,
          policy, last

vars == <<state, proposer, verifier, promoter, promotedFrom, promotedUnder,
          policy, last>>

Init ==
  /\ state = "NONE"
  /\ proposer = "none" /\ verifier = "none" /\ promoter = "none"
  /\ promotedFrom = "none" /\ promotedUnder = "none"
  /\ policy = "in_force"
  /\ last = <<"Init">>

Create(a) ==
  /\ state = "NONE"
  /\ state' = "PROPOSED" /\ proposer' = a
  /\ last' = <<"Create", a>>
  /\ UNCHANGED <<verifier, promoter, promotedFrom, promotedUnder, policy>>

Transition(a, r, e) ==
  /\ state = e.src
  /\ state \notin Terminal
  /\ r \in e.roles
  /\ Distinct(e) => a # proposer
  \* I5 and D-2026-148: a promotion names a policy in force
  /\ e.dst = "PROMOTED" /\ Bug # "withdrawn_policy" => policy = "in_force"
  /\ state' = e.dst
  /\ verifier' = IF e.dst = "VERIFIED" THEN a ELSE verifier
  /\ promoter' = IF e.dst = "PROMOTED" THEN a ELSE promoter
  /\ promotedFrom' = IF e.dst = "PROMOTED" THEN e.src ELSE promotedFrom
  /\ promotedUnder' = IF e.dst = "PROMOTED" THEN policy ELSE promotedUnder
  /\ last' = <<"Transition", a, r, e.src, e.dst>>
  /\ UNCHANGED <<proposer, policy>>

Withdraw ==
  /\ policy = "in_force"
  /\ policy' = "withdrawn"
  /\ last' = <<"Withdraw">>
  /\ UNCHANGED <<state, proposer, verifier, promoter, promotedFrom,
                 promotedUnder>>

Next ==
  \/ \E a \in Actors : Create(a)
  \/ \E a \in Actors, r \in Roles, e \in ModelEdges : Transition(a, r, e)
  \/ Withdraw

Spec == Init /\ [][Next]_vars

TypeOK ==
  /\ state \in States \cup {"NONE"}
  /\ policy \in {"in_force", "withdrawn"}

\* I4: nobody verifies or promotes a record they proposed
SeparationOfDuties ==
  /\ state \in {"VERIFIED", "PROMOTED"} => verifier \notin {"none", proposer}
  /\ state = "PROMOTED" => promoter \notin {"none", proposer}

\* I1: PROMOTED is entered from VERIFIED and nowhere else
PromotedOnlyFromVerified ==
  state = "PROMOTED" => promotedFrom = "VERIFIED"

\* D-2026-148: a withdrawn policy authorizes nothing
PromotedUnderAPolicyInForce ==
  state = "PROMOTED" => promotedUnder = "in_force"

\* I2: a terminal state is never left
TerminalIsFinal == [][(state \in Terminal) => (state' = state)]_vars
=============================================================================
