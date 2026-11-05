------------------------------ MODULE LogAppend ------------------------------
(***************************************************************************)
(* Concurrent event-log append (Assurance B, protocol 2).                  *)
(*                                                                         *)
(* agent_substrate.events.EventLog.append_decided: take the writer lock,   *)
(* read the verified head, decide, write one line naming the head as its   *)
(* predecessor, release. A process may crash anywhere: while it holds the  *)
(* lock (the operating system releases a flock with its holder), midway    *)
(* through a write (a torn final line), or after a complete write but      *)
(* before it learns so (and will try the same intent again). A torn tail   *)
(* fails closed: the head check refuses it, and only an explicit repair    *)
(* truncates it -- nothing appends over it.                                *)
(*                                                                         *)
(* Each process has intents to record; ``decide`` refuses an intent the    *)
(* log already holds, which is how every reducer here makes a retry        *)
(* idempotent.                                                             *)
(*                                                                         *)
(* NOT MODELLED: the hash itself (``prev`` is the predecessor's position,  *)
(* which is what the hash commits to), signatures, the head pointer file,  *)
(* and a filesystem on which flock does not exclude (NFS; the module       *)
(* docstring of events.py says so).                                        *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Procs, PerProc, MaxCrash, Bug

ASSUME Bug \in {"none", "no_lock", "append_over_torn", "no_idempotent_decide",
                "no_repair"}

\* What each process has to record: PerProc intents of its own.
Intents == [p \in Procs |-> {<<p, n>> : n \in 1..PerProc}]

VARIABLES log, lock, pc, view, dec, todo, crashes, last

vars == <<log, lock, pc, view, dec, todo, crashes, last>>

Recorded(i) == \E k \in 1..Len(log) : log[k].intent = i /\ ~log[k].torn
TornTail == Len(log) > 0 /\ log[Len(log)].torn
Locking == Bug # "no_lock"

Init ==
  /\ log = <<>>
  /\ lock = "free"
  /\ pc = [p \in Procs |-> "idle"]
  /\ view = [p \in Procs |-> 0]
  /\ dec = [p \in Procs |-> <<"none", 0>>]
  /\ todo = Intents
  /\ crashes = [p \in Procs |-> 0]
  /\ last = <<"Init">>

Acquire(p) ==
  /\ pc[p] = "idle" /\ todo[p] # {}
  /\ Locking => lock = "free"
  /\ lock' = IF Locking THEN p ELSE lock
  /\ pc' = [pc EXCEPT ![p] = "locked"]
  /\ last' = <<"Acquire", p>>
  /\ UNCHANGED <<log, view, dec, todo, crashes>>

Release(p) == IF Locking THEN "free" ELSE lock

\* The head check fails closed on a torn tail: nothing is decided.
Refuse(p) ==
  /\ pc[p] = "locked" /\ TornTail /\ Bug # "append_over_torn"
  /\ lock' = Release(p)
  /\ pc' = [pc EXCEPT ![p] = "idle"]
  /\ last' = <<"Refuse", p>>
  /\ UNCHANGED <<log, view, dec, todo, crashes>>

Decide(p, i) ==
  /\ pc[p] = "locked" /\ i \in todo[p]
  /\ ~TornTail \/ Bug = "append_over_torn"
  /\ IF Recorded(i) /\ Bug # "no_idempotent_decide"
     THEN \* already recorded: decide refuses, and the process learns so
          /\ todo' = [todo EXCEPT ![p] = @ \ {i}]
          /\ pc' = [pc EXCEPT ![p] = "idle"]
          /\ lock' = Release(p)
          /\ UNCHANGED <<view, dec>>
     ELSE /\ view' = [view EXCEPT ![p] = Len(log)]
          /\ dec' = [dec EXCEPT ![p] = i]
          /\ pc' = [pc EXCEPT ![p] = "decided"]
          /\ UNCHANGED <<lock, todo>>
  /\ last' = <<"Decide", p, i>>
  /\ UNCHANGED <<log, crashes>>

Record(p, torn) == [intent |-> dec[p], prev |-> view[p], torn |-> torn]

Write(p) ==
  /\ pc[p] = "decided"
  /\ log' = Append(log, Record(p, FALSE))
  /\ todo' = [todo EXCEPT ![p] = @ \ {dec[p]}]
  /\ pc' = [pc EXCEPT ![p] = "idle"]
  /\ lock' = Release(p)
  /\ last' = <<"Write", p, dec[p]>>
  /\ UNCHANGED <<view, dec, crashes>>

\* The adversary: bounded crashes, at the three places a crash matters.
CrashHolding(p) ==
  /\ pc[p] \in {"locked", "decided"} /\ crashes[p] < MaxCrash
  /\ pc' = [pc EXCEPT ![p] = "idle"]
  /\ lock' = IF lock = p THEN "free" ELSE lock
  /\ crashes' = [crashes EXCEPT ![p] = @ + 1]
  /\ last' = <<"CrashHolding", p>>
  /\ UNCHANGED <<log, view, dec, todo>>

CrashMidWrite(p) ==
  /\ pc[p] = "decided" /\ crashes[p] < MaxCrash
  /\ log' = Append(log, Record(p, TRUE))
  /\ pc' = [pc EXCEPT ![p] = "idle"]
  /\ lock' = IF lock = p THEN "free" ELSE lock
  /\ crashes' = [crashes EXCEPT ![p] = @ + 1]
  /\ last' = <<"CrashMidWrite", p, dec[p]>>
  /\ UNCHANGED <<view, dec, todo>>

CrashAfterWrite(p) ==
  /\ pc[p] = "decided" /\ crashes[p] < MaxCrash
  /\ log' = Append(log, Record(p, FALSE))
  /\ pc' = [pc EXCEPT ![p] = "idle"]
  /\ lock' = IF lock = p THEN "free" ELSE lock
  /\ crashes' = [crashes EXCEPT ![p] = @ + 1]
  /\ last' = <<"CrashAfterWrite", p, dec[p]>>
  /\ UNCHANGED <<view, dec, todo>>

\* An operator truncates a torn final line, holding the lock. The no_repair
\* variant has no operator: the log stays safe and stops.
Repair ==
  /\ Bug # "no_repair"
  /\ TornTail /\ lock = "free"
  /\ log' = SubSeq(log, 1, Len(log) - 1)
  /\ last' = <<"Repair">>
  /\ UNCHANGED <<lock, pc, view, dec, todo, crashes>>

Done ==
  /\ \A p \in Procs : todo[p] = {} /\ pc[p] = "idle"
  /\ ~TornTail
  /\ UNCHANGED vars

Proc(p) ==
  \/ Acquire(p) \/ Refuse(p) \/ (\E i \in Intents[p] : Decide(p, i))
  \/ Write(p)

Next ==
  \/ \E p \in Procs : Proc(p) \/ CrashHolding(p) \/ CrashMidWrite(p)
                      \/ CrashAfterWrite(p)
  \/ Repair \/ Done

\* Fairness, stated: every process keeps taking steps; the lock is granted
\* to a process, or to the operator, that keeps asking for it (STRONG
\* fairness -- flock itself promises no such thing; this is an assumption of
\* the model). TLC showed that weak fairness for the repair is not enough:
\* a writer refused by the torn tail retries and keeps taking the lock, and
\* the repair is never scheduled. A torn tail stops every writer until an
\* operator repairs it -- the cost of failing closed, stated rather than
\* hidden. Crashes are not fair: they are the adversary, bounded by MaxCrash.
Fairness ==
  /\ \A p \in Procs : SF_vars(Acquire(p))
                      /\ WF_vars(Refuse(p) \/ Write(p)
                                 \/ (\E i \in Intents[p] : Decide(p, i)))
  /\ SF_vars(Repair)

Spec == Init /\ [][Next]_vars /\ Fairness

\* Every record names the one before it: no fork, no lost update.
ChainIntact == \A k \in 1..Len(log) : log[k].prev = k - 1

\* Nothing is ever appended after a torn line.
NothingAfterATornLine == \A k \in 1..(Len(log) - 1) : ~log[k].torn

\* A retry after a crash records nothing twice.
NoDuplicate ==
  \A j, k \in 1..Len(log) :
    j # k /\ ~log[j].torn /\ ~log[k].torn => log[j].intent # log[k].intent

\* At most one process is between taking the lock and writing.
Exclusive ==
  Locking => Cardinality({p \in Procs : pc[p] # "idle"}) <= 1

\* Liveness: every intent is eventually recorded.
EveryIntentRecorded == <>(\A p \in Procs : \A i \in Intents[p] : Recorded(i))
=============================================================================
