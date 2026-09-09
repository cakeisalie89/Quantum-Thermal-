# Defect ledger

One record per finding, kept **whether or not the finding was flattering**.
A repaired defect stays here with its repair; a wrong conclusion I reached
myself stays here as a wrong conclusion. The ledger exists because the
recurring failure in this repository is not a bug that nobody fixed, it is a
bug that got fixed in one representation and stayed alive in another, and
the only way to see that pattern is to be able to read the list.

Each record answers the same questions: what was wrong, what invariant it
broke, why it was there, which representations were affected, what changed on
each path, how it is tested, what would have been a **fake** fix, what else
was swept for the same shape, and what previously-stated claims it
invalidates.

**Nothing here is a scientific finding.** These are defects in software
authority, provenance and verification machinery. `PASS` remains 0,
`automatic_gate_effect` remains `NONE`, and no record below changes that.

---

## D-2026-01 — `reconcile()`'s give-up branch could never execute

**STATUS** — repaired.

**DEFECT.** The retry budget's enforcement branch in
`Scheduler.reconcile()` — the one that FAILS a job whose lease lapsed with
`attempts >= max_attempts` — built its record and then had it refused by the
reducer on every single execution. `reconcile()`'s deliberately tolerant
`move()` helper swallows `JobTransitionError` and `SchedulerError` and
returns `None`, `continue` skipped the requeue, and the job stayed
`DISPATCHED` under a lease nobody held, forever. The branch was dead code
that looked live.

**INVARIANT.** A job whose retry budget is spent reaches a terminal state.
It does not remain dispatched to a worker that has stopped answering.

**ROOT CAUSE.** `reauthorize_job_edge` treated *every* edge out of
`DISPATCHED` into `{SUCCEEDED, RETRY_WAIT, FAILED}` as an outcome report and
required it to come from the lease holder while the lease was live. For
`DISPATCHED -> FAILED` written by the supervisor after a lapse, the actor who
"ought" to sign is by definition the dead worker, so the condition could
never be satisfied. `DISPATCHED -> READY` had already been given a handover
exemption; `DISPATCHED -> FAILED` had not.

**AFFECTED REPRESENTATIONS.** Implementation (`reconcile`), reducer
(`reauthorize_job_edge`), replay (same reducer runs on load), and the
mutation spec that claimed to cover the budget.

**IMPLEMENTATION FIX.** `reauthorize_job_edge` now names the handover
explicitly: an edge out of `DISPATCHED` to `READY` **or** `FAILED`, taken
when the lease is not live at the deciding position and the record grants no
new possession, is a supervisor handover rather than an outcome report. The
converse is guarded too — the same two edges taken by a non-holder while the
lease *is* live are refused, because reclaiming live possession hands one
attempt to two workers.

**WRITE-PATH FIX.** None needed: `transition()` dry-runs the record against
the reducer inside the writer lock, so the write path inherits the corrected
rule rather than restating it.

**REPLAY FIX.** Same function; the reducer *is* the replay rule.

**INDEPENDENT-READER FIX.** See D-2026-02, which this finding uncovered.

**ADVERSARIAL TESTS.** `tests/test_agent_scheduler.py` — thirteen budget
invariants, of which the load-bearing ones are
`test_repeated_worker_death_cannot_exceed_the_budget[1,2,3]` (attempts
counted from `READY -> DISPATCHED` records in the **log**, not from the
projection), `test_the_final_lapse_fails_the_job_rather_than_requeueing_it`,
`test_a_transient_report_and_a_lapse_draw_on_the_SAME_budget`,
`test_a_supervisor_restart_does_not_reset_the_budget` and
`test_a_worker_that_lost_the_dispatch_race_is_not_charged`.

**MUTATIONS.** The existing `X6_a_lapsed_lease_does_not_spend_the_retry_budget`
attacks the **guard** (`attempts >= max_attempts`); removing the guard
requeues past the budget and the long campaign notices. It cannot see this
defect, because with the guard in place the job simply sticks and
`attempts <= max_attempts` remains true of a stuck job.
`S64_the_give_up_edge_is_not_recognised_as_a_handover` attacks the
**action**: it restores this defect exactly, and dies to
`test_repeated_worker_death_cannot_exceed_the_budget[1]`, a test that did
not exist before this finding. `N46`'s anchor was repaired in place when the
rule it attacks was widened to cover the give-up edge -- the mutation keeps
its identity and its history rather than being retired and re-minted under a
new name for the same mutant.

**MUTATIONS CONSIDERED AND DROPPED.** Four candidates in the same rule were
written and then removed rather than kept as easy kills, which is the
discipline this ledger is supposed to hold:

* *handover exemption without the liveness test* -- stopped one guard later
  by `N46`;
* *handover exemption without the no-owner test* -- stopped one guard later
  by `N48`;
* *a second mutant of the reclamation guard* -- that is `N46` under a new
  name;
* *the attempt-accounting expression made trivially true* -- the matrix
  showed it dying to the same test as `N47`. Deleting the comparison and
  making it always succeed are one mutant reached two ways.

Each would have raised the kill count and measured nothing: three of them
test which guard fires first, which is implementation detail, and the fourth
duplicates a mutation that already exists. The attempt rule never lacked
coverage of the count. It lacked coverage of the give-up action.

**ACCEPTANCE CRITERIA.** A job dispatched `max_attempts` times, each attempt
ending in a lease lapse rather than a report, ends `FAILED` with
`attempts == max_attempts`, and its dependents are blocked.

**FORBIDDEN FAKE FIXES.** Widening `move()` to log its swallowed refusals
and calling that visibility. Removing the ownership guard on outcome edges
so the give-up "works". Raising `max_attempts`. Asserting on the projection
instead of the log, which cannot distinguish a stuck job from a finished one.

**SIBLING SWEEP.** Every other caller of the tolerant `move()` in
`reconcile()` — the `BLOCKED`, `READY` and `WAITING` convergence moves — was
re-examined. Those are genuinely racy (another process may have moved the job
between scan and write) and skipping is correct for them; none of them is a
branch whose *only* purpose is to enforce a bound, which is what made this
one different. Recorded rather than assumed: the distinguishing property is
"is there any other path that reaches this outcome", and for the budget there
was not.

**DISCOVERED BY.** A test written expecting the budget to hold. The
assertion failed on a job that was neither retried nor failed.

**INVALIDATED CLAIMS.** Any earlier statement that the retry budget was
enforced on the lapse path. It was enforced on the *report* path only.

---

## D-2026-02 — the second reader accepted a wider language than the first

**STATUS** — repaired.

**DEFECT.** `reconstruct.py`'s independent scheduler replay checked the
*shape* of a job transition (job exists, `src` matches, not leaving a
terminal state) and then applied the payload verbatim. It asked nothing about
**who wrote the record** and nothing about **what the record did to the
retry count**. Four forged records that the production reducer refuses
outright replayed clean, and the reader reported **no findings** on all four:

| forged record | production reducer | second reader (before) |
|---|---|---|
| outcome signed by a non-holder | refused | accepted, job `SUCCEEDED` |
| requeue while the lease is still live | refused | accepted |
| requeue that resets `attempts` to 0 | refused | accepted, budget restored |
| outcome that sets `attempts` to 9999 | refused | accepted |

**INVARIANT.** The two readers are independent in **implementation** and
identical in **accepted language**. A history one refuses is a history the
other reports on.

**ROOT CAUSE.** The second reader was built to answer "does an independent
implementation project the same *state*", and the ownership and
attempt-accounting rules live in the production reducer as *admission*
checks — they decide whether a record may exist at all, not what it projects
to. Admission was never restated, so it was never a dimension the second
reader could conserve.

**WHY THIS IS WORSE THAN IT SOUNDS.** A log carrying one of these records
cannot be loaded by the primary at all, so the primary-versus-reader
comparison never runs. On exactly the histories where a second opinion
matters, the second reader is the *only* reader — and it was the permissive
one.

**AFFECTED REPRESENTATIONS.** Independent reconstruction
(`_sub_job_transition`, `_sub_enqueue`) and, downstream, every report that
cites "the second reader found no anomalies".

**IMPLEMENTATION FIX.** Four rules restated in `reconstruct.py`'s own terms,
from the event header and this replay's own state, never from the payload:
possession is judged at `ev.seq - 1` (the position the writer decided from,
so a report written at the last legal moment is not refused); an outcome edge
out of `DISPATCHED` must be signed by the holder while possession lasts; the
two handover edges are refused from a non-holder while possession is live;
`attempts` may move only on the hand-out edge and only by one; and no record
outside the hand-out edge may grant possession. `_sub_enqueue` now carries
`max_attempts` so the budget has something to be measured against, and a
budget overrun is noted as a finding about the history however it arose.

**RESTATED, NOT IMPORTED.** `reconstruct.py` sits below `scheduler` in the
declared layering and still imports nothing from it. `_JOB_OUTCOME` is a set
of plain strings for the same reason `_JOB_TERMINAL` is: a second reader that
asks the scheduler which edges are verdicts agrees with it by construction,
including where the scheduler is wrong. The cost is real — the two can still
share a misunderstanding — and it is why an empty diff is evidence and not
proof.

**WRITE-PATH FIX / REPLAY FIX.** None; the production side was already
correct, which is precisely the defect's shape.

**ADVERSARIAL TESTS.** See the second-reader suite: each of the four forged
records above, plus the case the liveness rule cannot cover for — an
`attempts` reset on a *genuinely* lapsed handover, where possession really
has run out and only the attempt rule stands between the log and an
unbounded retry budget.

**ACCEPTANCE CRITERIA.** For each forged record: the production reducer
refuses it, the second reader records an anomaly naming it, and the second
reader does **not** fold it (the job keeps the state it legitimately had).

**FORBIDDEN FAKE FIXES.** Importing `OUTCOME_STATES`, `JobState`, or
`reauthorize_job_edge` into `reconstruct.py` — that converts the second
opinion into the first one run twice. Calling `Scheduler.load()` inside the
reader and reporting its exception as a finding. Asserting only that the two
readers *agree*, which they already did on every history the primary can
load.

**SIBLING SWEEP.** The question this finding generalises to is: *for every
subsystem with a second reader, does the reader restate the admission rules
or only the projection rules?* `_sub_lease_renew` restates ownership,
liveness, the renewal bound and the payload-may-not-name-its-own-end rule —
it is the model the transition reducer should have followed. The remaining
`_sub_*` reducers have not yet been swept to this standard and are tracked as
follow-up work below.

**DISCOVERED BY.** Following D-2026-01 outward: having fixed an ownership
rule in the production reducer, asking whether the independent reader had
ever had one.

**INVALIDATED CLAIMS.** Any earlier statement that the independent
reconstruction "would catch a forged job record". It would catch a forged
`src`, a revival from a terminal state, and a forged enqueue. It would not
catch a forged owner or a forged attempt count.

---

## D-2026-04 — an agent could sign a person's escalation answer

**STATUS** — repaired.

**DEFECT.** `agent.escalation.answer` records the decision in
`payload["answered_by"]` and the writer in the event header's `actor`.
Nothing compared them. Every check in the reducer interrogates
`answered_by` — that it is a registered principal, that it is
`PrincipalKind.HUMAN`, that it is not the principal who raised the
escalation, that the answer is among the options — so an **AGENT** could
append the record, name a real person in the payload, and satisfy all four
on the borrowed name.

Reproduced before the fix: an agent `x1` appended the record naming human
`h1`, and a fresh replay reported the escalation `ANSWERED`, `answer='yes'`,
`answered_by='h1'`.

**INVARIANT.** The record's account of who decided cannot differ from the
log's account of who wrote it.

**WHY IT MATTERS MORE THAN THE OTHERS.** `agents.py` opens by stating that
no agent may answer an escalation — that an escalation exists precisely
because the decision was not the agent's to make, and that no arrangement of
roles substitutes for a person. This was the one record in the system meant
to carry a human decision, and it was the one an agent could write.

**ROOT CAUSE.** Two causes, and the second is the interesting one.

*First:* the guard was written as a property of the named principal rather
than of the writer, so it asked the right question about the wrong string.

*Second:* **every existing forgery test set `answered_by` equal to the
event's actor.** The suite modelled an attacker who lies about the decision
while filling in the paperwork honestly. A forger who writes somebody else's
name was never modelled, so the missing comparison had nothing to fail. This
is the test-design failure to look for elsewhere: an adversarial test that
keeps the attacker internally consistent tests the checks that exist and
cannot discover the one that does not.

**AFFECTED REPRESENTATIONS.** `AgentDirectory.apply` (the reducer, which is
also the replay rule). The write path `answer()` already bound
`actor=answered_by`, which is why no honest run ever produced a record the
new rule refuses — and why the defect was invisible in normal operation.

**IMPLEMENTATION FIX.** The reducer refuses a record whose `answered_by`
differs from `ev.actor`, before any of the checks that read `answered_by`.
The withdraw branch immediately above it already did exactly this against
`raised_by`; so did the capability root, the idempotency owner and the task
executor. The answer was the gap in an otherwise uniform discipline.

**WHAT THIS DOES NOT ESTABLISH.** It is not authentication. `ev.actor` is
still a string the writer chose, and the log file is still the trust
boundary it always was. What it establishes is that the two accounts of who
decided cannot be played against each other.

**SIBLING SWEEP — AND A SECOND FINDING.** Every reducer that reads a
principal name out of a payload was enumerated and classified as bound to
the event header, bound to replayed state, or unbound:

* `capability.root` issuer, `idempotency.bind` owner, `memory.write` author,
  `scheduler.enqueue` submitter, `capability.issue` minter (all three
  minting cases), and the second reader's copies of each — **bound to the
  header** already.
* `task.transition` `executed_by` and the second reader's copy — **bound to
  replayed state**, which is stronger: the executor comes from the execution
  record, and separation of duties is checked against it.
* `capability.issue` `subject` and `agent.register` `instance_id` — **not
  identity claims about the writer**. A grant names its grantee and a
  registration names the instance being admitted; both are legitimately
  third parties, and the writer is checked separately.
* `task.create` `submitter` — **UNBOUND, in both readers.** Fixed here
  alongside the escalation: the submitter is what the policy gate was
  evaluated against when the task was admitted and what every later
  attribution question reads back, so a forged create attributes work to
  somebody who never asked for it. `scheduler.enqueue` had always compared
  it; `task.create` never had.
* `agent.retire` — **no authority check of any kind**, on the writer or the
  target, in either reader. Not repaired here: it is a destructive-authority
  defect and belongs with that work, where it is now a confirmed lead rather
  than a suspicion. Recorded so it cannot be lost.

**ADVERSARIAL TESTS.** An agent signing a person's answer; a *person*
signing another person's answer (two registered humans are still two
principals, and a decision recorded under the wrong one is one the named
person can truthfully deny making); an answer naming nobody, so a missing
field cannot read as agreement with the header; a forged `task.create` in
both readers, asserting the record is not folded as well as reported. Plus
anti-vacuity: an honest answer still replays, an honest create still
projects, and the write path is checked to bind the two fields — a writer
that could emit a record its own replay refuses is the defect D-2026-01
already recorded once.

**FORBIDDEN FAKE FIXES.** Deriving `answered_by` from `ev.actor` and
dropping the payload field, which destroys the disagreement instead of
detecting it. Checking only at the write path, where honest callers already
agreed and forgers never go. Treating the header as authentication.

**DISCOVERED BY.** Reading the reducer against the module's own opening
paragraph and asking which string each guard actually interrogates.

**INVALIDATED CLAIMS.** Any earlier statement that no agent could answer an
escalation, or that the human gate could not be satisfied by an agent. It
could, by naming a person. The prior audit of self-declared identity fields
established the header-binding discipline across the substrate and did not
reach this record.

---

## D-2026-03 — analyst conclusion error: the hosted byte check

**STATUS** — recorded, not a code defect.

**DEFECT.** I stated that the hosted byte-comparison check "cannot pass" and
that the `full-suite` job was expected to be red. Hosted run 34296217403
passed it. The claim was wrong.

**ROOT CAUSE.** I reasoned from the local cross-environment divergence to a
conclusion about the hosted check without reading what the hosted check
actually compares.

**WHY IT IS IN THIS LEDGER.** §3 of the governing directive treats a
self-asserted conclusion as an authority claim like any other. An analyst
statement that the evidence later contradicts is the same defect family as a
payload that authorises itself, and deleting it once it is corrected would
remove the only record that the reasoning path was unsound.

**FIX.** Corrected in the run that observed it. The general rule taken from
it: a prediction about a check is not evidence about that check, and the
check's own output is the only thing that settles it.

**INVALIDATED CLAIMS.** The stated expectation of a red `full-suite`.

---

## D-2026-05 — creation was guarded everywhere; destruction was guarded nowhere

**STATUS** — repaired.

**DEFECT.** Five destructive operations accepted a record from any actor at
all, on the write path and on replay, in both readers. Each was reproduced
against the code as it stood:

| operation | before |
|---|---|
| `agent.retire` | an **AGENT retired a HUMAN**, and a fresh replay agreed the person was gone |
| `memory.supersede` | `mallory` marked `alice`'s entry SUPERSEDED **by an entry mallory wrote** |
| `capability.revoke` | `mallory` revoked a grant the **root issuer** made |
| `network.grant` revoke | any actor withdrew egress authority somebody else granted |
| `secret.grant` revoke | the same, in the one store that has **no reducer at all** |

**INVARIANT.** Taking authority away takes authority. Whoever may destroy a
thing is a decision, not a default.

**ROOT CAUSE — one shape, five instances.** Every one of these modules
guards CREATION carefully and had nothing on the matching removal.
`_authorize_mint` asks who may bring a grant into existence;
`_check_may_register` says an agent that could admit a human would be one
step from answering its own escalations; `retract()` refuses withdrawing
somebody else's statement in a paragraph of argument. Removal is the same
authority reached from the other direction and had no counterpart anywhere.

The sharpest instance is `memory.supersede`. `retract` states the rule,
argues for it, and enforces it on **both** paths — and its sibling, which is
strictly worse, had no check at all. Retracting somebody's note removes it;
superseding it removes it **and** points every later reader at an entry the
superseder chose.

The capability ledger is the clearest evidence that this was a gap in
symmetry rather than in knowledge: it has ALWAYS recorded who issued each
grant in `_issued_by`, and `issuer_of()` says in its own docstring that it
is "not an authorization". The substrate knew who granted the authority and
did not consult that knowledge when the authority was destroyed.

**A SIXTH, FOUND ON THE WAY: retirement did not mean the same thing at every
gate.** `require()` has always refused a retired instance every role.
`_check_may_register` and the escalation-answer path did not consult
retirement at all. So "retired" meant a principal could no longer act,
*except* to admit new humans and to answer escalations — the two things it
most matters that they cannot do. Reproduced: a retired human registered a
new HUMAN and answered an open escalation.

**IMPLEMENTATION FIX.**

* `capability.revoke` — the issuer of that grant, or the root issuer.
  Explicitly **not** the subject: a capability is not the holder's to
  destroy, and an "issuer or subject" rule would let one captured worker
  take down authority the control plane relies on.
* `network.grant` / `secret.grant` revoke — the actor that issued the
  grant. Neither module recorded a granter, so one is recorded now; without
  it there was nothing for a revocation to be checked against, which is why
  revocation was checked against nothing.
* `agent.retire` — the principal itself, whoever admitted it, or an active
  human; and a HUMAN may be retired **only** by a human or by itself. One
  `_check_may_retire`, called by the write path and the reducer, for the
  same reason `_check_may_register` is one function.
* `memory.supersede` — the author of the old entry, mirroring `retract`, on
  both paths. `_AUTHORS_OWN` names the two statuses that WITHDRAW.
* Retirement conservation — `_check_may_register` and the escalation answer
  now require the acting principal to be **active**, not merely registered.

**WHAT IS DELIBERATELY NOT COVERED.** `INVALIDATED` and `STALE` are outside
the author's-own rule. Those say a premise an entry rested on stopped
holding — a fact about the world, cascaded over entries with many different
authors by whoever noticed it. Requiring authorship there would stop the
cascade doing the only thing it exists for. A mutation asserts this
(`M40`), because the over-correction is fail-closed and would otherwise look
like a strictly safer choice.

**A BOUNDARY, STATED RATHER THAN CLOSED.** `SecretStore` has no reducer: it
is rebuilt by the process that owns it and its log is an audit trail, not
the source of truth. So its revocation rule runs on the write path and there
is **no replay in that module** for it to also run on. The independent
reconstruction is the only thing that re-reads such a record. This is
asserted by a test that fails if a reducer is ever added, so whoever adds
one has to decide deliberately whether the rule belongs in it.

**INDEPENDENT-READER FIX.** All three revocation rules and the retirement
rule restated in `reconstruct.py`'s own words, from the event header and its
own replayed state. The reader now records who minted each capability and
who granted each egress/secret grant, since it had no more to check against
than the ledgers did. The bootstrap sentinel is spelled out rather than
imported, like every other constant there: if the two ever drift, the
divergence is the finding.

**ADVERSARIAL TESTS.** Each row of the table above, at the write path and at
replay, plus the subject-may-not-revoke case, the retired-principal cases,
and the second reader's version of every rule. Anti-vacuity throughout: the
issuer may still revoke, the root may still revoke a delegation, a registrar
may still retire what it admitted, a principal may still stand itself down,
an author may still supersede their own entry, an active human still
answers, and an invalidation cascade still crosses authors.

**A PATTERN IN THE TEST SUITE ITSELF.** Five existing tests revoked with an
arbitrary actor — `actor="owner"` against grants issued by `"scheduler"` —
and passed, because nothing checked. They were not testing who may revoke;
they were testing what a revocation does, and the arbitrary actor was an
unnoticed instance of the defect. Each now names the issuer, and says why in
a comment. This is the same failure recorded in D-2026-04: **a test that
supplies an internally inconsistent actor and still passes is evidence of a
missing check, and it reads as a passing test.**

**FORBIDDEN FAKE FIXES.** Enforcing only on the write path, where honest
callers already comply and forgers never go. Letting the grant's subject
revoke it. Making revocation require the *policy gate* alone, which
`scheduler.cancel` legitimately does but which for these would answer a
different question than "whose grant is this". Removing the retirement check
from `require()` so the three gates agree by weakening rather than by
conserving.

**SIBLING SWEEP.** Every destructive entry point in the substrate was
enumerated: `agent.retire`, `capability.revoke`, `netauth.revoke`,
`secrets.revoke`, `memory.retract`, `memory.supersede`,
`memory.invalidate_source`, `scheduler.cancel`, `scheduler.invalidate`,
`checkpoint.prune`, `execution.cancel`. `scheduler.cancel` was already
correct and is the model: it goes through the policy gate with its own
action. `memory.retract` was already correct. `checkpoint.prune` and
`execution.cancel` are local operations on local objects, not authority
decisions over a shared log. The other five are fixed here.

**MUTATION CAMPAIGN, AND THE TWO THAT SURVIVED.** 223 mutations over six
specs; 221 died on the first run. The two survivors were both real coverage
gaps in the tests I had just written, and neither was a redundant guard:

* `M39_the_write_path_lets_anyone_supersede` — removing the check from
  `supersede()` left the test passing, because the **reducer** then refuses
  the same record with the **same exception type and the same message**. A
  test asserting only "it raises" cannot tell the two layers apart. They do
  not guarantee the same thing: `_set_status` appends and *then* applies, so
  without the call-site check the record is durable before the reducer ever
  sees it — the caller gets its exception and the store is **permanently
  unloadable**. Verified directly: after the bypassed call the log had grown
  by one event and a fresh `load()` raised. The new test asserts the
  distinguishing property, that a refusal costs the log nothing, and kills
  the mutation.
* `G26_the_subject_of_a_grant_may_destroy_it` — I had written a
  subject-may-not-revoke test, and it exercised the **write path**, which
  refuses before the mutated reducer is ever reached. The log is the trust
  boundary, so the rule needed a test against a record that never went
  through the method. The new test appends the revocation directly.

Both are the §23 shape from opposite directions: one mutation survived
because a *different* guard produced an indistinguishable outcome, the other
because the test never reached the guard under attack. Neither was fixed by
weakening the mutation or by asserting an implementation detail.

**DISCOVERED BY.** The sibling sweep of D-2026-04, which turned up
`agent.retire` with no check of any kind and was recorded there as a lead
rather than followed immediately.

**INVALIDATED CLAIMS.** Any earlier statement that authority in this
substrate is guarded end to end. It was guarded on the way in. Any statement
that a retired principal cannot act — it could, at two of the three gates
that matter.

---

## D-2026-07 — a destination is an address and a port; the guard checked the address

**STATUS** — repaired.

**DEFECT.** `socket_guard` is the layer that sees the connection actually
being made. On every path where a request had been authorized, it checked
the address and never the port. A grant permitting `:443` on a pinned
address permitted a connect to `:22` on that address, and `:6379`, and
anything else.

Reproduced: a grant for `hosts=("localhost",), ports=(443,),
addresses=("127.0.0.1",)`, an authorized decision reading *"egress grant 'g1'
covers GET https://localhost:443/v1/x"*, and then connects to `127.0.0.1` on
443, 22 and 6379 all permitted identically by the guard.

**INVARIANT.** What the guard permits at connect time is what the decision
authorized. A destination is an address AND a port.

**ROOT CAUSE — a dimension lost between two layers that both had it.**
`_covers` checks nine dimensions of a request, port among them; it refuses a
target whose port the grant does not permit. `NetworkDecision` then carried
`pinned_addresses` and nothing else about the destination. So the guard had
the addresses to check against and *nothing to check the port against*, and
checked the half it had a field for. The port was present at the decision,
present in the grant, present in the reason string the decision records —
and absent from the object that travels to the enforcement point.

**WHAT I GOT WRONG WHILE FIXING IT, AND WHY IT IS RECORDED HERE.** I first
wrote that only the *pinned* branch skipped the port, and that the general
path "re-authorizes with the real port and always caught this" — I put that
claim in a production comment, in a test docstring, and in a test whose whole
premise was that the two branches disagreed. It is false. **Both** authorized
branches skipped it: the pinned one, and the one where the grant accepted the
unpinned window. The re-authorizing path does check the port, but it is
reached only when there is NO authorizing decision — the
dependency-reached-the-network case. Every connection made *under* a
decision, which is every connection the system means to make, skipped the
check. The test failed and told me so. Both comments and the test are
corrected; the claim is recorded because a wrong rationale left in a comment
is a defect that outlives the code it explains.

**IMPLEMENTATION FIX.** `NetworkDecision` gains `pinned_ports`, set from the
grant behind an allowed decision, and carried into `to_record()` so the
durable record says what the connection was confined to. The guard checks the
port **once, ahead of both branches**, because it is the same question
whether or not addresses are pinned — putting it inside one branch is how the
two came to disagree in the first place.

**ADVERSARIAL TESTS.** The pinned address on four ports the grant does not
name; a grant naming several ports, so the check is set membership and not a
comparison against one of them; the decision carrying the ports through to
its record, which is the conservation the defect was; and the two authorized
branches agreeing. Anti-vacuity: the granted port is still permitted, and a
guard that refused every port would pass all of the refusal tests.

**MUTATIONS.** `E45` restores the defect. `E46` drops `pinned_ports` from the
decision — the conservation failure itself rather than the missing check it
caused, so the two are covered separately. `E47` moves the check back inside
the pinned branch, re-creating the disagreement between the two authorized
paths.

**A THIRD TEST-SUITE FINDING.** My new helper was named `_connect`, which
already existed at module scope in that file, and shadowing it broke five
existing socket-guard tests. They failed loudly and were fixed by renaming
mine. Worth recording only because for several minutes I read those five
failures as evidence that the *existing tests had been passing on
unauthorized ports* — a plausible story, consistent with the two test-suite
findings already in this ledger, and wrong. Reading a failure as
confirmation of the pattern you are already looking for is its own hazard.

**FORBIDDEN FAKE FIXES.** Checking the port only on the pinned path, which
is the state that produced this. Deriving the permitted ports inside the
guard by reaching into `authority._grants` — the decision is what authorized
the connection, and re-deriving authority at enforcement time is how the two
drift. Treating the monkeypatch as containment: it is not, and `socket_guard`
already says so in its own docstring.

**SIBLING SWEEP.** The other dimensions the decision does not carry —
scheme, method, path — are genuinely unavailable at connect time, and the
fallback synthesises the most permissive plausible values
(`https`, `/`, `GET`). That is a real widening and is not fixed here: a
socket, once open, carries whatever the process sends. It belongs with the
containment boundary in D-2026-08 rather than being papered over with a
check that cannot see what it claims to.

---

## D-2026-06 — analyst conclusion error: the 3D solver's convergence check

**STATUS** — recorded, not a code defect.

**DEFECT.** I stated that `solve_thermal_3d` never checks `sol_obj.success`.
It does, at `thermal_3d_transient.py:268`, past the point where I had stopped
reading. I read to the end of the energy accounting, saw no check, and
reported the absence as established.

**ROOT CAUSE.** Reporting a *negative* — "this code does not do X" — from a
partial read. An absence is only established by reading the whole scope it
could appear in, and I had not.

**WHY IT IS IN THIS LEDGER.** Same reason as D-2026-03. A wrong analyst
conclusion is an authority claim that the evidence contradicts, and the
value of recording it is the pattern: this is the second time a confident
negative has come from an incomplete read, and both times the correction
came from looking at the thing itself rather than from re-reasoning.

**WHAT THE ACTUAL DEFECT TURNED OUT TO BE.** Sharper than the one I
reported, and recorded as the open item below: `require_converged()` — the
repository's own fail-closed contract, whose docstring says it exists
because "a failed BDF integration could still produce
FORECAST_READY_IF_MEASURED" — is called at exactly two sites, both in the 1D
coupled path, and at none of the 3D ones.

**INVALIDATED CLAIMS.** My statement that the 3D solver does not check
convergence.

---

## Open follow-up tracked from this ledger

These are named here so they cannot be closed by silence. They are **not**
claimed complete.

1. **The admission-rule sweep across every `_sub_*` reducer**
   (D-2026-02 sibling sweep). `_sub_job_transition` and `_sub_lease_renew`
   now restate admission; the capability, agent, memory, network, secret and
   context reducers have not been re-read against that standard.
2. **Escalations have no second reader at all.** `reconstruct_subsystems`
   replays nine subsystems and `agent.escalation.*` is not among them, so
   the human-decision records -- the ones D-2026-04 shows were forgeable --
   are reconstructed by nobody. Stated as a boundary, not a claim.
