--------------------------- MODULE ContextFreshness ---------------------------
(***************************************************************************)
(* Context and policy freshness at decision time (Assurance B, protocol 6).*)
(*                                                                         *)
(* A context is built under the policy in force when it is built, and      *)
(* names that policy. agent_substrate.inference.InferenceGateway decides a *)
(* reservation against the policy store folded to the head, under the     *)
(* writer lock, in the same step that writes the reservation: a context    *)
(* whose policy has since been superseded or withdrawn is refused. The     *)
(* point of deciding under the lock is that nothing can change between the *)
(* check and the write; the check_then_act variant shows what that buys.   *)
(*                                                                         *)
(* NOT MODELLED: what a policy says (only which version is in force),      *)
(* policy expiry by time (not_after), and the budget (protocol 3).         *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS MaxVersion, Bug

ASSUME Bug \in {"none", "check_at_build", "check_then_act",
                "ignore_withdrawal"}

Versions == 1..MaxVersion

VARIABLES version, withdrawn, contexts, decisions, pc, checked, last

vars == <<version, withdrawn, contexts, decisions, pc, checked, last>>

Init ==
  /\ version = 1
  /\ withdrawn = FALSE
  /\ contexts = {}
  /\ decisions = {}
  /\ pc = "idle"
  /\ checked = 0
  /\ last = <<"Init">>

\* A new version supersedes the one in force. A withdrawal is permanent:
\* PolicyStore refuses to publish a withdrawn policy again.
Publish ==
  /\ version < MaxVersion /\ ~withdrawn
  /\ version' = version + 1
  /\ last' = <<"Publish", version + 1>>
  /\ UNCHANGED <<withdrawn, contexts, decisions, pc, checked>>

Withdraw ==
  /\ ~withdrawn
  /\ withdrawn' = TRUE
  /\ last' = <<"Withdraw", version>>
  /\ UNCHANGED <<version, contexts, decisions, pc, checked>>

Build ==
  /\ version \notin contexts
  /\ contexts' = contexts \cup {version}
  /\ last' = <<"Build", version>>
  /\ UNCHANGED <<version, withdrawn, decisions, pc, checked>>

\* What the decision asks of the policy in force.
InForce(c) ==
  CASE Bug = "check_at_build"    -> TRUE
    [] Bug = "ignore_withdrawal" -> c = version
    [] OTHER                     -> c = version /\ ~withdrawn

Record(c) == [ctx |-> c, at |-> version, withdrawn |-> withdrawn]

\* Decided and written in one step, under the writer lock.
Decide(c) ==
  /\ Bug # "check_then_act"
  /\ c \in contexts /\ InForce(c)
  /\ Record(c) \notin decisions
  /\ decisions' = decisions \cup {Record(c)}
  /\ last' = <<"Decide", c>>
  /\ UNCHANGED <<version, withdrawn, contexts, pc, checked>>

\* The broken alternative: check, let the world move, then write.
Check(c) ==
  /\ Bug = "check_then_act" /\ pc = "idle"
  /\ c \in contexts /\ InForce(c)
  /\ pc' = "checked" /\ checked' = c
  /\ last' = <<"Check", c>>
  /\ UNCHANGED <<version, withdrawn, contexts, decisions>>

Act ==
  /\ pc = "checked"
  /\ decisions' = decisions \cup {Record(checked)}
  /\ pc' = "idle"
  /\ last' = <<"Act", checked>>
  /\ UNCHANGED <<version, withdrawn, contexts, checked>>

Next ==
  \/ Publish \/ Withdraw \/ Build \/ Act
  \/ \E c \in Versions : Decide(c) \/ Check(c)

Spec == Init /\ [][Next]_vars

\* Every decision was made on a context whose policy was in force, and not
\* withdrawn, when the decision was written.
DecidedOnlyWhenFresh ==
  \A d \in decisions : d.ctx = d.at /\ ~d.withdrawn
=============================================================================
