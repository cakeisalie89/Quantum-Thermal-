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

## D-2026-08 — "kernel-enforced bounds with no network authority" read as containment

**STATUS** — repaired, as a corrected claim and a pinned boundary. **No new
containment was built, and none is claimed.**

**DEFECT.** The completion report and the governance authority record both
said a task reaching `VERIFIED` means a declared tool *"ran under
kernel-enforced bounds with no network authority"*. Both halves are true
separately. Together they read as containment, and there is none.

**WHAT IS ACTUALLY ENFORCED.** `_apply_limits` sets five rlimits between
fork and exec: `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE`, `RLIMIT_NPROC`,
`RLIMIT_CORE`, plus `setsid`. **Not one of them restricts networking.**

**PROVEN, NOT ASSUMED.** A probe run through the real `run_bounded` under
these exact bounds opened a listening TCP socket and routed a UDP socket to
`8.8.8.8:53`, printing both, exiting 0, `Outcome.COMPLETED`.

**WHAT THE SUBSTRATE ACTUALLY WITHHOLDS.** Authority, not capability. No
egress grant is issued to a tool, and `socket_guard` refuses connections the
parent makes without one. That guard is a monkeypatch of
`socket.socket.connect` in the parent's own interpreter; its docstring
already says it is not containment and that code inside the block can
restore the original method. **It does not exist in a child process at all.**

**INVARIANT.** A claim names the mechanism that makes it true. "The kernel
enforces X" and "we grant no Y" are two statements, and joining them with
"with" transfers the first's force to the second.

**FIX.** All three sites now say which bounds the kernel enforces, that
networking is not among them, that what is withheld is authority, and that
the in-process guard is not containment and does not reach a child:
`docs/SESSION_REPORT.md`, the `authorities.json` `does_not_mean` record, and
`run_bounded`'s own docstring — the last because that is where a reader
forms the belief.

**PINNED BY ASSERTING WHAT IS NOT TRUE.**
`test_a_bounded_child_is_NOT_prevented_from_using_the_network` asserts a
child CAN open a socket. If anyone later adds a namespace, a seccomp filter
or anything else that genuinely contains a child, that test fails and they
must come to the claims written to match the old reality and update them
deliberately — rather than leaving prose that has quietly become true for
reasons nobody recorded. Paired with
`test_the_bounds_that_ARE_enforced_are_the_ones_claimed`, which shows the
address-space rlimit biting: a boundary test asserting only an absence would
pass in a build where nothing was enforced at all, so both halves of "these
and not those" are checked.

**FORBIDDEN FAKE FIXES.** Adding a network namespace and claiming
containment without evidence it holds under this environment's privileges.
Deleting the words "no network authority", which are true and load-bearing —
the defect is the join, not either half. Extending `socket_guard` to the
child by injecting a `sitecustomize`, which is a monkeypatch reaching one
process further and is exactly what §39 forbids calling kernel containment.
Recording the boundary and *also* leaving the original sentence somewhere
else in the repository.

**SIBLING SWEEP.** Every occurrence of "kernel-enforced" was enumerated:
three source sites, all corrected, plus `verification/stage10/rag/rag_index.json`,
which is derived from the report and regenerates. The scheme/method/path
widening noted in D-2026-07 belongs to this same boundary: a socket, once
open, carries whatever the process sends, and no connect-time check can see
it.

**DISCOVERED BY.** Reading the sentence against `_apply_limits` and asking
which of the five rlimits does the work the sentence attributes to them.

**INVALIDATED CLAIMS.** Any reading of the completion report or the
authority record in which a VERIFIED task was network-isolated by the
kernel. It was not, and is not now; the difference is that the documents say
so.

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

## D-2026-09 — the convergence contract was written once and applied twice

**STATUS** — repaired.

**DEFECT.** `require_converged()` is this repository's own fail-closed
contract for numerical solves. Its docstring says exactly why it exists:
*"solver_status used to be reported alongside the metrics as a passive string
while ready_terms was computed from the same result regardless, so a failed
BDF integration could still produce FORECAST_READY_IF_MEASURED."*

It had **two call sites**, both inside `run_coupled`, the 1D path where it
was written. `run_mode_sequence_3d` — whose own docstring says it runs the
canonical mode order *"exactly mirroring the 1D/2D `coupled_mode_solver`"* —
mirrored the mode order, the state hand-off and the species interlocks, and
not this. So every 3D result, the Mode-C readiness decision taken from it,
the campaign state, the falsification report and the machine FSM were built
on integrations nobody had asked about.

**INVARIANT.** A solve that did not converge carries no scientific
authority, on every path, not only the one where the rule was discovered.

**ROOT CAUSE.** The rule was fixed where the defect was found and never
swept. It also *lived* where it was found — defined beside the 1D coupled
solver, inside one of the things it governs, so the next sibling did not
inherit it by construction.

**THE VERIFIER TESTED AGAINST ITSELF.** `test_require_converged_rejects_a_failed_status`
and `test_require_converged_accepts_ok` construct a hand-made object with
`solver_status="failed"` and check the helper's return. They prove the
function works. They prove nothing about whether anything calls it — which
was the entire defect — and they sat in the file named for this contract,
reading like its coverage.

**IMPLEMENTATION FIX.**

* The contract moved **down the layering** into `numerics.py`, which every
  solver already depends on, and is re-exported from `coupled_mode_solver`
  so existing callers and tests are unaffected. Where a rule lives decides
  who inherits it.
* `require_converged` applied at all three 3D solves in
  `run_mode_sequence_3d` — Mode B, Mode C (which decides readiness, and Mode
  D is constructed only if it holds) and the Mode D sensing hold.
* Applied at the five solves in `reduction_checks_3d`, which are taken
  *outside* the sequence: a reduction check compares two solvers, and if
  either did not converge it measures the distance between one answer and
  one non-answer and reports it as a `rel_error` with a `within_tolerance`
  verdict beside a `solver_status` nobody reads.
* Applied at the three thermal solves in `runner.py`, which writes NV
  temperatures, hotspots, gradients and energy residuals — and the two
  `solver_status` strings beside them, which is exactly the passive
  reporting the contract exists to replace.

**A SECOND HALF OF THE CONTRACT, FOR RAW INTEGRATOR RESULTS.**
`surface_coverage`, `gas_transport_1d` and `verification.py`'s MMS check hold
a scipy `OdeResult` and never looked at `.success` at all. A failed
integration does not return garbage — it returns a **shorter trajectory**,
every value finite, so `assert_finite` passes and the numbers look ordinary.
Callers pairing `so.y` with the `t_eval` they asked for then hold two arrays
of different lengths, and the one describing time is the one still at full
length. `require_integrated()` says the same thing in that layer's
vocabulary.

**THE ENERGY ACCOUNTING WAS QUADRATURING AN EXTRAPOLATION.**
`solve_thermal_3d` integrates the solver's continuous interpolant over the
whole window, `sol_obj.sol(tq)` with `tq` spanning `[0, t_end]`. An
`OdeSolution` evaluated past the interval actually integrated **extrapolates
the last polynomial rather than refusing**, so a failed solve produced an
entirely ordinary-looking `rel_residual` — computed over time the integrator
never reached — and handed it back beside `solver_status="failed"` for a
reader to notice or not. The residual is now `NaN` on a failed solve, which
both readers already fail closed on: `closure_ok` is `abs(rel) < tol`, false
for NaN, and the accounting formats it as-is so an operator sees `nan`
rather than a plausible figure.

**ADVERSARIAL TESTS.** Failure injected at each of the three 3D solves,
with results reporting temperatures that satisfy every readiness term so
only the status check can refuse them; a reduction check against an injected
failure; a real `solve_thermal_3d` run with `success` flipped, asserting the
residual is NaN and closure is refused. Anti-vacuity throughout: an honest
sequence still runs, an honest solve still reports a residual, and
`require_integrated` still accepts a finished integration.

**THE MUTATION THAT SURVIVED, AND WHY.** `SC3` — removing the Mode D check —
survived the first campaign. The failure-injection tests reached solves 1 and
2 only, and the honest test asserted nothing about the Mode D hold, so the
branch ran and nothing looked at it. **A branch no test reaches is a branch
no test defends, however green the file is.** Fixed by injecting at the third
solve and by having the honest test assert Mode D was actually entered — so
the injection test is known to be reaching a branch that runs rather than one
skipped for unrelated reasons. 7/7 after.

**FORBIDDEN FAKE FIXES.** Leaving the contract beside the 1D solver and
importing it upward, which reproduces the layering that caused this.
Checking `solver_status` at each consumer instead of at the producer, which
is the passive-string pattern with more places to forget. Deleting the
`solver_stability` condition in `falsification_3d` now that the sequence
refuses to return a failed result — it is a second reading of a thing
enforced upstream, and it still catches a sequence assembled by hand.

**SIBLING SWEEP.** Every `solve_ivp` call site in the repository was
enumerated: `thermal_3d_transient`, `thermal_2d_axisymmetric`, `thermal_1d`
and `stack/fem_fenicsx` already checked `success` and recorded it;
`surface_coverage`, `gas_transport_1d` and `verification` did not and now do.
Every consumer of `solver_status` was enumerated too: `coupled_mode_solver`
(enforced), `uncertainty.py` and `falsification_3d` (checked in their own
words), `reduction_checks_3d`, `runner.py`, `vtk_export.py` and
`verification.py` (passive — the first two now enforce; the last two report
into records rather than deciding anything, and are left as reporting).

**DISCOVERED BY.** Asking where `require_converged` is actually called,
after wrongly reporting that the 3D solver never checked convergence at all
(D-2026-06). The wrong answer led to the right question.

**INVALIDATED CLAIMS.** Any statement that a non-converged solve cannot
produce readiness in this repository. That was true of the 1D coupled path
and of nothing else.

---

## D-2026-10 — PREMATURE CLOSURE: "all six P0 items are done"

**STATUS** — recorded. The repairs stand; the claim did not.

**DEFECT.** I wrote that all six P0 items were done. An external review of
the same head found further instances of the same defect classes still live.
The repairs were real and are retained. The *claim* was wrong, and it was
wrong in a specific, repeatable way: **I closed the examples that exposed
each defect and did not close the defect class.**

**WHAT THE SIBLING SWEEPS ACTUALLY COVERED.** Each P0 item's sweep looked
where the defect had been found and at structurally identical siblings I
could enumerate quickly. D-2026-04's sweep, for instance, enumerated every
reducer reading a principal name out of a payload — and *classified* the
escalation raiser and the message sender as part of records I had already
looked at, rather than checking each one. Two actor-bearing fields, in the
same file, in the same class, missed by a sweep whose whole purpose was to
find exactly them.

**ROOT CAUSE.** A sweep that enumerates by *reading* stops where attention
stops. There was no artefact — no table, no generated inventory, no test —
that could be checked for completeness independently of my having looked.
"I swept the siblings" was itself an unverifiable self-assertion of the kind
this ledger exists to distrust everywhere else.

**FIX.** D-2026-16 builds the inventory as an artefact with a guard, so the
question "has every durable action been classified" has an answer that does
not depend on my say-so.

**WHAT DOES NOT CHANGE.** The P0-1..P0-6 records stay exactly as written.
Each described a real defect, reproduced it, repaired it and tested it. None
of them is retracted. What is retracted is the sentence that the class was
closed.

**INVALIDATED CLAIMS.** "All six P0 items are done." The accurate statement
was: the known examples of six defect classes were repaired, and the sweep
for further instances was incomplete.

---

## D-2026-11 — an escalation could be raised in somebody else's name

**STATUS** — repaired.

**DEFECT.** `ACT_ESCALATION` rebuilt the escalation from its payload and
applied it verbatim. Nothing required `raised_by == ev.actor`, so any
principal could open an escalation attributed to any other.

Reproduced: agent `A` appended a record naming agent `B`; replay reported
the escalation OPEN, `raised_by='B'`. A second reproduction had `A` name a
registered **HUMAN**.

**INVARIANT.** An escalation is attributed to whoever raised it. The payload
may repeat that; it may not establish it.

**WHY IT IS WORSE THAN FALSE ATTRIBUTION.** The raiser may not answer their
own escalation — a rule added in D-2026-04. So naming a person as the raiser
is a way to *stop that person answering it*. A forged attribution becomes a
denial of the human gate.

**ROOT CAUSE.** D-2026-04 repaired the ANSWER side of this record and left
the CREATION side. The two are the same fact — who did this — serialized in
the same file, one branch apart. The `claim` branch three lines below has
always carried the rule, in these words: *a claim is attributed to the
instance that recorded it*.

**IMPLEMENTATION FIX.** The reducer refuses a record whose `raised_by`
differs from `ev.actor`. `escalate()` already binds `actor=raised_by`, so no
honest write can produce the mismatch — which is exactly why the reducer
needed the check: the only records that can differ are the ones that did not
come through the writer.

**ADVERSARIAL TESTS.** Agent-names-agent, agent-names-human,
human-names-agent, each parameterized; the honest path still replays; and
the writer is asserted to bind the two fields.

**MUTATIONS.** `A55` deletes only the binding and dies to the test written
for it.

---

## D-2026-12 — a message could be sent in somebody else's name

**STATUS** — repaired.

**DEFECT.** `ACT_MESSAGE` took `sender_instance` from the payload. One
principal could append a message attributed to another. Reproduced: `A`
appended, `sender_instance='B'`, and the projection attributed it to `B`.

**INVARIANT.** `message.sender_instance == ev.actor`. The durable event actor
is authority; the payload sender is serialization.

**WHY IT MATTERS.** Messages are what a later reader uses to reconstruct who
told whom what. A forgeable sender makes that reconstruction a record of
what the forger wanted it to say.

**ROOT CAUSE.** Identical to D-2026-11 and in the same reducer: the `claim`
branch immediately below states the rule and the message branch did not
inherit it.

**MUTATIONS.** `A56`. It dies to a test that reaches replay directly rather
than to an unrelated role check, which the directive asked for specifically.

---

## D-2026-13 — a redelivery could change everything a message id names

**STATUS** — repaired.

**DEFECT.** A duplicate `message_id` was treated as harmless redelivery when
`body_digest` matched. Five other immutable dimensions were never compared,
and the divergent record was then **silently discarded** as a duplicate.

Reproduced: a second record with the same id and body but a different
sender, recipient, task, subject and `in_reply_to` was accepted, and the
projection kept the first. Two records claiming one identity with different
semantics, and nothing anywhere said so.

**INVARIANT.** A duplicate immutable identifier is either an exact semantic
redelivery or a conflict. There is no partial-equivalence rule.

**ROOT CAUSE.** "What it said" was read as "its bytes". A message id names a
whole record: who sent it, to whom, about what, on which task, in reply to
what. The body is one dimension of six.

**IMPLEMENTATION FIX.** `MESSAGE_IMMUTABLE` names the dimensions an id
carries, once, and `message_identity_conflict()` returns the first
disagreement. Both the reducer and `send()` use it — the write path had the
same partial comparison. `sent_seq` and `delivered_to` are deliberately
excluded: the log assigns one and delivery accumulates the other.

**ADVERSARIAL TESTS.** One field at a time, parameterized, so each invariant
is independently established and the failure names which dimension caught
it; a multi-field case; the same partial resend through the writer; and
anti-vacuity — an exact redelivery is still an idempotent no-op, three times
over. Plus a test that every named dimension is a real `Message` field, so a
name that drifted would compare `None` to `None` forever rather than
silently checking nothing.

**MUTATIONS.** `A57` restores the body-only comparison exactly; `A58`-`A61`
drop one dimension each, so no single field rests on another's coverage.
`A18` and `A19`, which attacked the old body-only condition, were **repaired
in place** rather than retired — they attack the same rules at the rewritten
lines and keep their history.

---

## D-2026-14 — a network decision was a bearer token for its whole grant

**STATUS** — repaired. **This supersedes part of D-2026-07.**

**DEFECT.** Four holes, all reproduced against the head that D-2026-07 had
already repaired:

1. `pinned_ports` carried the **grant's** port set, so a decision issued for
   `a.example.com:443` was spendable on `:8443`.
2. `pinned_addresses` carried the **grant's** pin set, so the same decision
   was spendable on any other address the grant pinned.
3. In unpinned mode an **unrelated host** on a permitted port was allowed.
4. `_covers` guarded both the address-class and the pinning checks behind
   `if addr is not None`, so a request that simply omitted
   `resolved_address` **skipped both** — and a grant pinning exactly one
   address was satisfied by a request that resolved to nothing. The bypass
   was one keyword argument.

**INVARIANT — three levels, and the middle one was missing.** A GRANT is the
set of operations an actor might request. A DECISION is one particular
operation authorized at one particular point. The ACTUAL OPERATION must be a
realization of the decision. It is not enough that it independently
satisfies the grant.

**WHY D-2026-07 DID NOT CATCH THIS.** That repair asked "is the port checked
at all" and answered it correctly. It did not ask "checked against what".
Carrying `grant.ports` onto the decision *looked* like conservation — the
dimension was now present at the enforcement point — and was the bearer-token
shape written down. **A set on a decision is the defect.**

**IMPLEMENTATION FIX.** The decision carries `authorized_host`,
`authorized_port`, `authorized_scheme`, `authorized_address` and
`address_mode`, all from the REQUEST. `pinned_ports` is retired: a set of
ports on a decision is exactly the confusion. `pinned_addresses` stays as
audit context and is no longer the enforcement set. The guard checks the
port exactly in both modes, the exact resolved address in PINNED mode, and
the exact host **name** in UNPINNED_ACCEPTED mode when the connect target is
a name rather than an address.

**WHAT UNPINNED ACTUALLY WAIVES.** Not knowing which ADDRESS a name resolves
to. It does not waive which name was asked for, which port, which actor,
task, tool, scheme or method — those were consumed by the authorization and
cannot be re-established from a bare socket call, which is said in the code
rather than papered over.

**THE CLASS CHECK, MOVED RATHER THAN DEMANDED.** Requiring a resolution at
`authorize()` would have refused every unresolved request — the module's
normal calling convention — and 30 tests said so. That is a contract
redesign, not a repair. Pinning still requires a resolution, because a pin
is a statement about a specific address and skipping it is not passing it.
The address CLASS is now checked by `socket_guard`, which holds the address
the connection is actually going to and can answer what the authorization
could not. **Stronger than before and compatible with the callers.**

**TWO OF MY OWN P0-4 TESTS ASSERTED THE DEFECT.**
`test_every_granted_port_is_permitted_not_just_the_first` said a grant
naming several ports means the guard is "a set membership test", and checked
that a decision for `:443` also permitted `:8443`. That is the bearer-token
semantics written down as the requirement.
`test_the_decision_carries_the_ports_it_was_granted_for` asserted the field
whose existence was the defect. Both are replaced, and their replacements
say what they replaced and why. This is the third time this ledger records a
test that encoded a defect; the first two were pre-existing and this pair is
mine.

**MUTATIONS.** `E45`-`E51` attack the request→decision→socket composition —
the decision echoing the grant, the guard skipping the port, the decision
forgetting its resolution, the unresolved-pin bypass, the deferred class
check, and unpinned becoming any-hostname. `W24` was repaired in place.
`E47` was dropped as a duplicate mutant of `W24`. `E48` **survived its first
run** because my test resolved to the grant's *first* pinned address, where
a decision that merely echoed `g.addresses[0]` computes the identical value;
the test now resolves to the second, and says so.

---

## D-2026-15 — the production caller's docstring claimed containment it does not have

**STATUS** — repaired, as corrected prose. **No containment was added.**

**DEFECT.** `governed_stage10.py`'s module docstring said the tool runs
inside a network guard "so a dependency that phones home is refused", and
that "if the tool opens a socket, the run does not complete". The real tool
runs as a **subprocess**. The guard is a monkeypatch in the supervisor's
interpreter and does not exist there.

**THE FILE CONTRADICTED ITSELF.** The comment at the enforcement point
already said the guard "binds this process, not the child -- said here
because the difference matters **and the module says so too**." The module
said the opposite. Two representations of one fact, in one file, and the
honest one deferred to the overclaiming one.

**FIX.** The docstring now separates supervisor-level mediation from child
containment, states that the kernel bounds applied to the child restrict CPU,
address space, output size, process count and core dumps and **not**
networking, and points at the probe that demonstrates a child opening a
socket. The enforcement comment no longer claims the module agrees; it says
the module now says the same thing.

**SIBLING SWEEP.** `netauth.py`'s docstring was already exemplary — it names
a "kernel layer (NOT provided)" outright. The completion matrix was already
honest in five separate boundaries (R21, R22, R32, R33, R54), each saying
the guard binds this process and not the child. `AGENT_SUBSTRATE.md` had one
line reading as containment in an authority context, now qualified.
`test_execution_runs_inside_the_network_guard` was accurate but could be
misread, and now says what it covers. **The overclaim was localised to the
one file a reader of the production caller reads first.**

**FORBIDDEN FAKE FIXES.** Adding a `sitecustomize` to reach the child, which
is a monkeypatch one process further. Deleting "no egress grant", which is
true and load-bearing. Weakening the child-networking test so the stronger
prose becomes true.

---

## D-2026-16 — a failed 3D solve still CONSTRUCTED the future it must not report

**STATUS** — repaired.

**DEFECT.** D-2026-09 made a failed solve report `NaN` for its energy
residual. The quadrature that produced the number still ran: `sol_obj.sol(tq)`
was evaluated across the whole requested window, and an `OdeSolution` asked
for a time past the interval it covers **extrapolates its final polynomial
rather than refusing**. So every quantity in the accounting was computed
partly over time the integrator never reached, and then discarded.

**INVARIANT.** If the integration did not reach the required end state, the
full-window accounting must not pretend the missing interval exists — not in
what it reports, and not in what it computes.

**WHY THE DIFFERENCE IS REAL AND NOT PEDANTRY.** NaN stopped the arithmetic
being BELIEVED, which was the authority defect. It did not stop it being
PERFORMED. A quantity that must not be trusted should not be computed:
computing it invites some later reader to use it, and the act itself asserts
that the missing interval exists.

**IMPLEMENTATION FIX.** The failed case branches **before** any
interpolation and returns what was really integrated — the trajectory, its
times, the solver's message — with an accounting dict that says
`UNAVAILABLE_INTEGRATION_INCOMPLETE` and reports how far the integration
actually got against what was asked, so the failure is diagnosable.

**ADVERSARIAL TESTS.** The interpolant itself is spied on: the test asserts
**zero** evaluations on a failed solve, and names the integrated end time in
its failure message. Paired with the anti-vacuity twin — the converged path
DOES evaluate the interpolant, so the assertion establishes something about
convergence rather than about a code path nobody uses. Plus an assertion
that the truncated trajectory really is shorter than the request, so the
test cannot pass over a case it did not create.

**MUTATIONS.** `SC6` retargeted from the NaN to the short circuit — NaN
stopped belief, the short circuit stops the act. `SC8` makes the failed
branch announce itself converged; `SC9` makes it claim it reached the
requested end, which would also make the test's own anti-vacuity assertion
vacuous.

---

## D-2026-17 — the harness's own scratch directory became governed text

**STATUS** — repaired.

**DEFECT.** The RAG corpus scan walked `.mutation-quarantine/`, where the
mutation harness stashes a tracked file it finds changed under a running
matrix. Those are **copies of governed documents**. The scan offered them as
governed text in their own right, and
`tools/corpus_allowlist.py --write` **admitted one**: a quarantined copy of
`AGENT_SUBSTRATE.md` was written into `docs/corpus_allowlist.json` as a
governed document.

**INVARIANT.** A document becomes governed by being reviewed into the
allowlist, not by being created. That is the sentence `load_allowlist`
already carries, and the scan feeding it did not honour.

**HOW IT HAPPENED, WHICH IS THE INTERESTING PART.** I edited two tracked
files while a mutation matrix was running. The harness noticed, reverted
them, and quarantined my versions — behaving exactly as designed. The
quarantine directory then sat inside the repository root, and the next
`--write` swept it in. **A safety mechanism's output became an input to a
trust boundary.**

**CAUGHT BY.** The corpus completeness check, which compares the scan
against `git ls-files` in BOTH directions. It failed on "extra items in the
left set". Had I committed the regenerated allowlist and then removed the
quarantine, it would have failed in the other direction instead — naming a
document that does not exist. Either way it refused; a one-directional check
would have accepted the first.

**IMPLEMENTATION FIX.** `.mutation-quarantine` is named in `EXCLUDED_DIRS`
with its reason, and — the part that matters — **no dot-directory under the
root is governed text**. The named list has to be remembered; the rule does
not. `.git` and `.venv` were already listed individually, which is the same
fact discovered three times without being generalized.

**ADVERSARIAL TESTS.** A tree containing a quarantine copy, a `.git` file, a
`.scratch` file and a plausible future tool's directory, asserting only the
real document is offered. Paired with the anti-vacuity twin: a document whose
own FILE name begins with a dot is still governed, so the rule is about
directories rather than about leading dots.

**MUTATIONS.** `C11` restores the hole. `C12` is the over-correction —
excluding the file name too — which is fail-closed and would therefore look
like the safer choice while silently dropping real documents.

**MY OWN ERROR, RECORDED.** Editing tracked files while a mutation matrix
runs is a mistake this session has now made twice. The harness caught it
both times and both times the recovery cost real work. The rule is: **while
a matrix holds the tree, do read-only work only.**

**SIBLING SWEEP.** Other repo-local scratch that could reach a trust
boundary: `.exec-*` execution scratch directories are also dot-prefixed and
now excluded by the same rule. The manifest generator has its own policy and
refuses untracked files outright, which is why it reported the quarantine as
"untracked and not ignored" rather than absorbing it.

---

## D-2026-18 — secret authority was not event-sourced, and calling that a boundary was wrong

**STATUS** — repaired. Option A adopted.

**THE MISCLASSIFICATION FIRST.** D-2026-05 recorded "SecretStore has no
reducer" as a **stated boundary**. It is not a boundary. It is ordinary
engineering inside this repository, and the test for that is the question
the directive names: could ordinary work here implement the missing
behaviour without unavailable external evidence or privileges? Yes. Calling
it a boundary because the reducer did not exist confuses *what is absent*
with *what cannot be built*.

**DEFECT.** The store's in-process dicts were the authority; the log sat
beside them as an audit trail. That contradicts the architecture's own
statement that the log is the truth and everything else is derived from it.
Two consequences, both reproduced:

* **a fresh `SecretStore(log)` saw ZERO grants.** Secret authority did not
  survive a restart at all. Fail-closed for USE, and it meant the durable
  record and the live state were unrelated objects: the log said a grant
  existed and the system that reads the log disagreed;
* a forged revocation appended around `revoke()` was re-read by nothing in
  the module. The independent reconstruction noticed it, which made a
  **diagnostic reader the only enforcement**.

**THE CLEAREST EVIDENCE THAT NOTHING REPLAYED.** `secrets.py` had no
`grant_from_record`. Every other authority object in the package has had one
from the start. A module that never rebuilds its objects from records does
not need a function for it.

**IMPLEMENTATION FIX — OPTION A.** `load()` and `apply()` reconstruct grant
AUTHORITY from the log: grant ids, subject, task, tool, secret id, purposes,
issuance position, expiry, revocation and the granting actor. The reducer
re-authorizes the revoker, refuses a revocation naming a grant never issued,
refuses a grant that predates its own record, refuses a rebound id, and
verifies the grant digest against its body. `issue()` and `revoke()` now
**fold through `apply()`** rather than assigning beside it, so the write path
and the replay reach the projection by the same route.

**WHAT STAYS OUT OF THE LOG, AND WHY THAT IS THE POINT OF OPTION A.** The
secret VALUE, and **no digest of it either**. A digest of a low-entropy
credential is an offline guessing oracle, so the record carries the *grant's*
digest — over ids and purposes — and nothing derived from the bytes. A test
asserts the plaintext, its sha256, a 16-char prefix of that, and its base64
are all absent from the durable record.

**THE DELIBERATE CONSEQUENCE.** After a restart the authority is known and
the value is not, so `resolve()` raises `UnknownSecret` rather than losing
the grant. *"I may not have it"* and *"nobody ever granted it"* are different
answers, and the second would be a lie. Tested in both directions:
provisioning the value again makes the reconstructed grant work.

**A PIN THAT DID ITS JOB.** D-2026-05 left
`test_this_store_has_no_replay_and_says_so`, asserting
`not hasattr(SecretStore, "apply")`, with the note that whoever added a
reducer would have to come back and decide deliberately whether the
revocation rule belonged in it. Adding the reducer **failed that test**, and
the decision was made here rather than by accident. That is the mechanism
working, and it is the argument for pinning a boundary rather than only
writing it down.

**MUTATIONS.** `S31`-`S35` attack each reducer rule. `S27` was repaired in
place when `issue()` gained an early return.

**A MUTATION WRITTEN AND DROPPED.** `S36` made `issue()` assign the
projection directly instead of folding through `apply()`. It **survived**,
and it is equivalent by construction: `issue()` builds the grant itself,
stamps `issued_seq` from the log, computes the digest from the same object
and refuses a duplicate id before appending, so every reducer check passes
trivially and no test can tell the two routes apart. The fold is kept —
it is what stops a rule added to the reducer being missing from the writer —
but that is a property of *future* changes, not a behaviour available to
test now, and a mutation that cannot be killed honestly is not coverage.

**INVALIDATED CLAIMS.** D-2026-05's classification of this as a boundary,
and the statement in my previous report that "SecretStore has no reducer, so
its revocation rule runs on one path only". The completion matrix never made
this claim — its R33 boundaries are about value handling, redaction,
zeroing, provider kinds and egress composition, none of which this touches.

---

## D-2026-19 — escalations had no second reader, while the campaign said every subsystem had one

**STATUS** — repaired, and the claim replaced by a measurement.

**DEFECT.** `reconstruct.py` reconstructed nine subsystems and escalations
were not among them. The hosted workflow step was named *"mutation matrix --
the second reader for every subsystem"*. Both could not be true.

Escalations are the worst subsystem to omit: they carry the human decisions,
and D-2026-04 and D-2026-11 both found forgeries in exactly those records.

**IMPLEMENTATION FIX.** `_sub_escalation` and `_sub_escalation_answer`
restate the create, answer and withdraw rules in plain dictionaries: unique
id, raiser bound to the event actor, born OPEN, at least two distinct
options, a question that asks something, no answer carried at creation; then
existence, terminality, answerer bound to the actor, registered, active,
HUMAN, not the raiser, answer among the options; and withdrawal by the asker
alone. `ANSWERED` and `WITHDRAWN` are terminal.

**INDEPENDENCE, CHECKED OVER THE AST.** The reader imports nothing from
`agents`, `scheduler`, `policy`, `capability`, `memory`, `netauth`,
`secrets` or `context`. My first version of that test searched the source
TEXT for "AgentDirectory" and failed — on this module's own prose about the
layers it deliberately does not import. A check that fails for being right
is a bad check; it reads the parsed imports now.

**ADVERSARIAL TESTS.** Sixteen, including the composition case: an agent
that forges a HUMAN registration and then answers with it is refused at the
registration, and the escalation stays OPEN — so a bypass in one subsystem
is not answered by silence in the other. The asker-answers-their-own case is
isolated with a HUMAN raiser, because with an agent raiser the KIND check
catches it first and that rule is never reached.

**MUTATIONS.** `R29`-`R40`, one per restated rule. `R40` **survived** its
first run: nothing sent a decision state that was neither ANSWERED nor
WITHDRAWN, and without the check such a record falls past the withdrawal
branch into the answer branch and is projected as ANSWERED whatever it
claimed. The test now sends `OPEN`, `REOPENED`, `""` and `None`.

---

## D-2026-20 — the sweep had no artefact, so its completeness was unverifiable

**STATUS** — repaired. This is the fix for D-2026-10.

**DEFECT.** Every claim of the form "I swept the siblings" in this ledger
rested on my having looked. There was no table, no generated list and no
test that could be checked independently. That is the same
self-asserted-authority shape the substrate refuses everywhere else, applied
to the process rather than the code — and it is why the escalation raiser
and the message sender survived a sweep written to find exactly them.

**FIX — `docs/identity_inventory.json` and `tools/identity_inventory.py`.**
The classification is REVIEWED, because deriving intent from source would be
guessing and a guess that looks mechanical is worse than a judgement that
says it is one. Everything around it is MEASURED:

* every `ACT_*` constant must appear — a new durable action fails the check
  until somebody classifies it;
* no entry may name an action that no longer exists;
* every field classified `ACTOR` must record a write path, a replay rule and
  a regression test, **and that test must exist**;
* the recorded independent-reader coverage must equal what `reconstruct.py`
  actually dispatches on.

**THE GUARD CAUGHT ME IMMEDIATELY.** The first draft named five regression
tests that do not exist —
`test_a_forged_claim_cannot_be_attributed_to_another_instance` and four
others. Plausible names, guessed rather than checked. The checker failed on
all five within a minute of existing, and the real names came from the
source. It also caught `record.create`'s field being `proposer`, not
`submitter`.

**WHAT THE MEASUREMENT SAYS.** 37 durable actions. **28 independently
reconstructed, 9 not:** `agent.claim`, `agent.message`, `file.read`,
`network.result`, `secret.access`, `secret.provision`, `task.compensation`,
`task.reexecution`, `task.separate_verification`. The CI step title that
said "every subsystem" now says "the 28 of 37 it covers", and a test fails
if coverage ever becomes total without the prose being updated — the
over-claim guard pointed in both directions.

**ONE MORE FIELD FOUND BY BUILDING IT.** `task.compensation` carries
`answered_by`, a copy of the escalation's answerer. It is read from the
escalation projection, so it is not forgeable through the writer — but the
record is `continue`d by the reducer, so a hand-appended one could name any
person as having authorized destroying something, and nothing compared it.
It is classified `DIAGNOSTIC_DUPLICATE` and is now compared to the
escalation it cites. **A convenience that nothing checks is a field a forged
record sets freely.**

**FORBIDDEN FAKE FIXES.** Deriving the role classification with a regex over
field names and calling the table generated. Recording coverage as a number
somebody types. Marking the nine unreconstructed actions as boundaries —
they are ordinary engineering, and the inventory says so by naming them
rather than by excusing them.

---

## D-2026-21 — the convergence rule had five call sites and sixteen consumers

**STATUS** — repaired.

**DISCOVERED BY.** The §12 recheck of P0-6. Not by re-reading the P0-6 repair,
which is correct, but by asking the question §17 asks of every repair: *where
else is this same fact consumed?* The fact is "this solve did not converge".
The answer was: in eleven places that never asked.

**DEFECT.** `require_converged` was written for `run_coupled`, and D-2026-09
extended it to `run_mode_sequence_3d` and the reduction checks. That is five
call sites. This package had **sixteen** places that read a published number
off a solve result. The other eleven took whatever came back:

| where | what it published from a solve nobody asked about |
|---|---|
| `convergence_3d.convergence_report` ×3 | the mesh-convergence and time-convergence verdicts, and the baseline both are measured against |
| `sensitivity_3d._rise` | every normalized sensitivity in the OAT ranking |
| `stack/mdao_openmdao.evaluate` | the objective an OpenMDAO study optimises |
| `stack/sensitivity_salib.screening_response_K` | the response Sobol indices are computed from |
| `runner_3d` heavy pass | a probe timeseries, a hotspot table and an energy accounting, written to disk |
| `verification.mesh_convergence_1d` | `thermal_1d_converged` |
| `verification.mesh_convergence_2d` | `thermal_2d_converged` |
| `verification.axis_symmetry_2d` | `axis_grad_small_vs_bulk` |
| `verification.reduction_2d_to_1d` ×2 | `reduces_to_1d` |
| `verification.coupling_checks` | `optical_feeds_thermal` |
| `uncertainty.run_monte_carlo` ×3 | the Mode-C recool distribution, the readiness fraction, and the ensemble's own mesh criterion |

**INVARIANT.** A number derived from a solve carries the authority of that
solve. A solve that did not converge carries none, so no published quantity
may be derived from one — wherever the derivation happens.

**ROOT CAUSE.** The rule was applied where each defect was found. `numerics.py`
already says this about itself, in the docstring of the very function
involved: *"A rule that lives inside one of the things it governs is a rule
the next sibling does not inherit."* It was moved to the numerics layer for
exactly that reason and then still only called from the sites that had already
failed. Availability is not application.

**WHY THE DIRECTION OF THE ERROR MATTERS.** A failed integration does not
return garbage. It returns a **shorter** trajectory in which every value is
finite, so `assert_finite` passes and the numbers look ordinary. Sampled
`[-1]`, it reports the last time REACHED rather than `t_end` — which is
**cooler**. So an unguarded failure does not add noise:

* in a screening study it flips the **sign** of a reported sensitivity, because
  the perturbed configuration is the one that stiffens and its truncated probe
  reads colder than the base;
* in the recovery ensemble it reports a **faster** recool and a final
  temperature that never finished cooling — both in the direction of "ready";
* in `axis_symmetry_2d` a truncated field has a **smaller** axis gradient
  because it has had less time to develop one, so a failed solve is *more*
  likely to certify axis symmetry than a converged one;
* in `coupling_checks` a diverging solve produces a very large hotspot, which
  is precisely what "optical feeds thermal" is confirmed by.

Each of these is a check that a failure makes *more* likely to pass.

**AFFECTED REPRESENTATIONS.** Implementation at every site above. No durable
record, no replay and no second reader: this layer is forecast computation,
not the agent substrate. Prose: `numerics.require_converged`'s own docstring
claimed the move to the numerics layer solved the inheritance problem; it did
not, and it now says which sites call it.

**IMPLEMENTATION FIX.** `require_converged` at all eleven, phrased in each
site's own vocabulary so a failure message names what was being computed.
`uncertainty.run_monte_carlo` is the one exception to raising: it is an
ensemble that already counts a failed sample and carries on, and both of its
new guards sit inside the `except Exception:` that does exactly that — so the
shared rule lands on the shared counter instead of inventing a second
convention.

**A SECOND DEFECT, FOUND BY THE TEST FOR THE FIRST.** With the recovery solve
guarded, the two-sample ensemble reported `pde_stability_failure_count == 1`
**and** `n_evaluated == 2`. The Mode-B values were appended to the published
distributions *before* the Mode-C solve was attempted, so a sample the run had
itself classified as a PDE failure was also present in the Mode-B statistics,
and `n_evaluated + pde_fail` could exceed `n_samples`. A sample now enters the
distributions only once every solve it needs has succeeded. This was reachable
before this change too, through the `except Exception` that already existed.

**A REFACTOR THAT IS PART OF THE FIX.** `runner_3d`'s heavy pass was inline in
`run_3d_all`, so the only way to reach it was to run the entire 3D pipeline
first. The one call site in that module that never asked whether its solve
converged was also the one no test could reach, which is not a coincidence: an
unreachable branch is an unguarded branch. It is now `write_heavy_pass`, a
module-level function with the same body, called from the same place.

**ADVERSARIAL TESTS.** Twenty-one new in `tests/test_solver_failclosed.py`, one per
guard rather than one per module, because what a failed solve would have
produced is different at each. Every stub returns **usable** numbers rather
than nothing — deleting a guard must let the caller SUCCEED with a wrong
answer, so the test fails for the reason it was written for instead of on a
missing attribute. Injection is at a chosen call index, so the tests reach the
*third* solve of a three-solve function rather than only the first: SC12 and
`n=400` are exactly the mutations a test that only injects at call 1 leaves
alive. Anti-vacuity pairs: an honest convergence report takes all three
solves (asserted by count, so the at=3 injection is known to fire); an honest
heavy pass writes all four of its files; an honest two-sample ensemble
evaluates two samples and counts zero failures.

**ONE TEST IS NOT ABOUT CONVERGENCE AT ALL.** The tightened-integration guard
sits inside the `try/finally` that restores `cfg.solver.rtol`. A guard that
raised past the restoration would leave the shared configuration permanently
tightened for every later caller in the process. Asserted separately.

**MUTATIONS.** `SC10`–`SC25`, sixteen: one per restored guard, plus `SC25`
for the ensemble accounting — it restores the original ordering verbatim,
which is the defect exactly as it was found. 25/25 killed, sources restored
byte-identical. `SC20` and `SC21`
are deliberately both kept: the reduction check is a COMPARISON, and two
solves that both stopped early agree with each other, so a test that guards
only one side proves nothing about the other.

**FORBIDDEN FAKE FIXES.** Recording `solver_status` beside the result and
leaving the verdict computed from it regardless — that is the defect
`mesh_convergence_1d` already had. Catching `SolverFailure` at any of these
sites and substituting a default. Widening `SOLVER_OK` to admit a second
status. Making `energy_accounting_rows` tolerate a failed result's accounting
dict: it raises `KeyError` on one today, which is ugly but closed, and making
it *return* something would open it.

**SIBLING SWEEP.** Every call of `solve_thermal_1d`, `solve_thermal_2d` and
`solve_thermal_3d` in `qta_multiphysics/` was enumerated and checked; the
sixteen consumers above are the complete list, and `future_3d.py:58` is a
passthrough that returns the result to a caller rather than reading it.

**INVALIDATED CLAIMS.** Any earlier statement that P0-6 closed the
convergence contract. It closed the mode sequence and the accounting inside
the solver. The class stayed open for two more subsystems and the whole
forecast-screening surface.

---

## D-2026-22 — two mutations were written for D-2026-17 and never run

**STATUS** — repaired.

**DISCOVERED BY.** Hosted CI, at `104a6f1`. `agent-substrate` had run
thirty-seven mutation matrices green and failed on the thirty-eighth: step 47,
`corpus_allowlist`, `killed: 10/12`. Not by any local check, because the local
sweep that covered every spec predates the commit that added these two.

**DEFECT.** D-2026-17 repaired the corpus scan — no dot-directory is governed
text — and added `C11` (delete the rule) and `C12` (over-correct it to exclude
dot FILES as well) to the spec. It added no test that distinguishes either
from the fix, and the spec was not re-run in that sitting. So the record
claimed a repair whose only evidence was that the code looked right.

**WHY IT MATTERS MORE THAN "two survivors".** The seven steps after it in the
workflow — including `stage10_authority`, the corpus-allowlist completeness
check, the long-horizon campaign, the fuzz pass, the governed production path,
the auditor, and the two post-campaign source-integrity checks — are `skipped`
when a step fails. One unrun spec did not cost one matrix; it cost the tail of
the job.

**INVARIANT.** A mutation added to a spec is run before the sitting that added
it is called finished. A defect record's repair is evidenced by a mutation that
dies, not by a mutation that exists.

**IMPLEMENTATION FIX.** None — the code was already correct. Both survivors
were a missing test, which is the honest diagnosis and the reason this record
exists rather than a code change.

**ADVERSARIAL TESTS.** Two, paired on purpose, because the rule sits between
two mistakes and only running both distinguishes it from either:

* `test_no_dot_directory_anywhere_is_governed_text` plants a copy in
  `.mutation-quarantine/<stamp>/` **and** a file in `docs/.cache/`. The second
  is load-bearing: `.mutation-quarantine` is in `EXCLUDED_DIRS` as well, so a
  test using only that name passes with the rule deleted. The unnamed
  dot-directory is the one the class rule exists for.
* `test_a_document_whose_own_name_begins_with_a_dot_is_still_governed` plants
  `.release-notes.md` at the root. The over-correction is **fail-closed**, so
  it looks like the safer choice; silently narrowing what retrieval may quote
  is the same class of unreviewed change as admitting an extra document, in
  the other direction.

**MUTATIONS.** `C11` and `C12`, unchanged — they were already the right
mutations. 12/12, sources restored byte-identical.

**CONFIRMED HOSTED.** `agent-substrate` at `7dc9a1f` completed all 55
steps green, including steps 48–55 — the corpus-allowlist completeness
check, the long-horizon campaign at elevated scale, the fuzz pass, the
governed production path, the read-only auditor, `stage10_authority`,
"sources are unchanged after mutation testing" and "the substrate did
not touch the canonical tree" — none of which had run at `104a6f1`.

**INVALIDATED CLAIMS.** D-2026-17's implicit claim that its repair was
verified. It was implemented and asserted; it was not verified until now.
---

## §12 — the recheck of P0-1 … P0-6 after the sibling work

Not "the earlier repairs are still in the file". The directive asks eight
questions of each, and the eighth column is the one that matters: a mutation
spec nobody runs on a hosted runner protects nothing.

| | P0-1 lapse/budget | P0-2 identity | P0-3 destruction | P0-4 network | P0-5 child egress | P0-6 convergence |
|---|---|---|---|---|---|---|
| write path tested | yes | yes | yes | yes | n/a — prose | yes |
| replay tested | yes | yes | yes | yes | n/a | n/a — no durable record |
| second reader | yes | 28 of 37 actions, measured | yes | yes | n/a | n/a |
| sibling mutation spec | `agent_scheduler`, `agent_cross_process`, `agent_lease_renewal`, `agent_incremental` | `agent_agents`, `agent_actions` | `agent_delegation`, `agent_memory_context`, `agent_secrets`, `agent_secret_provider` | `agent_netauth`, `agent_service_authority` | `stage10_authority` | `solver_failclosed` |
| wired into hosted CI | yes | yes | yes | yes | yes | yes |

`tools/workflow_contract.py` answers the "wired" row by construction rather
than by inspection: it fails if any spec on disk appears in no workflow step.
It reports 38 of 38.

**The row that is not a tick.** P0-2's second reader covers 28 of 37 durable
actions. The nine it does not are named in this ledger's follow-up list and
printed by `tools/identity_inventory.py` on every CI run. That is a residual
engineering gap, **not a boundary** — §13's test is whether ordinary work in
this repository could implement it, and it could.

**The local sweep behind the "baseline green / post-mutation baseline green /
source restoration" columns.** Every spec whose target files this tranche
touched was re-run serially after the last edit — never two at once, because
the matrices edit sources in place:

`agent_actions` 6/6 · `agent_compensation` 15/15 · `agent_idempotency` 20/20 ·
`agent_job_graph` 12/12 · `agent_readpath` 21/21 · `agent_recovery` 13/13 ·
`agent_separate_verify` 11/11 · `agent_tasks` 32/32 · `governed_breadth` 17/17 ·
`agent_delegation` 27/27 · `agent_lease_renewal` 11/11 ·
`agent_service_authority` 14/14 · `agent_substrate` 46/46 ·
`agent_secret_provider` 18/18 — 263, none surviving, every run reporting
sources restored byte-identical. With the six run earlier in the tranche
(`agent_agents` 61, `agent_netauth` 50, `agent_secrets` 35,
`agent_second_reader` 40, `solver_failclosed` 25, `corpus_allowlist` 12) that
is 486.

Then the specs this table NAMES but whose target files this tranche did not
touch, so that no row above rests on a hosted result alone:
`agent_scheduler` 64/64 · `agent_cross_process` 8/8 · `agent_incremental` 9/9
· `agent_memory_context` 40/40 · `stage10_authority` 15/15 ·
`repo_contract` 12/12. **634 mutations across twenty-six specs, none
surviving.**

`stage10_authority` is the one that mattered most to run. It is step 53 of 55,
and the failure at step 47 recorded in D-2026-22 SKIPPED it — so P0-5's own
spec had no result at that commit, hosted or local. A spec that is wired into
CI and never reaches the runner is wired in the sense the contract checks and
not in the sense that matters.

**A checker that reported a defect that was not one.** `generate_manifest.py`
refuses any path that is untracked and not ignored, which is right. But
`qta_agent/execution.py` creates a `.exec-<random>/` scratch directory beside
the run and removes it in its `finally`, so a process that dies before that
`finally` — a SIGKILL, a killed test session — leaks one, and the next
manifest check reports MANIFEST DRIFT for a crash that had nothing to do with
the manifest. Observed here, on a test session stopped mid-run. `.exec-*/` is
now ignored, which is what the checker's own message suggests. Recorded
because a verification tool that cries drift for unrelated reasons is a
verification tool people learn to re-run rather than read.

**What the recheck actually found.** P0-6 did not survive it. The repair was
correct and the class was open: `require_converged` had five call sites and
sixteen consumers. See D-2026-21. This is the second time in this tranche
that "the example is closed" and "the defect is closed" turned out to be
different statements, which is the whole reason §12 exists as a separate
step rather than as a closing sentence.

---

## §15 — the P0 acceptance gate, condition by condition

The directive names twenty-three conditions and says not to write *P0
complete* until all are true. Each is answered by something that refuses when
it stops being true, or it is not answered.

| # | condition | what makes it true, and what would notice if it stopped being |
|---:|---|---|
| 1 | retry budget / lapse semantics remain closed | `reauthorize_job_edge` names the handover edges; the second reader restates the budget bound so an overrun is a finding about the HISTORY. Mutations in `agent_scheduler`, `agent_cross_process`, `agent_lease_renewal`. |
| 2 | every actor-bearing durable event classified | `docs/identity_inventory.json`, 37 actions. `tools/identity_inventory.py` fails if the file and the code disagree in either direction, and runs on every CI push. |
| 3 | every actor-duplicate payload field bound to `ev.actor` | Each ACTOR-role field in the inventory names its write-path binding, its replay rule and a regression test that exists — the checker verifies the test exists, because five of my first draft's names did not. |
| 4 | escalation raiser provenance enforced | `raise_escalation` binds `actor=raised_by`; the reducer refuses `esc.raised_by != ev.actor`. D-2026-11. |
| 5 | escalation answer provenance remains enforced | Unchanged from D-2026-04 and re-covered by the new second reader, which refuses an answer from anyone but the assignee. |
| 6 | message sender provenance enforced | `send` binds `actor=sender_instance`; the reducer refuses a mismatch. D-2026-12. |
| 7 | duplicate message ids cannot mutate semantic identity | `MESSAGE_IMMUTABLE` names seven fields, not one; `message_identity_conflict` reports which one changed; one mutation per dropped field. D-2026-13. |
| 8 | destructive authority remains explicitly governed | D-2026-05's guards plus the five existing revocation tests that were passing on arbitrary actors and now name the issuer. |
| 9 | a decision cannot be reused for another operation in the same grant | `NetworkDecision` carries the exact authorized host, port, scheme and address; `pinned_ports` is retired. D-2026-14. |
| 10 | unpinned networking is not unrelated-host authority | An unresolved request is authorized against the grant's address CLASSES at connect time, not against "any host on an allowed port". |
| 11 | child-egress prose matches implementation | The Stage-10 docstring now separates SUPERVISOR/IN-PROCESS MEDIATION from CHILD/DESCENDANT OS CONTAINMENT and states which one this is. D-2026-15. |
| 12 | failed 3D solves cannot influence scientific readiness | `require_converged` at every consumer — and this is the condition the §12 recheck failed on first pass. D-2026-21. |
| 13 | failed solves do not quadrature an extrapolated window | The failed case branches before any interpolation; a spy asserts **zero** interpolant evaluations, paired with its anti-vacuity twin. D-2026-16. |
| 14 | secret authority is internally consistent with the event-sourcing claim | `SecretStore` folds through `apply()` from the log; grants are reconstructed from records. Values stay process-local and neither they nor their digests reach a durable record — asserted directly. D-2026-18. |
| 15 | escalations have an independent reader, or the claim is weakened | Both: `reconstruct_subsystems` gained a tenth subsystem that does not import `AgentDirectory`, **and** the CI step that said "for every subsystem" now says "for the 28 of 37 it covers". D-2026-19, D-2026-20. |
| 16 | all corresponding tests are anti-vacuous | Every failure-injection test in this tranche has a paired positive: the converged path DOES evaluate the interpolant, the honest sequence DOES enter Mode D, the honest report DOES take three solves, the honest ensemble DOES evaluate its samples, the mesh-check branch DOES fire. |
| 17 | every new mutation spec is wired into CI | `tools/workflow_contract.py`: 38 of 38, checked by the file's own contents rather than by assertion. |
| 18 | every mutation is killed for the intended semantic reason | Each survivor in this tranche was diagnosed rather than papered over: E48 resolved to the grant's first address, R40 sent a state that was neither answered nor withdrawn, SC3 never reached Mode D, M39 could not tell two layers apart, G26 hit the write path instead of the reducer, SC24 ran with the mesh-check fraction at zero, SC25 was a mutation that did not restore its own defect, C11 and C12 were written and never run at all. One (S36) was dropped as equivalent-by-construction rather than forced. |
| 19 | full affected tests green | Full local suite, plus 634 local mutations across twenty-six specs with none surviving and every run reporting sources restored byte-identical. Hosted `second-interpreter` and `cross-environment-3d` green at `104a6f1`; `full-suite` red on `package_consistency_check.py` only, which is the documented R59 host divergence — its pytest step passes and the job's own diagnostic names the runner's kernel. `agent-substrate` was RED at `104a6f1` on step 47 of 55 (D-2026-22) and is **green at `7dc9a1f` across all 55 steps**, including the eight that the earlier failure skipped. This condition is satisfied for a commit when that commit's own `agent-substrate` run is green; commits after `7dc9a1f` in this tranche change only this ledger, `.gitignore` and the derived artifacts, and each is confirmed by its own run rather than inherited from its parent — which is the whole lesson of D-2026-22. |
| 20 | defect ledger updated | D-2026-11 … D-2026-21, plus the PREMATURE_CLOSURE record and two ANALYST_CONCLUSION_ERROR records for my own wrong claims. |
| 21 | PASS remains 0 | Unchanged; `package_consistency_check.py` asserts no PASS token in any output on every run. |
| 22 | `automatic_gate_effect` remains NONE | Unchanged. |
| 23 | PR #17 remains unmerged | Open, not merged, no merge requested. |

---

## D-2026-23 — two defect records described a repair no commit ever made

**CLASS** — `DOCUMENTATION_OVERCLAIM`, `ANALYST_CONCLUSION_ERROR`,
`PREMATURE_CLOSURE`.

**DISCOVERED BY.** An external hostile review, after `5e761fc` declared P0
complete. It read the live source instead of this ledger.

**DEFECT.** `qta_agent/governed_stage10.py` — the production caller, the file
a reader of this substrate opens first — claimed:

> the tool runs inside a network guard with no egress grant, so a dependency
> that phones home is refused rather than merely undeclared

and

> if the policy denies, if readiness fails, if the separation check refuses,
> **or if the tool opens a socket**, the run does not complete

The tool is executed as a **subprocess**. `socket_guard` replaces
`socket.socket.connect` in the process that enters it, and a subprocess does
not inherit a Python monkeypatch. Both sentences are false of the tool.

**WHAT MAKES THIS RECORD DIFFERENT FROM D-2026-08 AND D-2026-15.** Those two
already describe this defect. D-2026-15 is titled *"the production caller's
docstring claimed containment it does not have"*. Neither of the commits
carrying them modified `qta_agent/governed_stage10.py`. Not one line:

```
$ git show 4ece228 -- qta_agent/governed_stage10.py     # empty
$ git show 104a6f1 -- qta_agent/governed_stage10.py     # empty
$ git log --all -S "WHERE THE NETWORK GUARD REACHES" \
      -- qta_agent/governed_stage10.py                  # empty: never existed
```

The session record for that tranche states the module docstring was "rewritten
with WHERE THE NETWORK GUARD REACHES, AND WHERE IT STOPS". That heading has
never existed in any commit in this repository. **A repair was recorded, and
the repair was not made.**

**ROOT CAUSE.** Two, and the second is the one worth keeping.

1. The edit was lost. The mutation harness quarantines tracked files edited
   while a matrix is running, and that happened twice in that sitting. An edit
   to this file would have been reverted exactly like the two whose
   restoration *was* noticed.
2. **The repair was verified by re-reading the summary of the repair.** The
   ledger was written from intent, and the ledger was then what got checked.
   Nothing in the acceptance gate compared a prose claim against the file it
   was made about, so the loss was invisible to every later review — including
   two that were hunting this exact defect class.

**AND THE FILE CORROBORATED ITSELF.** The comment at the enforcement point
read: *"It binds this process, not the child -- said here because the
difference matters **and the module says so too**."* The module said the
opposite. A cross-reference to a sentence that does not exist is worse than no
cross-reference, because it reads as a second source agreeing.

**INVARIANT.** A documentation claim that a subprocess cannot reach the
network requires enforcement that constrains that subprocess. Parent-process
monkeypatching is not child-process containment. And a ledger entry claiming a
repair is not evidence that the repair exists.

**REPRODUCER.** The exact production configuration — guard entered in the
supervisor, no egress grant, work launched as a subprocess:

```
PARENT: refused by guard -> connect to 127.0.0.1:9 was not authorized:
                            no egress grant exists
CHILD:  rc=0  stdout='CHILD CONNECTED 127.0.0.1 38029'
```

**IMPLEMENTATION FIX.** Option B — honest process-local mediation. No
containment layer was invented to preserve the stronger prose, because none is
available in this architecture. The docstring now separates IN-PROCESS
MEDIATION (what the module has) from CHILD / DESCENDANT OS CONTAINMENT (what
it does not), names the kernel primitives that would be required, and points
at the test that measures the boundary. The enforcement comment now says what
it actually cross-references, and records that it used to claim corroboration
it did not have.

**AFFECTED REPRESENTATIONS.** Swept: the `governed_stage10.py` docstring and
enforcement comment (both false, both repaired); `completion_matrix.json` row
R32 `evidence`, which said "a connection attempted during execution is
refused" without saying *by whom* (repaired — its `boundaries` field was
already correct); `netauth.py`, whose "kernel layer (NOT provided)" section
was already honest and always had been; `docs/SESSION_REPORT.md`, already
honest — that is where P0-5's real work landed; `AGENT_SUBSTRATE.md`, which
names the guard in a feature table without claiming containment.

**ADVERSARIAL TEST.** `test_the_parent_guard_does_not_bind_the_child` enters
the production guard with no grant, proves the SUPERVISOR is refused, then
launches a child from inside that same block and proves it connects. Both
halves are load-bearing: without the first, the test would pass against a
guard that was never installed.

**THE GUARD AGAINST A THIRD RECURRENCE.** Prose is mechanically inspectable,
so it gets a mechanical check.
`test_no_project_text_reclaims_child_process_containment` sweeps every tracked
`.py`/`.md`/`.json`/`.yml` for the three sentences that would be false of this
substrate, exempting only this ledger (which must keep the historical claim)
and the file that defines the list. It asserts it examined more than a hundred
files first, because a sweep that walked nothing would report "nobody makes
this claim" for free — and a paired test proves the patterns match the
sentences they were written for.

**MUTATIONS.** `W5` removes the supervisor guard entirely — it never bound the
child, but it does bind the supervisor and its libraries, which is the half
the module now claims. `W6` removes the sweep's nonempty-scope floor. `W7`
removes the re-verification guard. 18/18, sources restored byte-identical.

**BOTH OF MY FIRST TWO MUTATIONS SURVIVED, AND EACH TAUGHT SOMETHING.**

`W6` — classification `EQUIVALENT_MUTATION`, as first written. The floor lived
inline in the test that walked the tree, so nothing could hand it an empty
scope and deleting it changed no observable outcome. The fix was not a cleverer
test but a restructure: `sweep_claims` now takes its scope as an argument, so
`test_the_claim_sweep_refuses_an_empty_scope` can give it the input it exists
to refuse. **A guard that cannot be handed the input it rejects is not tested
by anything**, however green the file is.

`W5` — classification `MISSING_TEST`, then `DEFENCE-IN-DEPTH MASKING`. The
first version of `test_the_supervisor_IS_bound_during_a_governed_run` passed
against the mutant. A governed run enters the executor **twice** — once to
execute, once to RE-EXECUTE under verification — and each call has its own
`socket_guard`. The spy recorded into a single slot, so the second, still
guarded call overwrote the first, and a run with its execution guard deleted
looked identical to one without. The spy now keeps every entry and asserts all
of them were refused, plus that there were at least two, so the test cannot
pass by reaching only one block. `W7` exists because that investigation found
a third guarded site that no mutation had ever attacked.

**FORBIDDEN FAKE FIXES.** Adding a monkeypatch to the child's entry point and
calling it containment: the tool is `python -m qta_agent._stage10_tool`, and
anything it imports can undo that as easily as the supervisor can. Deleting
the sentence without stating the boundary. Claiming rlimits restrict
networking — they do not, and
`test_a_bounded_child_is_NOT_prevented_from_using_the_network` already pins
that.

**SIBLING SWEEP.** Every claim in the repository of this shape — "X cannot
reach the network", "the run does not complete if X" — is now covered by the
mechanical sweep rather than by having looked.

**INVALIDATED CLAIMS.** D-2026-08's and D-2026-15's status of *repaired*, for
this file. Their analysis was right; their repair was never applied. Both stay
in this ledger unedited, because a record that quietly becomes true later is
not a record. And the P0 completion declared at `5e761fc`: the gate was
satisfied at `04f170d` on the evidence then available, and this defect was
open the whole time.

---

## D-2026-24 — the address class was checked only where it was already known

**CLASS** — `SECURITY_DEFECT`, `DOCUMENTATION_OVERCLAIM`.

**DISCOVERED BY.** The same external hostile review, reading `socket_guard`'s
UNPINNED_ACCEPTED branch against the comment printed directly above it.

**DEFECT.** The class check in `socket_guard` is guarded by
`if literal is not None`, where `literal = _maybe_ip(host)`. It therefore
answers only for a connect target that was **already an IP address**.
UNPINNED_ACCEPTED is the mode where the target is a **name** — that is what
the mode is for — and there the class was never checked at all. The OS
resolver then chose an address after the guard had finished looking.

So a grant reading

```
hosts=("localhost",)  address_classes=("PUBLIC",)  allow_unpinned_addresses=True
```

authorized, and the connection landed on `127.0.0.1`.

**AND THE COMMENT ABOVE IT SAID THE OPPOSITE:**

> This layer is holding the address the connection is actually going to, so it
> can answer the question the authorization could not.

It was not holding that address. In the branch where the check mattered, the
address did not exist yet.

**INVARIANT.** Do not claim an address-class restriction is enforced when the
enforcement point never observes the resolved address. "Unpinned" cannot mean
both *we do not know which address this name resolves to* and *we guarantee it
stays in the granted class* unless the resolution itself is mediated.

**REPRODUCER.** Fully local — no external networking, no resolver of our own.
`localhost` is a name that resolves to loopback:

```
authorize -> allowed=True mode=UNPINNED_ACCEPTED host=localhost addr=None
grant permits ('PUBLIC',); 'localhost' is LOOPBACK
CONNECT ALLOWED -> peer ('127.0.0.1', 45873)   <-- inside the perimeter
```

**IMPLEMENTATION FIX.** Directive Option A/B, the preferred outcome: resolve
under authority, then connect to the verified address. `_permitted_addresses`
resolves the name **once**, classifies every answer, and returns those the
grant permits; the guard connects to one of those rather than handing the name
back.

**RESOLVING HERE CLOSES A WINDOW RATHER THAN OPENING ONE.** The obvious
alternative — resolve, classify, then pass the NAME to the real `connect` — has
the OS resolve a second time, and a second resolution is a second answer. That
is the rebinding case, created by the check written to prevent it. Host and SNI
are set by the layer above from the URL it was given, not from what `connect`
received, so substituting the verified address preserves them.

**ADVERSARIAL TESTS.** Six. The defect itself (PUBLIC-only grant, name
resolving to loopback, refused, with both the class it found and the class it
permits named in the message); its anti-vacuity twin (a LOOPBACK-permitting
grant still connects, so the rule is not "refuse every unpinned name"); the
name binding and the port binding, each proved to survive the new resolution
step; `connect_ex` taking the same path as `connect`; and the substitution
itself.

**THE ONE TEST THAT NEEDED A REAL DISCRIMINATOR.** `getpeername()` cannot tell
"connected to the classified address" from "handed the name back", because
both end at a loopback address — so the mutation that reverted the
substitution survived its first test. The listener is now bound on
**127.0.0.2** and the resolver is steered to return only that, while the real
resolver still maps `localhost` to 127.0.0.1 where nothing listens. Connecting
to the classified address succeeds; handing the name back fails. That is the
difference the test exists to see.

**MUTATIONS.** `E52` deletes the branch, restoring the defect exactly. `E53`
resolves and classifies and connects anyway — the shape of a check that
reports rather than enforces. `E54` hands the name back to a second resolver.
`E55` makes every resolved address permitted, so `address_classes` bounds
nothing while the code still looks like it classifies. 54/54.

**AND THE HARNESS CAUGHT SOMETHING I DID NOT.** Adding `_with_host` introduced
a second copy of the line `if isinstance(address, (bytes, str)):`, which was
`W27`'s anchor. The matrix reported **ANCHOR DRIFT (tested nothing)** rather
than scoring a mutation that no longer applied anywhere in particular. The
helper now tests `not isinstance(address, tuple)` instead. A mutation whose
anchor has drifted is not a passing mutation; it is an absent one, and the
distinction is the reason that check exists.

**AFFECTED REPRESENTATIONS.** `socket_guard`'s CAN/CANNOT comment, which said
a second resolution made the class uncheckable — true of *which* permitted
address, false of the class, and now says which is which. Row R32's
`security_boundary` already described address classes and pinning without
claiming the unpinned name case, so it stands.

**FORBIDDEN FAKE FIXES.** Refusing every unpinned name — that removes the mode
rather than enforcing it, and the anti-vacuity twin fails if it happens.
Classifying and then connecting by name anyway (`E53`). Treating
`allow_unpinned_addresses` as waiving the class as well as the pinning: the
two are different waivers, and the grant field says pinning.

**SIBLING SWEEP.** Every `return` path in `_check` was re-read when it began
returning an address rather than `None`: PINNED, unpinned, and the
re-authorizing path each return the address to use, so no branch can silently
connect somewhere the check did not look.

**INVALIDATED CLAIMS.** Any reading of D-2026-07 or D-2026-14 as having closed
the address-class question. They closed the port and the exact-decision
binding. The class was enforced only where it was already known.

---

## D-2026-25 — a permitted address class does not make an endpoint usable

**CLASS** — `IMPLEMENTATION_DEFECT`, `SEMANTIC_DIMENSION_LOSS`,
`HOSTED_INTEGRATION_DEFECT`, `TEST_DEFECT / COVERAGE_GAP`.

**AFFECTED COMMIT** — `de7f0e699439251d01a2c3988f3e4d4f89518712`.

**DISCOVERED BY.** Hosted CI, on **both** interpreters: `agent-substrate`
(3.12) and `second-interpreter` (3.13) failed the same new test,
`test_an_unpinned_NAME_inside_a_permitted_class_still_connects`, with

```
TypeError: AF_INET address must be a pair (host, port)
  qta_agent/netauth.py  guarded_connect() -> original(self, _check(address))
```

`full-suite` failed during the full pytest stage, so **package consistency
never ran** — which is why the failure at that commit is *not* R59 and must not
be described as it.

**DEFECT.** D-2026-24's repair resolved a name under authority, classified the
answers, and returned a `sockaddr`. It kept the address and dropped everything
that came with it: **family, socket type and protocol**. On a dual-stack host
`localhost` resolves to `::1` first, so an `AF_INET` socket was handed an IPv6
sockaddr and CPython raised. Not a governed refusal — a crash.

**WHY IT PASSED LOCALLY.** This container has no usable IPv6 and resolves
`localhost` to IPv4 only. The local evidence was a property of the environment,
not of the code. That is the same shape as the finding it was fixing: a check
that appears to hold because the case it fails on never arose.

**INVARIANT.** A network endpoint decision must conserve every dimension the
connection needs. Permitted-by-authority and usable-by-this-socket are two
different questions, and answering only the first is how a security check
turns into a crash.

**REPRODUCER.** Deterministic on any host, because the resolver is controlled:
an `AF_INET` socket, a steered `getaddrinfo` returning `::1` before
`127.0.0.1`. Reversing the order must give the same answer — resolver order
must not decide policy.

**IMPLEMENTATION FIX.** `ResolvedCandidate` keeps `family`, `socktype`,
`proto`, `sockaddr`, `address` and `address_class` together.
`_resolve_candidates` resolves **once**, parameterized by the socket's own type
and protocol — a UDP socket must not be resolved as TCP and then told the
answer describes its operation — but deliberately with `AF_UNSPEC`, so a
refusal can say *"it resolved to `::1` and this socket is AF_INET"* instead of
failing with a bare `gaierror`. `_select_candidate` filters by class, then by
usability, and returns a **reason** rather than an unusable endpoint. `_check`
now takes the socket, and both `connect` and `connect_ex` go through it.

**TWO COMPATIBILITY SUBTLETIES, EACH ITS OWN TRAP.** `socket.type` may carry
`SOCK_NONBLOCK`/`SOCK_CLOEXEC`, and comparing a flagged type against
`SOCK_STREAM` by equality refuses every non-blocking socket — a fail-closed bug
that looks like security. And `socket.socket(AF_INET, SOCK_STREAM).proto` is
`0` while `getaddrinfo` reports `IPPROTO_TCP`, so requiring equality refuses
ordinary sockets. Both are normalized deliberately and both have mutations
(`E59`, `E60`) attacking the normalization from opposite directions.

**THE ONE SEMANTICS CHOSEN RATHER THAN DISCOVERED.** When a name resolves to
both permitted and forbidden answers, a permitted one is selected. The
connection reaches exactly one endpoint and that endpoint is inside the grant.
The stricter reading — any forbidden answer poisons the resolution — would make
a dual-stack host unusable whenever one family maps somewhere the grant does
not permit, while protecting nothing.
`test_a_mixed_resolution_selects_a_permitted_compatible_candidate` is what says
so.

**ADVERSARIAL TESTS.** The matrix is deterministic on every host: the resolver
is steered in each case rather than trusted. Both resolver orders and IPv4-only
for an `AF_INET` socket; permitted-but-incompatible refused **as a decision**;
both IPv6 cases (skipped loudly where the host has no IPv6, never deleted); the
original class rule and its anti-vacuity twin; mixed classes; name, port and
literal bindings surviving the new step; `connect` and `connect_ex` on both the
refusal and the crash path; UDP governed and UDP permitted; the IPv6 sockaddr
kept whole; and the substitution proved against a listener on `127.0.0.2`.

**A KILL BY TypeError IS NOT A SEMANTIC KILL.** `E56` and `E57` first died only
because CPython raised when the bad endpoint reached the socket. That proves
the crash happens, not that anything decided. Both rules are now asserted
directly — `test_an_incompatible_family_is_unusable_AS_A_DECISION` and
`test_selection_refuses_when_only_incompatible_candidates_are_permitted` — with
no socket call anywhere, and both mutants were confirmed by hand to fail those
with a plain `AssertionError`.

**MUTATIONS.** `E56` ignores family (the regression itself), `E57` selects on
class alone, `E58` ignores socket type, `E59` lets type flags defeat
compatibility, `E60` makes an implicit protocol incompatible, `E61` resolves
every socket as TCP, `E62` lets `connect_ex` skip the shared decision path.
Four earlier R11 mutations were retargeted onto the restructured code. 61/61.

**TEST-HARNESS DEFECT FIXED WITH IT.** The listener helper started a daemon
thread on `srv.accept()` and closed the socket underneath it, producing
`PytestUnhandledThreadExceptionWarning` on the hosted runner. It is now a
context manager that shuts down, closes, joins with a timeout and asserts the
thread did not outlive its test. The suite is run with that warning as an
error. A warning from a leaked thread is noise that hides real ones.

**FORBIDDEN FAKE FIXES.** Special-casing `localhost`. Preferring IPv4
unconditionally. Forcing resolver ordering. Catching `TypeError` and calling it
fail-closed — a policy refusal is an intentional governed decision, not an
accidental runtime type error.

**INVALIDATED CLAIMS.** The local closure of P0-R11 at `de7f0e6`. The
**security analysis** behind it stands: the PUBLIC→loopback defect was real and
the repair direction was right. The repair exposed a second, cross-layer loss
in the same code path. Both facts belong on the record.

---

## D-2026-26 — a reconcile decision outlived the facts it was decided on

**CLASS** — `CONCURRENCY_DEFECT`, `AUTHORITY_DEFECT`,
`HOSTED_INTEGRATION_DEFECT`.

**AFFECTED COMMIT** — present since the scheduler's `reconcile` was written;
surfaced at `d16179c`.

**DISCOVERED BY.** Hosted CI, in the step *before* the one being watched. The
R11 network fix was pushed and `agent-substrate` failed at step 9 — the agent
suites — with

```
j-0 was attempted 4 times against a budget of 3
```

from `test_a_long_mixed_campaign_never_leaves_an_unreplayable_log[250]`. The
assertion that caught it is the one D-2026-01 added, still doing its job three
tranches later.

**DEFECT.** `reconcile()` scans `expired_leases()` into a list, decides
give-up-versus-requeue from **what it read**, and writes later. Between the
scan and the write another process can dispatch the job — spending an attempt
— and let that lease lapse too. The stale decision then requeues a job whose
budget is now gone: it becomes READY at `attempts == max_attempts`, and the
next dispatch makes it `max + 1`.

`dispatch()` uses `expected_revision` for precisely this class of race.
`reconcile()`'s `move()` did not.

**WHY LOCAL EVIDENCE SAID NOTHING.** Six worker processes on a bigger runner
give the scan and the write enough room to interleave. Six local runs of the
same parameterisation passed. A stress test large enough to hit this by luck is
not a regression test, so the reproducer injects the competing dispatch
**inside** reconcile's write window — after the scan chose "requeue", before
that choice is written.

**WHAT D-2026-01 FIXED AND WHAT IT DID NOT.** It fixed the RULE: a lapse counts
against the budget, and the give-up branch became reachable. It did not bind
the DECISION to the state it was decided against. The rule was right and was
applied to a world that had moved.

**IMPLEMENTATION FIX.** `move_decided(job, ...)` passes
`expected_revision=job.revision`, so a job that moved between scan and write is
refused and the next reconcile decides against the world as it now is — which
is what `reconcile`'s docstring already promised.

**WRITE-PATH FIX.** `dispatch()` now refuses when `attempts >= max_attempts`.
The budget was consulted only on the RETURN edges, so "attempts never exceeds
max_attempts" held only as long as nothing could reach READY with the budget
gone. An invariant that depends on every other path being right is not an
invariant; `dispatch` is the line that increments the count, so it is the one
place that can state it locally.

**REPLAY FIX.** The reducer bounded the count's *arithmetic* — moves by one,
only on the hand-out edge — and never compared the result to the budget, so a
forged hand-out record replayed clean. It now refuses `want > max_attempts`.
Replay is where a hand-written record has to be refused; the write path only
stops callers who were not attacking.

**INDEPENDENT-READER.** Already present and unchanged: `_sub_job_transition`
reports a budget overrun as a finding about the history, deliberately checked
after the fold so that it is a statement about the whole log rather than about
one edge.

**MY OWN FIX MASKED AN EXISTING MUTATION.** `X6` — *a lapsed lease does not
spend the retry budget* — had been killed by the long campaign. After the
`dispatch` guard it **survived**: with reconcile's give-up branch deleted the
job is requeued forever, but dispatch now refuses it, so `attempts` never
exceeds `max_attempts` and the campaign assertion is satisfied. The job simply
sits READY forever with nobody running it and nothing failing it.

The count was never the whole invariant. D-2026-01 states it as *"a job whose
retry budget is spent reaches a TERMINAL state"*, and
`test_a_budget_spent_by_LAPSES_ALONE_reaches_a_terminal_state` asserts that
half directly. A new guard for one invariant hid the mutation covering
another, which is the exact shape this ledger already records twice.

**AND THE REPLAY TEST PASSED FOR THE WRONG REASON.** Its first version forged a
hand-out against a job that was FAILED, so replay refused it by the STATE rule
and `X12` survived. `FAILED` is terminal, so the staging record was illegal
too. The job is now brought to READY-with-spent-budget by the lapsed handover a
budget-unaware reconciler writes — legal, and it leaves the count alone —
before the forged record is appended.

**ADVERSARIAL TESTS.** Four, one per layer plus anti-vacuity, because a fixture
invalid in three ways would pass whichever guard fired first and prove nothing
about the other two: the stale decision, the write path, the replay, and
`test_an_honest_campaign_still_spends_its_whole_budget` — a rule refusing the
*second* attempt would satisfy all three while breaking retries entirely.

**MUTATIONS.** `X10` restores the stale write, `X11` removes the hand-out
guard, `X12` removes the replay bound. With `X6` back to a real kill: 11/11.

**AND THE HAND-OUT GUARD CREATED A LIVENESS HOLE, CAUGHT BY ANTI-VACUITY.**
With `dispatch` refusing a spent budget, a job that reaches READY with its
count gone can never run — and `reconcile`'s give-up branch only scans jobs
with an expired LEASE, so nothing could ever fail it either. It would sit in
the queue forever.

Nothing asserted that directly. What caught it was the long campaign's
anti-vacuity line — *"a run that never requeued, retried or failed anything is
not exercising the transitions this is about"* — which noticed the campaign had
stopped recording FAILED at all. A test written to refuse a vacuous run found a
liveness regression in the code it was not looking at.

`reconcile` now fails a PENDING job whose budget is spent, so the invariant is
*a job whose retry budget is spent reaches a terminal state* from **either**
side: dying mid-attempt (the lease path) or sitting with nothing left to spend
(the pending path). That state is reachable from a budget-unaware writer and
from an older implementation's record, not only from the guard that exposed it.

**FORBIDDEN FAKE FIXES.** Widening the campaign's assertion to
`attempts <= max_attempts + 1`. Making `reconcile` re-read inside its own loop
without binding the decision — that narrows the window instead of closing it.
Treating the hosted failure as flake: it reproduces deterministically once the
interleaving is written down.

**INVALIDATED CLAIMS.** Any reading of D-2026-01 as having closed the retry
budget. It closed the rule. The decision that applies the rule was unguarded,
and `attempts <= max_attempts` was one scan-write window away from being false
the whole time.

## D-2026-27 — the second reader asked the first reader whether the first reader would have allowed it

**CLASS** — `VERIFIER_INDEPENDENCE_DEFECT`, `AUTHORITY_DEFECT`,
`CIRCULAR_EVIDENCE_DEFECT`.

**AFFECTED COMMIT** — present since `reconstruct.py` was written; still true at
`b2787a0`.

**DISCOVERED BY.** P0-R12 of the second reopening. Not by a failing test —
every test passed, and that is the finding.

**DEFECT.** `qta_agent/reconstruct.py` is the package's independent reader: the
thing that answers *given only the log, what is canonical?* without trusting
the running system. For the nine subsystems at the bottom of the module it does
exactly that, in plain strings and plain dicts, and says so in
`SubsystemReconstruction`'s docstring:

> a second reader that lives beside the first, imports the first's enums and
> calls the first's helpers is not a second reader — it is the same decision
> run twice, agreeing with itself.

The two machines at the **top** of the same module did that. `reconstruct()`
imported `authority.check`, `TransitionRequest`, `State` and `Role` and handed
each replayed record straight to the production gate. `reconstruct_tasks()`
imported `tasks.check`, `TaskTransition`, `TaskState`, `TaskRole`, `Task` and
`Lease` and did the same. Thirty-one lines across the module.

**WHAT THAT COSTS.** Everything the differential comparison claims:

* Weaken a production rule and the second reader is weakened identically. It
  cannot disagree with the gate about a question it asks the gate.
* An empty diff between the two readers then means *the reducers fold the same
  way*, which is a much smaller claim than the one the module's docstring
  makes, and nothing distinguishes the two claims from outside.
* The failure is **silent by construction**. There is no assertion that can
  detect it from the outputs, because both readers produce the same correct
  answer right up until the moment production is wrong, and then they produce
  the same wrong one.

`separate_verify.py`'s docstring named the decay path exactly — *"one future
edit importing the primary reducer 'to remove the duplication' turns the
comparison circular while it goes on reporting agreement"* — and the import it
describes was already there, in the reader that subprocess runs.

**AND A TEST STOOD GUARD OVER THE COUPLING.**
`test_the_reconstruction_shares_no_reducer_with_the_projection` ended with

```python
# It MAY import the transition table -- re-authorizing against a
# different table would compare two different questions -- but it must
# derive the resulting state itself.
assert "tasks.check" in imported or "tasks" in imported
```

so the coupling was not an oversight that survived review; it was a
**requirement** something asserted. The concern behind it is real — two tables
that drift compare two different questions — but the remedy inverted the
module's purpose to serve it.

`test_the_second_reader_imports_none_of_the_layers_it_reads` listed eight
forbidden layers and omitted `authority` and `tasks`: the two it was actually
standing in front of.

**IMPLEMENTATION FIX.** Every load-bearing rule restated in `reconstruct.py`'s
own terms, following the pattern the nine subsystems below already used:

* `_AUTH_STATES`, `_AUTH_ROLES`, `_AUTH_INITIAL`, `_AUTH_TERMINAL`,
  `_AUTH_PROMOTED`, and `_AUTH_EDGES` — all fourteen edges with their roles,
  required evidence and separation-of-duties flags.
* `_auth_refusal()` — terminality (I2), edge existence, role membership,
  distinct actor (I4), evidence presence and digest shape (I6), policy
  identity in force (I5), in production's order.
* `_TASK_STATES`, `_TASK_ROLES`, `_TASK_INITIAL`, `_TASK_TERMINAL` and
  `_TASK_EDGES` — all twenty-two edges including the five cancellation edges
  and `VERIFIED -> INVALIDATED`, which is why terminality is checked only
  where no edge exists.
* `_task_refusal()` — edge existence with the distinct terminal message, role
  membership, lease possession (exists, id matches, holder is the actor, not
  lapsed), separation of duties, and `COMPLETED` requiring a result digest.
* `_is_digest()` — 64 lowercase hex characters, written as a set membership
  rather than a compiled pattern.
* `_parse_lease()` — the lease record's shape, so an unreadable one is a named
  anomaly rather than somebody else's `TypeError`.

`reconstruct.py` now imports nothing from `authority` or `tasks`, and calls no
function named `check`.

**THE RESTATEMENT IS A COST, PAID ON PURPOSE.** Two statements of one rule can
drift. The alternative cannot be recovered by any test at all: a reader that
calls the gate agrees with a broken gate perfectly and there is nothing to
assert. So drift is moved into the open — conformance tests compare the two
tables element by element and name the difference — and the reader stays
uncoupled. An empty diff is still evidence and not proof: two statements can
share a misunderstanding the log cannot reveal.

**A REFUSAL IS NOW A STRING, NOT AN EXCEPTION.** This module diagnoses rather
than enforces; raising somebody else's exception type was the shape that made
importing it feel natural. Two consequences fell out:

* A payload naming a state or role nothing defines used to reach
  `State(...)`/`TaskState(...)` and raise `ValueError`, which `reconstruct()`
  caught and `reconstruct_tasks()` did not — so a malformed record crashed the
  reader that promises never to raise on content. Both now report it.
* A lease whose `expires_after_seq` is not orderable against a sequence number
  made production's `is_live` raise `TypeError` out of the middle of a replay.
  It is now a finding. Booleans are deliberately *not* special-cased: Python's
  `True` is `1` in that comparison, so refusing them would be drift.

**`reauthorize=False` IS A DIAGNOSTIC MODE, NOT A PERMISSIVE ONE.** It turns
the gate off so a caller can ask what the log says at face value. It does not
license folding a state nothing defines into the answer, so both replays still
refuse to apply one, and paired tests assert that a KNOWN state — including one
the gate would refuse — is still folded in.

**ADVERSARIAL TESTS.** Three kinds, because the three ways this defect comes
back are different:

1. **The coupling is gone.** `test_the_second_reader_imports_neither_authorization_gate`
   over parsed imports (this file's prose names both modules constantly, so a
   substring search would fail for being right), and
   `test_the_second_reader_calls_no_function_named_check` over the call graph —
   because an import guard that reads only the top of the file misses
   `from .tasks import check` inside a function body.
2. **The restatement is faithful.** Five conformance tests comparing states,
   roles, initial, terminal, both edge tables, the lease shape, and `_is_digest`
   against `canonical.is_digest` over eleven inputs including uppercase,
   off-by-one lengths, bytes and `None`.
3. **The restatement is load-bearing.** Four tests that weaken the PRODUCTION
   table at runtime — separation of duties off, a `PROPOSED -> PROMOTED`
   shortcut added, task separation off, lease possession off — assert the
   weakened gate now ACCEPTS what it used to refuse (anti-vacuity: the
   weakening is real and reaches the gate), and then require the second reader
   to refuse anyway. Every one of these fails on the old code.

**PAIRED MATRICES.** Seventeen authority rows and seventeen task rows, each
refusing row sitting beside a row that differs only in the thing its rule is
about: `verified-with-prose-for-evidence` says something about the digest rule
only because `honest-verify` — the same move with a real digest — is not
refused. `requeueing-a-verified-task-is-not` is paired with
`invalidating-a-verified-task-is-allowed`, which is what keeps "terminal" from
being read as "sealed".

**MUTATIONS.** `R41`–`R74` on `tools/mutations/agent_second_reader.json`, in
three kinds matching the three test kinds: disable one restated rule, drift one
restated table away from production, and re-couple the reader to the gate by
import (at module scope AND inside a function body) or by call.

**FORBIDDEN FAKE FIXES.** Keeping the imports and adding a comment that the
duplication is deliberate. Restating the rules and then calling production
"to check the restatement" — that is the same circularity with an extra step.
Deleting the conformance tests because they duplicate the tables: they are the
only thing that can see drift, and drift is the price of independence.
Asserting the two readers agree on honest logs and calling that independence —
they agreed before, on everything, which is precisely the problem.

**INVALIDATED CLAIMS.** Any reading of the differential suite as evidence that
two independent implementations agreed about authority records or tasks, at any
commit before this one. They agreed about the fold. The authorization decision
was made once, by production, and read back twice.

**WHERE THE INDEPENDENCE STOPS, STATED RATHER THAN IMPLIED.** After this fix
`reconstruct.py` imports exactly two things from the package, and neither is
an accident:

* `events.EventLog` — the log's reader. Shared **deliberately**: the two
  readers must be looking at the same bytes, or a disagreement between them
  says nothing about either one's rules. A second parser would be a different
  and much smaller claim.
* `actions` — the manifest of which action strings this package writes,
  used to tell *another subsystem's event* from *an event nothing here
  writes*. That is a question about what EXISTS, not about what is ALLOWED,
  and its answer changes no authorization decision: an action misclassified
  either way is counted or reported, never applied.

Beyond those, the reader shares nothing — including `canonical.is_digest`,
which it now restates, because the digest-shape rule is load-bearing for I6.

This paragraph is here so that "independent" names a checkable boundary
instead of a mood. The AST guards enforce it.

### The gate's verdict, at `04f170d`

All twenty-three are true at that commit, and every one of them is answered by
something that refuses when it stops being true rather than by this sentence:

| job at `04f170d` | result |
|---|---|
| `agent-substrate` | **green, all 56 steps** — 38 mutation matrices, the model check, the identity-inventory check, the long-horizon campaign, the fuzz pass, the governed production path, the auditor, and both post-campaign source-integrity checks (`working tree clean`, `manifest in sync`) |
| `second-interpreter (3.13)` | green |
| `cross-environment-3d` | green |
| `stack-verify` (core, full) | green |
| `full-suite` | **the full pytest suite green**; `package_consistency_check.py` red |

The one red is the R59 host divergence and nothing else: the same 24 files by
name, on a runner without AVX-512, from a byte comparison rather than a test.
It predates this tranche and this tranche did not change it. Every other check
in that script passes at this commit, including the three that matter most
here — `can_PASS_now=NO for all 83 rows`, `PASS_count=0`, and `no PASS tokens
in any 3D output`.

So, at that commit, and on the evidence available then:

**`GATE_SATISFIED_AT_COMMIT: 04f170d`.**

That sentence originally read "P0 is complete." It was wrong the moment it was
written, and not because the gate was miscounted: D-2026-23 was open the whole
time, in the production caller's own docstring. The three states this ledger
now distinguishes are:

| state | meaning |
|---|---|
| `GATE_SATISFIED_AT_COMMIT` | every gate condition was true at a named commit, on the evidence then available. Historical, and it stays true forever. |
| `CURRENTLY_OPEN_FINDING` | a defect of the same class is known now. Reopens the tranche prospectively; does **not** retract the historical record. |
| `CURRENTLY_CLOSED` | no open finding, and the gate has been re-run at the current head. |

A previous green gate is evidence about a commit. It is not a claim about the
present, and it must never become one by being left unqualified — which is
precisely how "P0 complete" survived a defect that a `grep` of the live source
would have found.

The known examples were repaired in the first tranche and the sibling sweeps
were incomplete; that was recorded as `PREMATURE_CLOSURE` in D-2026-10 rather
than quietly corrected. This tranche closed the classes, and the two findings
it produced that nobody asked for — D-2026-21 and D-2026-22 — both came from
the recheck rather than from the original list, which is the argument for
having done it.

**This record is itself a documentation commit.** Its own hosted run is
confirmed separately; the gate was satisfied at `04f170d`, which is named
above rather than inherited.

**STATUS, SET BY THE SECOND REOPENING:** `CURRENTLY_OPEN_FINDING`. An external
hostile review reading the live source found D-2026-23 and further findings
recorded above it. The chronology is the point and is left intact: gate
satisfied → external review found a sibling defect → P0 reopened → repaired →
gate re-run.

## D-2026-28 — the coverage number measured string presence, not handling

**CLASS** — `MEASUREMENT_DEFECT`, `VERIFIER_SELF_DEFENCE_GAP`.

**AFFECTED COMMIT** — present since `tools/identity_inventory.py` was written
to close D-2026-19/20; still true at `f80caa8`.

**DISCOVERED BY.** P0-R13 of the second reopening. The number it reported was
correct. That is the finding.

**DEFECT.** `reconstructed_actions()` was one line:

```python
src = (PKG / "reconstruct.py").read_text(encoding="utf-8")
return {a for a in durable_actions() if f'"{a}"' in src}
```

and the CI step above it says the independent-reader coverage is *measured
rather than claimed*. It measured whether the action's name occurred anywhere
in the second reader's TEXT. So an action counted as independently
reconstructed when it was named:

* in the module docstring — which, in `reconstruct.py`, **lists the
  subsystems the reader does not cover**;
* in an anomaly message saying a record could not be interpreted;
* in a comment explaining why something is deliberately not handled;
* in a commented-out handler somebody disabled.

And it counted as *not* reconstructed when the handler was written with
single quotes, because the pattern was `f'"{a}"'`. One rule, wrong in both
directions.

**WHY THAT MATTERS MORE THAN THE NUMBER.** The 28-of-37 figure appears in the
CI step title, in `docs/identity_inventory.json`, in the second reader's own
test-module docstring, and in this ledger. It is the answer to *how much of
the system has an independent reader*, which is the claim P0-R12 was about.
A measurement that can be satisfied by a mention is a measurement that
**cannot detect the removal of the thing it measures**: delete a handler,
leave the action's name in the docstring above it, and the number does not
move.

The old function's docstring conceded the hole and argued it away: *"A reader
that mentions an action without handling it would be counted here wrongly --
which is why the mutations attack the handlers rather than this list."* That
is an argument that the number is defended somewhere else. It is not an
argument that the number is right, and the number is what the step title
reports.

**IMPLEMENTATION FIX.** `reconstruction_coverage()` parses `reconstruct.py`
and classifies every durable action into three categories:

| category | meaning |
|---|---|
| `DISPATCHED` | the literal is in a position that decides which branch runs: either side of a comparison, an element of a set/list/tuple literal (how `owned` and `_AUTHORITY_ACTIONS` are consulted), or a dict key |
| `MENTIONED` | the name is in the source and nothing branches on it |
| `ABSENT` | not in the source at all |

`reconstructed_actions()` returns only `DISPATCHED`. Comments never appear in
a parse tree, so a commented-out handler is excluded for free rather than by a
rule somebody has to remember. Quote style stops being a fact about coverage.

`MENTIONED` is its own category rather than being folded into either answer,
because it is exactly the case a reader could look at and reasonably believe
was covered — and the whole finding is that believing it was once enough. It
is printed by the CLI and asserted empty by a test, so introducing one is a
decision somebody has to make past an assertion.

**THE NUMBER DID NOT CHANGE.** Still 28 of 37, the same nine uncovered. The
measurement changed, not the answer — which is the point: it was right by
luck, and nothing would have said so when it stopped being.

**AND THE CHECKER'S OWN REFUSALS WERE ALMOST ALL UNEXERCISED.** `problems()`
has six refusals and one test ran one of them. For a file whose entire job is
to refuse, a branch nobody has seen fire is a branch nobody knows fires. Each
now has a test that damages a copy of the inventory and requires the specific
complaint, with `test_the_unmodified_inventory_produces_no_problems` as the
anti-vacuity partner for all of them: without it, a helper that damaged the
file on the way in would make every one pass for the wrong reason.

**A SURVIVOR, AND WHAT IT SAID.** `I9` — *an ACTOR field need not record a
write path or a replay* — survived the first run. `test_every_ACTOR_field_
names_a_test_that_exists` asserts on the document directly, so the branch in
`problems()` saying the same thing had never run. Classified `MISSING_TEST`
and killed with one case per key, because a check firing for
`regression_test` alone would satisfy a single blanked-field test while
leaving the write path and the replay unbound. Blanking `regression_test` is
also not caught by the rule below it — that one skips an empty name, which is
precisely the shape this refuses.

**TESTED AGAINST PLANTED SOURCES.** The measurement takes the second reader's
text as a parameter, so the tests can hand it a file where the old and new
definitions disagree. A test that only ever sees the real file — where they
happen to agree — cannot tell them apart. The first version instead pointed
`PKG` at a temporary directory, which also emptied `durable_actions()`; it
failed with a `KeyError`, which was the honest outcome, because it was
measuring an empty universe.

**MUTATIONS.** A new spec, `tools/mutations/identity_inventory.json`, wired
into `agent-substrate.yml` — the tool had none at all, which is its own small
instance of this defect class. `I1` restores the substring measurement
verbatim; `I2`–`I6` remove one dispatch position or one category each;
`I7`–`I13` remove one of the checker's refusals each. 13/13 killed.

**FORBIDDEN FAKE FIXES.** Keeping the substring search and adding a comment
that it is approximately right. Widening the pattern to both quote styles —
that fixes the false negative and leaves the false positive, which is the
dangerous half. Deleting the `MENTIONED` category because it is empty today:
empty is the answer, not the reason not to ask.

**INVALIDATED CLAIMS.** Any reading of "28 of 37, measured rather than
claimed" at a commit before this one as a statement about handling. It was a
statement about the presence of thirty-seven strings in one file.

## D-2026-29 — two true numbers, side by side, and nothing reconciling them

**CLASS** — `COVERAGE_RECONCILIATION_DEFECT`, `VERIFIER_COVERAGE_GAP`.

**AFFECTED COMMIT** — present since D-2026-19/20 recorded the measurement;
still true at `643d2c7`.

**DISCOVERED BY.** P0-R14 of the second reopening.

**DEFECT.** Two statements stood next to each other in this repository:

* `docs/completion_matrix.json`, validated on every run: **39/39 complete, 0
  residual gaps**;
* `tools/identity_inventory.py`, printed on every CI run: **28 of 37 durable
  actions have an independent reader**.

Both were true. Neither was wrong. And nothing anywhere said whether the nine
uncovered actions *mattered* — so a reader could take the first as the summary
and the second as a footnote, which is exactly the reading the first invites.

The nine were listed in the ledger's own follow-up section as "ordinary
repository engineering, not a boundary". That is an honest statement about
whose job it is. It is not an answer to the question a reviewer actually has:
**does a forged record of one of these change anything?**

**THE RECONCILIATION IS A CLASSIFICATION, NOT A NUMBER.** An action is
`authority_changing` when a forged record of it would change what the system
permits, what it treats as canonical, or whom it attributes a decision to.
Every one of the 37 is now classified in `docs/identity_inventory.json` with
its reason, and the rule is enforced: **an authority-changing action with no
independent reader is a failure**, not a footnote.

26 of the 37 are authority-changing. Two of them had no second reader.

**`agent.claim` — AUTHORITY-CHANGING, AND IT WAS UNCOVERED.** A claim is not a
state change, and for a long time that was treated as the same thing as not
being authority. It is the INPUT to conflict resolution: `QUORUM` counts
claims, `PREFER_ROLE` selects among them by role, and `REQUIRE_HUMAN` decides
that a disagreement is not an agent's to settle. So a claim attributable to
anyone, or made in a role its author does not hold, manufactures or suppresses
the disagreement two parties the system is about to call independent are said
to have.

The production reducer checks this — it was repaired for exactly that reason,
and its comment says so. Which means a log carrying a forged claim **cannot be
loaded by the primary at all**, so the comparison between the two readers
never runs and the second reader is the only one left looking at the history.
A second opinion that accepts a wider language than the first is not a second
opinion on the logs that matter. That is the same shape as D-2026-02 and
D-2026-19, recorded a third time.

`_sub_claim` restates every rule in this module's own terms: the claim is
attributed to the instance that recorded it; that instance is registered,
active at this seq, and holds the role named; the role is one this reader
knows; the value is a digest, because a claim carrying prose can be counted by
a quorum and contradicted by nothing; and a claim id is not reused.

**`task.compensation` — AUTHORITY-CHANGING FOR A DIFFERENT REASON.** It does
not move the task, and this reader does not fold it into one either: "was
compensated" and "did not happen" must not become the same answer. What it
carries is `answered_by` — a copy of the escalation's answerer, so an auditor
can see who authorized destroying something without joining two tables. A
convenience nothing checks is a field a forged record sets freely, and it
names a **person**.

`_sub_compensation` restates the cross-check and is **stricter than the
primary in one place, deliberately**: the primary looks the escalation up and,
when the lookup fails, checks nothing — so a compensation authorized by a
question nobody asked passes it silently. Here that is a finding, along with
one citing an escalation that is still OPEN. Saying what the log does not
support is this reader's job.

**THE OTHER SEVEN, EACH WITH ITS REASON.** `agent.message`, `file.read`,
`network.result`, `secret.access`, `secret.provision`, `task.reexecution`,
`task.separate_verification`. Every one was traced to its consumer before
being classified:

* `network.result` reaches `NetworkAuthority.apply`, which folds grants and
  service calls and, for a result, only advances its position in the log;
* `SecretStore.apply` folds `secret.grant` and returns False for everything
  else, so neither secret record changes a grant;
* `file.read` is written **after** readpath's gate has decided, and the gate
  consults capabilities and the path, never a past read;
* `task.reexecution` and `task.separate_verification` are facts about HOW a
  result was checked — the projection says so in a comment — and the
  transition each justifies is itself recorded as a transition, which IS
  reconstructed;
* nothing consults `agent.message` to permit anything; its sender binding and
  its redelivery immutability matter for audit, which is what D-2026-16 and
  D-2026-17 were about.

A forged one of these falsifies an audit trail. None of them permits anything.

**COVERAGE IS NOW 30 OF 37, AND THE LABEL IS CHECKED.** The number appears in
a CI step title, in a mutation spec title, in two test-module docstrings and
in this ledger. D-2026-19 put it in the step title on purpose — "for every
subsystem" was the overclaim it replaced — and then the number moved the
moment two readers were added. A number in a label that nothing checks is this
same defect one layer out, so
`test_the_labels_that_quote_the_coverage_number_still_match_it` reads the
measurement and requires the workflow and the spec to agree with it.

**AND THE PLANTED-ACTION TESTS QUIETLY STOPPED PROVING ANYTHING.** D-2026-28's
measurement tests all plant `agent.claim` into a synthetic source, because it
was a real action the reader did not dispatch on. Giving it a reader made
every one of those cases start from a positive, so the negative cases would
have passed no matter what the classifier did.
`test_the_planted_action_is_uncovered_in_the_real_reader` — the anti-vacuity
assertion written one commit earlier — is what said so, within a minute. The
anchor moved to `agent.message` and the comment records why it moved.

**ADVERSARIAL TESTS.** Eight for claims and five for compensations, each
refusal paired with an honest history it must not flag — including
`test_the_second_reader_follows_two_claims_that_disagree`, because a reader
that cannot tell a real conflict from a forged one is useless at exactly the
moment a conflict is what sends a decision to a person. Plus the inventory's
own: every action classified, every classification given a reason, the column
asserted to partition (a table saying everything changes authority, or that
nothing does, would satisfy every other assertion here), and the checker
required to refuse an unjudged action, an unreasoned one, and an
authority-changing one with no reader.

**MUTATIONS.** `R75`–`R89` on `agent_second_reader.json`: one per restated
claim rule, one per restated compensation rule, and one apiece that stops the
reader covering either action at all — the quieter of the two ways coverage
fails. Two are written as substitutions rather than deletions
(`out.agents.get(actor, {...})`, `out.escalations.get(eid, {...})`) so the
mutant produces a WRONG ANSWER instead of an `AttributeError`: a kill by
crash says Python rejected the file, not that the check was load-bearing.

**FORBIDDEN FAKE FIXES.** Reclassifying an uncovered action as
not-authority-changing to make the rule pass — which is why every
classification carries a reason and why
`test_the_checker_refuses_an_uncovered_authority_changing_action` exists.
Rounding 30 up to 37 by counting actions the reader mentions. Deleting the
number from the step title so nothing can drift: the number is the claim, and
a claim with no number was the overclaim D-2026-19 replaced.

**INVALIDATED CLAIMS.** Any reading of "39/39, 0 residual gaps" as implying
that every durable action has an independent reader. It never did, the
inventory always said so, and until now the two numbers sat in different files
with nothing obliging them to be read together.

## D-2026-30 — the checkpoint pinned a snapshot, and nothing anchored the pin

**CLASS** — `AUTHORITY_DEFECT`, `EVIDENCE_BINDING_DEFECT`,
`PROSE_OVERCLAIM`.

**AFFECTED COMMIT** — present since checkpointing was added; still true at
`3d809f0`.

**DISCOVERED BY.** P1 of the second reopening, by reading
`checkpoint.py`'s own docstring against what `store.load_from` does.

**DEFECT.** `checkpoint.py` says, in these words:

> **WHAT AUTHENTICATES A CHECKPOINT**
>
> Nothing in this module. Read that sentence again before relying on one.

and, three paragraphs earlier:

> a false statement about the log is still just a false statement -- it
> cannot make a forged record authoritative, because anyone can re-run the
> full verification and find the disagreement.

The second sentence is true of the **log** and was false of the **snapshot**.
A checkpoint file says *the state at seq K is blob D*. `AuthorityStore.
load_from` restored blob D, replayed the tail, and handed the result back.
Nothing compared D against anything in the hash chain, because the whole
point of a checkpoint is not to read records 0..K.

So: rewrite the checkpoint file with the same `seq`, the same `head_hash` and
the same byte offsets, change `state_digest` to a snapshot you wrote yourself,
recompute the file's self-hash — which anyone able to write the file can do —
and the load succeeds. **The log is untouched and `verify()` returns ok.**
Re-running the full verification finds no disagreement, because the forgery
was never in the log.

**WHAT THE FORGERY BUYS.** The reproducer promotes a record that the
authority gate could not have promoted: `PROPOSED -> PROMOTED` is not an edge
at all, and `VERIFIED -> PROMOTED` requires a distinct actor and an explicit
policy identity. The restored projection reports it in `canonical()`. That is
a canonical record no history could produce, handed back by a load that
reported no problem.

`loaded_prefix_verified = False` was the only signal, and it says the wrong
thing: it means *the records before the checkpoint were not read*. They are
fine. What was taken on faith is the snapshot, and nothing said so.

**IMPLEMENTATION FIX — THE CLAIM GOES IN THE LOG.** A new durable action,
`checkpoint.state`, appended by `AuthorityStore.checkpoint()`:

```
{"through_seq": K, "state_digest": D, "head_hash": H}
```

`load_from` now requires that record, matched on all three fields, in the
tail it already reads and has already verified. A checkpoint with no such
record is refused as unanchored; one whose digest disagrees is refused and
says which side the log is on. The record is inside the hash chain, so
rewriting it breaks `verify()` — which is the property the module's prose
claimed and did not have.

**ORDERING, WHICH IS NOT ARBITRARY.** The checkpoint is created first,
against the head its snapshot describes, so `cp.seq` names that position. The
record then lands at `cp.seq + 1`, inside the tail a checkpointed load
replays. A snapshot cannot contain the record announcing it, and the second
reader refuses a claim that says otherwise.

If the process dies between the append and the file write, the log carries an
anchor for a checkpoint nobody has: a fact about a blob that exists, and
harmless. The reverse order would leave a checkpoint nothing anchors, which is
exactly the state this record exists to make unloadable.

**INDEPENDENT READER.** `checkpoint.state` is authority-changing — it is what
authorizes restoring a projection instead of replaying the log — so D-2026-29's
rule requires a second reader, and `_sub_checkpoint` is it. It checks the
shape, that a claim does not cover its own position, that one position is not
given two different states, and the claimed head hash **when the claim names
the position immediately before it**, which is where an honest writer puts it.

That last bound is stated rather than hidden. Verifying a claim about an older
position would need a `seq -> hash` index over the whole log, which is memory
proportional to the history for one check; the reader records
`head_hash_checked: False` instead, so a reader of ITS output can tell a
verified claim from an unverifiable one. A verifier that implies it checked
more than it did is the defect this tranche keeps finding.

**AN EXISTING TEST CHANGED ITS NUMBER, NOT ITS INTENT.**
`test_a_checkpoint_describes_the_position_it_pins` asserted
`cp.seq == log.verify().head_seq`. The head now advances by one when the
anchor lands, so it asserts `cp.seq == head - 1` **and** that the anchor sits
at `head` naming `through_seq == cp.seq`. The comment says why the number
moved, because a silently adjusted assertion is how a test stops testing what
it was written for.

**ADVERSARIAL TESTS.** The reproducer, with the log asserted to verify at the
end; a separate test proving the forged snapshot really would have been
authoritative, so the refusal is about something; a checkpoint with no anchor
at all; an anchor for a DIFFERENT position, which a check asking only "is
there an anchor anywhere" would pass; and the honest checkpointed load still
agreeing byte for byte with a full load, because a check that refused
everything would satisfy all of the above while deleting the feature.

**FORBIDDEN FAKE FIXES.** Signing the checkpoint file with a key stored beside
it. Adding a second self-hash. Declaring the checkpoint directory trusted and
moving on — the module already says it is exactly as trustworthy as its
directory, and the fix is to stop needing that. Refusing checkpoints
altogether: verification that grows without bound is verification somebody
eventually switches off, which is the defect checkpointing exists to prevent.

**INVALIDATED CLAIMS.** `checkpoint.py`'s statement that a false checkpoint
"cannot make a forged record authoritative", at every commit before this one.
It could, and the full verification it appealed to would not have noticed.

## D-2026-31 — two suites said opposite things about canonical authority, and the weaker one was right

**CLASS** — `AUTHORITY_DEFECT`, `CONTRADICTORY_SPECIFICATION`,
`INVARIANT_OVERCLAIM`.

**AFFECTED COMMIT** — present since dependency invalidation was written;
still true at `3d809f0`.

**DISCOVERED BY.** Hypothesis, in `tests/test_agent_substrate_properties.py`,
the first time it generated a revocation of a record that had a promoted
dependent. The example then persisted in the local database and the failure
became deterministic, which is how it stopped being a curiosity.

**DEFECT.** Two parts of this repository stated incompatible things about one
condition, and both were written on purpose.

The property suite asserted, as an invariant checked after **every** rule:

> for every record whose state is PROMOTED, no dependency is STALE, REVOKED
> or REJECTED.

The audit suite asserted the opposite, with its own anti-vacuity partner and
an explanation:

> `store.py` applies one event at a time and never looks at dependents.
> `qta_agent.invalidation` cascades ONLY when a caller runs it. So a cascade
> nobody ran leaves a PROMOTED record resting on a REVOKED one, every
> individual transition legal, and nothing in the enforcement path able to
> notice.

The second was true. Revoking a foundation left every record promoted on the
strength of it PROMOTED, and **`store.canonical()` returned them** — which is
the function the rest of the system asks when it wants to know what carries
authority. The auditor reported the condition as a provenance gap, which is
an observation after the fact, not an answer to *what is canonical now*.

Reproduced without hypothesis in eight lines: promote `param`, promote
`result` depending on it, revoke `param`. `result.state` is `PROMOTED` and
`canonical()` returns `{"result"}`.

**WHY THE INVARIANT NEVER FIRED BEFORE.** Nothing else in the machine
produces a dead state under a live dependent. STALE arrives only through
`apply_invalidation`, which cascades by construction, so the invariant held
for the one path anybody drove. REVOKED and REJECTED arrive through ordinary
transitions, and nothing had generated one over a promoted dependent.

**IMPLEMENTATION FIX — CANONICAL MEANS WHAT IT SAYS.** A record is canonical
when its state says so **and** everything it rests on is canonical too,
transitively. Restated independently in `reconstruct.py`, so the two readers
are answering the same question rather than one asking the other.

Transitive by construction, because the shortcut is the bug
`invalidation.py` was written against: excluding only the immediate children
leaves the grandchild standing on the same withdrawn input, and its parent's
`state` still reads PROMOTED. A dependency cycle resolves to **not**
canonical: a cycle is a modelling error, and the fail-closed answer to "is
this authority sound" when the graph cannot say is no.

**WHAT WAS DELIBERATELY NOT DONE.** Revocation does not cascade. Making it
cascade would turn a withdrawal — the one operation that must always be
cheap and always available — into a multi-record write that can fail
halfway, and it would contradict the audit suite's deliberate design rather
than reconcile with it. A record's `state` still reads PROMOTED until
somebody runs the cascade, and the provenance gap the auditor reports for
that is still the right report. What changed is that reading the canonical
set no longer hands back authority resting on an input nobody believes.

**THE INVARIANT WAS RESTATED, NOT DELETED.** It now asserts the guarantee the
system actually makes — nothing in `canonical()` rests on a withdrawn
foundation, and the exclusion is transitive — and it additionally requires
the **second reader to agree**, from the log alone. Two readers that disagree
about what is canonical is precisely the divergence this package exists to
surface, and the condition is one they could easily answer differently: one
holds dataclasses, the other plain dicts, and neither asks the other.

Restating an invariant to match the implementation is normally the fake fix
this ledger forbids. It is not one here, and the distinction is worth being
explicit about: the implementation was changed **in the same commit** so that
the invariant asserts something stronger than the system previously provided.
The old form asserted a property of `state` that nothing ever guaranteed; the
new form asserts a property of `canonical()` that is now enforced at the only
place it is read.

**ADVERSARIAL TESTS.** Seven deterministic ones beside the property suite: a
sound chain that must still be canonical (without it the rule could exclude
everything and pass every other case), the revoked foundation, the transitive
grandchild, a REJECTED foundation, a dependency cycle, agreement between the
two readers before and after the withdrawal, and the two routes to "not
canonical" — exclusion and the cascade — reaching the same verdict, so the
system has no reading to prefer.

**MUTATIONS.** `M47`–`M50` on the store and `R98`–`R100` on the second
reader: ignore the foundations, check only the immediate ones, let a cycle
read as sound, and stop letting the state machine decide canonicity at all.
`M48` and `R99` are the interesting pair — they leave a check in place and
make it shallow, which is the shape that passes a test written about one
level of dependency.

**FORBIDDEN FAKE FIXES.** Deleting the invariant because another suite
contradicts it. Weakening it to "no PROMOTED record depends on a STALE one",
which would make it true by dropping the two states that actually reach it.
Making `apply_invalidation` run inside `transition` so the example goes away
while a withdrawal gains a failure mode it did not have.

**INVALIDATED CLAIMS.** Any reading of `store.canonical()` before this commit
as "the records that carry authority". It was "the records whose own state
field says PROMOTED", which is a different and weaker statement wherever a
dependency graph exists.

## D-2026-32 — the performance guard measured time, and the work grew underneath it

**CLASS** — `MEASUREMENT_DEFECT`, `QUADRATIC_PATH`, `IMPLEMENTATION_VS_DESIGN`.

**AFFECTED COMMIT** — present since `governed_stage10.py` was written; still
true at `577572d`.

**DISCOVERED BY.** P1 of the second reopening, by instrumenting
`EventLog.verify` and counting rather than timing.

**DEFECT.** One governed Stage-10 run performed **twenty-six full chain
verifications**. Measured on a fresh log, six consecutive runs:

| run | head | `verify()` calls | records read |
|---:|---:|---:|---:|
| 0 | 26 | 27 | 391 |
| 1 | 49 | 26 | 972 |
| 2 | 73 | 26 | 1576 |
| 3 | 98 | 26 | 2206 |
| 4 | 124 | 26 | 2862 |
| 5 | 151 | 26 | 3544 |

Records read per run grows by about 650 each time — twenty-six passes over
roughly twenty-five new events. Per-run work is linear in the history and the
total is quadratic.

Eighteen of those twenty-six were `self.log.verify().head_seq`: the caller
wanted **where the head is** and paid for a verification of the entire history
to find out. The write path never had this problem — `append` has always
carried an anchor and re-checked only the tail. It was the READ of the head
that went back to the beginning.

`EventLog.advance` exists for exactly this and names the pattern in its own
docstring:

> Doing that with a full read made each governed operation cost the whole
> history: a profile of 120 campaign cycles spent 10 of 13 seconds inside
> `read()`, and doubling the campaign length quadrupled its wall time. **That
> is the quadratic defect this repository has already recorded twice, in a
> third place.**

This was the third place. The mechanism was built, the docstring described
it, and the production caller did the other thing — which is the shape
D-2026-23 was recorded for.

**WHY THE GUARD THAT EXISTS DID NOT SEE IT.**
`test_one_governed_operation_does_not_get_slower_as_the_history_grows`
measures **wall time**, and records a ratio of ~1.1 against a ceiling of 4.0.
It is a real guard and it was measuring the wrong quantity: a governed run is
dominated by a subprocess, so hashing three thousand records is microseconds
against 1.2 seconds. Extrapolate ten thousand runs — a quarter-million events,
six and a half million records re-hashed per run — and the ratio the guard
watches would still have been about 1.1 for most of the way there.

A time guard on a subprocess-dominated operation cannot see the growth of the
work inside it. That is the same defect class as D-2026-28: a measurement
named after one thing, measuring another, and right by luck.

**IMPLEMENTATION FIX.** `GovernedStage10._head_seq()` holds an anchor. The
first call verifies the whole chain and fails closed; after that `advance()`
verifies only the records this caller has not seen, and refuses to move past
a broken chain. Every record is verified exactly once per caller instead of
twenty-six times per run. `projection()` still verifies in full, because a
reader that cannot say which records went through the gate must not hand back
a state.

Twenty-six becomes eleven, and the records read at run 5 fall from 3,544 to
1,484.

**WHAT IS NOT CLOSED, AND IS RECORDED RATHER THAN ROUNDED.** Eleven full
verifications remain per run, and every one is named: **eight** from
`projection()` — six inside `_move`, one in `run`, one in `recover` — and
**three** from `capability.issue`, which verifies to establish the seq a grant
is in force from. Per-run work therefore still grows with the history, by
about 270 records per run rather than 650.

Closing the rest means making `projection()` incremental — holding a folded
state and an anchor, as `AuthorityStore` does — which is a real change to the
production projection and is **not** made here. The honest state of this
finding is *deeply implemented with a measured residual gap*, and the residual
is written into the guard's own constant so nobody has to rediscover it.

**THE GUARD THAT REPLACES THE ASSUMPTION.**
`test_a_governed_run_verifies_the_whole_chain_a_bounded_number_of_times`
counts **full chain verifications**, not milliseconds, and requires the count
not to grow with the history. Thirteen on the first run of a fresh caller —
the one full pass the incremental path is built on — and eleven thereafter.
Putting `self.log.verify().head_seq` back into the production caller takes the
count straight past the ceiling.

It has an anti-vacuity partner that drives the count past the ceiling on
purpose and requires the probe to notice, because a counter that always
reported zero would satisfy the ceiling for any implementation at all. And
the main test asserts the later run read MORE records than the first — a run
that verified nothing would otherwise pass every line of it.

**FORBIDDEN FAKE FIXES.** Raising the time guard's ceiling. Replacing the
head reads with `log.head()`, which returns the witness and drops the chain
check — that is buying the speed by deleting the guarantee, and `advance()`
exists so the two are not a trade. Writing `1` into the ceiling because it
would be nicer: the residual eight and three are real.

**INVALIDATED CLAIMS.** Any reading of the governed-operation performance
guard as evidence that one governed run costs a bounded amount of work. It
was evidence that one governed run takes a bounded amount of TIME, on a
history short enough for the subprocess to dominate.

## D-2026-33 — a canonical output went stale when the code that writes it changed, and R59 hid it for four commits

**CLASS** — `STALE_DERIVED_ARTEFACT`, `MISCLASSIFIED_FAILURE`.

**AFFECTED COMMIT** — since the P0-R6 / D-2026-21 convergence work landed on
this branch; surfaced at `577572d`.

**DISCOVERED BY.** Hosted CI, on a runner that happened to have the same
arithmetic as the canonical set — so the one real divergence was not buried
among two dozen host-dependent ones.

**DEFECT.** `thermal_3d_verification_report.json`, the committed canonical
copy, was stale. The branch's own P0-R6 work added a `converged` flag to the
3D solver's energy accounting (`thermal_3d_transient.py`, so that a failed
solve cannot be read as a successful one), that flag flows through
`verification_3d.py` into the report, and **the committed artefact was never
regenerated.** It carried `"converged": null` where a regeneration produces
`"converged": true`.

One added line. No numeric value changed at all.

**WHY IT TOOK FOUR COMMITS TO SEE, AND WHY THAT IS THE INTERESTING PART.**
`package_consistency_check.py` reports the files whose committed copy differs
from a fresh regeneration. On the non-AVX-512 runners this branch kept drawing,
that list was **24 files long** — R59, the host-CPU-dependent divergence — and
the stale one sat inside it, indistinguishable by name from twenty-three files
that differ for a reason nobody can fix.

At `577572d` the job drew a runner reporting

```
openblas runtime kernel: SkylakeX
numpy SIMD found: ['X86_V3', 'X86_V4', 'AVX512_ICL']
```

— the canonical configuration. `results_gate_table.csv` matched byte for byte,
62 of 63 root outputs matched, and the list came back with **exactly one
name**. A real defect is visible only when the noise it hides in goes quiet.

**THE MISCLASSIFICATION THIS ALMOST BECAME.** I had just posted a comment on
PR #17 explaining that `full-suite` was red for R59 and that there was nothing
to fix. That comment was correct about `3d809f0`, where both the count (24)
and the diagnostic line (Haswell / `X86_V3`) supported it. Applying it to
`577572d` without re-reading the log would have filed a stale artefact under
a known-unfixable heading and left it there.

The §15 rule for classifying a failure as R59 — *pytest passed AND
package-consistency actually ran* — is necessary and **not sufficient**. Both
conditions held at `577572d` and the failure was still this PR's. The rule
that was missing: **R59 is a statement about host arithmetic, so it is only
available when the host's arithmetic actually differs.** The diagnostic step
prints exactly that, and on this run it said the host matched.

**IMPLEMENTATION FIX.** The canonical copy is regenerated. Verified before
committing it, on this container:

* a full 3D regeneration produces **36 of 37 outputs byte-identical** to the
  committed copies, so this host reproduces the canonical set and refreshing
  one file cannot smuggle in local arithmetic;
* the only file differing is this one, and its only difference is the
  `converged` field;
* `package_consistency_check.py` then returns `RESULT: PASS (all consistency
  checks passed)` locally.

**WHY THIS IS NOT THE FORBIDDEN "REWRITE A CANONICAL OUTPUT".** That
prohibition is about R59: rewriting an output so a host-arithmetic divergence
stops being reported, which destroys the comparison. This is the opposite
case. The code that writes the artefact changed deliberately and under review,
the artefact is derived, and leaving it stale is what makes the byte gate
report a difference nobody can act on. The distinguishing test is whether the
numbers moved: here nothing numeric changed, one structural field was added,
and 36 of 37 siblings were untouched.

**INVALIDATED CLAIMS.** Any reading of this branch's earlier `full-suite`
failures as *entirely* R59. The 24-file list at `3d809f0` contained 23
host-divergent files and one stale artefact, and nothing then distinguished
them.

---

## D-2026-34 — "usable" was decided by the log's size, and a test's precondition rode on two bytes

**CLASS** — `TRUE_DEFECT` (production), plus `WRONG_TEST` and a
`SIBLING_SWEEP_OMISSION` of my own making.

**AFFECTED COMMIT** — since checkpointing landed; surfaced at `9d7d3c2`.

**DISCOVERED BY.** The hosted `second-interpreter (3.13)` job at `9d7d3c2`,
which failed one test with `DID NOT RAISE CheckpointError`. That test was the
SIBLING of one I had fixed two commits earlier, in the same file, for the
same reason — and I did not sweep the file when I fixed the first one. The
class this project exists to close, committed by me, in the act of closing it.

**THE TEST DEFECT.** `prune` refuses when no checkpoint verifies against the
log. Two tests set that condition up by making the foreign log's single
record SHORTER than the checkpointed log's records, so that a byte-offset
comparison would reject every checkpoint. The margin was two bytes — the
difference between `t0` and `z` twice — and a record's length includes a
wall-clock float whose JSON repr varies by a byte or three. So the
precondition held by coincidence, and one hosted run lost the coin flip.

The first fix widened the margin to ~59 bytes on ONE of the two tests. That
is the move the directive names: it closed the example.

**THE PRODUCTION DEFECT UNDERNEATH IT — AND THIS IS THE REAL FINDING.** The
reason a byte margin could set up "no checkpoint describes this log" at all
is that `CheckpointStore.latest_usable` decided exactly that question with
`check_against`, whose own docstring says it answers the weaker one:

> Raise unless ``cp`` **could** describe ``log``. Cheap; reads no records.

It compares the checkpoint's end offset against `stat().st_size` and its seq
against the head witness. Both are properties of the log's SHAPE. Two
entirely different logs of similar length have the same shape, so a
checkpoint of one passed against the other, and its offsets seeked into the
middle of an unrelated record. `latest_usable`'s docstring meanwhile claimed
the strong reading — "the newest checkpoint that both parses and **describes**
`log`" — and both of its consumers acted on that claim:

* **`prune` deleted evidence in the exact situation its refusal exists for.**
  Against a foreign log of ten longer records, it found seq 9 "usable" and
  removed eight of ten checkpoints. Reproduced before the fix:

  ```
  latest_usable(other) -> 9
  REMOVED WHILE THE LOG IS IN DOUBT: (0, 1, 2, 3, 4, 5, 6, 7)
  ```

  Its docstring calls that "the one arrangement worse than an oversized
  store".

* **`load_from` stopped its backwards walk at the top.** Walking backwards is
  the whole design — an older valid checkpoint is strictly better than none —
  and a foreign checkpoint that merely fit ended the walk before the
  describing one was reached, so a recoverable store failed to recover.

**REPAIR.** "Describes" now means what the word means.

`EventLog.check_anchor(anchor)` reads the one record the anchor names and
requires it to BE that record: the byte range must hold exactly one record,
at the anchor's seq, hashing to the anchor's hash and to its own. It is O(1)
— a seek and a line — and it verifies nothing else, which is stated in its
docstring rather than left to be assumed. The rules it applies were already
in `verify_from`; they now live in `_read_anchored`, which both callers
share, so each rule exists in exactly one place.

`checkpoint.describes(log, cp)` is `check_against` plus that binding, and
`latest_usable` calls it. `check_against` keeps existing and keeps its weaker
contract, because `verify_with` follows it immediately with a real
verification that would catch any disagreement anyway.

**THE TESTS NO LONGER DEPEND ON LENGTH AT ALL.** Both fixtures now use a
foreign log whose records are deliberately LONGER, so every size comparison
passes and only content can tell the two logs apart — the harder direction,
and the one no timestamp can flip. `test_could_describe_and_does_describe_are_different_questions`
is their anti-vacuity pair: it asserts `check_against` ACCEPTS the same pair
`describes` refuses, so the stronger check is proved to name a real
condition rather than being decoration.

**A THIRD TEST WAS OVERCLAIMING AND IS RENAMED.**
`test_a_checkpoint_for_a_different_log_is_refused` refused `log_b` for being
two records long, not for being a different log. It is now
`test_a_checkpoint_for_a_SHORTER_different_log_is_refused_by_size` and
asserts the message it actually depends on. A test whose name claims more
than its assertion is a claim audit finding wherever it appears.

**MUTATION ANCHORS DRIFTED IN THE REPAIR, AND THAT WAS CAUGHT.** Extracting
`_read_anchored` dedented four anchors in `agent_checkpoint.json` (E21–E24),
which then matched **0 times** — absent, not passing — and the deduplication
of the truncation rule briefly made E25 match twice. All five are repaired,
and two mutations are added for the new rule: `E26` degenerates `describes`
back to `check_against`, `E27` points `latest_usable` at the weak check. Both
are killed by the tests above; the spec is 34 mutations.

**AND THE HARNESS MISDIAGNOSED IT, FOUR TIMES, IN WRITING.** The
`agent-substrate` job was red at `f80caa8`, `3d809f0`, `577572d` and
`9d7d3c2` — four commits — and the checkpoint matrix inside it reported
**32/32 killed, all sources restored byte-identical**, followed by:

```
POST-RUN BASELINE RED -- the matrix left this tree in a state the suite
rejects: ['test_pruning_refuses_when_nothing_verifies_against_the_log']
Restoration was byte-identical but not complete; something outside the
mutated sources survived the run.
```

The first line is an observation. The second is a **conclusion the harness
cannot draw from what it measured**: a nondeterministic test produces that
signature exactly — green before, red after, sources byte-identical, nothing
left behind — and that is what this was. The message sends the reader after a
stray file that was never there, which is expensive in a repository that
already has an open follow-up (0b) about a test damaging tracked files.

So the harness now asks one more question it can actually answer: it re-runs
the named tests on the tree as restored. If they pass, it says so and says
what that does and does not license:

> ...but re-running those tests alone on the SAME restored tree passes. The
> tree is not simply broken: either the test is nondeterministic or it
> depends on state the rest of the suite sets up. Do not conclude that
> something survived the run without evidence of the thing.

If they fail again, the residue wording stands — now earned. Both branches
are tested (`test_state_a_mutation_leaves_behind_is_caught_after_the_run`
asserts the residue message; its new pair asserts the other one and asserts
the residue wording is ABSENT), and `H19` mutates the re-check away.

Note what this does NOT do: it does not make the run pass. A red post-run
baseline still fails the matrix. The repair is to the diagnosis, not to the
verdict.

**WHAT I GOT WRONG, PLAINLY.** I fixed one instance of a fixture defect and
did not look for its sibling twelve lines below it in the same file. The
hosted run found it. And the byte-margin framing was itself the wrong level:
the fixture was only fragile because production was deciding a content
question with a size comparison, which no amount of margin would have fixed.

---

## D-2026-35 — the fix for a stale artefact left four artefacts pinning the old one

**CLASS** — `STALE_DERIVED_ARTEFACT`, `SIBLING_SWEEP_OMISSION`. Mine, again,
in the commit that closed the previous one.

**AFFECTED COMMIT** — `a956dea`, which is HEAD of this branch as this is
written. Hosted `full-suite` is RED there, and this is why.

**DISCOVERED BY.** The hosted `full-suite` job at `a956dea`, failing
`tests/test_stage8_data_provenance.py::test_mapping_registry_valid_and_complete`
— reproduced locally before any of the repair below.

**DEFECT.** D-2026-33 regenerated `thermal_3d_verification_report.json`, a
governed output. `MANIFEST_BOUNDARY.md` documents a regeneration order for
exactly that situation, because several generators hash artefacts that
earlier generators rewrite. `a956dea` ran the manifest half of that order and
none of the HDF5 half, so three tracked artefacts kept pinning the digest the
regeneration had just invalidated:

| artefact | what it held |
|---|---|
| `hdf5_output_mapping.json` | `sha256` for that output: `c482faf5…`, one line |
| `hdf5_schema.json` | the same digest, one line |
| `qta_scientific_results.h5` | the output's **bytes**, under `/native_json` — a stale COPY of a governed output inside the artifact that is supposed to be equivalent to it |

`validate_hdf5_equivalence.py` said so as soon as it was asked:
`RESULT: FAIL`, 1 problem, 469 datasets where the tree records 470.

A fourth file, `verification/snakemake/canonical_outputs.done.json`, also
pins the old digest and is **not** a defect: `verification/` is gitignored,
so that file is a local workspace marker, not part of the repository. Checked
rather than assumed, because "four files matched the grep" was the first
reading.

**WHY THE COMMIT'S OWN CHECKS DID NOT SAY SO.** `generate_manifest.py --check`
printed `manifest in sync (559 files; 2 detached by policy)` and that was
true. The manifest is LAST in the regeneration chain and hashes whatever it
finds, including a stale link earlier in the chain; "in sync" means every
tracked file is listed and every listed hash matches the bytes on disk, and
says nothing about whether two derived artefacts agree with each other. I
read the stronger claim off the weaker check — the same shape as D-2026-34,
committed the same day.

`package_consistency_check.py`, the byte gate that compares committed copies
against fresh regenerations, could not have separated this from the
twenty-three R59 host-arithmetic differences it reports on a non-AVX-512
runner. The suite could, and did, one commit later.

**REPAIR.** The documented order, from the point the change actually entered
the chain — `qta_full_sim.py` deliberately NOT re-run, since regenerating the
canonical outputs on this host would rewrite them with this host's
arithmetic:

```
build_hdf5_mapping.py     -> one digest in each of mapping and schema
build_hdf5.py             -> qta_scientific_results.h5 rebuilt
validate_hdf5_equivalence.py -> RESULT: EQUIVALENT, 470 datasets, 0 problems
ro_crate_tools.py / validate -> 30 entities, 24 files, VALID
tools/corpus_allowlist.py --write
generate_manifest.py
```

And `generate_manifest.py --check` now prints what it does **not** check,
beneath the line that misled me:

> (coverage and hashes only — it does not check that derived artifacts agree
> with their sources; after changing a governed output run the regeneration
> order in MANIFEST_BOUNDARY.md and then the test suite)

**AND THE GUARD THAT CAUGHT THE MATRIX MOVING WAS HALF WRONG ITSELF.**
Downgrading R41 fired `test_the_matrix_is_not_completed_silently`, which is
exactly its job: the complete count is written down so that moving it is a
decision somebody makes on purpose. Its second assertion was
`len(rows) == EXPECTED_COMPLETE` — true only while every row is complete, an
equality that held by circumstance and was written down as a rule. It
reported "one of the two numbers is wrong" about two numbers that were both
right. The row count is now its own constant, `EXPECTED_ROWS`, and the
comment says which mistake that fixes. The same shape, one more time: an
identity that happened to hold, stated as an invariant.

**WHAT I GOT WRONG, PLAINLY.** I regenerated a governed output, ran the
manifest, saw "in sync", and committed without running the suite that gates
derived artefacts. The documented order exists precisely because this is easy
to get wrong, and I did not open it. Two sibling-sweep omissions in one
session — this one and D-2026-34's — is the pattern, not the accident.

---

## D-2026-36 — four enforcement points in the second reader, unprotected, and a verdict that overstated why

**CLASS** — `MISSING_TEST` (two), `WRONG_TEST` / mis-scoped specification
(two), plus an overclaiming report in the verifier itself.

**AFFECTED COMMIT** — since `577572d`, the commit that made `canonical()`
transitive and added the mutations that were supposed to prove it.

**DISCOVERED BY.** The hosted `agent-substrate` job, which I had been reading
as "the flaky checkpoint test" for four commits. It was not only that. The
`agent_second_reader` matrix reported **96/100**, and the four survivors are
the interesting part:

```
R92_a_checkpoint_claim_need_not_name_a_head_hash                  SURVIVED
R98_the_replay_ignores_the_foundations_of_a_canonical_record      SURVIVED
R99_the_replay_checks_only_the_immediate_foundations              SURVIVED
R100_a_cycle_reads_as_sound_in_the_replay                         SURVIVED
```

All four reproduce at HEAD, applied by hand, one at a time, sources restored
byte-identical after each.

**DEFECT.** `reconstruct.canonical_ids()` restates the rule D-2026-31 put in
`store.canonical()`: a record is canonical when its own state says so AND
everything it rests on is canonical, transitively, with a cycle resolving to
no. The store side has four tests for that rule. The second reader's half had
**one** assertion anywhere in the tree — `canonical_ids() == ()` in a test
about a forged edge, where the answer is empty for an unrelated reason.

So the reader could have ignored foundations entirely, or checked only the
immediate ones, or called a cycle sound, and the suites the spec runs would
have stayed green. That is the definition this repository uses: a survivor
means the check is not load-bearing.

And R92: the sibling rule for `state_digest` — a snapshot is cited by digest
or it is not cited — has a test. The `head_hash` rule beside it did not.

**THE PART THAT IS ABOUT THE VERIFIER, NOT THE CODE.** A test that kills R98
and R99 **does exist**: `test_both_readers_agree_about_a_withdrawn_foundation`,
in `tests/test_agent_substrate.py`. The `agent_second_reader` specification
does not list that suite, so the matrix never ran it.

The harness then printed, for each survivor:

```
SURVIVED    <-- nothing detects this: ...
```

which is not what it measured. It measured that **the suites in this
specification** went green with the check removed. "Nothing detects this" and
"this spec's suites do not detect this" have different repairs — a missing
test versus a mis-scoped specification — and the stronger sentence sends the
reader after the wrong one. The wording is now scoped to what was run, and
the summary names the suite list and says that a mis-scoped spec and a
missing test look identical from inside the harness.

**REPAIR.** Six tests in `tests/test_agent_differential.py`, which the spec
does run, mirroring the store-side set onto the second reader and comparing
the two readers rather than asserting one:

| test | kills |
|---|---|
| a sound chain IS canonical in both readers | anti-vacuity for all of them |
| a record on a revoked foundation, in both | R98 |
| the GRANDCHILD of a revoked foundation, in both | R99 |
| a dependency cycle, in both | R100 |
| an anchor naming a head hash of prose | R92 |
| an anchor two records back is otherwise accepted | anti-vacuity for R92 |

The grandchild test carries its own anti-vacuity assertion: it computes what
a *shallow* reader would answer and requires it to differ from the transitive
one, so the fixture cannot quietly stop separating the two rules. The R92
pair is there because the anchor in that test names a position two records
back — if an older position were refused on its own, the digest rule would
never be reached and the test would prove nothing.

Verified one mutation at a time: all four killed, and R98 and R99 killed by
the grandchild test **alone**, so neither rides on the other's failure.

**IS THE MIS-SCOPING SYSTEMIC? I CHECKED, AND THE CHECK DOES NOT EXIST.** The
obvious sweep is mechanical: for every specification, find the test suites
that exercise the modules it mutates and are not in its suite list. Run that
and every spec reports dozens — `agent_checkpoint` alone shows 34 — because
the relation available to a script is "this suite imports that module", and
in this repository almost every agent suite imports `events`, `store` or
`governed_stage10`. The relation that would matter is "this suite tests the
rule this mutation removes", and nothing derives that from source.

**AND THE AUTOMATIC VERSION WAS COSTED, NOT ASSUMED.** The other mechanical
repair is to re-run each SURVIVOR against a wider suite set and let the
harness say which of the two cases it is. That is attractive because the cost
is zero on a green run — survivors are supposed to be none — and it is paid
only on a run that is already failing. Measured: the union of every
specification's suites is 45 files and takes **7m46s** on this container. At
four survivors that is half an hour added to a job that is already red, for
an answer a reader can get by running one suite against one mutation.

So it is not adopted, and the repair stands at the harness saying what it
measured plus the summary line telling the reader to check the wider suite
before recording a check as unprotected. Recorded with the number so that the
next person to consider it does not have to re-measure, and so nobody
concludes the sweep was skipped.

**WHAT I GOT WRONG, PLAINLY.** I wrote the transitive rule and the mutations
meant to prove it in the same commit, and never held evidence that those
mutations died — the local matrices kept being killed by process cleanup in
this container, and I treated "the code is right" as if it were the same
claim. It is the claim this repository exists to distinguish from. Then I
read four hosted failures as one flake because the first one I opened was.

---

## D-2026-37 — a guard that claimed to be immune to a busy machine, failing on a busy machine

**CLASS** — `WRONG_TEST` (a property measured in units that do not hold it),
plus a claim in two places that the failure falsified.

**AFFECTED COMMIT** — since the guard was written; surfaced at `423e51f`.

**DISCOVERED BY.** Hosted `full-suite` at `423e51f`, failing pytest — not the
byte gate this time:

```
FAILED tests/test_agent_performance.py::test_per_append_cost_does_not_grow_with_history
AssertionError: a burst of 100 appends onto a history of 400 cost 4.7x the
same burst onto an empty log
assert 4.738645984077977 < 4.0
```

**IS IT A REGRESSION? NO, AND THAT WAS MEASURED BEFORE IT WAS CLAIMED.** The
ratio was run seven times in this container at the same commit: **0.92, 1.02,
1.04, 1.04, 1.05, 0.92, 0.94**. The property holds with four times the margin
the ceiling asks for. Nothing in D-2026-34's change to `events.py` adds work
proportional to history — `verify_from` traded one `stat()` for one `fstat()`,
the same syscall count.

**THE DEFECT IS THE GUARD, AND THE SHAPE OF IT IS SPECIFIC.** The suite's own
docstring said:

> A machine ten times slower than this one passes them all, which is the
> point.

True of a ratio between two SIZES: an 8x spread puts healthy at about 8 and
quadratic at about 64, and no amount of CPU steal moves a measurement across
an order of magnitude. **Not** true of this guard, which compares the same
size at two different TIMES. It has no size signal at all — the only thing
separating pass from fail is how the machine felt during each of two bursts,
and a shared runner is not uniformly slow, it is slow in bursts. The word
missing from the docstring was *uniformly*, and it was doing all the work.

An earlier repair had already been tried on this same guard: a `min()`-of-N
estimator, with a comment saying "the answer to a noisy measurement is a
better estimator, not a looser bound". That was right and insufficient. A
better estimator narrows the distribution; it cannot supply a discriminator
that is not there.

**REPAIR: COUNT, DO NOT TIME.** The property is "the append path does not do
more work as the history grows", and the work is countable: every record a
verification checks is re-hashed exactly once, and every append hashes the
one record it writes. `_count_rehashes` patches `Event.recompute_hash` and
counts — the same move D-2026-32 made for the governed-run guard, one file
over.

Measured here:

| configuration | burst onto empty | burst onto 400 |
|---|---|---|
| `full_verify_every = 0` (the default, incremental) | 100 | **100** |
| `full_verify_every = 50` (whole-chain checking, the regression shape) | 251 | **1067** |

So the guard now asserts **equality**, where the old one allowed fourfold
growth, and it cannot be moved by a busy machine in either direction. This is
not a loosened gate; it is a strictly stricter one. `test_the_per_append_probe_can_actually_see_growth`
is its anti-vacuity partner: a counter that always returned the same number
would satisfy an equality assertion perfectly, so the partner turns periodic
whole-chain verification back on and requires the count to rise.

**THE CLAIM AUDIT, IN BOTH PLACES IT WAS WRONG.** The suite docstring now says
*uniformly* slower, and says where that stops being true and what was done
about it. R49 in the completion matrix said "a guard fails on shape, not on
wall time, so a slow machine cannot fail it" — a hosted machine had just
failed one — and is corrected and downgraded to
`DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS`. **37/39, two open.**

**AND THE GAP IS NAMED RATHER THAN CLOSED BY REWRITING.** Seven guards still
assert on a wall-clock ratio. Each compares two sizes with a wide spread,
none has produced a false failure, and converting them wholesale on the
strength of an argument about the one that did fail is how a suite gets
rewritten instead of repaired. What is honestly missing is that their
robustness is a judgement about spread rather than a measurement, and nothing
records how close any of them has come to its ceiling on a hosted runner.
That is the residual gap on R49, and the counting probe is the pattern if one
of them starts to drift.

**WHAT I ALMOST DID.** The quick reading was "4.74 against 4.0 on a shared
runner, that is noise, re-run it". That reading is available for every
timing guard forever, and it is how a guard stops being a guard. The rule
this repository already has — *flake is not a root cause* — is what sent me
to measure the ratio locally first and then to look at why the number could
move at all.

---

## D-2026-38 — the second reader spoke a wider job language than the scheduler, and nothing compared the two

**CLASS** — `TRUE_DEFECT` (the second reader admits forged moves), plus
`MISSING_TEST` for the parity that would have caught it the day it drifted.

**AFFECTED COMMIT** — since the job replay was written.

**DISCOVERED BY.** Working the P1 list item recorded as "scheduler language
parity". The authority and task machines have had restatement-vs-production
tests since D-2026-27; the job machine, which is the one on the production
scheduling path, had none, and the question "does anything compare these?"
answered itself in one grep.

**DEFECT.** `reconstruct.py` restates the scheduler's vocabulary rather than
importing it, which is right and is the point of a second reader. What it
restated was **less than the scheduler has**, and in the permissive
direction:

| the scheduler | the second reader | what that admits |
|---|---|---|
| `EDGES` — 18 moves | **no edge table at all** | any pair whose source matched the replay |
| `INITIAL` = `WAITING`, enforced at enqueue | `_JOB_INITIAL` = `{WAITING, READY}` | a job born READY, past the check that its dependencies hold |
| `SEALED` = {BLOCKED, CANCELLED, FAILED, INVALIDATED}, **derived from the table** | `_JOB_TERMINAL` = {SUCCEEDED, FAILED, CANCELLED}, hand-listed | a job revived out of BLOCKED; and SUCCEEDED called terminal though `SUCCEEDED -> INVALIDATED` is a move the machine has |
| `OUTCOME_STATES` | `_JOB_OUTCOME` | — agreed |

Reproduced before the repair, with a forged log and no mutation:

```
invented_edge   (WAITING -> SUCCEEDED): anomalies=NONE, final state 'SUCCEEDED'
revive_blocked  (BLOCKED -> READY):     anomalies=NONE, final state 'READY'
enqueue in READY:                       anomalies=NONE
```

A job that was never dispatched reads as verified work, and the reader whose
job is to disagree said nothing. The reducer's own comment, twelve lines
below the check that let it through, states the principle:

> A second opinion that accepts a wider language than the first is not a
> second opinion on the logs that matter.

That comment is about the lease and attempt rules. The structural check it
introduces — "everything above this point checks the SHAPE of the move" —
was checking position and a hand-listed terminal set, not legality.

**REPAIR.** `_JOB_EDGES`, 18 pairs, restated in this module's own terms for
the same reason `_AUTH_EDGES` and `_TASK_EDGES` are. `_JOB_SEALED` is
**derived** from it — as the scheduler derives its own `SEALED` from its own
table — so the summary cannot drift from the thing it summarises, which is
exactly how the set it replaces came to disagree. `_JOB_INITIAL` is one
state. `_JOB_TERMINAL` and `_JOB_PENDING` are gone: a set nothing reads is a
statement nothing tests, and the edge table now carries what they meant.

The refusal distinguishes the two cases an operator would want distinguished:
a move the machine does not have, and a move out of a state nothing leaves.

**AND THE HALF THAT MAKES IT STAY FIXED.** Five parity tests, each failing by
NAMING THE DIFFERENCE rather than reporting inequality: the edge tables, the
initial state, the sealed sets (two independent derivations from two
independently written tables), the outcome states, and every state the
scheduler has appearing in the reader's table — the last catching a drift a
pairwise edge comparison would miss. Plus the reproducer kept as a test and
its anti-vacuity partner, because a reader that refused every transition
would satisfy the first perfectly.

Two mutation anchors moved with the code they name (`D2`, and `D6` renamed to
what it now removes), and three were added: widening the initial state,
hand-listing the sealed set again, and giving the reader a move the scheduler
lacks. All five killed, applied one at a time, sources restored
byte-identical — and **two of them are killed by the new parity tests**,
which is what makes those tests load-bearing rather than decoration.

**TWO TESTS WERE PASSING FOR REASONS THEY ARE NOT ABOUT, AND SAID SO.**
Tightening the enqueue rule made
`test_the_second_reader_refuses_an_enqueue_with_no_retry_budget` fail: its
fixture enqueued in READY, which is now refused before the budget rule is
reached. Fixed at the fixture, not at the assertion. And
`test_a_terminal_job_cannot_be_revived` forged `SUCCEEDED -> READY` and
asserted "leaves terminal state" — the wrong reason, since SUCCEEDED is not
a state nothing leaves. It now asserts the edge message, and a new sibling
covers the state that really is sealed.

---

## D-2026-39 — two verifiers that checked nothing and said so in the affirmative

**CLASS** — `VACUOUS_VERIFIER`, twice, in a class this repository had already
named and swept once.

**AFFECTED COMMIT** — since both verifiers were written.

**DISCOVERED BY.** Working the P1 item recorded as "verifier anti-vacuity" —
by sweep, not by a failing run. Worth stating plainly given the week: the two
findings before this one were both handed to me by hosted CI after I had
shipped them.

**THE CLASS WAS ALREADY ON THE RECORD.** `tools/performance_baseline.py`
calls it "the vacuous ... defect"; `test_the_recorder_refuses_to_record_nothing`
says outright:

> This repository already carries that defect once, in a verifier that
> compared zero files and printed IDENTICAL.

The class was identified, one sibling was fixed, and **the sweep stopped
there**. Both instances below sit in the same step of
`MANIFEST_BOUNDARY.md`'s regeneration order, which describes them together:
"verifiers that also **emit a tracked report**".

**INSTANCE 1 — `validate_hdf5_equivalence.py`.** Every check appends to
`problems`, so the verdict was `EQUIVALENT` exactly when nothing went wrong,
including when nothing happened. Handed a mapping with no outputs and an
HDF5 file carrying a well-formed `/provenance` group:

```
equivalence: {'sources_checked': 0, 'datasets_checked': 0, ...} | problems 0
RESULT: EQUIVALENT
exit code: 0
report result field: EQUIVALENT
```

That word is written into `stage8_reports/hdf5_equivalence_report.json`,
which the RO-Crate publishes as an entity. The counts were printed all along
and that is not enough: the VERDICT travels, and a downstream reader sees the
word, not the zero beside it.

Note what masked it. A first attempt at the reproducer failed — but on
"extra unmapped dataset groups" and then on eight `/provenance missing attr`
problems, neither of which has anything to do with equivalence. The vacuity
was shielded by incidental checks, which is why the fixture in the test is
deliberately well-formed.

`build_hdf5.py` already refuses an incomplete output set. The validator that
checks its work did not.

**INSTANCE 2 — `ro_crate_tools.py validate`, found by sweeping for the
first's siblings.** Every checksum it verifies is verified inside a loop over
`hasPart`. With `hasPart` empty the loop runs zero times:

```
RO-Crate validation: 3 entities | 0 referenced files | problems 0 []
RESULT: VALID
exit code: 0
```

Also written into a tracked report, `stage8_reports/ro_crate_validation_report.json`.

**REPAIR, IN THE STRONGER FORM.** "Did you check anything" is the weak
question; "did you check what you said you would" is the one worth asking.

* The equivalence validator compares `sources_checked` against the mapping's
  own declared `n_governed`, so a mapping truncated to three outputs is
  refused as well as an empty one.
* The crate validator refuses an empty `hasPart` **and** reports any entity
  carrying a checksum that sits outside it — a hash nothing verifies is
  decoration. Contextual `#`-prefixed entities are excluded, because
  `#stage7-input-zip` is legitimately one and a rule that flagged it would be
  a rule about the wrong thing. That exclusion was measured against the real
  crate before it was written, not assumed.

**AND A CRASH IS NOT A REFUSAL.** The crate validator appended "root dataset
entity missing" and then indexed `by["./"]` anyway, so a crate without a root
died with a KeyError traceback instead of returning a verdict — in a
repository that keeps a whole suite named `test_hostile_input_no_traceback`.
It now returns FAIL.

**WHAT THE SWEEP FOUND ALREADY GUARDED**, recorded so the next reader does
not redo it: `completion_matrix`, `fuzz_substrate`, `performance_baseline`,
`repo_scope`, `workflow_contract`, `package_consistency_check`,
`_stage10_index_tool` and `mutation_matrix` each carry an explicit
anti-vacuity guard. `tools/independent_verify.py` does not refuse an empty
log — correctly, since an empty log is a legitimate state — but publishes
`events_replayed` and a per-subsystem count beside its verdict, with a
comment saying why. Not examined: `stage6_preservation_check`,
`manuscript_consistency_check`, `verify_release`, `tools/audit_log`,
`tools/model_check`, `qta_agent/separate_verify`. Saying which ones were not
looked at is the difference between a sweep and a claim about one.

---

## D-2026-40 — my own new test overwrote a tracked provenance artefact, and the suite stayed green

**CLASS** — `TEST_DAMAGES_TRACKED_FILE`. An instance of open follow-up **0b**,
created by me, in the same session in which I wrote that the discipline it
argues for is "do not edit tracked sources while a matrix holds them".

**AFFECTED COMMIT** — `3f26b27`, which carries the damage.

**DISCOVERED BY.** `tests/test_manifest_completeness.py` on the very next
run, with `hash mismatch stage8_reports/ro_crate_validation_report.json`.

**DEFECT.** `ro_crate_tools.validate()` wrote its verdict to a hard-coded
path — `stage8_reports/ro_crate_validation_report.json`, a tracked artefact
the RO-Crate step of the regeneration order produces and the manifest
hashes. D-2026-39's new tests call `validate()` against throwaway crates, so
each call overwrote the committed report with a verdict about a fixture:

```
-  "entities": 30,          +  "entities": 5,
-  "referenced_files": 24,  +  "referenced_files": 1,
-  "result": "VALID",       +  "result": "FAIL",
```

and `3f26b27` was committed with that in it.

**WHY THE FULL SUITE DID NOT CATCH IT, WHICH IS THE PART WORTH KEEPING.**
Pytest runs files in alphabetical order under `-p no:randomly`, and
`test_manifest_completeness.py` sorts **before**
`test_stage8_data_provenance.py`. So the manifest was verified, and then the
damage was done. The suite exited 0 over a tree it had itself made
inconsistent. Nothing *inside the pytest run* asks the question the mutation
harness asks after every matrix — whether the tracked tree is still the one
it was handed.

**AND SOMETHING ELSE DID ASK IT, WHICH I SHOULD NOT UNDERSTATE.** The
repository has a third guard for this class, and it worked: the Snakefile's
`s10_canonical_untouched` rule — "the substrate did not touch the canonical
tree" — failed at `3f26b27` naming the file exactly:

```
AssertionError in Snakefile line 446:
{'entries': 559, 'mismatches': 1,
 'mismatch_names': ['stage8_reports/ro_crate_validation_report.json'],
 'detached_hash_ok': True,
 'note': 'Stage-10 adapters wrote only under verification/'}
```

Three hosted jobs went red on that single cause — `stack-verify (core)`,
`stack-verify (full)` and, through its own derived-artifact step,
`agent-substrate` — within one commit of the damage being pushed. So the
correct statement is narrower than "nothing catches this": the canonical-tree
guard catches it, in a job the pytest suite does not run, and what the pytest
suite lacks is that check inside itself. The repair above is still the right
one — a function that writes a tracked artefact should take the path — but
the class was not unguarded, and saying it was would have been the same kind
of overstatement this ledger keeps recording.

**REPAIR.** `validate()` takes a `report_path`, defaulting to
`DEFAULT_VALIDATION_REPORT`, and the three tests pass a temporary one.
`validate_hdf5_equivalence.main()` already took its report path, which is why
D-2026-39's other three tests never had this problem — the same function
shape, one file over, with the parameter already there.

The guard is `test_the_crate_validator_writes_only_where_it_is_told`, and it
is deliberately **order-independent**: it does not ask whether the tree is
clean at some moment, which is a question whose answer depends on what ran
before it. It asks whether the function can be made to write the default path
while being handed another one, which is a property of the function.

**WHAT THIS SAYS ABOUT FOLLOW-UP 0b.** It does not identify the historical
instance 0b is about — that one was in an `agent_netauth` run and remains
unidentified. It does show the class is live rather than historical, and that
the mutation harness's post-run tree check is the thing that catches it,
which the plain pytest suite has no equivalent of. Recorded as a gap rather
than closed with a new instance that is not the one 0b names.

---

## D-2026-41 — the projection verified one read of the log and folded another

**CLASS** — `TRUE_DEFECT` (a coherent-read window on the production path),
plus a measurement that named a quantity it was not counting.

**AFFECTED COMMIT** — since the governed caller was written.

**DISCOVERED BY.** Working the P1 item recorded as "Stage-10 coherent read".

**DEFECT.** `GovernedStage10.projection()` read:

```python
self.log.verify().raise_if_bad()
for ev in self.log.read():
```

Two passes over a file that other processes append to. The records folded
are not the records checked. Reproduced, with a record whose chain link does
not hold landing in the window:

```
tasks before: ['task-b82e5fafacd3']
tasks after : ['t-forged', 'task-b82e5fafacd3']
FORGED TASK FOLDED: True
and the log now fails verification: True
```

A task attributed to `mallory`, folded into the projection the governed path
acts on, from a log that does not verify — under a docstring reading
"Rebuild task state from the verified log. Fail closed."

**AND THE SAME SHAPE WAS COSTING EIGHT PASSES A RUN.** Counted directly:

```
one governed run on a warm caller:
  EventLog.verify() calls : 11
  EventLog.read() passes  : 19   <-- the actual file passes
  passes the probe does not count: 8
```

The eight are exactly the eight `projection()` calls reading a second time.

**REPAIR.** `EventLog.read_verified()` returns `(report, events)` from ONE
pass — `verify()` already parses every record to check it, so handing the
list back costs nothing — and `verify()` now delegates to it, so there is one
implementation of the chain check rather than two. `projection()` uses it.
After: **11 passes on a warm caller, 14 at most.** Stronger and cheaper,
which is not the usual trade.

Stated narrowly, because the window is not all windows: records are read
once, so nothing can land between the check and the fold. A rewrite of
EARLIER bytes while the pass is in flight is a different matter and this does
not address it. That is the threat model `verify()` lives in generally, and
`qta_agent.checkpoint` already says how far the filesystem is trusted.

**THE MEASUREMENT WAS NAMING THE WRONG QUANTITY, AND IT IS THE GUARD
D-2026-32 BUILT.** `MAX_FULL_VERIFICATIONS_PER_GOVERNED_RUN = 13` counted
calls to `EventLog.verify` and called them full chain verifications. A warm
run made 11 of those and 19 passes. **The ceiling was 13, the real figure was
19, and the guard passed** — because it counted the wrong unit. On a fresh
log it was 22.

The probe now counts `EventLog.read`, which is a pass over the file whichever
function asked for it, and the constant is `MAX_FULL_LOG_PASSES_PER_GOVERNED_RUN`.
Every number in its docstring is measured:

| case | passes | records read |
|---|---|---|
| first run, brand-new caller and EMPTY log | **14** | 167 |
| second run, same caller (now warm) | 11 | 398 |
| third run, same caller | 11 | 653 |
| fresh caller over an EXISTING log | 13 | 1067 |
| same caller again | 11 | 1196 |

Records grow, passes do not. That flatness is the property; the ceiling is
where it sits.

**THE CEILING WENT FROM 13 TO 14 AND THAT IS A TIGHTENING, WHICH NEEDS
SAYING.** Raising a bound is the move this repository forbids without an
argument. The argument is the unit: against passes, the figures before the
repair were 19 warm and 22 cold, both far above a ceiling of 13 that was
never compared with them. Fourteen is the measured maximum of what remains
after eight passes per run were removed.

And the switch of unit was not cosmetic. With two entry points into a
whole-chain check, a probe watching `verify()` alone would have reported the
repair as **11 calls becoming 3** — an improvement it was not measuring, in
the direction that makes a guard look better while the system does the same
work. `test_the_probe_counts_BOTH_ways_into_a_whole_chain_check` is there
because that reading was available and wrong.

---

## D-2026-42 — the hardware gate's HUMAN authority was a self-declaration

**CLASS** — `TRUE_DEFECT` (an authority claim enforced by asking the
attacker to confess), plus `WRONG_TEST`: the test that held the rule closed
the example and not the class.

**AFFECTED COMMIT** — since the hardware-governance layer was written.

**DISCOVERED BY.** Working the P1 item recorded as "hardware HUMAN
authority".

**WHAT THE REPOSITORY SAID.** In three places. The module docstring: "No
tool authors a review record or promotes evidence automatically."
`governance_summary()`: `"review_authoring": "human-only; tools may verify,
never author or promote"`. `authorities.json`: "human-only review
authoring".

**WHAT THE CODE DID.**

```python
if rev.get("authored_by_tool"):
    reasons.append("review records authored by tools are invalid by "
                   "governance rule")
```

and `reviewer_id` checked only for being a non-empty string. So the rule was:
a tool that admits to being a tool is refused. Reproduced against the code as
it stood:

```
validate_review_record(tool-authored) -> True []
dossier n_entries: 1  n_excluded: 0
ADMITTED, reviewer recorded as: {'reviewer_id': 'automated-evidence-promoter-v3', ...}
TOOL-AUTHORED REVIEW ADMITTED TO GATE-EVIDENCE DOSSIER: True
```

A reviewer whose id is literally the name of a program, accepted as the human
review that admits a hardware record into a gate-evidence dossier.

**AND THE TEST THAT WAS SUPPOSED TO HOLD IT.**

```python
def test_review_completeness_and_human_only():
    ...
    ok, why = validate_review_record({**REVIEW, "authored_by_tool": True})
    assert not ok and any("human" in w or "tools" in w for w in why)
```

It is named `human_only`, and what it proves is that an *honest* tool is
refused. Mutation `H6_a_review_authored_by_a_tool_is_accepted` was killed by
it, and the matrix read 11/11. Both were true. Neither was about the rule.

**THE SAME SHAPE ON THE REQUEST PATH, TWICE.**
`validate_matrix_update_request` enforced separation of duties as
`doc["requester"] in doc["review_ids"]` — two strings nobody had resolved —
and never looked up `review_ids` at all:

```
invented review_ids                     -> True []
requester as reviewer, different case   -> True []
```

A request citing two reviews that do not exist validated; so did one whose
requester was the reviewer under a change of case. Respelling turned one
subject into two and the separation agreed.

**REPAIR.** A reviewer identity must now RESOLVE. `hardware_reviewers.json`
declares the roster; `resolve_reviewer()` requires an entry of kind `HUMAN`,
not retired at the date claimed, whose `registered_by` chain reaches
`OUT_OF_BAND_BOOTSTRAP` — refusing self-registration, cycles, and
registration by a `TOOL`. That is the standard `qta_agent/agents.py` already
holds `PrincipalKind.HUMAN` to, and the reason this repository has two
authority systems and only one of them was enforcing.

`validate_review_record`, `build_evidence_dossier` and
`validate_matrix_update_request` all decide through it. The dossier loads
the roster ONCE and threads it down, so one dossier is decided against one
authority rather than against whatever the file said at each record.
`authored_by_tool` is still refused and is no longer load-bearing; the
comment above the new check says which one is holding the rule up.

**WHAT THIS IS NOT, AND THE MODULE SAYS SO IN ITS OWN VOICE.** It is not
authentication. `agents.py` already put this plainly — "It cannot
authenticate a human… an installation that lets an agent perform the
bootstrap has given the agent human authority, and nothing in this file can
prevent that" — and the same holds here: whoever can write
`hardware_reviewers.json` holds hardware-review authority. What changes is
that the authority is a named file that can be read and diffed, that refuses
to vouch for itself, and that refuses every review when it is absent or
empty instead of accepting every one.

**THE ROSTER SHIPS EMPTY, AND THAT IS THE TRUE STATE.** No hardware data
exists, no review has been performed, nobody has been registered out of
band. So today no review record can be valid and no gate-evidence dossier
can have entries — the same shape as "this repository has no human
decisions", which is what `agents.py` says about escalations. It follows
that `matrix_update_examples/valid_example.json` is no longer *valid*
outright, and the test that asserted `ok` now asserts the two-sided thing:
every refusal it draws is a reviewer-authority refusal (so it really is the
structurally complete example it is for), and there is at least one (so
"valid example" is not read as "acceptable request").

**ANTI-VACUITY.** A dossier with no entries has two causes — nobody
registered, every record deficient — and they are not the same state.
`reviewer_authority` in the dossier and `review_authority` in
`governance_summary()` (and so in `thermal_3d_readiness.json`) report which
roster decided, whether it was present, how many reviewers it held, and what
the basis is. `test_the_roster_this_repository_ships_registers_nobody`
asserts the shipped count is zero, so a future registration has to be
acknowledged where a reader will see it.

**THE TESTS HAD TO MOVE, AND THAT IS THE POINT.** Ten existing tests failed
after the change, all for one reason: their reviewer was nobody. They were
repaired at the fixture — `tests/hw_reviewer_fixtures.py` registers the
reviewer those fixtures are authored by — and every call site in the
hardware suites now names the roster it decides under. That the positive
tests previously passed with an unregistered reviewer is the finding
restated: they were testing record completeness and hash binding and getting
the authority check for free, because there wasn't one.

**AND A GREP WAS STANDING IN FOR THE RULE.**
`stage6_preservation_check.py` verified requester/reviewer separation with

```python
check("requester/reviewer separation enforced by governance",
      "requester may not be a reviewer" in
      Path("qta_multiphysics/hardware_governance_3d.py").read_text())
```

A sentence in a source file survives the rule being deleted around it. It now
builds a document that must be refused for exactly that reason and asks the
validator. `qta_multiphysics/stage7_boundary_models.py` keeps its own copy of
the separation rule as a fast shape check, and
`test_constructing_the_model_is_not_the_authority` holds a document the model
admits and governance refuses, so the weaker copy cannot be mistaken for the
authority.

**MUTATIONS.** `tools/mutations/hardware_governance.json` goes 11 → 21.
H12–H21 attack the resolution itself: the kind, the chain of trust, the
bootstrap, retirement, exact naming, the two request-side identities, the
malformed-roster refusal, and the reviewer count the dossier reports about
its own authority.

**H17, H18 AND H19 SURVIVED THE FIRST RUN, AND THE HARNESS SAID WHY BEFORE I
DID.** Its note on a survivor: *"confirm no suite OUTSIDE this list already
covers it — a mis-scoped spec and a missing test look identical from here."*
They were covered, by `tests/test_stage6_roadmap.py` and
`tests/test_stage7_boundary.py`, which the spec did not name. The spec listed
the module's intake and dossier suites while mutating its request path too.
Recorded rather than fixed quietly, because SURVIVED was the correct output
and the wrong conclusion; the suites are now named and the run reads 21/21.

**WHAT VERIFYING THIS COST, AND THE TWO THINGS THE CHECKERS CAUGHT ME DOING.**
The full suite came back with seven failures, none of them in the code above.

*One: I left the derived chain half-regenerated, which is D-2026-35's class,
and I did it three weeks after writing that entry.* This change edits hashed
SOURCE files (`stage6_preservation_check.py` among them) and one governed
OUTPUT (`thermal_3d_readiness.json`, via `governance_summary()`). I
regenerated `final_manifest.json` and the corpus allowlist and stopped, as
though the manifest were the only hash authority. It is not. The RO-Crate
carries its own checksums, and the HDF5 mapping, schema and artifact carry
theirs. So `test_ro_crate_cli_validate_subcommand_works` ran the CLI, which
correctly recomputed

```
"problems": ["checksum mismatch: stage6_preservation_check.py"],
"result": "FAIL"
```

and wrote it over the tracked `VALID` report — moving a manifest-hashed file
mid-run, which took `test_manifest_completeness` (3), `test_manifest_policy`
(2) and `test_stage8_data_provenance` down with it. One omission, six
failures.

`MANIFEST_BOUNDARY.md` describes my exact mistake in its own words:
"`ro_crate_tools.py` run before a later source edit (leaving a stale
`qta_full_sim.py` checksum in the crate)". The document recording the lesson
was in the repository the whole time.

Run properly, the chain propagates by exactly the amount the change warrants,
and the diff sizes are the evidence rather than the claim: the readiness file
hashes to `76db15a1…`, which appears once in `hdf5_output_mapping.json` and
once in `hdf5_schema.json`; the rebuilt artifact hashes to `a5a9dfe4…`, which
appears once in the equivalence report; the crate rebuilds to `VALID`; the
manifest goes last. Four files, one line each.

*And my first diagnosis was wrong in a way worth keeping.* I said the manifest
cluster was an artifact of my having regenerated the manifest while the suite
was running. I had done that, and it was not the cause. The cause was a stale
crate, and the difference matters: one is a scheduling accident, the other is
a tree that was internally inconsistent and would have stayed that way.

*Two: the isolation scan caught my comments.*
`test_no_gate_computing_module_references_the_substrate` failed on
`qta_multiphysics/hardware_governance_3d.py`, because three comments named
the agent substrate's module while pointing at it as the precedent for this
repair. No import, no dependency — and the scan is right anyway. It is
textual, with no exception for gate modules and none for comments, and the
cheapest guarantee that no gate depends on the substrate is that the string
never appears in one. The rule predates the prose. The names are gone, a
comment says why the sibling is described and not named, and
`authorities.json` carries the cross-reference where it costs nothing.
Widening the scan to admit a comment was the one repair not available.

---

## D-2026-43 — the transition path's guard was correct, documented, and untested

**CLASS** — `MISSING_TEST` on a production guard, plus a second
`TRUE_DEFECT` in the same file: 38 lines of unreachable code.

**AFFECTED COMMIT** — since the guard was written.

**DISCOVERED BY.** `agent-substrate` step 43 at `423e51f`, which had been
SKIPPED in every previous run because the job died at step 26. The first
time it reported anything, it reported this. Confirmed again at `d425d47`:
two commits, two runners, identical result, so no re-run was spent ruling
out a flake.

```
X4_a_record_the_reducer_will_reject_is_written_anyway   SURVIVED
killed: 10/11
```

**IT IS NOT A MIS-SCOPED SPEC, AND THAT WAS CHECKED FIRST.** The harness
says so on every survivor — "confirm no suite OUTSIDE this list already
covers it" — and that warning had already been right once this same day,
for H17–H19 in `hardware_governance`. So all **39** agent suites were run
against the mutant, not the one the spec names. Every one passed. The
coverage is genuinely absent.

**THE DEFECT.** `Scheduler.transition()` builds a record, checks
`check_edge`, and calls `_dry_run` before writing. Its own comment says
why:

> check_edge above asks whether the state machine permits this edge. The
> reducer asks more: … whether a requeue is reclaiming a lease that is still
> live … Writing a record that satisfies the first and not the second leaves
> a log nobody can replay — which is what reconcile did the moment another
> process re-leased a job between the scan and the write.

Reproduced, on the real API, with and without the guard:

| | guard present | guard deleted |
|---|---|---|
| caller sees | `JobTransitionError` | `JobTransitionError` — *the same message* |
| records | 4 → 4 | 4 → **5** |
| replayable | **True** | **False** |

**THAT TABLE IS THE FINDING.** The caller cannot tell the two apart. The
exception is correct, identical, and raised in both. What differs is
whether the refusal arrives before the record or after it — and a refusal
that arrives after the record is durable is not a refusal. Every later
`load()` hits it again with nothing to catch it, and an authority log that
cannot be rebuilt cannot be repaired either, because the history *is* the
authority.

**WHY IT HID, NAMED EXACTLY.** The sibling test exists:
`test_a_refused_lease_renewal_writes_nothing` pins this property for the
`_dry_run` on the RENEWAL path, and pins it well. The identical guard on
the TRANSITION path — the reconcile-requeue race the comment describes —
never got one. A guard can be written, commented, correct, and load-bearing,
and still have nothing behind it; being right is not the same as being
tested, and a reviewer reading that comment would have believed it, because
it is true.

**REPAIR.** `test_a_refused_REQUEUE_writes_nothing` reproduces the live-lease
requeue and asserts the record count is unchanged AND the log still replays
— verified to fail without the guard (`assert 5 == 4`).
`test_the_requeue_guard_is_not_refusing_everything` is its anti-vacuity
partner: a guard that refused every requeue would satisfy the first
perfectly, so the lapsed-lease requeue must still go through, on the same
edge, from the same actor.

**AND THE FILE CARRIED A SECOND DEFECT, FOUND ON THE WAY.**
`qta_agent/scheduler.py` held **two byte-identical copies** of
`_probe_copy` and `_dry_run`, 38 lines apart. Python kept the second and
discarded the first. Present in `HEAD`, confirmed against `git show` rather
than assumed from the working tree.

Nothing had failed, because the copies agreed. What makes it worth more
than a deletion is what happens when they stop: someone repairs `_dry_run`,
edits the copy their editor jumped to, runs the suite, sees green, and has
changed nothing. And a mutation anchored inside such a block matches TWICE,
which this harness reports as `ANCHOR DRIFT -- TESTED NOTHING` rather than
as a kill, so the coverage it appears to have is also not there. No anchor
currently falls inside the block — checked, all `scheduler.py` anchors match
exactly once — so nothing was masked yet.

The copy is gone, and the check is a permanent test over the WHOLE
substrate, `test_no_definition_in_the_substrate_is_silently_shadowed`,
rather than a one-line deletion: an AST scan of every module and class in
`qta_agent/` for a name defined twice in one scope. Verified by
reintroducing the duplicate, which it names by file, symbol and both line
numbers. Two instances existed, both here; closing the example would have
left the class open.

**AND A THIRD THING, WHICH I CAUSED AND THE HARNESS CAUGHT.** The
`agent_cross_process` matrix confirming this repair finished 11/11 and then
reported:

```
TESTS DAMAGED TRACKED FILES under mutation (restored, but the test is
unsafe -- it must undo its own writes in a finally):
  X5_the_witness_is_sampled_after_the_log: ['docs/DEFECT_LEDGER.md']
```

No test did that. I was writing this entry while the matrix ran, and
`_restore` did what it exists for: reverted the file to the snapshot and
kept the discarded bytes under `.mutation-quarantine/`, from which this
entry was recovered intact. That mitigation exists because the same thing
happened twice before, and `QUARANTINE`'s own docstring says so — "a
rewritten completion matrix, and then this file's own previous version of
this fix". Mine is the third, and it cost a copy instead of the work, which
is exactly what the mitigation was built to achieve.

It is also the second time in this session that I edited the tree while a
verification run was in flight; the first invalidated a nineteen-minute
pytest run through the manifest. The pattern is mine rather than the
tooling's, and it is recorded here instead of tidied away.

**WHAT DID NEED FIXING IS THE WORDING.** "TESTS DAMAGED TRACKED FILES … the
test is unsafe" reports as a finding something the harness cannot observe.
All it sees is that a tracked file differs from the snapshot taken before
that mutation ran; a test writing outside its `tmp_path`, an editor, and a
regeneration script in another shell are indistinguishable from there. The
heading now reads "TRACKED FILES CHANGED DURING A MUTATION", names the
mutation as the TIME rather than the CAUSE, and says outright that nothing
there can tell them apart. `test_a_file_a_mutated_build_damaged_is_restored_and_kept`
was updated with it and now asserts the hedge as well as the heading — in
that test the suite really is the culprit, and the wording still has to
leave the other possibility open.

**AND THE SAME OMISSION A THIRD TIME, WITHIN THE HOUR.** `55ab53a` was
pushed with `final_manifest.json` regenerated and `docs/corpus_allowlist.json`
not, because the last edit before committing was to this file — which is a
corpus document. `stack-verify (core)` went red on its own commit and
`tools/corpus_allowlist.py` named it exactly:

```
CORPUS ALLOWLIST DRIFT
  1 allowlisted document(s) no longer hash to what the allowlist records:
  ['docs/DEFECT_LEDGER.md']. Regenerate the allowlist in the commit that
  changes the document, so the change is reviewed rather than absorbed
```

Three times in one session: the crate after a source edit, the HDF5 chain
after a governed output, and now the allowlist after this file. Each time I
regenerated the artifact I was thinking about and stopped. The chain is
documented, ordered, and enforced by four independent checkers, and all
three were caught by them within minutes — which is the system working. What
it says about the operator is that "regenerate the derived chain" is not one
action but several, and treating it as one is a habit that costs a red run
every time. The regeneration order in `MANIFEST_BOUNDARY.md` is the list; the
fix is to run it rather than to remember it.



---

## Hosted evidence, per commit

A gate condition is satisfied **for a commit** when that commit's own hosted
run is green. It is never inherited from a parent: `de7f0e6` passed locally
and its own hosted run then failed with

```
TypeError: AF_INET address must be a pair (host, port)
```

which is what made D-2026-25 a `HOSTED_INTEGRATION_DEFECT` rather than a
finished repair. That failure is recorded as a **failed R11 closure attempt**,
not as R59: R59 is host-CPU-dependent byte divergence, and it is only
available as a classification when pytest passed *and* package-consistency
actually ran. Neither was true at `de7f0e6`.

**AND THOSE TWO CONDITIONS ARE NECESSARY WITHOUT BEING SUFFICIENT.** Both held
at `577572d` and the failure was still not R59: it was a stale canonical
artefact (D-2026-33), visible only because that runner's arithmetic matched
the canonical set, so the drift list came back one name long instead of
twenty-four. The missing condition is the obvious one once it has cost
something: **R59 is a claim about host arithmetic, so it is available only
when the diagnostic step says the host's arithmetic differs.** That step
prints the OpenBLAS kernel and numpy's SIMD set immediately before the check,
and reading it is not optional.

**AND THERE IS A CHECK STRONGER THAN ALL THREE, WHICH WAS AVAILABLE ALL
ALONG.** The three conditions are things you read off the failing run. The
decisive one is a POSITIVE CONTROL: run `package_consistency_check.py` on a
host whose arithmetic MATCHES the canonical set, and see whether the drift
list is empty. If it is, the names on the failing runner are host arithmetic
and nothing genuinely stale is hiding among them. If it is not, the names
that survive are the defect — which is exactly how D-2026-33 was found, by
accident, because one runner happened to match.

Run at `ed1f569`, in this container:

```
openblas runtime kernel: SkylakeX
numpy SIMD found: ['X86_V3', 'X86_V4']
...
RESULT: PASS (all consistency checks passed)
```

`X86_V4` is the AVX-512 generation, so this container IS the canonical
configuration. Every one of the 24 files the hosted runner called stale
regenerates byte-identically here, and `results_gate_table.csv` matches too.
So the hosted `full-suite` failure at `ed1f569` is R59 **established**, not
R59 inferred from a matching pattern — and the count of genuinely stale files
among the twenty-four is zero, measured rather than assumed.

The rule is therefore: the three conditions make R59 *available*; a green
byte gate on a canonical-arithmetic host makes it *established*. Reach for the
second whenever a host that can run it is to hand, because the first is what
let D-2026-33 sit inside the list for four commits.

### What `ed1f569`'s own run said

| job | result |
|---|---|
| `stack-verify` (core and full) | green |
| `full-suite` | the **complete pytest suite green** — including `test_mapping_registry_valid_and_complete` and `test_repository_manifest_is_in_sync`, which is D-2026-35's named evidence — then `package_consistency_check.py` red on its two byte comparisons, established above as R59 |
| `agent-substrate`, `second-interpreter`, `cross-environment-3d` | superseded by `423e51f` before reporting; not treated as passed |

The pytest half of that run is the hosted evidence D-2026-35 named, and it is
green. The row moves on that basis; the byte-gate half of the same job does
not bear on it.

| defect | evidence required | commit | state |
|---|---|---|---|
| D-2026-24, D-2026-25 (P0-R11) | agent suites, second interpreter, full pytest, network-authority mutation matrix — all on the same commit | `b2787a0` | **`CURRENTLY_CLOSED`** — all four green on that commit's own run; the mutation matrix ran rather than being skipped |
| D-2026-26 | the cross-process `read-decide-write` mutation matrix | `423e51f` | **`CURRENTLY_OPEN_FINDING`**, and for the first time for a REASON rather than a skip: step 43 ran at `423e51f` and **failed**, 10/11, survivor `X4_a_record_the_reducer_will_reject_is_written_anyway`. Every earlier run skipped this step because the job died at 26. Opened as D-2026-43 |
| D-2026-27 (P0-R12) | `agent_second_reader` mutation matrix, and the agent suites | `423e51f` | **`CURRENTLY_CLOSED`** — `agent-substrate` step 26, "mutation matrix -- the second reader", `conclusion: success` at `423e51f` (12:30:33 → 13:11:44). Steps 1–42 are all success on that run. The job as a whole is red at step 43, which is D-2026-26's evidence and a separate finding |
| D-2026-28 (P0-R13) | `identity_inventory` mutation matrix, and the inventory step | `ed1f569` | **`CURRENTLY_CLOSED`** — `agent-substrate` steps 24 ("identity inventory agrees with the code") and 25 (the inventory's own matrix) both `conclusion: success` on that commit's run. The job as a whole is red at step 26, which is a different finding's evidence and not this one's |
| D-2026-29 (P0-R14) | `agent_second_reader` mutation matrix, and the inventory step | `423e51f` | **`CURRENTLY_CLOSED`** — `agent-substrate` step 26, "mutation matrix -- the second reader", `conclusion: success` at `423e51f` (12:30:33 → 13:11:44). Steps 1–42 are all success on that run. The job as a whole is red at step 43, which is D-2026-26's evidence and a separate finding; step 24, the inventory, is success on the same run |
| D-2026-30 (P1) | `agent_checkpoint` and `agent_second_reader` mutation matrices | `423e51f` | **`CURRENTLY_CLOSED`** — `agent-substrate` step 26, "mutation matrix -- the second reader", `conclusion: success` at `423e51f` (12:30:33 → 13:11:44). Steps 1–42 are all success on that run. The job as a whole is red at step 43, which is D-2026-26's evidence and a separate finding; step 12, checkpointing, is success on the same run |
| D-2026-31 (P1) | `agent_substrate` and `agent_second_reader` mutation matrices, and the property suite | `423e51f` | **`CURRENTLY_CLOSED`** — `agent-substrate` step 26, "mutation matrix -- the second reader", `conclusion: success` at `423e51f` (12:30:33 → 13:11:44). Steps 1–42 are all success on that run. The job as a whole is red at step 43, which is D-2026-26's evidence and a separate finding; steps 9 and 10, the agent suites and the substrate matrix, are success on the same run |
| D-2026-34 (P1) | `agent_checkpoint` mutation matrix, the agent suites, and `second-interpreter` — the job that found it | `ed1f569` | **`CURRENTLY_CLOSED`** — `agent-substrate` step 12 (checkpointing) and step 9 (agent suites) `success`, `second-interpreter (3.13)` green as a job, and **no `POST-RUN BASELINE RED`**: at `9d7d3c2` that same matrix ended with it, and the fixture that produced it no longer depends on record length |
| D-2026-35 (P1) | `full-suite` — the job that found it — green on this commit's own run | `ed1f569` | **`CURRENTLY_CLOSED`** — the complete pytest suite is green on that commit's own hosted run, `test_mapping_registry_valid_and_complete` included. The same job's byte-gate step is red and is R59, established by a positive control on a canonical-arithmetic host |
| D-2026-36 (P1) | `agent-substrate` — specifically the `agent_second_reader` matrix | `423e51f` | **`CURRENTLY_CLOSED`** — `agent-substrate` step 26, "mutation matrix -- the second reader", `conclusion: success` at `423e51f` (12:30:33 → 13:11:44). Steps 1–42 are all success on that run. The job as a whole is red at step 43, which is D-2026-26's evidence and a separate finding. `ed1f569`'s run had this same step red with exactly R92, R98, R99 and R100; the step that named the finding is the step that now passes |
| D-2026-37 (P1) | `full-suite`'s pytest step — the step that found it — green on this commit's own run | `e081c38` | **`CURRENTLY_CLOSED`** — step 5, "the FULL pytest suite, not only the agent suites", `conclusion: success` at `e081c38`, on a hosted runner of the same kind that failed the timed guard at `423e51f`. The job as a whole is red on step 7, the byte gate, which is R59 and does not bear on this |
| D-2026-38 (P1) | `agent-substrate` — the `agent_second_reader` matrix, now 103 mutations | `423e51f` | **`CURRENTLY_CLOSED`** — `agent-substrate` step 26, "mutation matrix -- the second reader", `conclusion: success` at `423e51f` (12:30:33 → 13:11:44). Steps 1–42 are all success on that run. The job as a whole is red at step 43, which is D-2026-26's evidence and a separate finding, on the commit that carries all 103 |
| D-2026-39 (P1) | `full-suite`'s pytest step, which carries `tests/test_stage8_data_provenance.py` | `d425d47` | **`CURRENTLY_CLOSED`** — step 5, "the FULL pytest suite", `conclusion: success` on that commit's own run. Step 7, the byte gate, is red and is R59 |
| D-2026-40 (P1) | `full-suite`'s pytest step, and the manifest-completeness suite inside it | `d425d47` | **`CURRENTLY_CLOSED`** — step 5 `success`, and `agent-substrate` step 8 ("derived artifacts are in step with their sources") `success` on the same commit: the step that was red at `3f26b27` for this exact file |
| D-2026-41 (P1) | `full-suite`'s pytest step, and `agent-substrate`'s `stage10_authority` and `agent_substrate` matrices | this commit | pending its own hosted run; locally the forged record is refused, the projection makes one pass, and G_R1 and M51 were applied by hand and killed |
| D-2026-42 (P1) | `full-suite`'s pytest step, which carries the four hardware suites, plus `package_consistency_check.py` for the regenerated readiness artifact | `5c7002f` | **`CURRENTLY_CLOSED`** — step 5, "the FULL pytest suite", `conclusion: success` on that commit's own run, and inside step 7 the line `[PASS] multiphysics: hardware governance (read-only, no hardware data, human-only review, automatic_gate_effect=NONE, ...)` on the regenerated artifact. Step 7 as a whole is red on exactly two byte comparisons, and that runner printed `openblas runtime kernel: Haswell` / `numpy SIMD found: ['X86_V3']` — no AVX-512, which is R59's signature and not this change |
| D-2026-43 | `agent-substrate` step 43, the `agent_cross_process` matrix, which must read 11/11 | this commit | pending its own hosted run; locally X4 is killed by the new regression, the unreplayable log is reproduced, and the shadowed-definition scan is verified against a reintroduced duplicate |

### What `3d809f0`'s own run said

| job | result |
|---|---|
| `stack-verify` (core and full) | green |
| `second-interpreter (3.13)` | green |
| `cross-environment-3d` | green |
| `full-suite` | the **complete pytest suite green**, then `package_consistency_check.py` red on its two byte comparisons |
| `agent-substrate` | still running when this was written; it carries the mutation matrices and the agent suites that D-2026-27, D-2026-28 and D-2026-29 name as their evidence, so those rows stay `CURRENTLY_OPEN_FINDING` |

**The `full-suite` failure is R59, and it meets the classification rule.** §15
allows R59 only when pytest passed **and** package-consistency actually ran.
Both are true here: the suite finished at `[100%]` with six skips and no
failures, and the package check then ran to completion and reported

```
[FAIL] generated vs packaged results_gate_table.csv byte-identical
[FAIL] root canonical outputs byte-match the canonical regeneration
       24 stale root copies (first 8 shown): [...]
```

The diagnostic step immediately before it printed the reason, in advance:

```
openblas runtime kernel: Haswell
numpy SIMD found: ['X86_V3']
```

`X86_V3` is the AVX2 generation. The committed canonical outputs were
produced with the SkylakeX kernel and numpy's AVX-512 loops, and
`tools/blas_kernel_sensitivity.py` reproduces this host's answer on demand.
The step's own text says what the failure below it would mean, and it meant
that.

Two details worth not glossing. The count here is **24 root canonical
outputs**, while the sensitivity table records 23 of 63 **3D outputs** for
Haswell with AVX2-only numpy — different file sets, not conflicting numbers.
And the count is printed with its total at all, which is the repair to the
slice-width defect R59 was partly made of, visibly working.

`de7f0e6` is retained in this table's history rather than deleted: a commit
whose closure attempt failed is evidence about how the class was actually
closed, and removing it would make the repair look like it worked the first
time.

### What `ed1f569`'s `agent-substrate` job actually said, step by step

The job is red, and reading that as "no evidence" would be as wrong as
reading it as "the matrices failed". Steps **1 through 25 are
`conclusion: success`**; step 26, the second-reader matrix, is `failure`;
steps 27-57 are `skipped` because the job stopped there.

```
killed:   96/100
SURVIVED: ['R92_a_checkpoint_claim_need_not_name_a_head_hash',
           'R98_the_replay_ignores_the_foundations_of_a_canonical_record',
           'R99_the_replay_checks_only_the_immediate_foundations',
           'R100_a_cycle_reads_as_sound_in_the_replay']
all sources restored byte-identical (verified by re-hashing)
```

Those are **exactly** the four survivors D-2026-36 was opened for and fixed
at `423e51f`. So this run is the hosted confirmation of the FINDING, at the
last commit before the repair — not a new one.

And what is NOT in that output matters as much: **no `POST-RUN BASELINE
RED`.** At `9d7d3c2` the same job ended with it, naming
`test_pruning_refuses_when_nothing_verifies_against_the_log`. The fixture no
longer depends on a record-length coincidence, and the line is gone.

**WHICH ROWS THIS MOVES, AND WHICH IT DOES NOT.** The rule is per finding and
per NAMED evidence, so a job that is red overall still settles the rows whose
evidence is a step that ran green — and settles nothing for the rest:

| finding | its named evidence | step | verdict |
|---|---|---|---|
| D-2026-28 | the inventory step and the `identity_inventory` matrix | 24, 25 | both green → **closed** |
| D-2026-34 | the `agent_checkpoint` matrix, the agent suites, `second-interpreter` | 12, 9, and its own job | all green → **closed** |
| D-2026-27, D-2026-29 | the `agent_second_reader` matrix | 26 | red → stays open |
| D-2026-30, D-2026-31 | `agent_checkpoint`/`agent_substrate` **and** `agent_second_reader` | 12/10 green, 26 red | the second half is red → stays open |
| D-2026-26 | the cross-process `read-decide-write` matrix | 43 | skipped → stays open, and a skipped step is not a passed one |

### What `423e51f`'s own run said

The run the check-ins had been waiting for since D-2026-36 was opened.

| step | name | conclusion |
|---|---|---|
| 1–25 | lint, contract, matrix, derived artifacts, agent suites, and fifteen mutation matrices | success |
| **26** | **the second reader** | **success** |
| 27–42 | scope, recovery, idempotency, ownership, delegation, job graphs, separate verify, compensation, service authority, lease renewal, model check, hardware gate, convergence, fuzz harness, incremental | success |
| **43** | **read-decide-write across processes** | **failure — 10/11, survivor X4** |
| 44–57 | everything after it | skipped |

Six rows close on step 26. Step 43 is a new finding: it had been skipped in
every previous run, so this is the first time it has reported anything at all.

Five rows stay open on a run in which twenty-five steps passed. That is the
rule doing its job rather than an accident of bookkeeping: the second-reader
matrix is the evidence those five name, and it was red for a reason now
repaired but not yet re-run.


---

## The second reopening, finding by finding

The three states above apply per FINDING, not per tranche. A tranche-level
verdict is what produced "P0 complete" while D-2026-23 was open in the
production caller's own docstring, so there is no tranche-level verdict here.

`GATE_SATISFIED_AT_COMMIT` is about a commit and stays true forever.
`CURRENTLY_CLOSED` means no open finding of that class AND the evidence the
finding itself named has been re-run at the current head. Anything not yet
green on its own commit's hosted run is `CURRENTLY_OPEN_FINDING`, including
when the local evidence is complete — local evidence is evidence about this
container.

| finding | what it was | state | evidence |
|---|---|---|---|
| P0-R10 / D-2026-23 | two defect records described a repair no commit ever made | `CURRENTLY_CLOSED` | 18/18 mutations; the prose and the enforcement now agree, and a test sweeps the corpus for the claim |
| P0-R11 / D-2026-24 | the address class was checked only where it was already known | `CURRENTLY_CLOSED` | 54/54; hosted at `b2787a0` |
| P0-R11 hosted regression / D-2026-25 | a permitted class does not make an endpoint usable | `CURRENTLY_CLOSED` | 61/61; hosted at `b2787a0`, on that commit's own run |
| D-2026-26 | a reconcile decision outlived the facts it was decided on | `CURRENTLY_OPEN_FINDING` | 11/11 locally, and the hosted step finally RAN at `423e51f` and read 10/11. The survivor is a different defect (D-2026-43), not this one; this row stays open because its step has still never been green |
| P0-R12 / D-2026-27 | the second reader called the gate it exists to second-guess | `CURRENTLY_OPEN_FINDING` | 74/74 locally at `f80caa8`; hosted pending |
| P0-R13 / D-2026-28 | the coverage number measured string presence | `CURRENTLY_CLOSED` | the inventory step and its matrix are both green at `ed1f569`, on that commit's own run |
| P0-R14 / D-2026-29 | two true numbers, side by side, unreconciled | `CURRENTLY_OPEN_FINDING` | 89/89 locally at this commit; hosted pending |
| P0-R15 | historical and current claims were not distinguished | this section, and the per-commit evidence table above |
| P0-R16 | the pull request body read as a completion announcement | the body is relabelled; see the PR |
| P1 / D-2026-30 | a checkpoint pinned a snapshot and nothing anchored the pin | `CURRENTLY_OPEN_FINDING` | the claim is now a record under the hash chain; hosted evidence pending |
| P1 / D-2026-31 | two suites said opposite things about canonical authority | `CURRENTLY_OPEN_FINDING` | `canonical()` excludes withdrawn foundations, transitively, in both readers — but the SECOND reader's half had no test in the suites its own spec runs until D-2026-36, so the code was right and the evidence for it was not there; hosted evidence pending |
| P1 / D-2026-32 | the performance guard measured time while the work grew | `CURRENTLY_OPEN_FINDING` | 26 full verifications per governed run down to 11, counted by a guard rather than timed; the residual 8+3 is measured and recorded, not closed |
| P1 / D-2026-34 | "usable" was decided by the log's size, and my own fix closed the example | `CURRENTLY_CLOSED` | `describes()` reads the record the checkpoint names; both fixtures rebased on content; E21-E25 anchor drift repaired, E26/E27 added; the checkpointing matrix is green at `ed1f569` with no POST-RUN BASELINE RED |
| P1 / D-2026-35 | the fix for a stale artefact left three artefacts pinning the old digest | `CURRENTLY_CLOSED` | the HDF5 half of the documented regeneration order run; equivalence EQUIVALENT with 470 datasets; `--check` now says what it does not check; the named hosted evidence is green at `ed1f569` |
| P1 / D-2026-36 | four second-reader enforcement points had nothing behind them, and the verdict said more than it measured | `CURRENTLY_CLOSED` | six tests in a suite the spec runs; the SURVIVED wording scoped to the suites actually run; **step 26 is green at `423e51f`** — the same step that was red at `ed1f569` naming exactly R92, R98, R99 and R100 |
| P1 / D-2026-37 | a guard claiming immunity to a busy machine, failed by a busy machine | `CURRENTLY_CLOSED` | the guard counts re-hashed records instead of timing them, asserting equality where it allowed 4x; anti-vacuity partner added; R49 corrected and downgraded to 37/39; the pytest step is green at `e081c38` on a hosted runner |
| P1 / D-2026-38 | the second reader spoke a wider job language than the scheduler | `CURRENTLY_CLOSED` | `_JOB_EDGES` restated, `_JOB_SEALED` derived from it, `_JOB_INITIAL` narrowed to one state; five parity tests that fail by naming the difference; step 26 green at `423e51f`, the commit carrying all 103 |
| P1 / D-2026-39 | two verifiers reported a verdict over a scope of nothing | `CURRENTLY_CLOSED` | both refuse an empty scope and a partial one; the crate validator returns FAIL instead of a KeyError traceback; the sweep names what it did and did not examine; the pytest step is green at `d425d47` |
| P1 / D-2026-40 | a test of mine overwrote a tracked artefact, and the suite's file order hid it | `CURRENTLY_CLOSED` | `validate()` takes a report path; the guard is order-independent; the artefact is restored, and the canonical-tree step that caught it is green again at `d425d47` |
| P1 / D-2026-41 | the projection verified one read of the log and folded another | `CURRENTLY_OPEN_FINDING` | `read_verified()` returns the records it checked, in one pass; 19 passes per warm run down to 11; the guard counts passes rather than `verify()` calls; hosted evidence pending |
| P1 / D-2026-42 | the hardware gate's HUMAN authority was a self-declaration | `CURRENTLY_CLOSED` | a reviewer must resolve to a roster entry of kind HUMAN whose registration chain reaches the out-of-band bootstrap; the roster ships empty, so nothing is admitted and the reports say so; the preservation check asks the validator instead of grepping for it; mutations 11 → 21; the pytest step AND the hardware-governance assertion are green at `5c7002f` |
| D-2026-43 | a guard that was correct, documented and untested, and 38 lines the interpreter never reached | `CURRENTLY_OPEN_FINDING` | X4 killed by a regression that reproduces the unreplayable log; the anti-vacuity partner keeps the lapsed-lease requeue working; the dead copy removed with a substrate-wide AST scan behind it; the harness no longer reports an inference as a finding; hosted evidence pending |

**WHY SO MANY ROWS SAY `CURRENTLY_OPEN_FINDING` WHILE THE WORK IS DONE.** They
say it because the rule is *the gate has been re-run at the current head*, and
a green local run is not that. Writing `CURRENTLY_CLOSED` beside "hosted
pending" would be the same move as "P0 complete" — a state name doing the work
that evidence is supposed to do. The rows move when the runs are green, and
not before.

---

## Open follow-up tracked from this ledger

These are named here so they cannot be closed by silence. They are **not**
claimed complete.

0. **Nine durable actions have no independent reconstruction:**
   `agent.claim`, `agent.message`, `file.read`, `network.result`,
   `secret.access`, `secret.provision`, `task.compensation`,
   `task.reexecution`, `task.separate_verification`. This is ordinary
   repository engineering, **not a boundary** — the directive's test is
   whether work here could implement it without unavailable external
   evidence or privileges, and it could. The count is measured by
   `tools/identity_inventory.py` and printed on every CI run, so it cannot
   drift quietly in either direction.
0b. **A test damages tracked files under mutation.** The harness reported
   collateral during an `agent_netauth` run, restored it, and said the test
   is unsafe because it does not undo its own writes in a `finally`. The
   mutation name was not captured. Still open: the `solver_failclosed`
   campaign since has reported "all sources restored byte-identical" on
   every run, so whatever it was, it is not in that spec. To be identified
   during the next full campaign, which runs every spec.

   **A NEAR MISS WORTH RECORDING, BECAUSE IT LOOKED LIKE THE ANSWER.** The
   `agent_second_reader` run for D-2026-29 reported exactly this condition
   and named a mutation: `D8_a_capability_id_may_be_reissued` damaged
   `tools/independent_verify.py`. It did not. **I** edited that file while
   the matrix was running, the harness hashed it at baseline, and the
   difference surfaced under whichever mutation happened to be in flight.
   The harness then restored the file to its baseline, silently undoing the
   edit -- which is the harness being right and me being careless.

   Recorded rather than quietly dropped for two reasons: a named suspect is
   how a real investigation goes wrong, and the next reader of this item
   would otherwise find `D8` in a log and close the wrong thing. The
   follow-up stays **open**, and the discipline it argues for is the one
   that failed here: do not edit tracked sources while a mutation matrix
   holds them.

1. **The admission-rule sweep across every `_sub_*` reducer**
   (D-2026-02 sibling sweep). `_sub_job_transition` and `_sub_lease_renew`
   now restate admission; the capability, agent, memory, network, secret and
   context reducers have not been re-read against that standard.
2. ~~**Escalations have no second reader at all.**~~ **CLOSED by D-2026-19.**
   It was recorded here as a boundary and it was not one: it was ordinary
   repository engineering, which is exactly the misclassification §13 warns
   about. `reconstruct_subsystems` now replays `agent.escalation.*` as a
   tenth subsystem, from an implementation that does not import
   `AgentDirectory`. Kept visible rather than deleted so the misclassification
   stays on the record.
3. **The undeclared-write inventory is scoped to the TOOL, not to the RUN.**
   `_undeclared_writes` compares a before/after inventory of
   `spec.writable_scope`, which for both Stage-10 tools is the whole
   `verification/stage10` prefix rather than the one run's workspace. So any
   *other* principal writing anywhere under that prefix while a run is in
   flight is attributed to that run, and the run fails EVIDENCE_FAILED for
   files it never touched. Found on 2026-09-10 by running the suite in the
   foreground while a background suite was still running: the audit-CLI
   fixture's run failed with ten undeclared writes, every one of them a
   `_pytest_governed/` log file belonging to the other process. Not a
   production defect today -- the substrate is single-writer per log -- and
   not a false negative: it fails closed. But it makes the check's meaning
   "nothing changed in the shared scope" rather than "this run wrote only
   what it declared", which is a weaker statement than the surrounding prose
   claims, and it makes the suite unsafe to run concurrently with itself.
