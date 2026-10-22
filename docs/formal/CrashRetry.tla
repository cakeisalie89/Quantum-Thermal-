------------------------------ MODULE CrashRetry ------------------------------
(***************************************************************************)
(* Crash recovery, retry and idempotency (Assurance B, protocol 4).        *)
(*                                                                         *)
(* agent_substrate.governed_tasks: a request is bound to one task by its   *)
(* idempotency key, and a resubmission finds the binding instead of making *)
(* a second task. A worker takes the task under a lease and runs the tool  *)
(* outside any lock; the execution record is then DECIDED under the writer *)
(* lock against the head, and refused unless the task is still executing  *)
(* under that worker's lease (D-2026-156). A worker can die anywhere.      *)
(* recover() returns a task whose lease has lapsed to the queue, and a     *)
(* resubmission takes it up as a new attempt -- unless the tool has        *)
(* EXTERNAL effects, where a re-run would repeat them: such a task becomes *)
(* UNCERTAIN and is escalated, never re-run (D-2026-110).                  *)
(*                                                                         *)
(* TIME. A lease outlasts the tool's own wall-clock limit, so a live       *)
(* worker finishes inside its lease -- unless the adversary stalls it      *)
(* past the lease (a paused process, a slow disk), which is the case the   *)
(* lease check exists for. A stall and a crash each spend one unit of      *)
(* MaxAdversity; time passes only while a task is executing.               *)
(*                                                                         *)
(* NOT MODELLED: the verifier and the verdict, compensation, the           *)
(* scheduler's retry budget, and the tool itself (an effect is counted     *)
(* when the tool starts, the conservative reading).                        *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS Workers, MaxAdversity, Bug

ASSUME Bug \in {"none", "rebind", "no_lease_check", "rerun_external",
                "no_recover"}

L == 1   \* the lease, in ticks

VARIABLES effect, tasks, state, holder, lease, leaseEnd, runs, recorded,
          wrongRecord, wpc, wlease, now, adversity, last

vars == <<effect, tasks, state, holder, lease, leaseEnd, runs, recorded,
          wrongRecord, wpc, wlease, now, adversity, last>>

Init ==
  /\ effect \in {"LOCAL", "EXTERNAL"}
  /\ tasks = 0 /\ state = "NONE" /\ holder = "none"
  /\ lease = 0 /\ leaseEnd = 0
  /\ runs = 0 /\ recorded = 0 /\ wrongRecord = FALSE
  /\ wpc = [w \in Workers |-> "idle"]
  /\ wlease = [w \in Workers |-> 0]
  /\ now = 0 /\ adversity = 0
  /\ last = <<"Init">>

\* A request arrives, or arrives again. The key binds it to one task.
Submit ==
  /\ tasks < 2
  /\ IF tasks = 0 \/ Bug = "rebind"
     THEN /\ tasks' = tasks + 1
          /\ state' = IF state = "NONE" THEN "QUEUED" ELSE state
     ELSE UNCHANGED <<tasks, state>>
  /\ last' = <<"Submit">>
  /\ UNCHANGED <<effect, holder, lease, leaseEnd, runs, recorded, wrongRecord,
                 wpc, wlease, now, adversity>>

\* A worker takes the queued task under a new lease and starts the tool --
\* or, for an external tool that has run before, refuses to run it again.
Take(w) ==
  /\ state = "QUEUED" /\ wpc[w] = "idle"
  /\ IF effect = "EXTERNAL" /\ runs >= 1 /\ Bug # "rerun_external"
     THEN /\ state' = "UNCERTAIN"
          /\ UNCHANGED <<holder, lease, leaseEnd, runs, wpc, wlease>>
     ELSE /\ state' = "EXECUTING" /\ holder' = w
          /\ lease' = lease + 1 /\ leaseEnd' = now + L
          /\ runs' = runs + 1
          /\ wpc' = [wpc EXCEPT ![w] = "running"]
          /\ wlease' = [wlease EXCEPT ![w] = lease + 1]
  /\ last' = <<"Take", w>>
  /\ UNCHANGED <<effect, tasks, recorded, wrongRecord, now, adversity>>

\* The tool has run; the execution record is decided under the lock.
Finish(w) ==
  /\ wpc[w] = "running"
  /\ wpc' = [wpc EXCEPT ![w] = "idle"]
  /\ IF state = "EXECUTING" /\
        (Bug = "no_lease_check" \/ (holder = w /\ wlease[w] = lease))
     THEN /\ state' = "COMPLETED"
          /\ recorded' = recorded + 1
          /\ wrongRecord' = (wrongRecord \/ wlease[w] # lease)
     ELSE UNCHANGED <<state, recorded, wrongRecord>>   \* refused, unwritten
  /\ last' = <<"Finish", w>>
  /\ UNCHANGED <<effect, tasks, holder, lease, leaseEnd, runs, wlease, now,
                 adversity>>

Crash(w) ==
  /\ wpc[w] = "running" /\ adversity < MaxAdversity
  /\ wpc' = [wpc EXCEPT ![w] = "idle"]
  /\ adversity' = adversity + 1
  /\ last' = <<"Crash", w>>
  /\ UNCHANGED <<effect, tasks, state, holder, lease, leaseEnd, runs,
                 recorded, wrongRecord, wlease, now>>

\* A lapsed lease returns the task to the queue; its holder may still be
\* alive and slow.
Recover ==
  /\ Bug # "no_recover"
  /\ state = "EXECUTING" /\ now > leaseEnd
  /\ state' = "QUEUED" /\ holder' = "none"
  /\ last' = <<"Recover">>
  /\ UNCHANGED <<effect, tasks, lease, leaseEnd, runs, recorded, wrongRecord,
                 wpc, wlease, now, adversity>>

\* Time passes while a task executes. Past a live holder's lease only as a
\* stall the adversary pays for.
Live(w) == w \in Workers /\ wpc[w] = "running"

Tick ==
  /\ state = "EXECUTING" /\ now <= leaseEnd
  /\ ~Live(holder) \/ now < leaseEnd \/ adversity < MaxAdversity
  /\ now' = now + 1
  /\ adversity' = IF Live(holder) /\ now = leaseEnd THEN adversity + 1
                  ELSE adversity
  /\ last' = <<"Tick">>
  /\ UNCHANGED <<effect, tasks, state, holder, lease, leaseEnd, runs,
                 recorded, wrongRecord, wpc, wlease>>

Done ==
  /\ state \in {"COMPLETED", "UNCERTAIN"} /\ \A w \in Workers : wpc[w] = "idle"
  /\ UNCHANGED vars

Next ==
  \/ Submit \/ Recover \/ Tick \/ Done
  \/ \E w \in Workers : Take(w) \/ Finish(w) \/ Crash(w)

\* Fairness: workers take and finish work, time passes, recovery runs. A
\* crash or a stall is the adversary, bounded by MaxAdversity.
Spec == Init /\ [][Next]_vars
        /\ WF_vars(Submit) /\ WF_vars(Tick) /\ WF_vars(Recover)
        /\ \A w \in Workers : WF_vars(Take(w)) /\ WF_vars(Finish(w))

OneTaskPerKey == tasks <= 1
RecordedOnce == recorded <= 1
RecordedByTheLeaseHolder == ~wrongRecord
ExternalEffectsAtMostOnce == effect = "EXTERNAL" => runs <= 1

\* Liveness: the request reaches an outcome -- done, or handed to a human.
ReachesAnOutcome == <>(state \in {"COMPLETED", "UNCERTAIN"})
=============================================================================
