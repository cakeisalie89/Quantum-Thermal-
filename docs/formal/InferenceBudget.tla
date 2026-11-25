--------------------------- MODULE InferenceBudget ---------------------------
(***************************************************************************)
(* Budget reservation and settlement (Assurance B, protocol 3).            *)
(*                                                                         *)
(* agent_substrate.inference.InferenceGateway: a model call is RESERVED at *)
(* its worst-case cost before anything is sent, DISPATCHED only while its  *)
(* lease still covers the whole call, and SETTLED once -- by the gateway   *)
(* that made it, or, after the lease, by recovery (NOT_SENT if it was      *)
(* never dispatched, LOST if it was). Each action is one decision under    *)
(* the writer lock against the log, so a gateway process holds nothing     *)
(* the model must track: a crash is a gateway that stops taking actions,   *)
(* and only recovery settles what it left.                                 *)
(*                                                                         *)
(* Time is in ticks. tests/test_formal_conformance.py replays traces of    *)
(* this model against the gateway with one tick = LEASE_GRACE_S = the      *)
(* request timeout, so T = G = 1 here is the gateway's own arithmetic.     *)
(*                                                                         *)
(* NOT MODELLED: rate windows, token accounting beyond cost, the policy    *)
(* and context checks (protocol 6), and what the provider says.            *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Reqs, MaxAttempts, Reserve_, Usage, Cap, MaxTime, Bug

ASSUME Bug \in {"none", "check_held_only", "double_dispatch", "late_dispatch",
                "recover_ignores_lease", "no_recovery"}
ASSUME Usage <= Reserve_

T == 1   \* the request's timeout, in ticks
G == 1   \* LEASE_GRACE_S, in ticks

Attempts == 1..MaxAttempts
Open == {"reserved", "dispatched"}

VARIABLES phase, outcome, lease, deadline, dispatches, inflight, charged,
          held, now, last

vars == <<phase, outcome, lease, deadline, dispatches, inflight, charged,
          held, now, last>>

\* Per-attempt state is a function of the request, then the attempt number.
Each(v) == [r \in Reqs |-> [n \in Attempts |-> v]]

Init ==
  /\ phase = Each("none")
  /\ outcome = Each("none")
  /\ lease = Each(0)
  /\ deadline = Each(0)
  /\ dispatches = Each(0)
  /\ inflight = Each(FALSE)
  /\ charged = Each(0)
  /\ held = 0
  /\ now = 0
  /\ last = <<"Init">>

Used(r) == {n \in Attempts : phase[r][n] # "none"}
NextAttempt(r) == Cardinality(Used(r)) + 1
HasOpen(r) == \E n \in Attempts : phase[r][n] \in Open
Answered(r) == \E n \in Attempts : outcome[r][n] = "OK"

\* The budget check: what is held plus this reservation. check_held_only is
\* IG8's mutation -- it asks only whether what is already held fits.
Fits == IF Bug = "check_held_only" THEN held <= Cap
        ELSE held + Reserve_ <= Cap

Reserve(r) ==
  LET n == NextAttempt(r) IN
  /\ n <= MaxAttempts
  /\ ~HasOpen(r) /\ ~Answered(r)
  /\ Fits
  \* a modelling bound, not a rule: every lease ends inside the horizon
  /\ now + T + G < MaxTime
  /\ phase' = [phase EXCEPT ![r][n] = "reserved"]
  /\ lease' = [lease EXCEPT ![r][n] = now + T + G]
  /\ held' = held + Reserve_
  /\ last' = <<"Reserve", r, n>>
  /\ UNCHANGED <<outcome, deadline, dispatches, inflight, charged, now>>

Dispatch(r, n) ==
  /\ IF Bug = "double_dispatch" THEN phase[r][n] \in Open
     ELSE phase[r][n] = "reserved"
  /\ Bug # "late_dispatch" => now + T <= lease[r][n]
  /\ phase' = [phase EXCEPT ![r][n] = "dispatched"]
  /\ dispatches' = [dispatches EXCEPT ![r][n] = @ + 1]
  /\ inflight' = [inflight EXCEPT ![r][n] = TRUE]
  /\ deadline' = [deadline EXCEPT ![r][n] = now + T]
  /\ last' = <<"Dispatch", r, n>>
  /\ UNCHANGED <<outcome, lease, charged, held, now>>

\* The provider answers, or the worker is killed at its deadline.
CallEnds(r, n) ==
  /\ inflight[r][n]
  /\ inflight' = [inflight EXCEPT ![r][n] = FALSE]
  /\ last' = <<"CallEnds", r, n>>
  /\ UNCHANGED <<phase, outcome, lease, deadline, dispatches, charged, held,
                 now>>

\* The settlement rule: never dispatched costs nothing, OK costs its usage,
\* anything else dispatched costs its whole reservation.
Cost(r, n, o) == IF phase[r][n] = "reserved" THEN 0
                 ELSE IF o = "OK" THEN Usage ELSE Reserve_

Close(r, n, o) ==
  /\ phase' = [phase EXCEPT ![r][n] = "settled"]
  /\ outcome' = [outcome EXCEPT ![r][n] = o]
  /\ charged' = [charged EXCEPT ![r][n] = Cost(r, n, o)]
  /\ held' = held - Reserve_ + Cost(r, n, o)

\* The gateway that made the call settles it once the call has ended.
Settle(r, n, o) ==
  /\ phase[r][n] \in Open /\ ~inflight[r][n]
  /\ IF o = "CANCELLED" THEN TRUE ELSE phase[r][n] = "dispatched"
  /\ Close(r, n, o)
  /\ last' = <<"Settle", r, n, o>>
  /\ UNCHANGED <<lease, deadline, dispatches, inflight, now>>

\* Recovery settles an attempt whose lease has passed.
Recover(r, n) ==
  /\ Bug # "no_recovery"
  /\ phase[r][n] \in Open
  /\ Bug # "recover_ignores_lease" => now > lease[r][n]
  /\ Close(r, n, IF phase[r][n] = "reserved" THEN "NOT_SENT" ELSE "LOST")
  /\ last' = <<"Recover", r, n>>
  /\ UNCHANGED <<lease, deadline, dispatches, inflight, now>>

\* Time passes; a call in flight ends by its deadline, so time does not pass
\* it.
Tick ==
  /\ now < MaxTime
  /\ \A r \in Reqs, n \in Attempts : inflight[r][n] => deadline[r][n] > now
  /\ now' = now + 1
  /\ last' = <<"Tick">>
  /\ UNCHANGED <<phase, outcome, lease, deadline, dispatches, inflight,
                 charged, held>>

Next ==
  \/ \E r \in Reqs : Reserve(r)
  \/ \E r \in Reqs, n \in Attempts :
        \/ Dispatch(r, n) \/ CallEnds(r, n) \/ Recover(r, n)
        \/ \E o \in {"OK", "ERROR", "CANCELLED"} : Settle(r, n, o)
  \/ Tick
  \/ (now = MaxTime /\ UNCHANGED vars)

\* Fairness: time passes, a call ends, and recovery runs. The gateway's own
\* settlement is NOT fair -- it may crash and never settle.
Spec == Init /\ [][Next]_vars
        /\ WF_vars(Tick)
        /\ \A r \in Reqs, n \in Attempts :
              WF_vars(CallEnds(r, n)) /\ WF_vars(Recover(r, n))

NeverOverspent == held <= Cap

AtMostOneOpen ==
  \A r \in Reqs : Cardinality({n \in Attempts : phase[r][n] \in Open}) <= 1

AtMostOneOK ==
  \A r \in Reqs : Cardinality({n \in Attempts : outcome[r][n] = "OK"}) <= 1

DispatchedOnce == \A r \in Reqs, n \in Attempts : dispatches[r][n] <= 1

\* No attempt is settled while its call is in flight -- settled as LOST, or
\* NOT_SENT, while the provider may still be answering and billing it.
NothingSettledInFlight ==
  \A r \in Reqs, n \in Attempts : phase[r][n] = "settled" => ~inflight[r][n]

\* What is held is what the attempts say.
Slots == Reqs \X Attempts
HeldIsTheLedger ==
  held = LET Sum[S \in SUBSET Slots] ==
               IF S = {} THEN 0
               ELSE LET s == CHOOSE x \in S : TRUE IN
                    (IF phase[s[1]][s[2]] \in Open THEN Reserve_
                     ELSE charged[s[1]][s[2]])
                    + Sum[S \ {s}]
         IN Sum[Slots]

\* Liveness: every attempt reserved is eventually settled.
EverySettled ==
  \A r \in Reqs, n \in Attempts :
    phase[r][n] \in Open ~> phase[r][n] = "settled"
=============================================================================
