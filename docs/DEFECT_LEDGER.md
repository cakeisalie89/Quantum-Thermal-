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

## D-2026-44 — "must cite a run id" was satisfied by a sentence saying there was no run

**CLASS** — `TRUE_DEFECT` in a validator rule, `WRONG_SPECIFICATION` in the
matrix it validates, and a `WRONG_TEST` of my own found by the harness while
repairing them.

**AFFECTED COMMIT** — since the matrix was built.

**DISCOVERED BY.** The P1 item recorded as "implementation-vs-evidence
split".

**THE RULE, AND ITS OWN STATEMENT OF PURPOSE.**

```python
# A hosted-CI claim must cite a RUN, not a mood. "green", "passing"
# and "should be fine" are all things this field has been tempted to
# say; a run id is a thing somebody can open.
if not re.search(r"\b\d{8,}\b", hosted):
```

**WHAT SATISFIED IT.** Fourteen rows, each classified
`COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT`:

```
"hosted_ci": "pending: added after run 33939090740"
```

A sentence whose MEANING is that no hosted run has ever covered the row,
passing a check about citing runs **because it names one in the act of
denying it**. The regex found `33939090740` and stopped asking.

**AND THE OTHER TWENTY-ONE.** They cite runs `33905260267` and
`33909571694` — runs #1 and #2 of the workflow, at `a3515e7`, **99 commits**
behind head. Every one of those rows has had implementation files change
since. Worse, and checkable: **33 of the 39 rows name at least one
implementation file that did not exist at the commit they cite**. R22 offers
those two runs as its evidence while naming five files — `tools.py`,
`readpath.py`, `netauth.py`, `secrets.py`, `capability.py` — that were not in
the tree when they ran. The cited run could not have tested the named
implementation.

**THE TWO AXES.** `classification` says what is BUILT. Nothing said what had
been CHECKED, and the summary printed only the first number:

```
37/39 complete, 0 blocked, 2 open
```

A reader takes that for both. Measured, it is not:

```
hosted evidence, derived by recomputing each row's implementation digest:
    0  COVERS_CURRENT_IMPLEMENTATION
   21  PREDATES_CURRENT_IMPLEMENTATION
    4  COMMIT_NOT_RECORDED
   14  NEVER_RUN
  0/39 rows have hosted evidence that covers the code they describe
```

Both lines print together now. Neither is the answer on its own.

**REPAIR, AND WHY THE VERDICT IS COMPUTED RATHER THAN STORED.** A row
records only facts about the run: which runs, at which commit, and the
digest of that row's implementation AS IT WAS then. `evidence_state()`
recomputes the digest from the working tree and derives the verdict. A row
cannot declare its own evidence current, for the same reason a review record
cannot declare its own author human (D-2026-42): a self-declared field is
not an authority. The digest is deliberately independent of git history, so
it gives the same answer in CI's shallow checkout, where there is nothing to
diff against.

`COMMIT_NOT_RECORDED` is a fourth state on purpose. Four rows cite real runs
whose commit nobody wrote down; calling them `PREDATES` would report a
measurement that was not made. "This is stale" and "I cannot tell whether
this is stale" are different, and the matrix now says which it means.

**THE RULE THAT REPLACED IT** holds prose to the one thing prose can get
wrong here: naming a run when the evidence record says there is none. Run
ceremony — the commit, the digest — is checked structurally, where a regex
cannot be talked out of it.

**AND THE HARNESS CAUGHT MY OWN TEST BEING WEAKER THAN ITS NAME.** C15
removes the `ABSENT` marker from the digest, and
`test_absence_is_part_of_the_implementation_digest` — written in this same
commit, for this exact mutation — passed over it. The path is hashed either
way, so present-vs-absent still changes the digest and the assertion never
noticed what was lost. The marker's real job is DELIMITING, and without it
path boundaries are ambiguous:

```
implementation ['ab']  and  ['a', 'b'], both absent
  without the marker -> fb8e20fc2e4c3f24...   IDENTICAL
  with the marker    -> 42c13e28... / 4f2a96d0...   distinct
```

Two different implementation lists hashing the same is a digest that cannot
say whose evidence it is. Not an equivalent mutation — a real weakness, and
17/18 was the only reason I looked. Both tests are kept: the first is true
and reads as though it covers this; the second says what the marker is for.

I had written a ledger entry about exactly this class earlier the same day.
Writing one does not confer immunity; running the mutation does.

---

## D-2026-45 — `PASS = 0` was enforced by a statement Python deletes on request

**CLASS** — `TRUE_DEFECT`, fail-OPEN, on the package's central scientific
claim.

**AFFECTED COMMIT** — since the gate writer was written.

**DISCOVERED BY.** The P2 item recorded as "production assertions under
`python -O`".

**THE DEFECT.** `_write_gate_csv` is the function that emits the canonical
gate table. It held the three claims this repository exists to make:

```python
for r in rows:
    assert r["status"] in _ALLOWED_STATES, f"illegal gate state {r['status']}"
    assert r["can_PASS_now"] == "NO"
    assert r["measured_in_this_system"] == "false"
```

`assert` is a debugging construct whose documented contract is that it MAY
VANISH. Under `python -O` it does. Reproduced:

```
=== normal python ===
refused: illegal gate state PASS
file written? False

=== python -O ===
WROTE THE FILE. Does it contain a PASS row? True
    FORGED,,,,,,,PASS,,,true,,YES,,,
```

A row claiming `status=PASS`, `can_PASS_now=YES` and
`measured_in_this_system=true` written into the canonical gate table, with
no error and exit 0.

**AND THE INTAKE BOUNDARY, THE SAME WAY.** `ingest_and_compare` refuses a
measurement file whose `schema_version` is not this one — with an `assert`,
inside the `try` whose `except` produces `REJECTED_FILE`:

```
normal:  ingestion_status: REJECTED_FILE
-O:      ingestion_status: OK
```

Both fail OPEN, which is the wrong direction twice over: the invariant is
that no PASS row exists and that foreign schemas do not enter, so losing
each check produces exactly the artifact the invariant forbids.

**TWO MORE, DIFFERENT IN KIND.** `coupling_ledger_3d._c` let an arbitrary
status string into the ledger whose whole job is a closed honesty
vocabulary. And `species_transport_3d` asserted `T is not None` before
computing a mean free path: under `-O` that becomes a `TypeError` somewhere
inside the arithmetic, which the directive is explicit about — a refusal
must be an intentional governed refusal, not an accidental runtime type
failure. The two are told apart by whoever reads the traceback and by
nothing else.

**THE SUBSTRATE WAS ALREADY CLEAN, WHICH IS WHY THIS IS P2 AND NOT WORSE.**
`qta_agent` and `tools/` carry ZERO asserts. Every authority decision in
this repository is already a real refusal. The defect is confined to the
scientific tree, and the worst of it sits in the one function that writes
the number the whole package is judged by.

**REPAIR.** Four sites converted to explicit raises. The intake's raise
stays INSIDE its own `try`, so the function still answers with a report
rather than an exception: the caller's contract is unchanged, only the
flag-dependence is gone. The remaining three asserts are self-checks on
values the same function just constructed, or on module constants, and are
enumerated by site.

**THE TEST HAS TO START A SECOND INTERPRETER.** `-O` is decided at compile
time; there is no way to un-strip an assert inside a running process.
Anything patched in-process would be testing something else and reporting it
as this. `tests/test_optimized_mode_invariants.py` runs each probe under
both builds as a subprocess, and carries an anti-vacuity case asserting the
two builds really differ (`0 True` vs `1 False`) — without it, both
parametrised cases would run the same build twice and pass while measuring
half of what they claim.

Verified against the original code: `[python -O]` fails on both invariants
while `[python]` PASSES. That asymmetry is the finding — an ordinary test
run cannot see this defect at all.

**AND A SCAN, SO THE NEXT ONE CANNOT ARRIVE QUIETLY.**
`test_no_new_assert_has_taken_over_an_enforcing_path` refuses any assert in
`qta_agent` or `tools/`, and pins the scientific tree's remaining sites by
file. A new one has to be classified by a person -- self-check or
enforcement -- rather than absorbed into a count nobody reads.

---

## D-2026-46 — six timing guards, one converted, and the argument for the rest

**CLASS** — `WRONG_TEST` (a correct property measured by an instrument that
cannot hold it), plus `ARCHITECTURAL_MISUNDERSTANDING`: a stated reason for
leaving five siblings alone, falsified by a hosted run.

**DISCOVERED BY.** `full-suite` step 5 at `bf8a2f8`, which went red on a
tree whose property was intact:

```
appending 800 records cost 37.1x appending 100; linear is about 8x and
quadratic about 64x.
assert 37.06185812898332 < 20.0
```

Measured here immediately afterwards: **8.07**, linear to three significant
figures. The runner was busy; the code was not slow.

**WHAT MAKES IT A DEFECT RATHER THAN A FLAKE.** D-2026-37 had already found
this shape, converted `test_per_append_cost_does_not_grow_with_history` from
wall time to counting re-hashes, and written down why the others could stay:

> Every other ratio guard compares two SIZES with an 8x spread, so healthy
> reads about 8 and quadratic about 64 and a busy runner cannot move a
> measurement across that gap.

A busy runner moved it to 37.1 — past the 20.0 ceiling, more than four times
the healthy value, well into the gap the argument called uncrossable. The
reasoning was careful and it was wrong, and it was wrong in the direction
that costs a red run on correct code, which is how a ceiling gets widened,
and widened again, until the guard cannot see the regression it exists for.
The file says so itself, one function above: *"The answer to a noisy
measurement is a better estimator, not a looser bound."*

**THE SWEEP CLOSED THE EXAMPLE.** One guard converted, five left timed on an
argument that had not been tested. That is the failure this ledger keeps
recording, committed by the fix for the previous instance of it.

**REPAIR — LINEARITY AS AN EQUALITY.** Counting re-hashes gives an exact
work unit, and it turns out every guard shares one invariant: if the work is
linear, going from SMALL to LARGE records costs exactly `LARGE - SMALL` more
re-hashes. Measured, identical in all five, 700 = 800 - 100:

| guard | SMALL | LARGE | difference |
|---|---|---|---|
| appending | 99 | 799 | 700 |
| `verify()` | 100 | 800 | 700 |
| `AuthorityStore.load()` | 101 | 801 | 700 |
| `reconstruct()` | 100 | 800 | 700 |
| `AuditIndex.from_log()` | 100 | 800 | 700 |

`assert_linear_in_records` asserts that difference. It needs no tolerance,
no ceiling and no estimator, and quadratic growth misses it by orders of
magnitude rather than by a factor a scheduler could supply.

**TWO MORE CONVERTED, AND ONE OF THEM WAS SKIPPING ITSELF.**
`test_incremental_verification_does_not_grow_with_the_prefix` becomes an
equality — a fixed 10-record tail costs exactly 10 re-hashes behind a
100-record prefix and behind an 800-record one — where the timed form
allowed a 3.0x window a prefix-dependent cost could sit inside.

`test_checkpoint_load_beats_a_full_replay` called `pytest.skip` when the
full load fell below the measurement floor, so on a fast machine it did not
run at all — "a hole with a green tick over it", in this file's own words
about a different guard. Counting has no floor. Measured: full replay 802
re-hashes, checkpoint load **3**.

**THE RESIDUE, NAMED — AND THE FIRST COUNT OF IT WAS WRONG.** This entry
originally said "thirteen guards count; two still time". THREE still timed.

The scan that produced the tally classified each test by which HELPER it
called — `_per_call`, `_time`, `_time_min` — and
`test_one_governed_operation_does_not_get_slower_as_the_history_grows` times
inline with `time.perf_counter()` instead. It matched no helper, fell
through both buckets, and was reported as neither. It then failed the next
hosted run at **5.88x against a 4.0 ceiling**, on a tree whose property was
intact.

That is the D-2026-47 error committed one entry earlier: a scan whose scope
silently excludes part of its subject, reporting a tally as measured when
the instrument could not see all of it. Both times the blind spot was in
what the pattern could match, and both times the number looked exactly as
authoritative as a correct one.

Rescanned by what the code DOES rather than what it calls — any
`perf_counter`, `monotonic`, `process_time` or `time()` call anywhere in the
test — the answer is 13 counted, 3 timed, and the third is now converted
too.

Its unit is worth recording, because the obvious one reads zero.
`_count_full_passes` counts `EventLog.read`, and the scheduler never calls
it: catching up incrementally is D-2026-32's own repair, so the guard for a
quadratic history path cannot be built on the counter that repair made
silent. Re-hashes are what a history re-read shows up as. Measured, the
work is IDENTICAL at both sizes:

```
prefix=  50 -> 239 re-hashes
prefix=1200 -> 239 re-hashes
difference : 0   (history grew by 1150 records)
```

So it asserts equality, like the others.

**WHAT ACTUALLY REMAINS TIMED: two.** `Scheduler.ready_queue` (a projection
query) and `EvidenceStore.get` (a digest-to-bytes lookup) hash nothing, so
there is no unit to count and no honest conversion. Both are annotated in
place as residue rather than left looking like the others. Neither has
failed yet, which is an observation and not a guarantee, and if one does the
answer is a countable unit rather than a wider bound.

**ANTI-VACUITY.** Five assertions now read `large - small == LARGE - SMALL`,
and a counter wired to something that does not grow would satisfy all five
while measuring nothing.
`test_counting_re_hashes_would_SEE_a_quadratic_append_path` builds the
original defect's shape — re-verifying the whole chain on every append — and
requires both that the count explodes and that `assert_linear_in_records`
rejects it.

**DEAD CODE REMOVED WITH IT.** `_time_min`, `_ratio`, `_time` and `REPEATS`
had no callers left. Deleting them is the same judgement applied to
`scheduler.py` in D-2026-43 an hour earlier: a helper nothing calls is a
statement nothing tests, and `_time_min`'s docstring — the "better
estimator, not a looser bound" lesson — is superseded by the stronger answer
and carried forward into the converted guards.


---

## D-2026-47 — a coverage count that stopped tracking what it counted

**CLASS** — `STALE_ANCHOR` in prose: a number that was true, describes a
thing that changed, and has no owner.

**DISCOVERED BY.** The P2 item recorded as "stale counts", by extracting
every numeric claim from the completion matrix and checking the ones that
can be checked.

**DEFECT.** R51's evidence — the row whose subject is how much mutation
coverage exists — opened with:

> 34 committed specs run in CI

Thirty-nine run. Nothing was missing: all 39 spec files exist and all 39 are
invoked, and the two sets match exactly in both directions. The number
simply stopped tracking its subject as specs were added, in the sentence
that is the row's entire claim.

That is a small error with a specific shape worth naming: a count in prose
is a claim with no owner. Every other coverage figure in this repository is
produced by the thing it describes; this one was typed, and typed numbers
decay silently while reading exactly as they did when true.

**REPAIR.** The row now says 39 and says how it came to say 34.
`test_the_spec_count_the_matrix_claims_is_the_count_that_runs` gives it an
owner: it compares the spec files on disk against the specs the workflows
invoke — **in both directions**, so a spec present but never run and a spec
invoked but absent are each caught — and then requires R51's stated number
to equal that count. Verified by restoring 34, which fails the test naming
both figures, and restoring 39, which passes.

**AND THE MEASUREMENT THAT FOUND IT WAS ITSELF WRONG FIRST.** The initial
scan for invoked specs used

```
tools/mutations/[a-z_]*\.json
```

and reported that `stage10_authority.json` is never invoked by CI — which
would have been a far more serious finding, since D-2026-41's evidence names
that matrix. It was false. The character class has no digits, so the pattern
could not match the name it was searching for, and a scan that silently
drops part of its subject reads exactly like a finding about that part.

The workflow step was there all along, on a continuation line, twelve lines
above where I stopped reading. What caught it was checking the surprising
result against the primary source rather than reporting it: step 55 is
literally named "mutation matrix -- Stage-10 write authority and retrieval
trust", and a step that does not exist does not get a name.

This is the same defect class as the finding it was hunting — a check whose
scope quietly excludes part of what it claims to cover — committed by the
instrument in the act of auditing. It is recorded because the near miss is
the useful part: the wrong answer was alarming, and alarming wrong answers
are the ones that get believed.


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
| D-2026-44 | `agent-substrate` step 7 ("completion matrix is self-consistent") and step 46, the `completion_matrix` mutation spec, now 18 | this commit | pending its own hosted run; locally 18/18 with C15 killed by the delimiter test it exposed, and the summary prints both axes |
| D-2026-45 | `full-suite`'s pytest step, which now carries `tests/test_optimized_mode_invariants.py` | this commit | pending its own hosted run; locally both invariants hold under `python -O` in a subprocess, and the three tests fail against the assert-based code |

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
| D-2026-44 | "must cite a run id" was satisfied by a sentence saying there was no run | `CURRENTLY_OPEN_FINDING` | the evidence axis is derived by recomputing each row's implementation digest, not stored; 14 rows' prose no longer names a run while denying one; the summary prints 0/39 covered beside 37/39 complete; mutations 12 → 18; hosted evidence pending |
| D-2026-45 | `PASS = 0` was enforced by a statement `python -O` deletes | `CURRENTLY_OPEN_FINDING` | four sites converted from `assert` to intentional governed refusals; the gate table and the measurement intake both failed OPEN under `-O` and now refuse identically in both builds; a subprocess suite tests both builds with an anti-vacuity case proving they differ; a scan keeps the substrate at zero asserts; hosted evidence pending |

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

0. ~~**Nine durable actions have no independent reconstruction.**~~
   **CLOSED by follow-up A (D-2026-84, D-2026-85).** The count was seven, not
   nine, from the day after this was written: D-2026-29 gave `agent.claim`
   and `task.compensation` readers and left the list alone (D-2026-84). The
   other seven -- `agent.message`, `file.read`, `network.result`,
   `secret.access`, `secret.provision`, `task.reexecution`,
   `task.separate_verification` -- now have second readers in
   `qta_agent/reconstruct.py`; `tools/identity_inventory.py` measures 38 of
   38 from the parse tree, and the CI step title says so and is checked.
   Each of the seven stays classified NOT authority-changing, and
   `tests/test_second_reader_audit_actions.py` shows a forged one moving no
   authority-bearing view while the second reader names it.
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

## D-2026-48 — the only cross-environment instrument counted files

**CLASS** — `WRONG_SPECIFICATION`: a check whose unit cannot express the
property anyone actually cares about, so its output licenses no statement
either way.

**DISCOVERED BY.** Reading the `full-suite` failure at `8c268ab`, where step
5 — the full pytest suite — passed for the first time and left the byte gate
as the only red. A red that is the only red is worth reading properly.

**DEFECT.** R59 was thoroughly investigated and never answered. Two
instruments existed: `package_consistency_check.py`, which reports "24 stale
root copies", and `tools/blas_kernel_sensitivity.py`, which attributes those
copies to the BLAS kernel and numpy's SIMD dispatch and reproduces a GitHub
runner exactly — 40 of 63 files, the same 23 by name. That attribution is
correct and this entry does not disturb it.

Both count FILES. Byte-identity emits the same output whether a residual's
last digit moved or a gate flipped from CONDITIONAL to PASS, so neither
instrument could distinguish the two, and "expected red, see R59" was as far
as the evidence could reach. The package could not say whether its own
forecasts were host-independent — not because nobody looked, but because
nothing in the repository measured in a unit that could answer.

**WHAT THE MEASUREMENT FOUND.** Reproduced locally by forcing
`OPENBLAS_CORETYPE=Haswell` with numpy's AVX-512 loops disabled, which
regenerates `results_gate_table.csv` at SHA `f6e09148410108d7` — the exact
value the hosted runner reported at `8c268ab`.

Two scopes were measured and they agree, which is why both are named here
rather than one being quoted as "the" result. Over the 3D collector's set:
62 files, 23 differing, 4871 leaves. Over the full canonical regeneration
that CI compares — the set `package_consistency_check.py` reports on: 87
files, **24 differing, which is the same 24 the hosted runner called stale
root copies**, 6215 leaves. The classification below is the first figure's;
the second adds one further zero-crossing and no decisions. In both:

- **0 of 83 gate decisions change.** Every `status`, `can_PASS_now`,
  `threshold` and `reason` is identical. But the invariance is STRUCTURAL,
  not earned: most thresholds in this package read `REQUIRES_MEASUREMENT`,
  and a gate cannot flip on a number that is not yet compared to anything.
  That reason stops holding the day hardware supplies a threshold, so it is
  recorded as a fact about today rather than a property of the model.
- **27 exact-zero → nonzero transitions.** `n_CH4_modeC_1m3` is
  `0.000000000e+00` in all 120 rows of the committed
  `gas_transport_profile.csv` and nonzero in 18 of 120 on AVX2 hardware, up
  to `1.56e-02 m^-3`. `RESIDUAL_SPECIES_MODE_D_CHECK` publishes
  `CH4=0.00e+00` and computes `CH4=4.00e-09` there. Both readings are the
  same physics — 0.016 molecules per cubic metre is some twenty orders below
  one molecule in the chamber — but only one of them is a claim of
  EXACTNESS, and the package makes it about the mode whose whole job is
  clearing methane before Mode D.
- **9 apparent sign flips in `thermal_3d_hotspots.csv` are not sign flips.**
  `T_peak_K` is identical to every digit; the coordinates permute. The peaks
  are symmetry-degenerate, and the rank order among them is not a physical
  quantity. Repaired in D-2026-49, which also corrects what this paragraph
  first said about the mechanism: it called the cause a missing stable
  tie-break, and measuring it showed there are no ties to break.
- **180 ordinary precision divergences**, largest relative difference 0.68.
  That figure looks alarming and is not: it sits on
  `energy_ledger_cumulative_3d.csv`'s `phase_residual_J`, which is
  `3.70e-23 J` against source terms of `1.26e-17 J`. The residual IS the
  accumulated rounding error, so a different accumulation order is expected
  to move it and the relative difference is 33% of the noise floor. Naming
  it by its relative difference alone would have been the error this ledger
  keeps recording: classifying by a proxy instead of by the quantity.

**HOSTED EVIDENCE.** `full-suite` at `c8ce388`
(job 103449077811), step 8. The runner reproduced the local forced-dispatch
prediction to every figure: 87 files compared, **24 differing — the same 24
its own byte gate calls stale root copies**, 6215 leaves, DECISION 0,
ZERO_CROSSING 28, SIGN_FLIP 9, PRECISION 180 with a largest relative
difference of 6.822e-01, and the same zero-crossing list by name and value.
Step 7 refused in the same job, on the same two file sets, for the reason it
always did. That pairing is the whole claim: the bytes differ and no
decision does.

**REPAIR.** `tools/cross_env_semantics.py` asks the question in the unit
that can answer it. It strips numeric tokens from both sides and compares
what is LEFT: if the residue differs, a status, boolean, unit or label
changed, and that is a changed claim whatever the numbers did. Only then are
the numbers classified, into `ZERO_CROSSING` (a published exactness the
other environment contradicts), `SIGN_FLIP`, and `PRECISION`.

It refuses on exactly one thing — a decision-bearing token that is not
identical — because that is the only class here that is unambiguous. The
others are measured and reported and never refused. A gate that goes red for
benign reasons is how R59 became background noise for months, and rebuilding
that in a new file would have been a poor trade.

The byte gate is NOT weakened, exempted or replaced, and CI is no greener
for this commit: `package_consistency_check.py` still refuses on a runner
whose dispatch differs, for the reason it always did. The new step runs with
`if: always()` precisely because the old one is expected to fail first, and
compares that runner's own regeneration — already sitting in `outputs/` by
the time the byte gate refuses — against the committed copies. Same two file
sets, two different questions: "do these differ" and "is any difference a
decision".

`check_scope` refuses a verdict drawn from an empty comparison, because "no
decision changed" is trivially true of nothing and a renamed output
directory produces exactly that. The scope numbers — 62 files, 4871 leaves —
travel in the report rather than being left implied.

Verified by positive control: flipping one token of
`thermal_3d_readiness.json` from `FORECAST_ONLY_IMPLEMENTED` to
`VALIDATED_ON_HARDWARE` — the precise claim this package exists to prevent —
is refused, while `cmp` reports it identically to a moved digit. Every
refusal test in `tests/test_cross_env_semantics.py` is paired with the
control that proves the rule names a real condition.

**ONE ERROR WORTH RECORDING, because it is this ledger's recurring one.**
The first wiring compared every file in `outputs/` against the root copy of
the same NAME, and was immediately refused on
`deep_surrogate_readiness.json`: `TRAINED_NOT_TRUSTED` at the root,
`NOT_IMPLEMENTED` in `outputs/`. That is not an environment difference at
all. `MANIFEST_BOUNDARY.md` states that `outputs/` is not a mirror of the
root, and those two files are different artifacts from different stages —
the root copy is the deep layer's authoritative record, the other a stub the
direct expdesign engine emits into its own run directory.
`package_consistency_check.py` already carried `_REGEN_EXEMPT` for exactly
this, with the reason in a comment above it.

So the scope had been chosen by name match — a proxy — rather than by
membership in the declared canonical set, which is the same substitution
recorded against a helper name in D-2026-46 and a path spelling before it.
Had it not refused on the first run it would have shipped a gate that goes
red on every host for a reason that has nothing to do with the environment:
precisely the unreadable red this entry exists to end. `REGEN_EXEMPT` now
mirrors the byte gate's set and
`test_the_exemption_matches_the_one_the_byte_gate_already_uses` reads the
literal out of `package_consistency_check.py` by AST and requires the two to
be equal, so neither can drift alone.

**MUTATION COVERAGE.** A new gate with no mutation spec is a gate nothing
has tried to defeat, so `cross_environment.json` — R59's instrument spec —
gained four, and its suites gained this tool's tests.
`E12_a_changed_word_is_classified_as_a_number` removes the residue check,
which is the whole instrument: without it a status flipping to PASS is never
a DECISION and the gate reports that nothing changed.
`E13_a_published_exact_zero_is_demoted_to_a_precision_event` folds a zero
crossing back into PRECISION at a relative difference of 1.0.
`E14_a_verdict_drawn_from_an_empty_comparison_is_accepted` and
`E15_the_exemption_swallows_every_file` both attack the scope check from
opposite ends — nothing lined up, and everything exempted. All four are
killed, 15/15 for the spec. E15 is killed by
`test_the_scope_check_accepts_a_comparison_that_did_look_at_something`,
which is the CONTROL for the refusal test next to it: the pairing that
exists to stop a rule being vacuous is also what catches an exemption
growing to cover the tree.

## D-2026-49 — a rank decided by the digits the file does not print

**CLASS** — `WRONG_SPECIFICATION`: an ordering derived from a quantity the
model does not determine to that precision, so the output carried a fact
about one accumulation order as though it were a fact about the system.

**DISCOVERED BY.** D-2026-48's semantic comparison, as the class it could
not classify: nine divergences in `thermal_3d_hotspots.csv` that the tool
called `SIGN_FLIP` and that are not sign flips. `T_peak_K` was identical to
every printed digit on both hosts; the coordinates permuted.

**DEFECT.** `hotspot_rows` ranked cells by

```python
order = np.argsort(Tpk)[::-1][:top_n]
```

A symmetric beam heats four cells to the same peak by construction. The
model does not compute them to be equal: measured on the committed
configuration, the top quartet is

```
flat=528  0x1.b75d6826d70f5p+3
flat=540  0x1.b75d6826d70c5p+3
flat=648  0x1.b75d6826d70bdp+3
flat=660  0x1.b75d6826d708cp+3
```

— four distinct float64 values, agreeing to about 1e-14 relative and
printing identically at the `.9e` the file reports. Each cell accumulates
its reduction in a different order, so the ranking among them WAS the
rounding error. On a host whose BLAS kernel accumulates differently the
order changed while every reported temperature stayed the same, which is
what the byte gate had been reporting as three stale files without being
able to say that much.

**The first diagnosis in D-2026-48 was wrong and is corrected there.** It
called this a ranked output with no stable tie-break. There are no ties:
`np.argsort` is deterministic, no two peaks are exactly equal, and neither a
stable sort nor a tie-break on the cell index would have changed one row.
Checking it cost one query — print the candidates as hex — and the plausible
story would otherwise have produced a fix that changed nothing and a ledger
entry claiming it had.

**REPAIR.** The key stops carrying digits the model does not determine.
`PEAK_FMT` is now one constant used both to serialise `T_peak_K` and to
build the sort key, so a rank can never be decided by a digit the file does
not print; cells whose peak is identical as reported are ordered by cell
index, which is a fact about the grid.

```python
rank_key = np.array([float(f"{v:{PEAK_FMT}}") for v in Tpk])
order = np.lexsort((np.arange(Tpk.size), -rank_key))[:top_n]
```

Verified by running the solver under the committed dispatch and under
`OPENBLAS_CORETYPE=Haswell` with numpy's AVX-512 loops disabled: the ten
ranked rows are now the same cells in the same order on both, where before
the coordinates permuted. Measured end to end by the instrument that found
it, over the same 87-file comparison: **SIGN_FLIP falls from 9 to 0**, and
PRECISION from 180 to 179 with its largest relative difference dropping
6.822e-01 -> 3.279e-01, because that outlier WAS a permuted coordinate
rather than a quantity. What is left at 3.279e-01 is the energy-ledger
residual, which is the rounding floor and belongs there. The table also now reads as what it is — each
peak's four symmetric copies consecutive, in coordinate order — instead of
interleaved by noise.

**WHAT THIS DOES NOT FIX, stated because the same run shows it.** Across
those two dispatches the ranked rows still differ, now in the reported value
rather than the ordering: `1.373015220e+01` against `1.373015221e+01`, the
tenth significant digit. That difference was always present and the
reshuffling hid it, so the repair makes the file MORE divergent to `cmp` and
more honest to read. It also leaves a residual: if a peak's printed value
itself differs between hosts, two cells can still rank differently. The rank
now depends only on what the file shows, which is the property worth having;
it is not host-independence, and nothing here claims it is.

The byte gate is no greener for this either. The 27 zero-crossings and 180
precision divergences are untouched, so `package_consistency_check.py` still
refuses on a foreign runner exactly as before.

## D-2026-50 — a crashed log and a corrupt log got the same answer

**CLASS** — `TRUE_DEFECT` in the durability claim, surfaced by a test that
was asserting the outcome of a race.

**DISCOVERED BY.** `second-interpreter (3.13)` going red at `34a9d68` on
`test_a_worker_process_killed_mid_execution_leaves_a_recoverable_log`. The
commit it failed on changed a 3D solver's hotspot ranking and touched
nothing in `qta_agent/`, so the first question was whether it was this
change's. It is not — and it is not somebody else's either: `qta_agent/`
does not exist on the base branch, so the substrate and this test are both
this PR's work. Ownership established by checking, not assumed either way.

**DEFECT.** `read(strict=True)` raised `MalformedEvent` on any unparseable
line, and `read_verified` turned that into

```python
return (VerifyReport(False, 0, -1, ZERO_DIGEST, problems + [str(exc)]), [])
```

A SIGKILL between two appends leaves a clean log; a SIGKILL *inside* an
append leaves a half-written final line. The second is the ordinary crash
signature — the reader's own non-strict path calls it "the expected crash
signature" in a comment — and on that path `verify()` answered `ok=False`
with **`count=0`**: every complete record in the log discarded because the
last one was torn. A governed substrate whose durability story is "rebuild
from the log" could not rebuild anything from the exact failure it exists to
survive, and could not tell that log apart from a corrupt one.

The strict message said "if this is the final line the log was truncated
mid-append" — it posed the question and then answered it by throwing the log
away. The rest of the file was one read away the whole time.

**Why it stayed hidden.** The test kills a real process in a tight append
loop and asserts `report.ok`. Which landing it gets is the scheduler's
choice, and it had been getting the clean one; 15 consecutive local runs
still do. A hosted 3.13 runner landed mid-write. So the test was not flaky
in the sense that licenses a re-run — it was **asserting the outcome of a
race**, and the outcome it lost is the one the test is named for.

**REPAIR.** The reader now answers the question it was posing. On an
unparseable line in strict mode it reads the rest of the file: if anything
complete follows, that is damage and `MalformedEvent` is raised as before;
if nothing follows, it raises `TruncatedTail`, which carries the records
that WERE complete. `read_verified` catches that case, keeps the records,
checks the chain over them as usual, and sets `truncated_tail` on the
report.

**`ok` IS STILL FALSE, and the first version of this repair got that
wrong.** Making a torn tail verify cleanly was the obvious way to turn the
red test green, and it is a defect. Two existing tests said so within one
run: `test_a_partial_trailing_line_is_reported_as_truncation`, which pins
the refusal deliberately, and — the one that matters —
`test_the_log_refuses_to_grow_onto_a_partial_line`, whose docstring had
already written down the consequence:

> The next append is where a partial line either gets noticed or gets
> buried under a valid record that makes the file parse again.

Had `verify()` returned ok, the next `append()` would have proceeded onto
the partial line and the record after it would have made the file parse
again, burying the torn bytes where nothing could find them afterwards.
Chasing a green check would have traded a recoverable crash for silent
corruption. The tests caught it because they were written to state the
consequence rather than the behaviour.

So the refusal stands and only the loss was repaired: `truncated_tail` says
which kind of refusal it is, and `count` and the returned events are the
complete records before the tear. Whether records were LOST remains the
witness's question: `_check_witness` still reports `TRUNCATED` when the
witness records a seq the log no longer reaches. Verified directly — a log
cut from 5 records to 3 with a torn tail returns `ok=False` with
`TRUNCATED: witness records seq 4 but the log ends at 2`, and now also
reports the 3 that survived. Same refusal, strictly more information.

The landings are now tested directly instead of raced for: a torn final
append is refused AND yields all five records, readable and in order; a
clean log carries no problems and `truncated_tail=False` (the control,
without which the flag could be hardwired); an unparseable record with
complete records after it stays refused as damage with `count=0`, because
what follows damage cannot be trusted to be the original chain. The fourth
is the one that must not be softened by the others — records actually
missing are still `TRUNCATED`. Verified by reverting the distinction: two
of the four fail, including that one, and both controls pass.

The SIGKILL test now asserts the property that holds on BOTH landings
rather than on the lucky one: the complete records are there either way,
and if the tail is torn the log is still refused. Asserting `ok` was what
made it a race in the first place, and tolerating the tear to keep that
assertion would have been fixing the thermometer.

**AND THE LOOK-AHEAD ITSELF WAS A RACE.** The first implementation
iterated the open handle and then asked that same handle what remained:

```python
if fh.read().strip():        # two questions, two moments
```

Whether a torn line is the LAST line is a comparison, and both halves have
to come from the same bytes. Under concurrency they did not. Five other
processes append to a shared log, so a line that is final when it is parsed
can have a complete record after it a microsecond later, and the reader then
calls the ordinary crash signature "damage" and raises.
`test_a_long_mixed_campaign_never_leaves_an_unreplayable_log` — six workers,
250 rounds — did exactly that on a hosted runner, inside a worker rather
than at the final verification, while passing eight times out of eight here.

Worth being exact about blame: the OLD code raised on any unparseable line
whatever followed it, so that campaign would have failed identically before
this repair. The look-ahead did not create the failure; it made a decision
that needed a consistent view of the file and took two views instead.

`read()` now takes one snapshot — `read_bytes()` once, then classify against
those lines — which removes the second moment entirely.
`test_the_reader_classifies_against_a_single_snapshot_of_the_file` counts
the reads rather than racing for the symptom, and fails if a second look is
added back.

**MUTATION COVERAGE.** Two anchors went stale on this edit and were
re-anchored to the same intent, not deleted: `M51` (the `except
EventLogError` return, which the new clause separated from its `try`) and
`F9` (the strict branch, now inverted). Two mutations were added for the
new logic. `M52_a_torn_tail_stops_being_a_refusal` demotes the problem to a
note — the mistake above, now a permanent tripwire — and is killed.

`R53_damage_after_the_tear_is_read_as_a_torn_tail` makes the reader skip
the look-ahead, so corruption mid-log would be recovered from rather than
refused. It first SURVIVED, and the reason is the one the harness warns
about in its own docstring: a mis-scoped spec and a missing test look
identical from here. The killing test exists — it just lives in
`test_agent_crash_recovery.py`, which `agent_substrate.json` does not list.
Recording it as an unprotected check would have been false; so would
widening that spec's suites to cover a crash-recovery concern. It moved to
`agent_recovery.json`, whose suites already include that file, and is
killed there.

## D-2026-51 — a checkpoint refused the ordinary state of a live log

**CLASS** — `WRONG_SPECIFICATION` at a composition: two components each
correct alone, wrong together. Recorded OPEN and undiagnosed first; this
entry replaces that with the diagnosis and the repair.

**HOW IT WAS SEEN.** `tests/test_agent_checkpoint.py::
test_appending_while_a_checkpoint_is_taken_leaves_both_consistent` failed
once locally during D-2026-50 and could not be reproduced — 1 failure
against 35 passes, including 24 under six parallel streams. It was written
down as neither a flake nor a defect, because both would have been guesses,
and what would close it was named: the traceback.

**WHAT CLOSED IT.** `agent-substrate` at `2514a20` — a commit that changed
only the ledger, the allowlist and the manifest — went red at step 12, the
checkpointing mutation matrix, which had been green at `bf2f902`. None of
those files can affect that matrix, so the failure was nondeterministic by
elimination before anything was read. The harness said the rest itself:

> killed: 34/34
> all sources restored byte-identical (verified by re-hashing)
> POST-RUN BASELINE RED -- the matrix left this tree in a state the suite
> rejects: ['test_appending_while_a_checkpoint_is_taken_leaves_both_consistent']
> ...but re-running those tests alone on the SAME restored tree passes.

Not mutation residue, then: sources verified identical by re-hashing. That
narrowed it to the harness's own disjunction — nondeterministic, or
dependent on state the rest of the suite sets up — and named the context to
reproduce in. Running the spec's five suites together locally reproduced it
in **1 run of 8**, with the traceback kept this time:

```
checkpoint: ChainBroken('refusing to checkpoint a log that does not verify
  -- a checkpoint past a break makes the break invisible to every later
  verification: ... partial final record (JSONDecodeError); the log was
  truncated mid-append and the 10 complete record(s) before it are intact')
```

**DEFECT.** `checkpoint.create` verified the log and refused unless
`report.ok`. The test's appender thread appends continuously, so a reader
catches a write in flight: `verify()` sees a torn final line, classifies it
correctly as a partial append, records it as a problem — which is right, and
D-2026-50 explains why it must stay a problem — and `create` refuses.

Both halves are behaving exactly as designed. The composition is the defect:
**a live log is torn constantly**, so a checkpoint path that refuses on a
torn tail refuses at random under concurrent load. In a deployment it would
fail intermittently for the entire life of the system, and the failure would
look like this one: unreproducible, and blamed on the test.

The refusal's own stated reason shows the boundary. It protects against a
checkpoint recorded PAST a break, which would hide it from later
verification. A partial final append is not past anything: `head_seq` is the
last COMPLETE record, so the checkpoint names a position strictly BEFORE the
torn bytes and cannot conceal them. The refusal was right about damage and
wrong about the ordinary state of a log being written to.

**REPAIR.** `VerifyReport.torn_tail_only()` — true when the torn tail is the
single entry in `problems`. It can be that simple because the torn-tail
message is appended before the chain walk and before the witness check, so
anything else objecting adds a second entry. `create` now refuses unless the
report is ok OR the only complaint is a torn tail, and then checkpoints the
verified prefix that D-2026-50's repair made available — before that change
`verify()` returned `count=0` and there was no prefix to name.

**IT IS NOT A SOFTER `ok`, AND THE TESTS SAY SO.** A record removed from the
middle plus a torn tail still refuses; a witness recording a seq the log no
longer reaches still refuses; a clean log reports no torn tail at all, which
is the control without which `torn_tail_only()` could return True always and
the other three would still pass. The checkpoint's POSITION is asserted, not
merely the absence of an exception: `cp.seq == 4` on a five-record log whose
sixth append is half-written.

**VERIFIED.** The five suites that reproduced it 1-in-8 now run **16 of 16
clean**. The checkpointing matrix is 36/36 with `C1` re-anchored and two
mutations added for the new condition:
`C34_the_torn_tail_tolerance_swallows_every_failure` widens it to accept any
failing log, and `C35_torn_tail_only_stops_checking_for_other_problems` drops
the "nothing else objected" half. Both are killed, C35 by the narrowness
test written for it.

**HOSTED EVIDENCE.** `agent-substrate` at `d6ea525`, job 103514015384 in
run 34679021715, **step 12 green, 07:12:01-07:13:55Z**. That is the same
step, in the same job of the same workflow, that went RED at `2514a20` on
this defect — so the repair is confirmed by the instrument that found it,
on the hardware that found it, rather than by the sandbox where it was
diagnosed. The paragraph above reports local verification; this is the
other half of it.

**THE METHOD NOTE FROM THE OPEN VERSION STANDS.** That entry could not be
closed because the suite had been run as `pytest -q 2>&1 | tail -6` and the
traceback scrolled past the six lines kept. What closed it was a hosted
harness that kept its own evidence and said precisely what it did and did
not know. A run cheap enough to repeat is not evidence cheap enough to throw
away.

## D-2026-52 — the linter had been reporting it the whole time

**CLASS** — `TRUE_DEFECT` (176 lines of dead code) sitting behind a
`WRONG_SPECIFICATION` in the checking apparatus: a rule enforced over a
named list of paths rather than over the property it protects.

**DISCOVERED BY.** The P2 item recorded as "duplicate residue", by running
D-2026-43's shadowed-definition scan over the whole repository instead of
over `qta_agent/`, which is all it was ever pointed at.

**DEFECT.** `qta_multiphysics/deep_expdesign/runner.py` defined
`run_deep_expdesign_full` **twice**, at lines 120 and 298. Python keeps the
last, so the first was dead.

Worse than the scheduler case that started this class. There the two copies
were byte-identical; here they had **diverged** — 176 lines against 213,
`Optional[DeepConfig]` against `DeepConfig | None`, and materially different
bodies: the live one records readiness through `readiness_record` and
`ReadinessState`, fits OOD against a validation context as well as the
training set, and imports `transforms` and `eig_surrogate`. The dead one did
none of that. Someone had maintained an earlier draft in place and it stayed
there, 176 lines that read as the implementation and were not.

`_trust_decision` existed only to serve the dead copy and died with it.
`_dependency_ok` did NOT: it is also called from `run_deep_expdesign` at
line 58, which a name-set comparison of the two `_full` bodies missed and a
grep caught. Deleting it would have broken a live function to tidy up a dead
one.

**THE PART THAT MATTERS.** Ruff had been reporting this for as long as the
second copy existed:

```
F811 Redefinition of unused `run_deep_expdesign_full` from line 120
```

The tool that finds this defect was installed, was correct, and was running.
CI's lint step names its paths —

```
ruff check qta_agent qta_multiphysics/stack tests/test_agent_*.py ...
```

— and `qta_multiphysics/deep_expdesign/` is not among them, so nothing ever
read the finding. The bespoke AST scan written after D-2026-43 is a narrower
re-implementation of F811 pointed at one package; it guarded the substrate
faithfully while the same defect sat in the scientific tree.

That is the defect class, and it is not about duplicate functions: **a rule
scoped to a list of paths protects those paths, not the property.** The list
is a proxy for "the code that matters", and proxies drift.

**REPAIR.** The dead definition and its orphaned helper are gone — 186
lines. Proven a no-op rather than assumed: the live function's source
sha256, its signature and the package's `__all__` are unchanged, and the
only differences in the module surface are its first line number (298 ->
112) and `_trust_decision` leaving the namespace.

`ruff check --select F811 .` now runs over the WHOLE tree, in CI and as a
test. One rule everywhere, because the rest of ruff cannot be turned on
here: the legacy scientific tree carries some 1400 findings, overwhelmingly
E501, and pretending otherwise would produce a gate nobody can keep green.
F811 is exactly "a name defined twice, the first is dead, and nothing says
so", and the tree is now clean of it.

Two further F811s surfaced in `deep_expdesign/likelihood_model.py` and are
NOT the same defect: a function-local `from .simulator_adapter import
_temp_factor, _coverage_factor, _N_OMEGA` re-importing two names already
bound at module level, shadowing them with the same objects. Redundant, not
dead — classified as such rather than counted as a second find. Only
`_N_OMEGA` is unavailable above, so the local import now asks for that
alone, and the sweep is clean.

**AND THE FIRST REPAIR COMMITTED THE SAME DEFECT.** The test as first
written shelled out to `ruff --select F811`. It went red on
`second-interpreter (3.13)` within the hour:

```
FileNotFoundError: [Errno 2] No such file or directory: 'ruff'
```

That job builds a bare environment — numpy and scipy, nothing else — so the
tool was absent and the rule simply stopped being enforced there. The
obvious fix, skipping when `ruff` is missing, would have been this entry's
own defect in different clothes: a check whose scope follows the environment
instead of the property, silently covering less than it claims.

So the sweep is stdlib-only, `ast` over the tree, and runs on every
interpreter the suite runs on. Verified by running it with `ruff`
unreachable on `PATH`, which is the failing condition reproduced rather than
argued about. CI still runs `ruff check --select F811 .` as a separate step:
two independent implementations, neither depending on the other, one of them
broader and one of them always present.

**AND THE SECOND REPAIR SCOPED BY A NAME LIST, WHICH IS THE THIRD TIME.**
The stdlib rewrite skipped directories by name — `.venv`, `.git`, `attic`,
`outputs`. The Python 3.13 job builds its environment at **`.venv-alt`**,
which is on no list and is not in `.gitignore` either, so the sweep walked
scipy's `site-packages` and reported things like

```
scipy/stats/_multivariate.py: random_state at lines 267 and 281
```

Two defects at once, both mine. The scope was a name list — this entry's own
subject, written a third time. And the rule was wrong: those are
`@property`/`@setter` pairs, ordinary Python that rebinds a name on purpose,
which ruff's F811 knows about and a naive AST walk does not. It passed here
because this sandbox's environment happens to be called `.venv`.

**SCOPE IS NOW ASKED OF GIT.** `git ls-files '*.py'` is not a better list;
it is the definition of what this repository ships, and it answers correctly
for every environment, cache and quarantine directory that will ever exist,
whatever it is called. The CI step resolves its scope the same way, so the
two implementations agree about WHAT is checked and differ only in HOW —
`ruff check .` would have walked `.venv-alt` for exactly the same reason.
The tracked tree is 273 files and clean.

**AND THE RULE IS DECORATOR-AWARE**, before it needs to be. No tracked file
currently redefines a name under a decorator, so this fixes nothing today;
it is written now because the first `@property` setter someone adds would
make the gate cry wolf, and a gate that cries wolf is a gate that gets
switched off. `test_a_decorated_redefinition_is_not_a_shadowed_definition`
plants a property/setter pair and requires silence.

`.venv-alt/` is also added to `.gitignore`, independently of all this: an
untracked and unignored directory would trip `generate_manifest.py --check`,
which refuses exactly that.

**ANTI-VACUITY.** A sweep that quietly stopped covering the tree it was
added for would pass forever.
`test_the_F811_sweep_is_actually_looking_at_the_scientific_tree` plants a
shadowed definition inside `qta_multiphysics/` and requires the same
invocation to report it, then removes it — the scope is demonstrated rather
than trusted to a path list, which is the failure this entry is about.

The probe is staged with `git add -N` so that `git ls-files` reports it:
planting an untracked file would prove nothing about the path the sweep
actually takes.
`test_the_sweep_is_not_reporting_on_an_empty_file_list` guards the other
way, because a `git ls-files` returning nothing — no git, wrong directory, a
pathspec typo — would report "nothing is shadowed" forever.

The other half is checked too:
`test_the_sweep_does_not_police_an_installed_environment` plants the probe
at `.venv-alt/lib/site-packages/`, the exact path that caused the failure,
and requires silence — without `.venv-alt` appearing in any list anywhere. A rule that
policed code this repository did not write and cannot fix would be
unkeepable, and would be turned off — which is how a rule stops protecting
anything.

## P2 DISPOSITION — the locked release signer is not mine to lock

**NOT A DEFECT.** Recorded so the P2 list is accounted for rather than left
looking open, and so the reason is on the record rather than in a summary.

**WHAT WAS ASKED.** "Locked release signer" — the last open P2 item.

**WHAT IS THERE.** `QTA_stage9_release_verification/release_trust_policy.json`
is `bootstrap_state: UNINITIALIZED` with every trust-bearing leaf carrying
`PENDING:` — `authorized_ref`, `oidc_issuer`, `signer_identity`,
`pinned_revision`, `reviewed_payload_sha256`, and the one entry of
`trusted_builders`. Six unresolved leaves, zero wildcards. The release
workflow signs keylessly through Sigstore and then verifies against those
pins, failing closed on a missing signature, on a pin still PENDING, and on
a wildcard anywhere.

**WHY IT STAYS UNLOCKED.** The policy says so itself:

> Resolution is an act of authorization performed by a human in a reviewed
> commit; no automated step may perform it.

Writing an exact `signer_identity` is a statement about who may sign
releases for this repository. Deriving it mechanically from the owner, repo
and workflow path would produce a plausible string and would still be an
authorization nobody granted — the same shape as the hardware roster's
"this is a DECLARATION, NOT AN AUTHENTICATION", and outside what this work
is permitted to do. It stays PENDING, and the release path stays
fail-closed, until the owner resolves it in a reviewed commit.

**WHAT WAS ACTUALLY CHECKED, because "it fails closed" is a claim.** The
policy's note asserts the PENDING rule is "enforced structurally over every
leaf". After D-2026-52 — a rule that protected a list of paths rather than a
property — that claim was probed rather than read:

| probe | caught |
|---|---|
| a NEW top-level field carrying PENDING | yes |
| PENDING nested two levels inside a new object | yes |
| PENDING appended as a second `trusted_builders` entry | yes |
| a nested object *named* `note` | yes — the exemption is the exact path `$.note` |
| a field merely containing the substring `note` (`footnote`) | yes |
| `" pending "` lower-case and padded, and `"PeNdInG"` | yes |
| a wildcard inside `note` | yes — wildcards have no exemption at all |
| a `?` wildcard in a new field | yes |

`_leaves()` walks the document recursively and the one exemption is a
frozenset of exact JSON paths, not a weakened matcher. The asymmetry is
deliberate and correct: `note` is exempt from the PENDING scan because it
must discuss the rule to state it, and is NOT exempt from the wildcard scan.

**NOTHING WAS ADDED.** `tests/test_release_trust_enforcement.py` already
covers all of it — 68 tests, including `test_pending_anywhere_fails`,
`test_pending_inside_trusted_builders_fails`, a parametrized
`test_pending_case_and_whitespace_tricks_fail`,
`test_note_field_may_discuss_pending_without_tripping_the_scan`,
`test_rejects_wildcard_in_any_leaf`, and `test_unknown_field_rejected`,
which refuses an unrecognised field outright and is stricter than the probe
assumed. 129 tests across the three release files pass. Writing duplicates
of tests that already exist would have made the suite longer and the
guarantee no stronger.

## HOSTED EVIDENCE — the first complete `agent-substrate` run

Not a defect. A record, because for weeks the honest answer to "does the
substrate's own verification actually pass end to end on hosted hardware"
was that nobody knew: the job had never finished.

**THE RUN.** `agent-substrate`, job 103481104861 in run 34667096079, head
`bf2f902`, 02:14:19Z -> 05:27:15Z on 2026-09-12. **All 57 steps green,
conclusion success, 3h 13m.** Every previous run died before the end —
step 43 on several, step 9 on the one before this.

Three things that had never been observed before are now observed:

- **Step 43** (`read-decide-write across processes`) is green. It is where
  run after run stopped, so everything past it was unmeasured rather than
  passing.
- **Steps 44-57**, all green and all seen for the first time: durability
  under a failing filesystem (44), external secret providers (45), the
  completion matrix validator (46), the second governed workflow (47), the
  cross-environment measurement (48), corpus membership (49), the allowlist
  describing the committed corpus (50), long horizon at elevated scale (51),
  parser and trust-boundary fuzzing (52), the governed production path
  actually running (53), the read-only auditor answering for that run (54),
  Stage-10 write authority and retrieval trust (55).
- **Steps 56 and 57**, read from the log rather than inferred from the
  conclusion: `working tree clean` and `manifest in sync (565 files; 2
  detached by policy)`. Mutation testing restored every source and the
  substrate did not touch the canonical tree.

**WHAT A GREEN MUTATION STEP MEANS HERE, checked rather than assumed.**
`tools/mutation_matrix.py` ends with

```python
return 0 if not (survived or anchors or timeouts or drifted or collateral ...)
```

so a step passing rules out survivors AND stale anchors AND timeouts AND
source drift AND collateral damage, not merely "the command ran". That is
what makes the step numbers below evidence instead of decoration.

**WHAT IT COVERS.** Stated as the steps that exercise each item, which is
checkable, rather than as a claim that one step proves one defect:

| Defect | Steps that exercise it, all green |
|---|---|
| D-2026-43 (a record the reducer rejects is written anyway) | 9, 18, 43 |
| D-2026-44 (completion-matrix evidence axis) | 9, 46 |
| D-2026-45 (`python -O` deletes the enforcement) | 9, 39 |
| D-2026-46 (timing guards converted to counting) | 9 |
| D-2026-47 (a coverage count with no owner) | 9, 46 |
| D-2026-49 (a rank decided by digits the file omits) | 9 |
| D-2026-50 (a crashed log and a corrupt log) | 9, 28, 43, 44 |
| D-2026-48's four new mutations (E12-E15) | 48 |

Step 48 green is the one worth naming twice: the mutations added for
`tools/cross_env_semantics.py` — strip the residue check, demote a zero
crossing, accept an empty comparison, exempt every file — were all killed on
hosted hardware, not only in the sandbox that wrote them.

**AND THE MEASUREMENT THAT EXPLAINS D-2026-50's LAST FAILURE.** At `715dabf`
the `full-suite` job's step 5, which runs the FULL pytest suite, was green
for fifteen minutes while `agent-substrate`'s step 9 ran the same agent
tests on a different runner, at the same commit, in the same workflow, and
went red on the six-worker campaign. Same bytes, same tests, two concurrent
hosted runners, one red and one green.

That is as direct as evidence gets that the look-ahead failure was
timing-dependent rather than deterministic, which is exactly what the
single-snapshot diagnosis predicts and what a genuine logic error would
not. At `bf2f902` step 9 is green — so the repair is confirmed on the
hardware that found the defect, rather than assumed from a sandbox that
passed eight times out of eight before the defect was known.

**AND A SECOND COMPLETE RUN.** `agent-substrate` at `d6ea525`, job
103514015384 in run 34679021715, 06:46:58Z -> 10:56:34Z: **all 57 steps
green, 4h 09m**. No step regressed against the run above, where all 57 were
also green.

Two complete runs matter more than twice one. A single green run of a job
that had never finished is compatible with having been lucky; two, at
different commits, on different runners, with the second 56 minutes slower
than the first, are not. Steps 43-57 are repeatable rather than observed
once. The spread in duration — 3h13m against 4h09m for the same 57 steps —
is also worth having on record, because every check-in scheduled against
this job so far has mistimed it, twice badly enough to read nothing.

**WHAT THIS DOES NOT SETTLE.** `full-suite` step 7 is still red at
`bf2f902`, for the reason it has always been red: `package_consistency_
check.py`'s byte gate refuses on a runner whose CPU dispatch differs from
the committed outputs' (R59, D-2026-48). Step 8 alongside it reports 0
decision changes over 24 differing files. Nothing here makes the package
byte-reproducible across hosts and nothing here claims to. D-2026-51 also
remains OPEN: no log in this run carries the traceback that would close it.

---

## D-2026-53 — the file states the number and not what the method could see

**CLASS** — `TRUE_DEFECT` in the scientific outputs: a serialised value that
claims more resolution than the method that produced it, in a column whose
name makes it a contamination claim.

**DISCOVERED BY.** The claim audit, following the exact-zero crossings that
the D-2026-48 semantic comparator had reported and that were deliberately
left open at the time as "a claims-audit decision".

**THE MEASUREMENT.** `gas_transport_profile.csv` states

```
n_CH4_modeC_1m3 = 0.000000000e+00     x 120 cells
```

and `gas_transport_metrics.csv` states `residual_mode_D_density_m3 = 0.0`
for CH4. That column is the residual methane density at Mode D entry — the
number a reviewer would quote for "how much process gas is left when the
10 mK sensing mode starts".

The Mode-C solve did not produce zero. Instrumented, its final methane
profile is **negative in all 120 cells**, between `-1.888e+01` and
`-1.847e-10`. `solve_gas_transport_1d` runs with `atol=1e3` per cubic metre,
so every one of those values is eight to eleven orders of magnitude inside
the integrator's own absolute tolerance: noise. `np.clip(so.y, 0.0, None)`
maps the lot to exactly `0.0`, and `write_profile_csv`'s `%.9e` states it to
ten significant figures.

On a runner whose BLAS dispatch differs the same noise lands positive, the
clip leaves it alone, and the file states `1.6e-02` instead. Same model, same
commit, two machines, two different claims about residual contamination —
which is how this surfaced, as a ZERO_CROSSING in the cross-environment
comparator.

H2 is the same defect without the clip: 5 of its 120 cells sit below the same
floor and are stated to ten figures.

**THE DEFECT IS NOT THE ZERO.** He3 and He4 also read `0.000000000e+00` in
that file, and their zero is exact: helium has no inlet source and no initial
content in Mode C, so the solution is identically zero in floating point and
the tolerance never enters. Three different statements — absent by design,
present but unresolved, resolved and small — were written identically,
because the file carried values and not the resolution of the method. The
tolerance is not in the file, and no reader can reconstruct it.

**WHY THE OBVIOUS REPAIRS ARE WRONG.**

* *Round to the floor.* Maps `1.6e-02` to `0.0` and calls the divergence
  resolved. It conceals the finding instead of reporting it, and the next
  sub-tolerance value in the next output is equally unmarked.
* *Tighten `atol`.* Moves the floor; does not declare it. The class survives
  at the new floor, one order of magnitude down.
* *Drop the clip.* Serialises negative densities. A worse claim.

**REPAIR.** An output states the resolution of the method that produced it.

`qta_multiphysics/numerics.py` gains `resolution_class` — in the numerics
layer every solver already depends on, for the reason `require_converged`'s
docstring gives about rules that live inside one of the things they govern.
It classifies a **raw** value, before any clip, into one of four:

| class | meaning |
|---|---|
| `EXACT_ZERO` | the model gives zero: no source, no initial content |
| `RESOLVED` | `abs(raw) >= floor`, inside the physical range |
| `BELOW_RESOLUTION` | `abs(raw) < floor`: not distinguishable from zero |
| `OUT_OF_RANGE` | outside the range by more than the floor — a solver problem, not a precision footnote |

`trivially_zero` is **not** "the value is small". It is the solver's record
that no source and no initial content existed, computed where both halves of
the reason are in hand. Deciding it by magnitude would file helium — absent
by design — as merely unresolved, which is this project's recurring error
written one more time: classifying by a proxy for the property instead of by
the property.

The gas and coverage solves now carry their raw solution, their floor and
their trivially-zero set; `gas_transport_profile.csv`,
`surface_coverage_profile.csv` and both metrics files state the class beside
every value, with the floor and the below-resolution cell count.
`package_consistency_check.py` **re-derives** the classification from the
value and the declared floor rather than trusting the column, and refuses a
run in which it compared nothing.

**THE DECISION THAT RESTED ON IT.** `coupled_mode_solver.run_coupled`
computed Mode-D readiness with

```python
"gas_residual_ok": (D_res_CH4 < 1e12 and D_res_H2 < 1e12),
```

on the clipped `0.0`. That comparison **is** sound — `1e12` is nine orders of
magnitude above the floor, so whatever the digits were, the residual is below
the threshold — but nothing established it, and a bare `<` would go on
deciding a readiness term on noise if the threshold were ever tightened
towards the floor. `decide_below` now makes the band explicit and raises
`UndecidableComparison` when a threshold lies inside it. Readiness is
unchanged: `FORECAST_NOT_READY`, as before.

**THE BREADTH, MEASURED.** `tools/clip_provenance.py` instruments every
`numpy.clip` in the scientific tree during a real canonical run — the same
three entry points `qta_full_sim.py` drives — and reconciles the measurement
against `docs/clip_provenance.json`. Reading the source would have found the
two gas-transport sites; it would not have established that the others never
fire, which is the half of "the defect is confined to gas transport" that
makes it a measurement rather than an impression. A clip that starts moving
values fails the check until somebody says what it does to the output.

**WHAT THIS DOES NOT CLAIM.** Nothing here makes the methane residual known.
It makes it *stated*: below `1e3` per cubic metre, which is what this solve
establishes and all it establishes. `PASS = 0` is unchanged, no gate moved,
and the forecast remains forecast.

---

## D-2026-54 — the checker printed a count it had never compared

**CLASS** — `WRONG_SPECIFICATION` in the checking apparatus, twice in four
lines: a message naming a quantity other than the one measured, and a guard
that reports agreement about a field it never read.

**DISCOVERED BY.** The claim audit, reconciling the package's countable
claims against the data.

**DEFECT.**

```python
if mc.get("total_gates") and int(mc["total_gates"]) != CANONICAL_EXPECTED["total_gates"]:
    fail(...)
else:
    ok("MC summary total_gates matches canonical (63)")
```

`CANONICAL_EXPECTED["total_gates"]` is **83**, and 83 is what
`monte_carlo_summary.csv` carries. Nothing in this package has ever had 63
gates: 83 = 47 CONDITIONAL + 23 BLOCKED + 11 DERIVED_CHECK + 2 UNKNOWN + 0
PASS. A reader reconciling the checker's own output against the artifact it
checks would have found a contradiction that does not exist — the same shape
as R59 spending days on a number that turned out to be a slice width.

And `if mc.get(k) and ...` takes the **OK** branch when the field is missing
or empty. An absent `total_gates` printed "matches canonical" beside the
`fail` for the missing row — two messages about the same field, one of them
false.

**REPAIR.** The message interpolates the value it compared, so the two cannot
drift again. A missing or empty field is `NOT CHECKED`, which is a failure:
a field that is not there has not been checked, and silence there reads
exactly like agreement. Same for `PASS_count`.

---

## D-2026-55 — the strongest hardware claim was enforced by nothing

**CLASS** — `MISSING_ENFORCEMENT`: a claim in the package's canonical claims
file with no rule behind it, and three proxies that looked like one.

**DISCOVERED BY.** The claim audit, taking `CLAIMS_BOUNDARY.md` clause by
clause and asking what refuses each one.

**THE CLAIM.**

> No validated hardware. Every hardware item in BOM.csv is either
> DESIGN_SPECIFIED, NOT_INSTALLED, INSTALLED_UNVERIFIED, or
> MANUFACTURER_SPEC. **No item is in-system VERIFIED.**

**DEFECT.** `package_consistency_check.py` audits BOM.csv in eight rules and
none of them is that one. What is there:

* Rules 4 and 5 forbid `MEASURED` and `INSTALLED` — **for `B081..B131` only**,
  51 of 121 rows. The other seventy were unconstrained.
* Rule 6 forbids `INSTALLED`/current for rows whose *name* contains
  "dilution", "cryostat" or "refriger".
* Rule 8 scans the whole row for `\b(validated|verified)\b`.

Rule 8 is the one that looks like the enforcement, and it is not:

```python
>>> re.search(r"\b(validated|verified)\b", "INSTALLED_VERIFIED", re.I)
None
```

An underscore is a word character, so there is no boundary before the `V`.
`INSTALLED_VERIFIED` and `IN_SYSTEM_VERIFIED` pass; a bare `verified` in prose
is caught. The rule catches the sentence and misses the status.

Put together: a status of `INSTALLED_VERIFIED` on any row outside
`B081..B131` whose name does not mention a cryostat passed every check in the
file. Nothing in the package would have said the claims boundary had been
broken.

**AND THE VOCABULARY DID NOT MATCH.** The claim names four statuses. The data
uses three: `DESIGN_SPECIFIED` (117), `NOT_INSTALLED` (3) and
`MANUFACTURER_SPEC_TARGET` (1) — a fifth name, not on the list.
`INSTALLED_UNVERIFIED` is on the list and used by nothing. So a reviewer
checking the claim against the file would find a status the claim does not
permit, and a permitted status that does not occur.

**REPAIR.** An allowlist, which is the property rather than a proxy for it:

```python
ALLOWED_BOM_STATUS = {
    "DESIGN_SPECIFIED", "NOT_INSTALLED", "INSTALLED_UNVERIFIED",
    "MANUFACTURER_SPEC", "MANUFACTURER_SPEC_TARGET",
}
```

Enumerating the permitted set also removes the thing that made this hard to
write as a pattern: `INSTALLED_UNVERIFIED` contains `VERIFIED` as a
substring, so no substring rule separates the negation from the assertion.
Membership does. Widening the set is a change to the claims boundary and now
reads like one.

`CLAIMS_BOUNDARY.md` states the same five, with the current counts, and says
the list is the complete permitted vocabulary. An empty BOM is a refusal
rather than a clean audit of nothing.

`tests/test_bom_status_claim.py` drives the rule over a **constructed** BOM
carrying `INSTALLED_VERIFIED` on a row outside `B081..B131` and requires a
refusal, with the unmodified table as the control — so the test establishes
what the checker would reject, not merely that today's data is clean. It
reads the allowlist out of the checker rather than restating it, because a
copy of the rule inside the test would keep passing while the checker's copy
rotted. The word-boundary behaviour is pinned in its own test, so that
reintroducing a `\bverified\b` scan as the enforcement fails with the reason
attached.

**WHAT DID NOT CHANGE.** No BOM row. The committed table was already clean;
what was missing was anything that would have noticed if it were not.

---

## D-2026-56 — the claims boundary was a fifth enforced, and nothing said which fifth

**CLASS** — `MISSING_ENFORCEMENT` at scale, plus `UNMEASURED_COVERAGE`: the
package's canonical claims file and the checker that is supposed to hold it up
were never reconciled, in either direction.

**DISCOVERED BY.** The claim audit. D-2026-55 found one unenforced clause by
reading. The obvious next question was how many others there were, and that is
a measurement, not a reading.

**THE MEASUREMENT.** Take every **Forbidden:** bullet in
`CLAIMS_BOUNDARY.md`, and ask of each: if this exact sentence appeared in a
live document, would `package_consistency_check.py` refuse it?

**19 of 24 would have passed.**

Uncovered, every one of them:

```
QTA has validated radiation shielding.
QTA shielding proves 10 mK Mode D operation.
Cryo-baffles prove contamination is solved.
Mode B processing and Mode D sensing occur simultaneously.
Monte Carlo validates the shielding stack.
RF/IR shielding is sufficient without measurement.
The radiation shutter stack has been experimentally proven.
The cryopanels solve Mode B -> Mode D contamination without measurement.
The magnetic shield is compatible with NV sensing without bias-field validation.
QTA has selected RTB/JT cooling.
QTA has installed RTB/JT cooling.
QTA RTB/JT cooling is validated.
RTB/JT replaces the dilution refrigerator.
RTB/JT unlocks any PASS gate.
...
```

The **entire** shielding list — the nine sentences the file itself calls "the
canonical statements that bound what the package claims about shielding" —
had no enforcement at all. Among them, `Mode B processing and Mode D sensing
occur simultaneously`, which is the sentence the whole mode-exclusive
architecture exists to deny, named in the not-authorized list, and enforced by
nothing.

The five that were covered were covered **incidentally**: `25 RTB`, `25 JT`,
`25 reverse-turbo-Brayton`, `JT provides 10 mK`, `JT validates` — patterns
written to catch stale module counts, which happened to fall across five
claim sentences.

`RTB/JT unlocks any PASS gate` is the near miss that shows how thin the
coverage was. The checker had `RTB[/ ]?JT\s+unlocks\s+PASS`. The bullet says
"unlocks **any** PASS gate". One word apart, and the rule did not fire.

**WHY IT WAS INVISIBLE.** Nobody had claimed the list was enforced. Nothing
had measured that it was not. A claims file whose entries are enforced and one
whose entries are not look identical from the outside, and the checker's
output — 90-odd green `[PASS]` lines — reads as though the claims are among
the things being held up.

**REPAIR.** `FORBIDDEN_CLAIM_PATTERNS` in the checker: one entry per forbidden
bullet, each carrying the bullet **verbatim** as the thing it enforces, scanned
over the five live documents with the negation handling the README check has
always had (a line that denies the claim is not the claim) and the existing
`Forbidden:`/superseded exemption, so that the claims file may quote its own
forbidden sentences.

`tools/claims_enforcement.py` reconciles the two lists **in both directions,
by matching rather than by naming**:

* every bullet must be matched by some pattern, or it is reported uncovered —
  so a new forbidden claim arrives unenforced and fails, instead of joining
  the silent 19;
* every pattern must name a bullet that still exists, so the table cannot
  drift into describing a claims file that has moved on;
* every pattern must **match** the bullet it names. Naming a claim is not
  enforcing it, and a table of regexes that matched nothing would otherwise
  report full coverage.

Now 22 of 22. The count moved from 24 because two entries were removed from
the list and restated as prose above it: "a value of 0.000000000e+00 does not
mean the species is absent" and "do not quote a serialised value without its
resolution class" are rules of reading, not sentences anyone would write.
Nothing can enforce them automatically, and a list that must be enforced entry
by entry cannot carry entries that nothing can enforce without misreporting
its own coverage.

**WHAT THIS DOES NOT ESTABLISH — AND THE TOOL SAYS SO IN ITS OWN OUTPUT.**
One bounded question: would this *exact sentence* be refused? A paraphrase is
not caught. No string rule catches one, and a number that implied otherwise
would be worth less than no number. What has changed is that the number is now
measured, on every commit, and that the shielding claims are inside it.

---

## D-2026-57 — the unit was read off the spelling of the column name

**CLASS** — `TRUE_DEFECT` in the archival artefact: a semantic dimension
resolved by a proxy, losing the dimension on 91 of 162 columns and getting it
**wrong** on three.

**DISCOVERED BY.** The semantic-dimension audit. Identity, payload,
destination, port, socket family/type/protocol and (as of D-2026-53)
resolution each have a conservation rule and a test. Units did not, and
`validate_hdf5_equivalence.py` had been printing the evidence in its own
report the whole time:

```json
"unresolved_unit_columns": 91
```

— a number it counted and gated nothing with.

**THE MEASUREMENT.** `build_hdf5_mapping.unit_of()` matched the **end of the
column name** against a thirty-entry suffix table and returned `"unresolved"`
otherwise. 162 numeric columns cross into the governed HDF5, where the unit
becomes a dataset attribute a consumer reads as fact.

*91 lost the dimension entirely* — including every column whose unit is
written in its own name:

```
Q_laser_W_m3, Q_mw_W_m3      W/m^3   -> unresolved
heat_flux_W_m2               W/m^2   -> unresolved
n_CH4_modeC_1m3 (+3 species) 1/m^3   -> unresolved
eig_nats, prior_entropy_nats nats    -> unresolved
N_per_m2, admitted_per_m2    1/m^2   -> unresolved
Ts_mK                        mK      -> unresolved
eps_pct                      percent -> unresolved
DeltaGamma_rads              rad/s   -> unresolved
```

*and three did not fall through — they came out wrong:*

```
gradient_K_per_m       ends "_m"  -> published as METRES   (it is K/m)
dose_flux_open_m2_s    ends "_s"  -> published as SECONDS  (it is 1/(m^2 s))
dose_flux_closed_m2_s  ends "_s"  -> published as SECONDS  (it is 1/(m^2 s))
```

A temperature gradient published as a length and a flux published as a time.
That is worse than no unit, because a wrong one is credible: a consumer has no
reason to doubt an explicit attribute.

**AND A SECOND CONFLATION INSIDE THE WORD.** "unresolved" was doing two jobs.
`cycle`, `step`, `rank` and `n_batches` are counts. `theta_CH4`,
`attenuation_factor`, `SNR` and `Cc` are dimensionless ratios. `cost`,
`duration` and `risk` are `{1,2,3}` difficulty scores — **not** currency and
**not** time, which is exactly what a suffix rule would have been free to
call `duration`. And `value` in a long-format metric/value table genuinely
carries its unit in the row. Four different situations, one word, the same
shape as D-2026-53's zero that was exact sitting beside a zero that was noise.

**REPAIR.** `docs/unit_inventory.json` declares the dimension of all 162
governed numeric columns, reviewed. There is no `unresolved` in the
vocabulary: a column with no unit says **why** —

| word | meaning |
|---|---|
| `DIMENSIONLESS` | a physical ratio: a fraction, a coverage, a contrast, an SNR |
| `COUNT` | an integer count or index |
| `ORDINAL` | a rank-scale score with no physical dimension |
| `PER_ROW` | a long-format table; the entry names the column carrying the unit |

`unit_of()` reads the declaration and **raises** on an undeclared column
rather than defaulting. Any default there is a dimension nobody reviewed,
written into an archive as fact.

`tools/unit_inventory.py` reconciles both directions — every governed numeric
column declared, no entry naming a column that no longer exists, no
`unresolved`, and a `PER_ROW` entry must name a column that is **actually in
that CSV's header**, so "the unit is in the row" is checked rather than
asserted. One rule runs the other way: a column whose own name ends in an
unambiguous unit token may not be declared dimensionless. That is the error
direction that matters, and the suffix table survives only there — as a
refusal, never as a resolver.

`validate_hdf5_equivalence.py` turns the statistic into a gate and reports
what is worth counting: **94 columns carry a physical unit, 68 declare why
they carry none.**

**THE MUTATION THAT SURVIVED, AND WHY IT MATTERED.** `UI8` disables the new
refusal in the equivalence validator. It survived the first run: every other
test of that report reads the **committed** report, which no source change can
move, so the refusal had no test at all. The harness says a mis-scoped spec
and a missing test look identical from there — this was the missing test. It
is driven now against a constructed one-column fixture, with controls for
each of the six legitimate dimensions, in the file that owns the validator.
8/8.

**WHAT DID NOT CHANGE.** No value. 483 datasets still compare exactly against
their sources; the result is `EQUIVALENT` as before. What changed is that the
numbers now arrive with the dimension they were always supposed to carry, and
three of them are no longer labelled with the wrong one.

---

## D-2026-53a — the test written to defend the finding committed the finding

**CLASS** — `WRONG_SPECIFICATION` in a test: a host-dependent quantity
asserted as a property, in the test defending the discovery that it is
host-dependent.

**DISCOVERED BY.** The hosted `full-suite` at `0358af0`, step 5. Not by me.

**DEFECT.**

```python
assert np.all(raw < 0.0), "expected the Mode-C methane solve to be noise"
...
assert np.all(written == 0.0), "expected the clip to write exact zeros"
```

On a GitHub runner the same solve returns a **mixed-sign** profile — 18 of
120 cells positive — so the clip leaves `+1.48e-03` and `+2.39e-04` in the
file where this machine writes zeros, and both assertions fail.

Which is D-2026-53, verbatim. The **sign of that noise is the
host-dependent quantity**: it is the entire mechanism by which one machine's
artefact says `0.0` and another's says `1.6e-02`. Two commits after writing
that down, I pinned it in the test written to defend it.

The finding survived. Everything that states the *property* passed on the
runner untouched — `test_methane_in_mode_C_is_marked_below_what_the_solve_
can_see` is not in the failure list, because the classification is
`BELOW_RESOLUTION` in all 120 cells on both machines. Only the assertions
that reached past the property to the digits broke, and they broke on the
first machine that disagreed.

**THE METHOD DEFECT UNDERNEATH.** This repository has a documented one-line
way to reproduce a GitHub runner's arithmetic —

```
OPENBLAS_CORETYPE=Haswell NPY_DISABLE_CPU_FEATURES="X86_V4 AVX512_ICL AVX512_SPR"
```

— written down during R59 for exactly this purpose. I did not run the new
tests under it before pushing, having just written a defect report about
host-dependent numerics. Run now, it reproduces the runner exactly: raw min
`-1.062372e+02`, max `+9.899635e-01`, 18 positive cells, and
`max displacement 1.062372e+02` — the same figure the runner's own
clip-provenance step printed.

**REPAIR.** The test asserts what holds on both machines, which is also what
the finding actually claims:

* the integrator's answer lies entirely inside its own absolute tolerance;
* it is not zero, so the published zero is the clip's and not the model's;
* every written value is non-negative and below the floor;
* every cell classifies `BELOW_RESOLUTION` whatever the signs were.

`test_the_class_is_stable_where_the_digits_are_not` is new and states the
guarantee directly, checked against **both** hosts' extremes — this machine's
min and max, and the runner's `-3.86e-01`, `+1.48e-03`, `+9.97e-09` — so it
cannot quietly become a statement about one host's arithmetic. The committed-
artefact test no longer pins `"0.000000000e+00"` either: a regeneration on a
different runner writes `+1.48e-03` in some of those cells and is equally
correct, and what must hold of the committed file wherever it was produced is
that every value is inside the floor that same file declares.

Verified under the runner dispatch, not assumed.

**WHAT THE SAME RUN ALSO CONFIRMED.** With step 5 red, `set -e` skipped the
byte gate, so `outputs/` was never regenerated — and step 8 printed

```
SCOPE REFUSED: compared 0 files: no output in the other tree has a committed
counterpart by name. 'no decision changed' would be a statement about an
empty set.
```

The anti-vacuity guard in `cross_env_semantics.py` firing on real hardware,
in the one situation it was written for, rather than reporting "0 decisions
changed" over nothing. It was never a consequence anybody had observed
before.

And step 9, `tools/clip_provenance.py`, passed on that runner on its first
outing: 11 clip sites observed, the same 2 moving, `inventory agrees with the
measurement` — with element counts of 210412 and 4344 against this machine's
981668 and 6352. The inventory compares `moves`, a boolean, and not the
counts, which is why a host-dependent measurement reconciles against a
host-independent judgement.

---

## D-2026-58 — which code runs was decided by the host's SIMD dispatch

**CLASS** — `WRONG_SPECIFICATION`: a governed record stating a
host-conditional measurement as a property of the thing measured, and a test
pinning one host's answer.

**DISCOVERED BY.** Running the full suite under the runner-emulating dispatch
after D-2026-53a — the sweep that the earlier failure said should have
happened before pushing. One failure in it was not mine and not the manifest:

```
tests/test_stage10_stack.py::test_rust_open_item_matches_the_measured_verdict
E  assert True == (True is False)
```

**THE MEASUREMENT.** `rust_kernel.kernel_parity()` adopts a Rust kernel only
if it is **bit-for-bit identical** to the NumPy reference — a strict rule, and
the right one. But the reference side of that comparison is NumPy, and
NumPy's `**` loop moves with the CPU:

| NumPy SIMD in force | `conductivity_power_law` | max ulp | `backend_in_force` |
|---|---|---|---|
| `X86_V3+X86_V4` (AVX-512) | REJECTED | 2 | numpy |
| `X86_V3` only | **ADOPTED** | **0** | **rust** |

Same kernel, same seed, same 4096 test values, same code on both sides.
`face_conductance` — pure division and addition — is bit-identical either
way; `powf` is not.

So the adoption verdict is a fact about **(kernel, host)** and was recorded as
a fact about the kernel. `STACK.md` printed `| 2 | REJECTED | numpy |`,
`stack.json` listed the rejection as an open item, and the test asserted it.
On a host without AVX-512 all three are wrong, and `dispatch()` would hand a
solver the Rust kernel instead of NumPy.

This is R59 one level up. R59 is about a printed digit differing between
hosts; this is about **which implementation runs** differing between hosts,
decided by the same underlying fact.

**SEVERITY, STATED HONESTLY.** Nothing turns on it today. `rust_kernel.py`'s
own record says "no solver imports these kernels yet", and a sweep confirms
it: outside the module and its tests, nothing references `dispatch()`,
`parity_ok()` or the kernels. The live consequence was a test that fails on
hardware this repository has not happened to run on — a latent CI failure
driven by runner silicon, on code nobody had touched.

**REPAIR.** The verdict carries the conditions of its own measurement, which
is D-2026-53's rule applied to a decision instead of a number.
`numpy_dispatch()` reports the SIMD extensions in force, and every parity
record — including the unavailable and shape-mismatch branches — now carries
`numpy_dispatch` and `verdict_is_dispatch_conditional`. `status_report()`
states the conditionality in prose beside the verdicts.

`STACK.md` and `stack.json` state both measurements rather than one. The test
asks for the relationship: the note must acknowledge the dependence, the
verdict must have been measured under the dispatch the report names, and
`REJECTED` is required only where AVX-512 is actually in force. It passes
under both dispatches now, checked under both rather than reasoned about.

**WHAT IS NOT CHANGED.** The parity rule. "Bit-identical or not adopted" is
the standard, and 2 ulp is still a rejection. The defect was never the rule;
it was a record that read as though the rule had reached a permanent verdict.

---

## D-2026-53a, caught a second time — and the harness refused rather than scored

`agent-substrate` at `463b4e8` ran **50 steps green over 3h 46m** and failed
at step 51, the new `output_resolution.json` matrix, after seven seconds:

```
16 mutations over 3 suite(s)

BASELINE RED -- mutation results would be meaningless.
Every mutation would 'fail the suite' for this pre-existing
reason and the report would read as a perfect score:
['test_the_written_methane_zero_is_the_clip_not_the_model']
```

The same defect as D-2026-53a — the test that pinned the sign of the Mode-C
methane noise — found by a second, independent instrument on the same
hardware, in a different job.

Worth recording for what the harness did rather than for what broke. A
mutation runner that did not check its baseline would have run all sixteen
mutations against an already-red suite, observed every one of them "fail the
suite", and printed **16/16 killed** — a perfect score meaning nothing. It
refused instead, named the test, and said why the number would have been
worthless. Already fixed at `c1bd8ea`; nothing to repair here.

---

## D-2026-59 — a sync command that names a group the project does not have

**CLASS** — `TRUE_DEFECT` in CI configuration, invisible to every local run
by construction.

**DISCOVERED BY.** `dispatch-sensitivity` at `c1bd8ea`, on the job's first
outing. It failed in **one second**, at step 4 of 6:

```
error: Group `stack` is not defined in the project's `dependency-groups` table
```

**DEFECT.** I wrote `uv sync --frozen --group stack`. There is no `stack`
group: `pyproject.toml` defines `dev` and `workflow`, and every sibling job
in the same file uses `--all-groups`. The command was invented rather than
copied from the four jobs directly above it.

**WHY NOTHING LOCAL COULD HAVE CAUGHT IT.** A local run reuses an
already-synced `.venv` and never executes a sync line at all. The command was
written, read back, committed, and **first executed on a hosted runner** —
so no amount of local verification, including the two full-suite runs under
two dispatches that preceded the push, could have touched it. That is the
shape of the defect, not an excuse for it: reading the sibling jobs would
have found it in seconds.

The job's own dispatch guard never ran — `set -e` stopped at the sync — so
the one step that asserts "this job really got a different dispatch" was
skipped. Correct ordering; worth noting that a green guard is not what made
this visible.

**REPAIR.** `--all-groups`, matching every sibling. And
`tools/workflow_contract.py` now refuses any `--group NAME` in a command a
workflow runs where `NAME` is not in `dependency-groups`, with the empty-set
and no-commands-found cases as refusals of their own.

**AND THE FIRST VERSION OF THAT CHECK WAS THE PROXY ERROR AGAIN.** Scanning
the whole file for `--group \w+`, it immediately reported two undefined
groups — both of them quoted inside the **comments** explaining this very
defect. Matching the text of a file is not matching what the file runs.
`_run_commands()` now extracts `run:` values, one-line and block form, drops
comment lines, and reports how many commands it scanned (323) so a parser
that silently found nothing cannot pass everything. Verified by planting a
bad group in a real `run:` line and requiring the refusal, with the clean
tree as the control.

---

## HOSTED EVIDENCE — the resolution class is stable where the digits are not

`full-suite` at `c1bd8ea`, step 8, on a GitHub runner, with the byte gate
having regenerated the canonical tree first so the comparison was real:

```
DECISION          0
ZERO_CROSSING    28
SIGN_FLIP         0
PRECISION       179   largest relative difference 3.279e-01

  gas_transport_profile.csv [r102c1]: 0.000000000e+00 -> 1.561645593e-02
  gas_transport_profile.csv [r116c1]: 0.000000000e+00 -> 1.483302101e-03
  gas_transport_metrics.csv [r1c3]:   0.0 -> 3.999936115734321e-09
  coupled_mode_state_summary.json
      [.metrics.Mode_D_residual_CH4_density_m3]: 0.0 -> 3.999936115734321e-09

No decision-bearing token differs between the two environments.
```

That `1.561645593e-02` is D-2026-53's `1.6e-02`, reproduced on hosted
hardware at the commit that closed it. `Mode_D_residual_CH4_density_m3` is
the contamination claim itself: `0.0` on the committed side, `4.0e-09` on the
runner's.

**And the resolution columns do not appear in the list.** They are
non-numeric tokens; had a single cell classified differently between the two
machines it would have been a DECISION and the gate would have refused.
Twenty-eight numbers moved, some by sixteen orders of magnitude, and every
statement about what the solve could see stayed put. That is the guarantee
D-2026-53 was built to provide, measured rather than argued.

Step 5 — the full pytest suite — was **green** on that runner, which is
D-2026-53a confirmed repaired on the hardware that found it. Step 9,
`clip_provenance.py`, passed in 4m22s with the same two moving clip sites and
host-dependent counts (210412 and 4344 against this machine's 981668 and
6352). Step 7 is the standing R59 byte gate, red as always and already
reported.

**THE COMPARATOR WAS OVERSTATING, AND THEN MY FIX FOR IT WAS A PROXY.** Its
ZERO_CROSSING message said, of every crossing, that a published
`0.000000000e+00` "states that the model determined the quantity to be
exactly nothing". True when written; false since D-2026-53 for exactly the
files this repository just fixed, which publish the zero beside
`BELOW_RESOLUTION` — the opposite claim. An instrument that keeps asserting
it overstates what the artefact says, which is the class it exists to find.

`declares_resolution()` now reads the artefact and splits the report into
DECLARED and BARE. The first version read **only the CSV header**, and
immediately called `coupled_mode_recovery_metrics.csv` BARE while that file
declares `Mode_D_residual_CH4_resolution, BELOW_RESOLUTION` on a row of its
own. It is a metric/value table — the PER_ROW shape D-2026-57 had to give a
name to because its meaning lives in the row — and a header-only check is a
proxy that fails on precisely the shape this repository had already
identified and named. Header *and* first column now, both read from the file.

Reproduced locally under the runner dispatch after the repair: **24 of 24
zero-crossings declared, 0 DECISION.** Before it, 22 of 24, with the two
long-format rows wrongly filed as bare. E16 and E17 mutate both halves back;
17/17 killed.

---

## D-2026-60 — the gate table published the unresolved residual as an exact zero

**CLASS** — `TRUE_DEFECT` in the canonical gate table: D-2026-53's defect, one
propagation step further than that repair traced.

**DISCOVERED BY.** The DECLARED/BARE split, on its first hosted run —
`full-suite` step 8 at `280cb3d`:

```
ZERO_CROSSING is not a precision event, and 26 of 28 are already declared
as such by the file they are in.
  ...
  BARE -- the file states the number and nothing about what its method
  could resolve.
    energy_ledger_cumulative_3d.csv [r2c9]: 1.615587134e-27 -> 0.000000000e+00
    results_gate_table.csv [r82c4]:         0.00e+00 -> 4.00e-09
```

The instrument was written one commit earlier to answer "is this crossing
already declared?", and the first thing it did on real hardware was name a
file I had not reached. `results_gate_table.csv` row 82 is

```
gate_id  RESIDUAL_SPECIES_MODE_D_CHECK
name     Residual process species cleared for Mode D
computed CH4=0.00e+00; H2=2.60e+11          unit 1/m^3
status   BLOCKED     threshold REQUIRES_MEASUREMENT
```

— the **gate table**, publishing the residual methane at Mode D entry as an
exact zero, in the column a reviewer reads as the gate's value. On a runner
it reads `4.00e-09`.

**WHAT DID NOT HAPPEN.** No gate moved. `status` stays `BLOCKED`, `threshold`
stays `REQUIRES_MEASUREMENT`, `measured_in_this_system` stays `false`,
`can_PASS_now` stays `NO`, and the comparator reports **0 DECISION** across
both environments — those are non-numeric tokens and they are identical. The
gate is blocked for an independent reason (Mode D sensing is itself not
validated). What was wrong was the published value, not the verdict.

**REPAIR.** `numerics.render_with_resolution()` publishes what the method
establishes:

```
CH4<1.00e+03; H2=2.60e+11
```

An unresolved quantity becomes the **bound**, with the operator carried as
part of the statement. The resolved one keeps its value. `OUT_OF_RANGE` is
marked rather than rendered as an ordinary number.

This is not the rounding D-2026-53 rejected. Rounding writes `0.0` and says
nothing about why; an explicit inequality naming the floor states the
finding. And it is strictly better than declaring the crossing, because the
bound is the **same text on every host** — the cell stops diverging between
environments instead of merely being marked as allowed to.

`build_gate_specs` reads the resolution class and floor that the coupled
metrics have carried since D-2026-53, so the fix is a change of rendering and
not of physics. 483 datasets still compare exactly; the equivalence result is
unchanged at `EQUIVALENT`, 94 columns with a physical unit and 68 declaring
why they have none.

**STILL OPEN, AND NAMED SO IT STAYS NAMED.**
`energy_ledger_cumulative_3d.csv`'s `cumulative_dU_J` crosses
`1.615587134e-27` -> `0.0` between hosts. It is the difference of two
~`1.5e-09` J sums, so it is float cancellation, not a species density with a
declared solver tolerance — its floor is a property of the summation, roughly
`|source| * eps * n_terms`, and declaring it honestly is real numerical work
rather than a rendering change. Left open deliberately. The instrument will
keep printing it under BARE until somebody does it, which is the point of
having the split.

## D-2026-61 — the release gate compared the outputs that turned up

**CLASS** — `TRUE_DEFECT` in `package_consistency_check.py`, the instrument
every commit message in this branch quotes. The rule that would have caught it
was already written, in the mode the file itself documents as *not* the
release gate.

**DISCOVERED BY.** A stray concurrent process, and then by looking at what it
did. Two regenerations overlapped on `outputs/`; one was killed part-way. The
surviving run completed and printed

```
RESULT: PASS (all consistency checks passed)
```

over a directory holding **84** files. A clean regeneration produces **89**.
The five that were absent:

```
coupled_mode_recovery_metrics.csv
coupled_mode_state_summary.json
mesh_convergence_summary.csv
multiphysics_verification_summary.csv
numerical_stability_summary.csv
```

`coupled_mode_state_summary.json` carries `Mode_D_residual_CH4_density_m3`.
That is the contamination claim — the quantity D-2026-53 and D-2026-60 were
both about. Its committed root copy was compared against nothing, and the run
reported that the root canonical outputs byte-match the canonical
regeneration.

**THE MECHANISM.** `regen_root_byte_drift()` iterates the **generated**
directory and, for each file, compares the root copy of the same name:

```python
for p in sorted(gen_dir.iterdir()):
    rootp = root_dir / p.name
    if not rootp.exists():
        continue
```

That is the right rule for what it does. It also means the comparison's scope
is whatever the pipeline happened to produce, and nothing established that the
pipeline produced everything it is supposed to. An output that stops being
emitted does not drift and does not error: it leaves the comparison, no step
mentions it, and the headline is reported of a set that no longer contains it.

**THIS WAS KNOWN, IN THE OTHER MODE.** `--verify-existing` refused any set
that was not exactly 89 files, and its comment said why in as many words:

```
# the canonical complete-set size is exactly 89 files ...
# (missing files would otherwise escape Step 2b, which iterates
#  only files that exist).
```

The escape was identified, written down, and closed for the mode whose own
banner reads `NOT the release gate`. The authoritative mode — full
regeneration, which is what CI runs — never consulted it. `_OUTPUTS_STATE` is
assigned only inside `if VERIFY_EXISTING:`; in the release path it is
unconditionally `"USABLE"`. Every test in
`tests/test_checker_missing_outputs.py`, a file written specifically for this
defect class and containing a test called
`test_missing_outputs_yields_no_vacuous_byte_match_pass`, passes
`--verify-existing`.

Closing the example and not the class, in the file that measures whether the
package is consistent.

**AND THE RULE ITSELF WAS A COUNT.** `_n != 89` is satisfied by the right 89
files and equally by 89 files with five of them renamed — the canonical output
gone, its stale root copy never compared, the gate green. A count is a proxy
for set membership in exactly the way a name suffix was a proxy for a unit
(D-2026-57) and a file's whole text was a proxy for what it runs (D-2026-59).
The fixtures had the same shape: the test for a truncated set supplied
eighty-eight files called `f0.json`, none of them a canonical output, and
asserted on the string `"88 files present"`.

**REPAIR.** `CANONICAL_EXPECTED["canonical_outputs"]` names the 89, and
`canonical_set_problems()` reconciles the produced directory against it — by
name, in both directions, in **both modes**:

- `INCOMPLETE_{EXISTING,REGENERATED}_OUTPUTS` — declared, not produced. Named,
  not counted.
- `FOREIGN_{EXISTING,REGENERATED}_OUTPUTS` — produced, not declared.
- `UNROOTED_CANONICAL_OUTPUT` — declared and produced, with no root copy, so
  Step 2b would skip it and the byte gate would report agreement over it.
- `EMPTY_CANONICAL_DECLARATION` / `DUPLICATE_CANONICAL_DECLARATION` — the
  anti-vacuity guard on the declaration itself. It is the scope of all three
  reconciliations above; empty, they all agree.

The PASS line now states its scope: *the produced output set is exactly the 89
declared canonical outputs — 88 of them compared byte-for-byte in Step 2b, 1
exempt by design.*

**A SECOND DEFECT, FOUND BY THE NEW FIXTURE.** The old fixtures used names
that are not canonical outputs, so the checker refused early and never read
them. Supplying the **real** names — present, empty — takes the run to Step 4:

```
File "package_consistency_check.py", line 595, in <module>
  if "tau_c_canonical_threshold_us" not in tcs[0]:
IndexError: list index out of range
```

A present-but-empty canonical output — a truncated write, a disk-full, a
generator emitting a header and no rows — crashed the release gate with a
traceback at Step 4 and lost every later diagnostic. §47: an accidental
runtime failure is not a governed refusal. Now `EMPTY_CANONICAL_OUTPUT` and
`MALFORMED_CANONICAL_OUTPUT`, classified and read past.

**WHAT THIS DOES NOT ESTABLISH.** That the 89 are the right 89. The
declaration records what the default profile currently produces; it makes a
change to that set visible and reviewable, which is all a declaration can do.
Nor that no other verifier has the same shape — the sibling sweep below is
what was actually checked, not an argument that nothing else could.

**SIBLING SWEEP — can a verifier here report success having examined
nothing?** Driven against each tool's real entry point, not grepped for:

| verifier | on an empty scope |
|---|---|
| `tools/cross_env_semantics.py` | refuses; reports files and leaves compared |
| `tools/unit_inventory.py` | `ScopeError` — "a reconciliation over an empty set would report full coverage" |
| `tools/claims_enforcement.py` | `ScopeError`, exit 2, verified by driving it |
| `tools/corpus_allowlist.py` | `build()` refuses to write an empty allowlist |
| `tools/workflow_contract.py` | refuses when no `run:` command is found; reports 323 scanned |
| `tools/identity_inventory.py` | reconciles both directions, so an extraction that matches nothing shows up as entries naming actions that do not exist |
| `tools/mutation_matrix.py` | refuses on a red baseline rather than scoring against it |
| `package_consistency_check.py` | **this defect** |

The one that had the weakest guard is the one at the top of the tree.

**MUTATION MATRIX.** `tools/mutations/canonical_output_set.json`, 8 operators:
the rule back in one mode, the set back to a count, each direction of the
comparison dropped on its own, the declaration emptied and duplicated, the
Step-4 traceback restored, and the scope number removed from the headline.

## D-2026-62 — the comparator's conclusion did not say what it was drawn from

**CLASS** — `TRUE_DEFECT` in `tools/cross_env_semantics.py`: a tautology and a
measurement printed in the same words. The instrument written to tell a
changed verdict from a changed digit could not tell its own two cases apart.

**DISCOVERED BY.** Reading the hosted evidence at `f118e6a`. `full-suite`
finished **green end to end** — including step 7, the byte gate, which has
been the standing R59 red. That runner's dispatch matched the committed tree,
so every canonical output regenerated byte-identically. Which means step 8,
the cross-environment comparison, ran over a tree with nothing in it to
compare, and printed:

```
compared 87 file(s); 0 differ byte-for-byte; 0 leaves put side by side
  DECISION          0
  ZERO_CROSSING     0
  SIGN_FLIP         0
  PRECISION         0   same sign, largest relative difference 0.000e+00

No decision-bearing token differs between the two environments.
```

Reproduced locally, verbatim, on the default dispatch. That closing sentence
is the tool's headline. It is the same sentence it prints after putting 5404
leaves side by side and finding every status, verdict, boolean and label
identical. Here it was asserted having classified nothing.

**WHY THE SCOPE GUARD DID NOT CATCH IT.** `check_scope()` refuses two things:
zero FILES compared, and zero leaves *while files differ* (a shape mismatch).
Neither fires. 87 files were compared — the scope looks healthy — and no file
differed, so the second guard is out of scope by construction.

The count that was healthy is files. The claim is carried by leaves. Counting
one unit and making a claim in another is what R59 was: eight names from a
`[:8]` slice read for months as an eight-file divergence. The same substitution,
in the instrument built to stop it.

**THIS IS NOT AN ERROR, AND MUST NOT BE REFUSED.** Two byte-identical trees
are the outcome this project wants — it means the regeneration reproduced the
committed bytes on that host. Refusing it would turn a good result into a red
gate. What is not allowed is publishing a reproduction and an invariance in
the same words, because only one of them measured anything.

**REPAIR.** The report carries `basis`, `IDENTICAL_TREES` or
`MEASURED_ACROSS_DISPATCHES`, and the two print different conclusions:

```
IDENTICAL_TREES: all 87 compared file(s) are byte-identical, so 0 leaves were
put side by side and no token was classified. This run establishes that the
regeneration REPRODUCED the committed tree on this host. It establishes
nothing about invariance across dispatches -- there was no difference to be
invariant under, and the PRECISION line above reads 0.000e+00 for the same
reason.
```

and, where something was actually compared, the sentence now carries its
scope: *(5404 leaves compared, 23 file(s) differing)*.

**WHAT THIS DOES NOT CHANGE.** Both bases still exit 0. A `DECISION` still
refuses. The measurement that matters — 27 zero crossings, 26 of them
declared, 0 DECISION, reproduced under the runner dispatch at `f118e6a` — is
unaffected; what changed is that a log can no longer be read as that
measurement when it is not.

## D-2026-63 — three test modules imported only because something else ran first

**CLASS** — `TRUE_DEFECT` in test isolation. Found while adding the D-2026-62
tests: `pytest tests/test_cross_env_semantics.py` alone died at collection
with `ModuleNotFoundError: No module named 'tools'`, on the committed file,
before any edit of mine.

**THE MECHANISM.** `tests/` has no `__init__.py`, so pytest puts `tests/` on
`sys.path` and not the repository root. There was no `conftest.py`. 103 of the
106 test modules insert the root themselves, each with its own copy of

```python
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
```

and three did not:

```
tests/test_agent_authority_boundaries.py
tests/test_cross_env_semantics.py
tests/test_hotspot_ranking_determinism.py
```

They passed in every full run, because the alphabet put a module that inserts
the path ahead of them. Measured, not supposed: `pytest <file> --collect-only`
over all 106 modules, three collection errors.

**WHY IT IS NOT COSMETIC.** The suite's green was partly a fact about
collection order. A `-k` selection, a shard, a parallel worker or a
reordering plugin can schedule one of the three first, and a suite that has
been green for months fails to collect. It also means the cross-environment
instrument's own tests could not be run by a developer on their own, which is
how a test file stops being consulted.

**REPAIR.** `conftest.py` at the repository root. pytest imports it before
collecting anything, so the path is there whatever runs first — structural
rather than a convention that a hundred files happen to follow. The
per-module inserts stay; they are harmless and removing them is churn.

`tools/test_isolation.py` keeps it closed by running a real
`pytest --collect-only` per module and refusing on any that fails, with the
count reported and an empty scope refused. It checks the property, not the
file: a `conftest.py` that exists and does nothing would satisfy a grep and
fail this.

**WHAT IT DOES NOT ESTABLISH.** That the tests PASS in isolation. Only that
they collect, which is the import-order failure this was written for. A test
whose assertions depend on another module's side effects is a different
defect, and this tool does not look for it.

## Hosted evidence, `c1bd8ea` and `f118e6a`

Recorded because both answer questions left open in earlier entries, and
because a gate condition is satisfied for a commit when THAT commit's own
hosted run is green — never inherited from a parent.

**`agent-substrate` at `c1bd8ea` completed SUCCESS: 62 of 62 steps, 4h05m**
(22:20:33 → 02:25:29). This is the first run to get past the refusal recorded
in D-2026-59:

```
step 51  mutation matrix -- the cross-environment measurement   SUCCESS
step 52  mutation matrix -- the dimension of every published number   SUCCESS
step 53  mutation matrix -- what a serialised number is allowed to mean   SUCCESS
```

At `463b4e8` step 51 ran 50 steps green over 3h46m and then refused in seven
seconds with `BASELINE RED`, naming
`test_the_written_methane_zero_is_the_clip_not_the_model` — D-2026-53a, found
a second time by a second instrument. It was repaired at `c1bd8ea`, and these
are the first hosted scores of the two matrices added for D-2026-57 and
D-2026-53..56. Steps 55–62 are green too: the corpus allowlist, the long
horizon, the fuzz harness, the governed production path, the read-only
auditor, Stage-10 write authority, sources unchanged after mutation testing,
and the canonical tree untouched.

Not inherited: `dispatch-sensitivity` FAILED at that commit, in one second, on
the `--group stack` line — that is D-2026-59, fixed at `d0b85d2`. `full-suite`
failed at step 7 only, the standing R59 byte gate.

**`full-suite` at `f118e6a` completed SUCCESS, every step, step 7 included.**
That runner's dispatch matched the committed tree, so every canonical output
regenerated byte-identically and the byte gate passed for the first time on
hosted hardware in this branch.

It is one runner, not a closure of R59. GitHub's runners are not one machine,
and the local reproduction under `OPENBLAS_CORETYPE=Haswell
NPY_DISABLE_CPU_FEATURES="X86_V4 AVX512_ICL AVX512_SPR"` still produces 23
differing files. What that green run did do is surface D-2026-62: with
nothing differing, step 8 compared zero leaves and printed the sentence it
prints after comparing five thousand.

**The measurement that is not vacuous**, reproduced locally under the runner
dispatch at `f118e6a`, and the sharp test of D-2026-60:

```
compared 87 file(s); 23 differ byte-for-byte; 5404 leaves put side by side
  DECISION          0
  ZERO_CROSSING    27
  PRECISION       179   largest relative difference 3.279e-01

ZERO_CROSSING is not a precision event, and 26 of 27 are already declared
as such by the file they are in.
  BARE -- ...
    energy_ledger_cumulative_3d.csv [r2c9]: 1.615587134e-27 -> 0.000000000e+00
```

`results_gate_table.csv` is **gone from the crossing list entirely** — not
moved to DECLARED, gone, because `CH4<1.00e+03` is the same text on every
host. That was the test stated in advance for whether D-2026-60's repair
took. 28 crossings became 27; the one BARE entry is the summation-cancellation
floor left open and named there.

## D-2026-64 — a green baseline does not make a kill mean anything

**CLASS** — `GAP` in `tools/mutation_matrix.py`, the harness whose score every
completion-matrix row and every commit message on this branch quotes.

**THE QUESTION IT COULD NOT ANSWER.** The harness already refuses a RED
baseline, with the reason written out: every mutation would "fail the suite"
for a pre-existing reason and the report would read as a perfect score. It
also refuses a baseline that flakes. Both are checks on the *unmutated* tree.

Neither can see the case where the suites fail whenever the mutated FILE
changes — because it is hashed, or compared against a manifest, a crate, an
allowlist or a checked-in digest. There, every mutation is killed by the edit
rather than by the behaviour removed, and the matrix reports 20/20 having
measured nothing. A baseline check cannot detect it by construction: the
baseline tree is unedited.

**REPAIR — the null control.** Before mutating, each file in the
specification is edited in a way that changes its bytes and nothing else: a
trailing comment, which is behaviour-preserving by definition of the language.
The suites must stay green. If they do not, the run refuses with
`NULL_CONTROL_RED` and says which file and which test, because no kill in that
specification would mean anything.

It is the same refusal as the red baseline, one step along — there the suites
fail for every mutation because they were already failing, here because they
cannot tell a semantic change from a byte change.

`--null-only` runs the baseline and the control and stops, so the question
"would a kill here mean anything?" can be asked across every specification
without paying for the mutations.

**WHERE IT RUNS.** Everywhere, by construction: the control is part of every
`mutation_matrix.py` invocation, before the first mutation. 43 specifications
are invoked across the workflow, covering 114 (specification, file) pairs, so
each CI mutation step now answers the question for its own specification
rather than a separate sweep answering it for all of them once.

Verified locally by running it. An earlier full sweep was interrupted before
it finished, and its result was lost because the wrapper piped a two-hour run
through `tail` -- nothing incremental survived the interruption. The
instrument was fine; the way it was invoked threw the evidence away, which is
worth writing down next to a defect about measurements that report the wrong
thing. The per-specification runs below carry the evidence instead.

**WHAT IT DOES NOT ESTABLISH.** That the operators are strong, that they cover
the interesting paths, or that a kill was by a RELATED test. It establishes
one thing: that a kill in this specification is not merely a fact about the
file having been touched.

**WHAT THE HARNESS'S OWN SUITE SAID ABOUT IT.** Adding a suite run per mutated
file shifted every run-count-dependent behaviour by one, and
`test_mutation_harness.py` caught it immediately:
`test_fails_on_the_third_run_only` is a fixture that goes red on the THIRD
suite invocation, chosen because that used to be the post-run baseline. With
the null control it is the fourth. The fixture failed loudly rather than
quietly targeting the wrong run, which is what you want from a test that
encodes a sequence. It is now `test_fails_on_the_post_run_baseline_only`, with
`POST_RUN_BASELINE_RUN` and the five-run order written out, so the next change
to the number of invocations has to be re-derived rather than absorbed.

**AND A NOTE ON RUNNING IT.** The first attempt to sweep all 43 specifications
was orchestrated badly enough to be worth recording beside a defect about
measurements. It was piped through `tail`, so when it was interrupted nothing
incremental survived. Worse, stopping it did not stop it: killing the wrapper
shell orphaned the script, which kept walking the specifications for another
hour, concurrently with a verification run — producing test errors and a
checker failure that were artifacts of two processes writing one tree, and
leaving `qta_multiphysics/verification.py` carrying an injected null-control
line that a manifest regeneration then hashed.

Twice in that sequence a clean `git status` was read as evidence that the
writer had stopped. A clean tree at one instant is not the absence of a
writer; it is the gap between two writes. The harness has a collateral-damage
detector for exactly this, and running two of them at once defeats it. Same
substitution as everything else in this ledger: a proxy read for the
property.

## D-2026-65 — who may invoke a credentialed agent, on a public repository

**CLASS** — `TRUE_DEFECT`, security, in `.github/workflows/claude.yml`.

`cakeisalie89/Quantum-Thermal-` is **public**. The job condition was the
mention alone:

```yaml
if: |
  (github.event_name == 'issue_comment' && contains(github.event.comment.body, '@claude')) ||
  ...
```

and the job it guards holds

```yaml
permissions:
  contents: write
  pull-requests: write
  issues: write
  id-token: write
env: ANTHROPIC_API_KEY
```

So any GitHub account, with no association to this repository whatsoever,
could start a credentialed agent with write access to it by leaving a comment.

**THE HALF THAT WAS ALREADY CLOSED.** The checkout step reasons carefully
about the other door, and says so:

```
# DO NOT add `ref:` here. This job can be triggered by a comment on a
# pull request from a fork, and it holds secrets. Checking the PR head
# out at the workspace root would run untrusted code with
# ANTHROPIC_API_KEY in the environment
```

Untrusted CODE beside the secret was identified and refused. Untrusted
INSTRUCTIONS reaching an agent that holds the secret is the same exposure
through the other door, and nothing was closing it.

**REPAIR.** The condition now also requires
`author_association ∈ {OWNER, MEMBER, COLLABORATOR}`. That value is computed
by GitHub from the actor's relationship to this repository and is not settable
by the commenter. Those three are the people who already have write access, so
the agent is reachable by exactly those who could make the same change by
hand. Everyone else is ignored silently — the job does not start. Fail-closed
by construction: an association this list does not name (CONTRIBUTOR,
FIRST_TIME_CONTRIBUTOR, NONE) or one GitHub adds later does not match.

**ON THE UPSTREAM ACTION.** `anthropics/claude-code-action` may well perform
its own permission check. That is a property of a third-party SHA, read from
its own repository, and the authorization of an agent that can push to this
one should not rest on a default nobody here has read. This is the rule the
rest of the package applies to proxies, applied to its own CI.

**NOT CLAIMED.** That this is the only exposure in the workflow surface. What
was checked, by reading rather than assuming: no `pull_request_target`
anywhere; no `${{ github.event.* }}` interpolated into any `run:` block (the
only expressions in shell are `matrix.*`, which come from the workflow file
itself); `permissions: {}` default-deny at workflow level with per-job grants;
`persist-credentials: false` on every checkout; every action pinned by SHA
with its tag recorded, enforced by `tools/workflow_contract.py`.

## Scientific sweep — where the resolution discipline has actually reached

Not a defect. A measurement, recorded because the alternative is that the
number stays unstated.

D-2026-53 established that a serialised number must carry the resolution of
the method that produced it. The instrument that reports on it is the
DECLARED/BARE split in `tools/cross_env_semantics.py`, and it only ever
examines a column that happens to CROSS ZERO between two hosts. A column that
is tiny but nonzero on both machines is never looked at. The discipline's
apparent coverage is therefore defined by an accident of which machines ran.

Measured directly against `docs/unit_inventory.json`, over every governed
numeric column rather than the ones a divergence exposed:

```
41 governed source artefacts, 162 columns
  declare a resolution class:  4 artefacts,  19 columns
  declare none:               37 artefacts, 143 columns
```

The four are `gas_transport_metrics.csv`, `gas_transport_profile.csv`,
`surface_coverage_metrics.csv` and `surface_coverage_profile.csv` — the two
solvers a floor was defined for. 143 governed columns publish a number with no
statement of what their method could resolve.

That is the honest scope, and it is not closed here. Closing it means a
defensible floor per solver, which is numerical work and not a rendering
change — the same reason `energy_ledger_cumulative_3d.csv`'s `cumulative_dU_J`
is still open. What is recorded now is the ratio, so it is a tracked number
rather than an impression.

## Hosted evidence, `d0b85d2` and `f118e6a` (the sibling jobs)

Same-commit, step by step, never by job conclusion.

d0b85d2 (run 35032370201): dispatch-sensitivity ALL SIX STEPS GREEN on its
first successful outing -- sync passed, the guard asserting X86_V4 absent
passed, full suite on that dispatch green in 15m27s. second-interpreter
(3.13) green, cross-environment-3d green. full-suite step 5 green, step 7 the
standing byte gate, steps 8-9 green.

f118e6a (run 35042991811): full-suite SUCCESS end to end (step 7 included);
dispatch-sensitivity SUCCESS, all six steps, full suite on the AVX-512-
disabled dispatch green in 10m06s; second-interpreter (3.13) SUCCESS, fifth
consecutive.

c1bd8ea (run 35030465947): agent-substrate SUCCESS, 62/62, 4h05m, steps 51,
52 and 53 scored for the first time.

## Hosted evidence, `9de2281` — the three repairs on a runner

`full-suite` (run 35050738592), step by step. Runner dispatch, from step 6:

```
openblas runtime kernel: Haswell
numpy SIMD found: ['X86_V3']
```

No AVX-512 — the side of the dispatch the committed outputs were NOT produced
on, and the one `OPENBLAS_CORETYPE=Haswell NPY_DISABLE_CPU_FEATURES=...`
reproduces locally. So this run is the informative case, not the identical-tree
one that `f118e6a` happened to land on.

**Step 5, the full pytest suite: SUCCESS**, 15m09s. `conftest.py` and every
test added for D-2026-61..63 pass on a runner.

**Step 7, package consistency: one failure, and it is R59.**

```
[PASS] the produced output set is exactly the 89 declared canonical outputs
       88 of them are compared byte-for-byte in Step 2b; 1 exempt by design
...
RESULT: FAIL (1 checks failed)
  [FAIL] root canonical outputs byte-match the canonical regeneration
         23 stale root copies
```

D-2026-61's repair, first hosted run, on hardware whose regeneration differs
byte-for-byte in 23 files: the SET is exactly right while the BYTES are not.
That is the distinction the check was built to make, measured rather than
argued. The byte gate's refusal is the standing R59 condition and is neither
silenced nor exempted.

**Step 8, the cross-environment comparison: the measured basis, with its
scope in the sentence.**

```
compared 87 file(s); 23 differ byte-for-byte; 5404 leaves put side by side
  DECISION          0
  ZERO_CROSSING    27
  SIGN_FLIP         0
  PRECISION       179   largest relative difference 3.279e-01

ZERO_CROSSING is not a precision event, and 26 of 27 are already declared
as such by the file they are in.
  BARE -- ...
    energy_ledger_cumulative_3d.csv [r2c9]: 1.615587134e-27 -> 0.000000000e+00

No decision-bearing token differs between the two environments
(5404 leaves compared, 23 file(s) differing).
```

Three things at once. D-2026-62's repair: the closing sentence carries the
leaf count and the differing-file count, so it can no longer be read as the
identical-tree case. D-2026-60's sharp test, now on hosted hardware:
`results_gate_table.csv` is **gone from the crossing list entirely** — 28
became 27, 26 declared, and the single BARE entry is the summation-
cancellation floor left open and named. And 179 numbers moved, by up to a
third in relative terms, while **zero** decision-bearing tokens differed.

**Step 9, clip provenance: green**, with this runner's own counts —
210412/1596240 and 4344/115200 elements moved, max displacement 1.533143e+03
and 1.062372e+02. Host-dependent counts, reconciled as booleans, exactly as
D-2026-53 requires.

## D-2026-66 — the resolution discipline's coverage was an accident of which machines ran

**CLASS** — `GAP`, closed where a floor exists and named where none does.

**HOW IT LOOKED.** D-2026-53 requires a serialised number to carry the
resolution of the method that produced it. The only instrument reporting on
that requirement is the DECLARED/BARE split in
`tools/cross_env_semantics.py`, and it examines a column **only when the
column happens to cross zero between two environments**. A quantity that is
tiny but nonzero on both machines is never looked at.

So every "26 of 27 declared" in this ledger is a statement about the columns a
dispatch difference exposed, not about the package. Measured instead over
every governed numeric column in `docs/unit_inventory.json`:

```
41 governed source artefacts, 162 columns
  declare a resolution class:  4 artefacts,  19 columns
  declare none:               37 artefacts, 143 columns
```

**THE PART THAT WAS A DEFECT, NOT A GAP.** Of the four artefacts the
mechanism had reached, it had reached them *partially*.
`gas_transport_metrics.csv` publishes, in one row, out of one solve, against
one floor:

```
max_density_m3                   2.82e+17     (bare)
sample_region_density_modeB_m3   5.66e+16     (bare)
residual_mode_D_density_m3       0.0          BELOW_RESOLUTION
```

The residual carried its class because D-2026-53 was about the residual. The
maximum and the region mean beside it did not, and `max_theta_modeB` in
`surface_coverage_metrics.csv` did not either — while
`resolution_of_region_mean` already existed and `resolution_final` already
existed. A quantity left bare beside one that is explicitly not claiming
exactness reads as the exact quantity the other is declining to claim.

Closing the example, in the file where the example was closed.

**REPAIR.** `GasTransport1DResult.resolution_of_max()` (classified on the RAW
maximum, because a peak the clip lifted out of negative noise is not a
resolved peak and after the clip the two are the same number), and three new
columns: `max_density_resolution`, `sample_region_density_modeB_resolution`,
`max_theta_modeB_resolution`. 486 datasets now compare exactly, still
`EQUIVALENT`, 94 columns with a physical unit and 68 declaring why they have
none.

**AND THE INSTRUMENT THAT STOPS IT RECURRING.**
`docs/resolution_inventory.json` gives every governed numeric column a basis,
and `tools/resolution_inventory.py` reconciles both directions and refuses on:

- a governed column with no basis, or a basis for a column that no longer
  exists;
- a `CARRIER` naming a column absent from the artefact;
- a `CARRIER` whose values are not all resolution classes — naming a column
  is not carrying a class, the same anti-proxy rule `claims_enforcement.py`
  applies to a pattern that names a claim without matching it;
- an exemption that states no reason;
- **within-artefact incompleteness**: where at least one column of an artefact
  declares a carrier, no column of it may be `NO_FLOOR_DEFINED`. That is the
  rule that makes closing-the-example impossible here. It is what refused the
  three columns above, before they were written.

Current state, reported rather than gated:

```
162 governed numeric columns in 41 artefacts: 13 carry a resolution class
(4 artefacts), 25 are exact by construction, 2 are coordinates, 2 are floors,
and 119 have no floor defined
```

**WHAT IT DELIBERATELY DOES NOT REFUSE.** The 119. Refusing them would force a
floor to be invented for every solver in the package, and a fabricated floor
is worse than an absent one: the artefact would then state a resolution
nobody derived. They are counted, named under `--verbose`, and left open, in
the same way `energy_ledger_cumulative_3d.csv`'s `cumulative_dU_J` is.

**KNOWN LIMITATION, STATED RATHER THAN ROUNDED AWAY.** A metric/value table
declares per ROW — the PER_ROW shape D-2026-57 had to name.
`coupled_mode_recovery_metrics.csv` carries `Mode_D_residual_CH4_resolution`
on a row of its own, and this inventory cannot say "some rows of this column
carry a class". Those columns are counted as `NO_FLOOR_DEFINED`, which
UNDERSTATES them. Counting them as declared without checking which rows would
overstate, and of the two errors the understatement is the one that leaves the
gap visible.

## D-2026-67 — a knob nobody turns is a comment, and nothing checked that it was turned

**CLASS** — `GAP` in `tools/workflow_contract.py`, plus a sub-defect in its own
command extractor found on the way.

**THE CLAIM AND ITS SUPPORT.** `tests/test_agent_long_horizon.py` compiles in

```python
CYCLES = int(os.environ.get("QTA_HORIZON_CYCLES", "260"))
```

and the workflow has a step called **"long horizon at an elevated scale"**
whose comment says, in as many words, *"A knob nobody turns is a comment."*
Nothing checked that the step still turns it. Delete the assignment and the
suite runs at 260 cycles, every assertion in it still passes — they are
absolute floors, `report.count > 2000` and `total >= 1000` governed
operations, not functions of the variable — the step goes green, and its
**name** becomes a claim the run does not support.

That is the same shape as everything else in this ledger: the evidence for
"elevated" was the step's title.

**REPAIR.** `unturned_knobs()` requires at least one command the workflow
actually runs to invoke that suite with `QTA_HORIZON_CYCLES` **above** the
default, and re-derives the default from the test's own source rather than
repeating it, so raising it there cannot silently satisfy this. Setting the
variable is not raising it: `QTA_HORIZON_CYCLES=10` is refused.

**THE SUB-DEFECT.** The check failed against a workflow that does turn the
knob. `_run_commands()` returned one entry per LINE, so

```yaml
run: |
  QTA_HORIZON_CYCLES=1200 uv run python -m pytest \
    tests/test_agent_long_horizon.py -q -p no:randomly
```

arrived as two unrelated strings and no check could ever see both halves. A
command split over a backslash is one command; returning it as two was the
same substitution one level down from the defect that function was written
for (D-2026-59, where it scanned comments instead of commands). It would have
done the same to a continued `uv sync --frozen` followed by its `--group`
argument — so the group check had the hole too, unexercised only because no
command here is split that way.

Continuations are joined now, and `test_a_continued_command_is_read_as_one_command`
pins it.

**MUTATIONS.** K1 removes the check. K2 makes setting the variable count as
raising it. K3 splits continued commands again.

**AND K1 SURVIVED THE FIRST RUN.** 14 of 15 killed, with K1 -- the operator
that deletes the single line wiring `unturned_knobs()` into `problems()` --
reported as SURVIVED. Every test written for the new check called
`unturned_knobs()` **directly**. The function was covered; its USE was not.
The wiring could be deleted with the whole suite green and a test file that
looks like coverage.

That is D-2026-56's shape -- rules present, 5 of 24 actually wired -- arriving
one level up from the defect being closed: a check that nobody checks is
called. `test_the_knob_check_is_actually_consulted` drives `problems()` with a
workflow body that never raises the cycle count and requires the refusal to
come back from the AGGREGATE, not from the function. 15/15.

Worth recording plainly: nothing else in this repository would have caught it.
The suite was green, the new tests all passed, and the coverage was real --
of the wrong thing.

## D-2026-68 — a verifier that exists, is tested, has a matrix, and that nothing requires the workflow to run

**CLASS** — `GAP` in `tools/workflow_contract.py`. Found by the hostile fresh
review, against the current head rather than from memory of it.

**THE SHAPE.** K1 of the repo-contract specification made this concrete one
level down: a check can be deleted from `problems()` while every test still
passes, because the tests called the function and nothing tested its use. The
same hole exists one level out, for the tools themselves.

Delete the workflow step that runs `tools/test_isolation.py`, keep its
mutation step, and every contract check is still satisfied — the tool exists,
its tests pass, its specification is wired — while the 107 collections it
performs never run again. **A mutation matrix scores a tool's TESTS. It never
takes the tool's verdict on the actual repository.**

**MEASURED.**

```
15 tools/*.py are mutated by some specification
13 are runnable verifiers
 1 is named in REQUIRED_COMMANDS   (completion_matrix.py)
```

Eleven were being run and nothing said they had to be, among them
`unit_inventory.py`, `claims_enforcement.py`, `identity_inventory.py`,
`model_check.py` and `cross_env_semantics.py` — all of which predate this
session — and the two added in it.

**REPAIR.** `unwired_verifiers()` re-derives the candidate set from the
specifications on disk rather than from a list, so a verifier added with a
matrix is covered without anyone remembering. Two exemptions, each with its
reason stated in the code: `mutation_matrix.py` IS the harness, and
`independent_verify.py` is a subprocess spawned by `separate_verify.py` with a
log path on argv rather than a standalone gate.

**AND THE MATRIX CORRECTED THE REPAIR.** V2 SURVIVED, and it was right to.

The first version asked whether the tool's path appeared in a command that was
not a `mutation_matrix` invocation. But a specification is passed as
`tools/mutations/x.json`, which does not contain `tools/x.py` — one ends
`.json` and the other `.py`. Verified against the real workflow: no command
invokes the harness while naming another tool's `.py` path. **The clause could
never fire.** The test written for it could not discriminate either, which is
why it passed against both the mutant and the original.

An enforcement point that cannot be wrong is indistinguishable from one that
is absent. That is the same sentence as everything else in this ledger, and it
was in the repair rather than in the subject.

The risk that DOES exist is the opposite one, and the guard did nothing about
it: `ruff check tools/test_isolation.py` mentions the tool and runs nothing in
it, and a substring test counts that as the verifier having reported on the
tree. The check now matches the SHAPE OF EXECUTION — `python <path>` or `uv
run python <path>` — which handles the harness case structurally, because a
specification argument is never captured as an executed path.
`V2_a_mention_counts_as_a_run` now has something real to detect, and
`test_mentioning_a_verifier_is_not_running_it` kills it, with
`test_a_verifier_that_is_executed_counts` as its control. 18/18.

**AND THE LESSON APPLIED BEFORE IT HAD TO BE TAUGHT AGAIN.**
`test_the_unwired_check_is_actually_consulted` drives `problems()` rather than
the function, written when the check was written rather than after a survivor
pointed at it. V1 mutates the wiring away and dies.


## Architecture convergence, tranche 1 — Phase 0 and Phase 1

The directive of this tranche: turn the repository into a governed
scientific-agent framework with replaceable scientific models, retire the QTA
hardware ontology as an authority, and begin with Phase 0 (baseline, file
dispositions, dependency cutover) and Phase 1 (trust-boundary defects) only.
Nothing was deleted or moved. `ARCHITECTURE_CONVERGENCE_PLAN.md`,
`FILE_DISPOSITION.csv` and `DEPENDENCY_CUTOVER.md` are the Phase 0 record;
the CSV is generated by `tools/file_disposition.py` from an explicit rule
table with no catch-all, and `tests/test_file_disposition.py` fails when a
file has no disposition. The entries below are Phase 1 and the two defects
the baseline surfaced.

## D-2026-69 — a running sum published without the resolution of its method (OPEN)

**CLASS** — `GAP` in `qta_multiphysics/campaign_state_3d.py`
(`attach_energy_ledger`) and `tools/resolution_inventory.py`. Found during
the D-2026-66 sweep; recorded here rather than carried in memory.

`energy_ledger_cumulative_3d.csv` publishes cumulative energy as running sums.
The forward error bound of a running sum is `n·eps·Σ|tᵢ|`, which at row 2 is
8.1e-27 J; the hosted comparison at `71b58cb` reports that row's
`1.615587134e-27 -> 0.000000000e+00` as the one BARE zero crossing -- both
values below the method's resolution, and the file says nothing about it.
`resolution_inventory.py`'s within-artefact rule keys on the artefact as a
proxy for the method, which is why it did not ask. **Deferred**: the repair
(a resolution basis for passthrough and accumulated columns, naming its
source) changes a byte-gated output, which is Phase 4-6 work on this file's
disposition (`campaign_state_3d.py` is `REWRITE_GENERIC`).

**STATUS UPDATE (Phase 2)** -- the generic requirement is MET; this entry
stays **OPEN**. `scientific.quantity.Quantity` carries value, unit,
resolution, resolution class and basis, reporting digits and uncertainty
class, and tells an exact zero from a value below resolution. The original
quantity is representable: `1.615587134e-27` J and `0.0` J, each with the
running sum's 8.1e-27 J `ACCUMULATED_BOUND`, both classify
`BELOW_RESOLUTION`, compare as indistinguishable, and neither prints as a
result (`test_the_d_2026_69_crossing_is_one_result_not_two`). What is still
wrong is the artefact: `energy_ledger_cumulative_3d.csv` publishes the digits
without the basis, and moving it onto the mechanism is the Phase 4-6 work on
`campaign_state_3d.py` this entry names.

## D-2026-70 — seventeen reducers verified one read of the log and folded another

**CLASS** — `DEFECT`, trust boundary, `qta_agent/`. The directive named the
pattern; the sweep found how far it reached.

**THE SHAPE.**

```python
log.verify().raise_if_bad()
for ev in log.read():
    apply(ev)
```

Two reads of a file other processes append to. A record landing between them
is folded without its chain link ever being checked by the call folding it.
D-2026-41 closed it in `governed_stage10.projection` and, as the phrase goes,
nowhere else. It stood in:

* the `load()` of `agents`, `capability`, `idempotency`, `memory`, `netauth`,
  `policy`, `secrets`, `scheduler` and `store`;
* the full-read fallback of `store._fold_new` and `scheduler._fold_new` --
  the path taken after damage forced the anchor away, i.e. exactly when an
  unchecked record does most harm;
* `AuditIndex.from_log` (twice: the full index and the windowed one);
* all three reconstructors in `reconstruct.py`, which reported the head of
  the verified read and replayed the second -- the independent reader
  disagreeing with itself;
* **`EventLog.advance`**, the O(new) catch-up every live projection uses: it
  called `verify_from(anchor)` and then opened the file again to read the
  tail for the caller. The anchor it returned was built from the second read,
  so a forged record became the trusted prefix for every later call;
* **`AuthorityStore.load_from`**, which verified the tail through
  `checkpoint.verify_with` and folded `log.read_from(anchor)`, under the
  comment *"The anchoring record is in the tail this load already reads and
  already verified."* It was the tail a second read returned.

The name-matching scan the sweep started with found fifteen of these and
missed the last two: it matched `verify` followed by `read`, and those two
were `verify_from` followed by `_read_tail` inside one method, and by
`read_from` in another. Reading the code found them. A scan is a proxy.

Beyond the pattern: three helpers in `governed_stage10` -- `_tool_of`,
`_execution_record`, `_artifacts_of` -- read the log with **no verification
at all**. `_tool_of` chooses the tool spec a task is verified against and the
compensator that undoes it, so an appended record with a broken link could
choose its own checker.

**REPAIR.** One pass everywhere. `read_verified()` for the whole chain,
`read_verified_from(anchor)` (new) for a tail, `checkpoint.read_verified_with`
(new) for a checkpointed load; `advance` and `verify_from` share one private
`_verified_tail`. The events returned are the **verified prefix**: a failed
report used to come back holding every parsed record, including the ones it
refused, and a caller that looked before -- or instead of -- raising got them.
`read_from`, an unverified tail parse meant to be "paired with" `verify_from`,
is removed: a primitive whose correct use needs a second primitive and a
promise is the window with an API. The governed helpers go through one
`_verified_events()`, which `projection()` shares, so G_R1's anchor still
names a single site.

**TESTS.** `tests/test_agent_snapshot_coherence.py` opens the window on
purpose: after the FIRST read returns, it appends a self-consistent record
(valid own hash) whose chain link is broken, then asserts that every record
the reducer receives is, byte for byte, a line of the snapshot the
verification read -- the directive's "exact bytes reconstructed are the bytes
verified", checked on bytes rather than counts. Every test also proves the
window opened and that the forgery really breaks the chain. 25 tests; against
the pre-repair code all 25 fail -- 23 on their assertions, 2 because the
primitive they exercise does not exist there.

**MUTATIONS.** `tools/mutations/agent_snapshot_coherence.json` restores the
second read at each of the 17 sites plus the three prefix rules: **20/20
killed**, null control green. Ten mutations in five existing specifications
were re-anchored to the new code with their meaning unchanged: M16, M20, M30
(`agent_substrate`), N6, N10 (`agent_incremental`), L3 (`agent_execution`),
A1, Q1 (`agent_audit`), S22, E27 (`agent_checkpoint`).

## D-2026-71 — a checkpoint of one reducer restored into another

**CLASS** — `DEFECT`, `qta_agent/store.py`, `qta_agent/checkpoint.py`.

A snapshot at seq K is what the reducer of its day made of 0..K. The snapshot
carried `snapshot_version: 1` and nothing about the code that produced it, so
`load_from` restored it and folded K+1..N with whatever reducer was running
now. Change `_apply` -- a branch, a validation, a field folded differently --
and the result is a state no version of the code computes from the log.
**Same event log + changed reducer semantics does not imply an old projection
remains valid.**

**REPAIR.** `qta_agent/projection.py` (new): `ReducerIdentity` =
`projection_kind`, `reducer_id`, `reducer_version`, `reducer_digest`,
`projection_schema_version`. The digest is sha256 over the source of every
module defining a class in the projection's MRO plus every in-package module
those import, found by AST so imports inside functions count -- `store.py`
imports `checkpoint` lazily, and a top-level walk misses it (RI5). The
snapshot (schema v2) carries the identity inside the pinned bytes, so the
`checkpoint.state` record in the log covers it. `load_from` compares it before
restoring anything; a different reducer, an older schema, or no digest on
either side replays from genesis -- or refuses under `require_checkpoint` --
and `checkpoint_refusal` says why. "Cannot tell" is never "same".

**LIMITS, STATED.** The digest over-approximates: a comment edit invalidates
every checkpoint. That is the only direction a proxy for semantics may err in.
It cannot see a behaviour change arriving through a dependency outside the
package; `REDUCER_VERSION` is the human declaration for that, and a test
proves a bump alone invalidates.

**TESTS.** In `tests/test_agent_checkpoint.py`: a subclass whose `_apply`
ignores `legacy` records loads a base-reducer checkpoint and must equal its
own genesis replay, not the restored state; the reverse direction; the
control (the same reducer still uses its checkpoint); `require_checkpoint`;
version bump; unreadable source; lazy-import coverage; MRO coverage. **AND A
TABLE THAT WAS MEASURING ONE LINE.** `test_a_malformed_snapshot_is_refused`
held eight malformed version-1 snapshots. Under v2 every one was refused by
the version check before its own malformation was looked at, and the suite
stayed green -- eight tests measuring one line. They are rebuilt on a valid
v2 base, with that base as a control.

**MUTATIONS.** RI1-RI7 in `agent_checkpoint.json`: identity ignored, missing
digest matches, a snapshot claims the base reducer, the leaf class only, a
top-level-only closure, restore without identity, version bump invisible.

**AND THE MATRIX FOUND THE K1 SHAPE IN THE REPAIR.** First run: 42/43, and
RI4 SURVIVED -- `identity_of` digesting only the leaf class's module, so a
subclass that inherits the reducer is identified by a file containing none of
the code that folds its events. The test written for it asked `mro_modules()`
and passed against the mutant, because the mutant stopped CALLING
`mro_modules()`. The helper was covered; its use was not. The same lesson as
K1 and D-2026-67, one layer further in. `test_editing_the_inherited_reducer_changes_the_subclass_identity`
now edits the base reducer's source as the digest sees it and requires an
inheriting subclass's identity to change; RI4 dies to it.

## D-2026-72 — a summary of something that was never provided, and an omission invented for it

**CLASS** — `DEFECT`, provenance, `qta_agent/context.py`.

`ContextBuilder.add` checked that `summary_of` looked like a digest and that
`summarizes_item` came with one. Nothing checked the item existed or that the
digest was of its bytes. `build()` then walked the replaced items and, for a
source nobody had added, **wrote an omission anyway**: tier guessed as
`RETRIEVED_EVIDENCE`, content digest `""`, length 0, reason "replaced by a
summary; the full text was not shown" -- a durable record, in the manifest
that `record_context` appends to the log, of material that never existed.

**AND A TEST PINNED IT.** `test_a_replaced_source_is_recorded_as_an_omission_with_a_pointer`
added a summary of `"long"`, never added `"long"`, and asserted the invented
omission. It is rewritten: the source exists, is left out for budget while
its summary is shown, and the omission carries the source's own digest,
length and tier.

**REPAIR.** At build (a summary may precede its source): the source exists
and is not the summary itself; `summary_of` equals the digest of the source's
exact bytes; one source, one summary. At add: `summary_of` without a source
item is refused -- the builder can only vouch for a digest of bytes it holds.
The fabrication branch is gone; with every source real, a source is either
shown or omitted for budget and there is no third case to invent. The
manifest READER states the rule independently, because manifests are durable
and older ones exist: an omission must carry a real digest, and one that
names a replacing summary must name a shown item whose `summary_of` is its
content digest.

**TESTS / MUTATIONS.** 11 new tests and the one rewritten; the 8 refusals
among them all fail against the old code; X5 re-anchored (its loop only ever fired for a phantom) and
X14-X21 added, X21 being the wiring: the check exists and `build()` stops
calling it. `agent_memory_context.json` **48/48 killed**.

## D-2026-73 — ten interlocks that `python -O` deleted, and the guard that could not see them

**CLASS** — `DEFECT`, `qta_full_sim.py`; `GAP`, `tests/test_optimized_mode_invariants.py`.

`SystemState.validate()` enforced IL-01..IL-10 with `assert`. Measured:

```
python     -c '...SystemState("X", LCVD_on=True, sensing_on=True).validate()'  -> refused IL-01
python -O  -c '...same...'                                                      -> IL-01 NOT enforced
```

and the self-test built for exactly this prints `correctly blocked: {caught==1}`,
which under -O is `False` followed by a check mark.

D-2026-45 converted the gate-table asserts and added a guard: no assert in
`qta_agent/` or `tools/`, and the scientific tree's self-checks "pinned". The
guard scanned three directories, and `qta_full_sim.py` is at the root. And the
pin compared the SET OF FILES holding asserts while its docstring said "the
count is pinned", so a second assert in a classified file joined the first
unseen.

**REPAIR.** The interlocks raise `InterlockViolation` explicitly. It
subclasses `AssertionError` only so callers written against the old `assert`
keep catching it -- the machine FSM retires in Phase 5 and churning its
callers first buys nothing; what matters is that `-O` cannot remove it. The
four mode preconditions and the multiphysics status check raise `ValueError`.
`qta_full_sim.py` now has no assert. The guard scans every tracked production
file through `tools/repo_scope`, allows asserts only in the scientific tree,
and pins them per file by count; a `-O` subprocess probe proves IL-01 holds
under both builds.

**NOT CHANGED, AND WHY.** The multiphysics status check sits inside
`except Exception: print(WARNING)`, so a forbidden status drops the whole
layer from the gate table rather than failing the run -- the same as before,
explicit or not. `package_consistency_check.py`'s gate count catches it
downstream. It retires with the orchestrator (Phase 6), and is recorded
rather than fixed so this tranche does not change what `qta_full_sim.py`
emits.

**MUTATIONS.** `tools/mutations/enforcement_asserts.json` (new): an interlock
back to an assert, the guard back to a directory list, the pin back to a set.
**3/3 killed.**

## D-2026-74 — a trust test depends on a package nothing declares (OPEN)

**CLASS** — `GAP`, `pyproject.toml`. Found by the Phase 0 baseline.

`tests/test_bootstrap_workflow_contract.py` and
`tests/test_release_workflow_contract.py` import `yaml`. PyYAML is in no
dependency group; it arrives through `snakemake` -> `conda-inject` / `yte`
(workflow group). A local environment synced without that group fails
collection; CI passes because it syncs `--all-groups`. The Phase 6 workflow
rewrite would remove the only thing providing a dependency two trust tests
need, and nothing would say why they broke. **Deferred**: declaring it in the
`dev` group regenerates `uv.lock` and, through it, the SBOM; that belongs in a
change reviewed on its own, before Phase 6.

**STATUS UPDATE (Phase-1 closure)** — repaired. PyYAML is declared in the
`dev` group (`pyyaml>=6.0`; the lock already resolved 6.0.3 through
snakemake, so no version moved -- `uv.lock` gains two lines naming it in
`dev`). Not promoted to runtime: two tests import it and nothing else does.
Measured, not argued: an environment built by a bare `uv sync --frozen`
(runtime + `dev`, no `workflow`) from the **old** lock fails both modules at
collection with `No module named 'yaml'`; from the new lock, the same two
modules pass 39 tests with `snakemake` absent. `tools/dependency_declarations.py`
now checks the class rather than the instance: an import executed at import
time must come from runtime + the default groups; any other import must be
declared somewhere or be named as environment-supplied with a reason. Against
the replaced `pyproject.toml` it reports exactly these two sites. See
D-2026-77 for what regenerating the derived records found.

## D-2026-75 — a shown summary's source was not written down, so the reader could not check it

**CLASS** — `DEFECT`, trust boundary, `qta_agent/context.py`. The durable
half of D-2026-72.

D-2026-72 made the builder refuse a summary whose source was never provided
and made the reader refuse an omission it could not account for. But
`ContextItem` persisted `summary_of` (a digest) and **not which item** it
summarized. When the source was shown too, the reader had a digest and
nothing to compare it with: tamper `summary_of` on a manifest where both
items are shown, and the read-back accepted it. The claim "this summary is
of those bytes" survived the round trip as a string nobody checked.

**REPAIR.** `ContextItem.summarizes_item` is persisted; the manifest carries
`manifest_version = 2`. The reader refuses an unknown version, a version-1
record that claims any summary relationship (v1 cannot prove one), and a
version-2 key in a version-1 record. For every claim it checks: ids unique
and non-empty across shown and omitted items; each digest a digest; the named
source exists, is not the summary itself, and is recorded with exactly the
digest claimed; no source claimed by two summaries; an omitted source's
`summarized_by` points at the summary that names it, with that summary's
digest -- not at another summary of identical bytes.

**TESTS.** 23 new or rewritten in `tests/test_agent_context.py` (70 pass),
including an 11-case table of forged relationships. Against the pre-repair
code, 17 fail -- the both-shown tamper among them, with `DID NOT RAISE`.

**MUTATIONS.** `agent_memory_context.json` X22-X33 (the durable item drops
its source; the builder records another item or another digest; each reader
check skipped in turn; v1 may claim): the spec runs **60/60 killed**, null
control green.

## D-2026-76 — verify-then-read was repaired at every site and prevented at none

**CLASS** — `GAP`, `qta_agent/events.py` and the tree. The prevention half
of D-2026-70.

D-2026-70 repaired twenty sites and `test_agent_snapshot_coherence.py` proves
them -- by name, from a table. A table says nothing about the twenty-first
reducer. `EventLog.read()` stayed public, unverified, and one keystroke from
`read_verified()`.

**REPAIR.** Two guards, because each alone is a proxy.

* **Static** -- `tools/verified_read_guard.py` walks the parse tree of every
  tracked production file (not `tests/`, not `attic/`; `qta_agent/events.py`
  is where the primitives live) and refuses `RAW_READ` (`<log>.read()`, or
  iterating a log, or `list()`/`sorted()` of one), `TAIL_PARSE`
  (`._read_tail`), `READ_FROM` (the removed primitive, called or defined)
  and `FILE_READ` (`<log>.path` opened directly). "Log" is decided
  syntactically and the rule says so; same-function aliases are followed.
  One allowlisted site, pinned by count with its reason: the fuzz harness's
  parser target, where the unverified parse is the thing under test.
* **Runtime** -- `EventLog.read()` and `__iter__` refuse any caller whose
  module is under `qta_agent.` other than `qta_agent.events`, decided by the
  calling frame rather than by what the caller named the object, so an alias
  the static rule cannot follow still stops. The refusal,
  `UnverifiedReadRefused`, is deliberately **not** an `EventLogError`:
  reducers catch that to fall back to a full read, and a refusal must not
  become a quiet fallback. Tests, tools and diagnostics may still parse the
  log -- they fold nothing into authority state -- so forensics is unchanged.

**ANTI-VACUITY.** Run against `71b58cb` (before D-2026-70), the static
guard reports **20 sites in 12 modules**: every reducer load and fallback,
both audit indexes, the three reconstructors, the three unverified
`governed_stage10` helpers and `load_from`'s `read_from`. It cannot see
`EventLog.advance`, which is inside `events.py` and out of its scope by
design; the snapshot-coherence tests hold that one. Every agent suite passes
with the runtime guard in place (the full-suite run is in the plan's
checkpoint report).

**TESTS / MUTATIONS.** `tests/test_verified_read_guard.py`, 36 tests: 16
dangerous shapes, 8 controls that must not be flagged, the real tree, the
allowlist pin, a stale allowlist entry failing the verifier, and the runtime
refusal from a synthetic `qta_agent.*` caller with its controls.
`tools/mutations/verified_read_guard.json`: **11/11 killed**.

**THE INVARIANT.** Every event folded into authority state belongs to the
exact verified byte snapshot that established its integrity.

## D-2026-77 — two derived records described an older lock, under a disposition that said "every change"

**CLASS** — `WRONG_CLAIM`, my own, in `tools/file_disposition.py` (Phase 0);
and `GAP`, `stage7_reports/dependency_inventory.json`.

Regenerating the records downstream of `uv.lock` for D-2026-74 found that
neither had been regenerated since before Stage 10. The Stage-9 SBOM names
the lock of `f922022` (`3597…`); `365b5c8` changed the lock and left the
bundle alone, so the SBOM lacks the three packages Stage 10 brought in
(`contourpy`, `cycler`, `dill`). The dependency inventory said 71 locked
packages (there are 89) and omitted `h5py`, a declared runtime dependency.
The Phase-0 disposition called both `REGENERATE`, "every change". Nothing
checked either, which is how both stayed wrong while the table said otherwise.

**REPAIR.** The inventory is now derived: `tools/dependency_declarations.py
--write` builds it from `pyproject.toml` + `uv.lock` (versions, classes,
count and both source digests; hand-written reasons are carried forward, new
entries take pyproject's own comment and the files that import them), and a
test holds the committed file to that output. The Stage-9 bundle is **not**
regenerated in place: its SHA256SUMS, index and provenance bind it to a
release zip and a revision, and rebuilding it here would name ones that do
not exist. Its disposition now says what it is -- a historical Stage-9
record (`RETIRE_TO_HISTORY`), superseded by the bundle `release.yml` builds
from `uv.lock` at release time and by the Phase-7 set -- except
`release_trust_policy.json`, which `release.yml` reads live and is kept.

**MUTATIONS.** `tools/mutations/dependency_declarations.json`: **8/8
killed** (the one that first survived -- a lazy import of a real but
undeclared package -- was killed by a test added for it).

## D-2026-78 — a wall-clock bound that a fast process could outlive and still complete

**CLASS** — `DEFECT`, `qta_agent/execution.py`. Found by cut C1, not by
looking for it.

`run_bounded` waits for the child in 50 ms slices and looks at the deadline
only when a slice times out. A process that exits PAST its deadline but
inside one slice was classified by its exit status alone: exit 0, COMPLETED.
A run that outlived its declared bound was accepted as a completed run.

It stayed hidden because every governed tool started slowly.
`test_a_timed_out_run_never_reaches_completed_or_verified` gives the Stage-10
tool a 1 ms bound and says "no Python interpreter starts in a millisecond, on
any runner", which is true -- but what made the run TIME OUT was not the
interpreter: importing `qta_multiphysics` loaded the whole orchestrator, about
a second of imports, so the tool was always still running at the first deadline
check. C1 made the package import nothing; the tool now exits in tens of
milliseconds, inside the first slice, and the execution came back
COMPLETED (the task then ended FAILED at a later check, not TIMED_OUT). The
Commit-B full suite failed on it,
deterministically -- three reruns alone, 0.4 s each, all red. Not a flake, and
not the test's fault: the test's premise was right, the executor's
enforcement was not.

**REPAIR.** The exit time is recorded, and a run that would otherwise be
COMPLETED (exit 0, no signal) but exited after its wall bound is TIMED_OUT,
with the elapsed time in the reason. Narrow: a failure or a signal keeps its
own classification. The polling interval is unchanged -- the bound is now
checked at exit, which is the check that was missing, rather than approximated
more finely.

**TESTS.** `test_a_run_that_exits_after_its_bound_is_not_completed`
(`python -c pass` under 1 ms: fails on the pre-repair executor, passes after)
and its control `test_a_run_inside_its_bound_still_completes`; the Stage-10
timeout test is deterministic again.

**MUTATIONS.** `agent_execution.json` gains
`L_BOUND_a_late_exit_counts_as_completed`.

**SIBLING SWEEP.** The other bounds this function enforces are kernel limits
(CPU, address space, output size, process count), applied by `setrlimit` in
the child and enforced by the kernel whatever the timing; the idle bound is
checked in the same slice loop and is a liveness rule for a process still
running, which is the case it is defined for. The wall bound was the only one
whose check depended on observing the process alive.

## D-2026-79 — the verify-then-read runtime guard crashed the governed production rule

**CLASS** — `DEFECT`, mine, introduced by D-2026-76 in `f18b5f0`; found by
hosted CI on that commit, not by any local run.

`_refuse_unverified_caller` read the calling frame's `__name__` and called
`.startswith` on it. Snakemake runs a rule body with `__name__` = None, and
the `s10_governed` rule reads the log directly for its assertions, so the
guard raised `AttributeError` inside the governed production rule. Hosted at
`f18b5f0`: both stack-verify jobs red at "Stage-10 workflow", and the
agent-substrate job red at "the governed production path actually runs". The
local validation of that commit ran the full pytest suite, PCC, isolation,
every verifier and 289 mutations -- and not `snakemake s10_governed`, which is
the one command that executes rule code. That omission is the finding as much
as the crash is.

**REPAIR.** A caller whose module name is not a string is treated as what it
is -- not the authority layer -- and may look. `tools/verified_read_guard.py`
now says in its docstring that the Snakefile is outside its scan (Snakemake
syntax is not Python `ast` parses), and what the Snakefile's four raw reads
are: assertions over the run's history in `s10_governed` and
`s10_governed_index`, folding nothing.

**TESTS / MUTATIONS.**
`test_a_caller_with_no_module_name_is_not_crashed` (`__name__` None and 42):
fails on the `f18b5f0` guard, passes after. `verified_read_guard.json` gains
VG12. Validation from here on runs `snakemake --cores 1 s10_governed` and
`s10_full` locally, as the two workflows do.

## D-2026-80 — a matrix row said two entry points open the governed-writer scope; five do

**CLASS** — `WRONG_CLAIM`, mine, introduced in `776fe97`; and `WRONG_CLAIM`,
older, in `.github/workflows/stack-verify.yml`. Found while updating R55 for
the third governed workflow, not by any check.

R55's second boundary says `write_text_deterministic` refuses a writer in the
governed-only subtrees that is not inside a `governed_writer` scope, "which
only the two tool entry points open". Since `776fe97` the scientific-model
tools `scientific/_governed_run.py` and `scientific/_governed_check.py` open
it too, and this tranche adds `scientific/_governed_identity.py`: five, not
two. The scope's own docstring ("opened by the governed tool entry points and
by nothing else in the production tree") stayed true; the row's count did
not, and I did not re-read the row when I added the first two. Separately,
stack-verify's scope comment called `s10_full` an "11-step" DAG; it was 13
jobs before this change and is 14 after it.

**REPAIR.** The row names the governed tool entry points and says which
they are; the comment gives the job count Snakemake reports for the DAG
(14, three governed). Neither the guard nor its enforcement changed -- the
model tools write only outside `GOVERNED_ONLY` today, and what they write is
read back by digest from the evidence store, not from the workspace.

**NOT DONE.** Nothing checks prose counts like these against the code. The
spec count in R51 is checked against the workflow (D-2026-47); these two are
not, and are recorded as fixed rather than as prevented.

## D-2026-81 — R49 counted seven timed guards when two were left, and I copied the count

**CLASS** — `WRONG_CLAIM`: R49's residual in `docs/completion_matrix.json`,
stale since `f7cb50b` (D-2026-46); and mine, repeated into
`ARCHITECTURE_CONVERGENCE_PLAN.md` section 9.3 in Phase 0 without opening the
test file. Found while preparing the first R49 conversion this tranche.

R49's residual said "seven guards still assert on a wall-clock ratio: the
four LINEAR_CEILING comparisons, the scheduler-readiness one, and the two
checkpoint/evidence comparisons". D-2026-46 had converted all but two of
them to counting re-hashes -- append, `verify()`, `AuthorityStore.load()`,
`reconstruct()`, `AuditIndex.from_log()`, incremental verification, the
checkpoint-versus-replay comparison and the governed-operation guard -- and
named the two it left timed in the test file itself: scheduler readiness
and evidence lookup. The row was not updated with that entry; the count in
it described the file before D-2026-46. My plan quoted the row.

A stale count here has a direction: it overstates what is left to do, which
reads as caution. It also made the next step look like a campaign of seven
conversions when it was two, one of which the file declared impossible.

**THE ONE DECLARED IMPOSSIBLE WAS NOT.** The scheduler guard was left timed
because `ready_queue` "hashes nothing, so there is no work unit to count".
It hashes nothing, and everything it does is per job: one job record out of
the job map, one policy evaluation, and no history read. Counted, linearity
is an exact equality again (33 jobs -> 33 records examined and 33 policy
evaluations; 266 -> 266 and 266; zero re-hashes at both sizes), and a
planted quadratic -- readiness totting up in-flight resources, a scan of
the whole queue, per job -- reads n + n^2 on the same probe. What the
counter does not see is pure computation that touches none of the three
units; the equality says so rather than claiming to be stronger than the
ratio in every respect.

**REPAIR.** R49's residual and plan 9.3 now say one guard remains timed,
evidence lookup, and why it has no unit in this code (its cost is a path
lookup in a fan-out directory, a filesystem property). The row stays
`DEEPLY_IMPLEMENTED_WITH_RESIDUAL_GAPS`.

**NOT DONE.** As with D-2026-80: nothing checks a prose count of guards
against the test file. The R51 spec count is checked (D-2026-47); this kind
is not, and this entry records a correction, not a prevention.

## D-2026-82 — a mutation run stopped during its null control left the control in the source

**CLASS** — `DEFECT`, `tools/mutation_matrix.py`. Found by this tranche's
own local run: I stopped a matrix with SIGTERM, compared the working tree's
diff digest with the one taken before the run, and it differed.

`qta_agent/authority.py` -- a file nothing in this tranche edits -- ended in
the null control's line ("# mutation-harness null control: this line
changes the bytes of this file and nothing it does"). There was no
`.mutation-recovery.json`, and `--recover` said "nothing to recover". R51
says a recovery sidecar and SIGTERM/SIGINT handlers restore sources; they
did, for mutations. The null control was added later and placed BEFORE the
sidecar is written and before the handler is installed, so for the whole
null-control phase a stop took the default SIGTERM action -- the process
dies, no `finally` runs -- over a target that had just been edited. The
harness's own comment above the handler says exactly that SIGTERM does not
run `finally`; the null control was put where that comment did not reach.

The line is inert by construction, which is why it is a comment. It is
still an unrestored edit to a tracked source that nothing reports: a
manifest regenerated over it, or a `git add -A`, commits it.

The same stop killed the suite's pytest mid-test, and a probe file a test
in `tests/test_agent_substrate_isolation.py` plants and intent-adds was left
behind (`qta_multiphysics/_shadow_sweep_probe.py`); its cleanup is a
`finally` in the test, which a killed process does not run either. That is
a property of stopping any test run, not of the harness, and the digest
comparison is what found both.

**REPAIR.** The recovery check, the sidecar and the signal handlers now
come before the null control, and the null control runs inside the one
`try/finally` that restores every target and removes the sidecar -- every
write to a target happens under it.

**TEST.** `test_a_run_stopped_during_the_null_control_restores_the_source`:
the synthetic suite SIGTERMs the harness when, and only when, it sees the
null control's line. It fails on the previous harness (the line survives)
and passes after; it also records whether the sidecar already existed at
the moment of the stop, since a SIGKILL there would leave nothing else to
recover from. **MUTATIONS.** `mutation_harness.json` gains
`H_NC1_the_null_control_runs_without_a_sidecar` and
`H_NC2_the_stop_handler_is_not_installed`.

**RECOVERED HERE.** Both leftovers were undone by hand -- `git checkout
HEAD -- qta_agent/authority.py`, and the probe removed from the index and the
disk -- and the diff digest then matched the pre-run one again.

## D-2026-83 — a scientific result's admission was decided once, live, and replayed as authority

**CLASS** — `DEFECT`, `qta_agent/store.py` and `qta_agent/reconstruct.py`.
Recorded as part of the trust-closure work this entry belongs to; reproduced
against `f003e38` before any of it was written.

Tranche 3's content rule put the question "does the cited
evidence support this scientific result" on the store's edge into VERIFIED
and PROMOTED -- in `AuthorityStore.transition`, the LIVE path. Replay did
not ask it. `load()` re-authorizes every transition against the state
machine (roles, separation of duties, digest-shaped evidence) and then
applied the edge; so did snapshot restore, and so did the independent reader
in `qta_agent/reconstruct.py`, whose restated rules are the same machine.
A record.transition line appended to the log directly -- the writer the
replay re-authorization exists to catch -- moved a `scientific_result` into
VERIFIED on a report that says FAIL, and both readers agreed with it.

Reproduced at `f003e38` on a genuine governed history (thermal 1D run and
checked as governed tasks): the live transition citing a FAIL report is
refused ("the check reported FAIL"); the same transition appended to the log
reloads as `VERIFIED` in the store and `VERIFIED` in the second reader, with
nothing unauthorized. Presence in the log was authority, for the one record
kind whose authority depends on what its evidence says.

The content rule also read content only. A well-formed PASS written into the
evidence store by hand -- the genuine report's own fields in other bytes --
satisfied it, live or replayed; plan 9.7 recorded that residual.

**REPAIR.**
* `result_rules` states the rule as a named, versioned ADMISSION POLICY,
  `scientific_result.admission/1`: exact field sets, a PASS about this
  bundle from an independent implementation establishing independent
  numerical agreement and nothing more, a simulation result whose invariants
  all hold -- and ORIGIN: the report captured by a governed verification task
  before that task's own VERIFIED verdict and still VERIFIED at the
  transition, the bundle likewise by a governed model run, and the check
  executed by none of the proposer, the decider and the run's executor.
  A transition records the policy it was admitted under; a name this code
  does not know, or none, is refused on replay rather than re-read under the
  current rule.
* The store re-decides every such admission on replay and on snapshot
  restore (SNAPSHOT_VERSION 3). Evidence that resolves and does not support
  the transition is a refusal, like any unauthorized transition. Evidence
  that cannot be read here -- archived, lost, no evidence store, no view of
  governed execution -- is not a pass and not a refusal: the record keeps
  its position and reads UNVERIFIABLE, which is not canonical and not
  reused.
* Origin is the governed-execution view in `governed_model` (the store sits
  below the task layer, so it is injected): one verified read of the log,
  folded by the task projection's own reducer, judged AS OF the transition.
* The independent reader decides the same admission again in its own code:
  its own field sets, policy set, tool sets and canonical digest; its
  content rule as a table of questions; origin from its own task replay,
  which now keeps when each artefact was captured and who each execution
  record names. `compare()` compares admission.

**FOUND ON THE WAY, BEFORE COMMIT.** My first origin check asked only that
the report be an artefact of a VERIFIED check task. The task projection
does not fold `task.evidence` at all, so one appended record could attach a
hand-written report to a check task verified long before -- and the check
passed. Origin now requires capture BEFORE the task's verdict, in both
readers, and the forgery is a test (`late-evidence`). Judged at the head, the
same check would also have refused to load a history whose check task was
invalidated after the result was admitted; it is judged at the transition.

**TEST.** `tests/test_scientific_authority_replay.py`: a genuine history
admitted by both readers (control); twelve forged histories -- wrong subject,
FAIL, NOT_RUN, producer's own code, experimental validation, a failed
invariant, a forged PASS, a check decided by its own executor, late
evidence, a check invalidated first, an unknown policy, no policy -- each
refused by both, with the store's reasons shown to differ; a forged
promotion; lost evidence UNVERIFIABLE to both and canonical to neither;
snapshot restore re-derived; the auditor refusing without evidence; and the
second reader's content table checked question by question against the
store's. **MUTATIONS.** `scientific_authority_replay.json` (new, 25);
`authority_result_rules.json` re-anchored and extended (17).

**NOT DONE.** Actors in this log are names, not keys: a writer who appends a
complete, rule-abiding governed lifecycle under other names is refused by no
replay here, for this record kind or any other. And the task projection
still folds a `task.execution` record appended after a task's verdict --
the executor it reports changes; admission measures the executor at the
verdict instead. Both are in plan 9.7.

## D-2026-84 — "nine actions have no second reader" was seven from the day after it was written

**CLASS** — `WRONG_CLAIM`: ledger follow-up 0, written in `7dc9a1f`; and
mine, copied into `ARCHITECTURE_CONVERGENCE_PLAN.md` section 9.4 in the
Phase-1 closure (`f18b5f0`) without running the tool that measures it.
Found when this tranche's follow-up A ran `tools/identity_inventory.py`
before writing a line.

The follow-up listed nine durable actions with no independent reader.
D-2026-29 (`3d809f0`, the next day) gave two of them one -- `agent.claim`
and `task.compensation` -- and updated the inventory, which has measured the
number from the second reader's parse tree ever since: 31 of 38, seven
missing. The follow-up and my plan kept saying nine. Like D-2026-81, the
stale number points the safe way -- it overstates what is left -- and like
it, it made the next piece of work look larger than it was.

**REPAIR.** Follow-up A done: the seven have readers (D-2026-85 is what one
of them found), follow-up 0 and plan 9.4 A are closed, and the inventory
reads 38 of 38. **NOT DONE**: nothing checks a prose count of this kind
against the tool that measures it; the step title is checked, the ledger's
own text is not.

## D-2026-85 — provisioning a secret made every reducer on the log refuse the whole history

**CLASS** — `DEFECT`, `qta_agent/actions.py`. Found by follow-up A's first
test: appending a genuine `secret.provision` record to a governed history
made the task projection raise `UnknownAction` -- "no module in this package
writes it".

`SecretStore.provision` writes `secret.provision` (`ACT_SECRET_PROVISION`),
and the action registry every reducer consults to tell another subsystem's
event (FOREIGN: skip it) from one nothing writes (UNKNOWN: refuse the log)
did not list it. It was the only `ACT_*` constant missing. So the moment a
deployment provisioned a secret onto a shared log, the authority store, the
task projection, the scheduler and every other reducer that meets it on the
way past refused to load that log at all -- fail-closed, and total.

Nothing noticed because no test put a provisioning record on a log that
anything else then read. The identity inventory enumerates every `ACT_*`
constant; nothing held the registry to the same list.

**REPAIR.** Registered. **TEST.** `test_every_durable_action_is_registered`
holds the registry to every `ACT_*` constant in `qta_agent/`, parsed from
source; `test_a_provisioned_secret_does_not_stop_the_other_readers` is the
consequence, end to end. **MUTATION.**
`second_reader_audit_actions.json` `AR_REG_secret_provision_is_unregistered`.

## D-2026-86 — an execution record appended after the verdict renamed the executor, in all three readers

**CLASS** — `DEFECT`, `qta_agent/governed_stage10.py` (the task
projection), `qta_agent/reconstruct.py` (the independent task reader) and
`qta_agent/audit.py`. Recorded in D-2026-83's NOT DONE and plan 9.7;
reproduced against `fafa7c4` before the repair.

The execution record is the one durable statement of WHO RAN a task, and
the executor is what verification must differ from. Every reader folded it
wherever it appeared. On a genuine governed run (`stage10.emit_artifact`,
VERIFIED), one appended `task.execution` -- a copy of the genuine payload,
written by the verifier -- reloaded with the task projection's
`executed_by` equal to the verifier and the second reader's equal to the
verifier: two readers, one renamed executor, and `compare_tasks` empty,
because a shared defect is exactly what a differential comparison cannot
see. The auditor took the LAST execution record as the executor in its
separation-of-duties check, so the same line could silence a real
self-verification finding (a history where one actor ran and verified its
own work, then a record naming somebody else) or invent one (an honest
verdict, then a record by the verifier). Scientific admission was not
reached -- both of its origin views already take the executor as of the
verdict -- which is why this was recorded as a residual and not a hole in
admission.

**REPAIR.** `tasks.check_execution`: an execution record is accepted only
while the task is EXECUTING, and only from the actor holding its lease --
the one point the governed runner writes it, between the tool's run and its
outcome. The plan's "done when" said LEASED/EXECUTING; LEASED is narrower
than it reads (the tool has not started), so the rule is EXECUTING alone.
The task projection refuses (TaskTransitionError); the reconstruction
restates the rule in its own code and records the refusal as unauthorized
without folding it (with `reauthorize=False`, the gate off, it folds, as
every other gate there does); the auditor reports an execution record
outside EXECUTING as a gap and judges the executor as of the verdict.
Retries are unaffected: a requeued task is LEASED and EXECUTING again, by
its new holder.

**TEST.** `tests/test_task_execution_phase.py`: the genuine run as control
(the record sits between the move into EXECUTING and the outcome, by the
lease holder, and no reader objects); a record after the verdict by the
verifier, by a stranger and by the worker itself, refused by the
projection, refused and not folded by the reconstruction (the executor and
the execution list unchanged), reported by the auditor; hand-built
lifecycles with the record while QUEUED, while LEASED, and in EXECUTING by
a non-holder, refused by both task readers; the auditor judging the
executor as of the verdict in both directions. **MUTATIONS.**
`task_execution_phase.json` (new, 10).

## D-2026-87 — a scientific result stayed VERIFIED when the governed task it rested on was invalidated

**CLASS** — `GAP`, `qta_agent/governed_model.py`, `qta_agent/invalidation.py`.
Plan 9.7 "Admission does not follow invalidation".

Admission is judged where the transition stands (D-2026-83), so a check or
model-run task invalidated afterwards left the result VERIFIED and ADMITTED
-- right about the history, silent about the present. Reuse refused it
(it asks whether origin holds now); the record itself and anything
depending on it did not know. Nothing in the governed path moved a
task to INVALIDATED at all: the edge existed in the task machine and only
tests wrote it, straight to the log.

**REPAIR.** `GovernedStage10.invalidate` writes VERIFIED -> INVALIDATED
through the gate (SYSTEM, with the reason). `GovernedModelRuns.invalidate_task`
follows it: every `scientific_result` citing an artefact that task
captured before its own verdict -- the bundle of a model run, the report of
a check -- goes STALE when its origin no longer holds NOW (a second
governed task that produced the same bytes keeps it standing, reported as
kept), and so does every record depending on it, by the same transitive
walk a record origin gets (`invalidation.plan_from`, which calls
`plan_invalidation` per root rather than restating it). The STALE record
cites a stored `{"origin", "reason"}` (`task:<id>` or `evidence:<digest>`).
`withdraw_evidence` starts from an artefact -- a bundle, a report, or any
file a model run produced -- and invalidates every task standing VERIFIED
that captured it before its verdict: the bytes are content-addressed
history and stay; what gave them authority is withdrawn. REVOKED, REJECTED
and undecided records are reached and left, never moved. Nothing already
written changes; both readers replay the result to the same states.

The task move and the STALE records are separate appends, so a writer that
stops between them leaves a result VERIFIED on a dead origin.
`GovernedModelRuns.settle` follows every INVALIDATED task again, citing it;
a task already followed reaches nothing, so settling twice is settling once.
Nothing calls it automatically: it is the recovery step, as
`GovernedStage10`'s stranded-task recovery is.

"Model artifact withdrawn" is read here as an artefact the model RUN
produced. Withdrawing a model VERSION from the registry -- every result any
run of it produced -- has no representation in this repository, and is not
claimed.

**TEST.** `tests/test_scientific_invalidation.py`, on one genuine
thermal-1D history with two decided results, copied per case: the control;
the producing run invalidated; the check invalidated; the STALE record's
citation resolving to what changed; the report withdrawn; a model artefact
other than the bundle withdrawn; bytes nobody vouched for, and bytes
attached to a task after its verdict, refused; transitive through two
dependent records; an unrelated task changing nothing but its own result; a
second origin keeping the result standing until the report itself is
withdrawn; REVOKED and REJECTED left as they are; a task not standing
VERIFIED refused, with nothing written; the stale result neither reused nor
re-admitted (re-verification asks origin again, at its own position); the
history's prefix byte-identical; an interrupted invalidation settled, and
settling again a no-op. Every case replays through the store, the
independent reader, and both task readers, with no refusal and no
divergence. **MUTATIONS.** `scientific_invalidation.json` (new, 14).

## D-2026-88 — replay built one view of governed execution per admission, and the last timed guard compared one store with itself

**CLASS** — `GAP`, `qta_agent/governed_model.py`, `qta_agent/store.py`,
`qta_agent/reconstruct.py`; and `METHOD`, `tests/test_agent_performance.py`.
Plan 9.7 "Replay cost" and plan 9.3 / R49's remaining timed guard.

Every scientific admission is re-decided on replay (D-2026-83), and origin
is part of it. Each origin question built its own view -- one verified read
of the whole log and one task fold -- and asked twice per admission, once for
the report's producers and once for the bundle's. A load was O(n*k) in
admitted results, and nothing measured it. Counted on a history of five
admissions with sharing switched off -- which leaves every question building
its own view, as before this change -- one load builds ten. The independent
reader already built its task replay once per reconstruction, but nothing
counted it, so nothing would have noticed it stop.

**REPAIR.** `GovernedOrigins.shared()`: in the block, the first question
builds the view and the rest reuse it; nested blocks share the outer view.
The view is of the whole history and every question is still asked AS OF
its own position, so sharing changes the cost and not the answer -- the same
history loads to identical records, admissions and bases with and without
it (tested). The store asks for one view per load, per catch-up (anchored
and full) and per snapshot restore, where its origin view offers one; a
single question shares between its two lookups; reuse and the invalidation
cascade ask every record of one view. Counters: `questions` and
`views_built` on the origin view, `admissions_decided` and `task_replays`
on the independent reader's reconstruction. Five admissions now cost one
view; with sharing switched off the same probe reads ten.

The evidence-lookup guard was the last one timed, and the plan's own reason
held: its cost is one name lookup in a fan-out directory, the filesystem's
work. So it is split in two. The part that belongs to this code is COUNTED:
a lookup opens the same descriptors at 100 blobs and at 800 (300 for 100
lookups, three each) and enumerates no directory entry; a planted lookup
that confirms the blob by listing its bucket reads 272 entries at 100 blobs
and 778 at 800 -- the unit that sees directory growth, which the plan asked
for, reading zero. The filesystem's part stays timed, with the method
changed: it compared the same blobs read before and after filling ONE
store, the same-size-two-times shape that failed a hosted run elsewhere in
the file. Now two stores hold the same probe blobs under the same names,
one with 700 more; they are measured alternately, five rounds each, and each
keeps its best. It is not recorded in `docs/performance_baseline.json`:
a recording names the commit it measured, and this was measured on an
uncommitted tree.

**TEST.** `tests/test_replay_origin_view.py` (new), on a genuine history of
three results and five admissions: a load, a snapshot restore, a full and an
anchored catch-up, one question, a reuse search over two candidates and an
invalidation cascade asking after two results each build one view; the
probe reads one view per question with sharing off; the independent reader
replays tasks once for five admissions; answers identical either way.
`tests/test_agent_performance.py`: the lookup counter, its control, and the
re-measured timed guard. **MUTATIONS.** `replay_origin_view.json` (new, 11);
`performance_counters.json` (3 -> 5: a lookup that lists its bucket, one
that searches the store). Two anchors moved with the code they mutate and
were re-anchored unchanged in meaning: `agent_snapshot_coherence.json`
`V_store_fallback_reads_the_log_twice` and `scientific_authority_replay.json`
`SA5_a_snapshot_is_inherited`.

**NOT DONE.** The timed guard's margin on a hosted runner is still not
recorded; no CI job publishes performance numbers.

## D-2026-89 — actors were names: an authentication seam, with production keys left external

**CLASS** — `GAP`, `qta_agent/principals.py` and `qta_agent/ed25519.py`
(new), `qta_agent/store.py`, `qta_agent/reconstruct.py`. Plan 9.7 "Actors
are names, not keys"; recorded in D-2026-83's NOT DONE.

Every reader re-authorizes a history against the state machines, and the
hash chain makes it tamper-evident. Neither makes it authentic. A writer
holding the file can rewrite it end to end -- every record re-hashed, the
head witness too -- and it verifies (`test_the_chain_alone_does_not_see_a_
rewrite` shows exactly that); it can append a complete, rule-abiding
lifecycle under any actor's name, and every reader agrees with it.

**REPAIR, AS A SEAM.** An ATTESTATION binds one event's hash -- which
covers its position, the previous hash, actor, action, target and payload --
to an Ed25519 signature under a domain of its own. A KEY REGISTRY, supplied
from outside the log (a key registered in the log it authenticates is chosen
by whoever writes that log), says which principal each key speaks for; key
ids are derived from the keys, never chosen. `principals.authenticate`
refuses, for every event whose actor must authenticate: no attestation, an
actor with no key, a key nobody registered, a key registered to another
principal (actor substitution, a wrong key), a signature that does not
verify (tampered signature or payload, a rewritten history, a signature
made for another purpose), an unreadable attestation line, and an
attestation of an event the history does not contain. Attestations live
beside the log, not in it: the log refuses any field its hash does not
cover, and adding the signature to the hashed body would change the
canonical form every existing log was written in. The authority store,
given an `Authenticator`, refuses an unauthenticated history on load and on
every catch-up, and restores from a checkpoint by replaying in full (a
snapshot says nothing about who wrote the records before it). The
independent reader, given one, records each refusal and does not fold the
event. Without one, both read names as they always have.

The primitive is Ed25519 from RFC 8032, standard library only because the
substrate imports nothing else, held to the RFC's vectors 1-3 and to the
refusals a verifier owes (another message, a flipped bit, another key, a
short input, the malleable s + Q). It is not constant time: fine for
verifying public data and signing with test identities, not a signer for a
production key.

**PENDING, EXTERNAL.** No production key exists in this repository and none
is invented: `principals.PRODUCTION_KEYS` is unset and
`production_registry()` refuses (`NotConfigured`) instead of returning an
empty registry that would authenticate nothing and look as though it had.
The only keys are deterministic TEST identities, whose secrets anyone can
derive from their names; their key ids are marked, and a registry not built
for tests refuses them. Nothing in the governed path signs yet, and the task
projection and scheduler do not authenticate; provisioning keys, a
production signer and the writers that use it are deployment work.

**TEST.** `tests/test_actor_authentication.py` (new, 23). **MUTATIONS.**
`actor_authentication.json` (new, 19).

## D-2026-90 — the run identity could not see the numeric backend

**CLASS** — `GAP`, `scientific/run_identity.py`. Plan 9.7 "The run identity
does not see CPU dispatch"; R59.

The environment in a RunIdentity was the interpreter and the numpy and
scipy VERSIONS, read from metadata. R59 is the counterexample: the same
versions on a runner whose CPU has a different SIMD set dispatch a
different kernel and change digits. A different thread count (the order of
a reduction), a dispatch or core-type override, and another native build of
the same version do the same. Reuse was within one history on one machine,
which kept it from biting; the identity still said two such runs were the
same computation.

**REPAIR.** The environment record now carries the backend, read without
importing numpy: the CPU's SIMD features and core identity from
`/proc/cpuinfo` (microcode, virtualisation and mitigation flags left out --
they change no kernel, and would make every patched host a different
machine; a CPU that cannot be read names its host, so the run is reused
only there); the CPU count and every thread, dispatch and core-type variable
a numeric library reads; and each distribution's native build -- every
extension module and bundled library, numpy's OpenBLAS among them, by the
digest its own wheel RECORD holds. `environment_record(environ=...)`
describes the environment a governed tool actually ran in, which is pinned
(`OMP_NUM_THREADS=1` and its siblings), and the governed reuse test now
compares against that rather than the test process's own.

**TEST.** `tests/test_run_identity_backend.py` (new, 15): each part of the
backend changed -- a SIMD feature, the core, a thread count, a dispatch
override, a core-type override, the native build, an unreadable CPU --
changes the environment digest and the RunIdentity, and `may_reuse` refuses
naming `environment_digest`; a mitigation flag and a variable no kernel
reads change nothing; the native digest follows the wheel's recorded hashes
of compiled files only; the record is read in a fresh interpreter with
neither numpy nor scipy imported. **MUTATIONS.** `run_identity_backend.json`
(new, 9).

**NOT DONE.** Reuse is still refused, not reconciled, across backends: there
is no equivalence policy saying when two backends agree well enough for a
given model (directive 11). The identity says "different"; it cannot yet say
"different and equivalent".

## D-2026-91 — the long campaign's start line was a sleep, and its anti-vacuity check counted labels

**CLASS** — `TEST_DEFECT` (instruments of a test), `tests/test_agent_cross_process.py`.
Not R59: nothing numerical is involved, and no scheduler code changed.

**DISCOVERED BY.** Hosted CI at `b683977`, PR run 36338018443, job
`second-interpreter (3.13)` 108672503268:
`test_a_long_mixed_campaign_never_leaves_an_unreplayable_log[250]` failed

```
the campaign only ever recorded ['DISPATCHED', 'READY', 'SUCCEEDED']
+  where 3 = len(Counter({'READY': 19, 'DISPATCHED': 19, 'SUCCEEDED': 12}))
```

The chain verified and the queue rebuilt -- both assertions come first and
passed. The same code passed the same job on `ced0a42`, and the same test in
two other jobs on `b683977` itself. Sixty-two local campaigns and a fully
serial one never produced fewer than four destinations.

**DEFECT.** Two, both in the campaign's own instruments, and an ordering.

1. The anti-vacuity rule counted distinct DESTINATION labels and wanted four.
   That run's 19 moves into READY were 12 promotions out of WAITING and 7
   DISPATCHED -> READY requeues: a lease lapsing and the job being recovered,
   seven times, invisible to a count of labels because READY had already been
   reached on the ordinary path. The assertion's own message named a requeue
   as enough; its measure could not see one. Whether a fourth label appeared
   depended on a TRANSIENT report landing while its sender still held the
   lease, or a budget running out, before all twelve jobs finished -- on the
   schedule, not on the code.
2. The campaign worker did not wait at the start line. And the start line was
   not a rendezvous for any racer in the file: the parent slept 1.5 s and
   released, so a worker still spawning or importing when the sleep ran out
   was released on arrival, and how concurrent a "race" was depended on how
   fast the runner started processes.
3. The safety assertions -- a holder for every DISPATCHED job, no job past its
   retry budget -- ran after the anti-vacuity one, so a run both thin and
   unsafe would have been reported only as thin.

**REPAIR.** The measurement and the start line; the scheduler is unchanged,
because nothing in the evidence points at it.

* The start line is a rendezvous. `_wait_for_start` ANNOUNCES (one marker per
  arrival) and then waits; `_line_up` releases only once every worker has
  announced. A line that never fills fails rather than starting whoever
  turned up, and a worker that died before arriving is reported with its own
  error. `_run` uses it for every racer, and so does the staged forgery test
  that had its own sleep. `_stress_worker` waits at it after opening its
  scheduler and before its first operation.
* Safety first. The chain is verified and the queue rebuilt, then
  `_judge_campaign` checks every job's safety, and only a safe campaign is
  then judged for vacuity.
* Coverage on EDGES: READY -> DISPATCHED and DISPATCHED -> SUCCEEDED, and at
  least one way off the ordinary path OUT OF DISPATCHED -- a requeue
  (-> READY), a retry (-> RETRY_WAIT) or a failure (-> FAILED). Only scheduler
  transitions count, and repeating an edge adds nothing.

**TEST.** Deterministic controls, on real single-process scheduler histories:
the ordinary path alone is not coverage; the ordinary path plus a requeue,
plus a retry, plus a failure each is, and each control is checked to contain
exactly the one recovery edge it claims. On synthetic records: the ordinary
path repeated a hundred times, task and authority records shaped like
requeues, recovery-shaped edges that do not leave DISPATCHED, and each
ordinary edge missing, are each not coverage. The order: a history both
unsafe and thin is reported UNSAFE; safe and thin, VACUOUS. The start line: a
worker arriving after 2 s -- later than the old sleep -- finds the race not
yet started; the campaign worker writes nothing before it is released; a line
that never fills fails without releasing; a worker that dies before the line
is reported as itself. The module went from 25 tests in 17.5 s to 44 in about
7 s: the sleeps were most of its time.

**EVIDENCE.** 80 local campaigns with the rendezvous, 40 unconstrained and 40
pinned to two CPUs: all pass. Every one recorded at least two requeues (at
least six unconstrained). The old rule would also have passed all 80 -- none
fell below four destinations -- and twelve sat exactly at its minimum, one
label from the hosted failure: which is why it never reproduced here.

**MUTATIONS.** `stress_campaign_coverage.json` (new): the start line on both
sides, the coverage measure, and the safety judgement's order and content,
15/15. `agent_cross_process.json` re-run against the new campaign, 11/11.

**NOT DONE.** Coverage is still sampled, not arranged: a campaign is judged
on what its interleaving happened to exercise, and a schedule with no
requeue, retry or failure at all is possible in principle -- it would now be
reported as VACUOUS by name, with its safety already checked. Generating
cross-process schedules deterministically would need control of the kernel's
scheduling -- R50 states that boundary.

## D-2026-92 — the reference signer could stand in for production, and authentication was optional by omission

**CLASS** — `GAP`, `qta_agent/principals.py`, `qta_agent/events.py`,
`qta_agent/ed25519.py`. Directive 6, sections 34-38 and 41.

Three things the actor-authentication seam (D-2026-89) left open:

1. **The primitive had no assurance boundary.** `qta_agent/ed25519.py` -- a
   pure-Python transcription of RFC 8032 -- was the only implementation, and
   nothing distinguished "the oracle the tests use" from "what production
   trusts". Constant time was the only stated caveat; the others are larger:
   one unaudited transcription, point-encoding and non-canonical-input edge
   cases, the small-order behaviour vetted libraries themselves disagree
   about, the malleability class, and a review surface nobody else maintains.
2. **Authentication was optional by omission.** A reader handed no
   authenticator read every event. A caller could skip authentication by
   leaving out one argument, and nothing in a history said it had to be.
3. **Coverage was unstated.** The store and the independent reader could
   authenticate; whether any other reader could was not recorded anywhere.

**REPAIR.**

* `qta_agent/signature.py` (new): a provider states its scheme and its
  ASSURANCE. `REFERENCE_ONLY` verifies and signs only TEST identities -- a
  `Signer` holding a production key refuses to sign through it. Production
  authentication (`principals.production_authenticator`) needs a provider
  that states `VETTED` and passes a conformance gate judged on BEHAVIOUR --
  RFC 8032's vectors 1-3 (key, signature, verification) and every refusal a
  conforming Ed25519 makes: another message, a flipped bit, another key, a
  short signature, a short key, the malleable `s + Q`, and a verifier that
  raises rather than answering is refused too -- and a provisioned registry.
* THE PROFILE (`qta_agent/events.py`): a history is written under
  `UNAUTHENTICATED_LEGACY` (every existing log, read exactly as before) or
  `AUTHENTICATED_REQUIRED`, declared by its FIRST event
  (`principals.begin_history`, attested) and by no other. It is enforced in
  `read_verified` and `read_verified_from`, the primitives every reader in
  `qta_agent` reads through: a REQUIRED history gives a reader with no
  authenticator no events; with one, any finding fails the report and the
  read ends before the first refused event; a writer's own head check is the
  same gate, so nothing is appended on top of an unauthenticated event. A
  declaration past genesis or of an unknown profile is refused on write and
  on read. A deployment that pins the profile (`EventLog(...,
  required_profile=...)`) refuses a history rewritten without its
  declaration -- a DOWNGRADE, which the declaration alone cannot stop, like
  the chain and its external witness. The profile a log object remembers
  is genesis's, remembered only once there IS a genesis: a draft of this
  commit also remembered the answer an EMPTY history gave, so a reader that
  had looked before the first event kept "legacy" and folded a REQUIRED
  history's anchored tail with no authenticator. Found while building the
  signed append on top of it, before any push; tested and mutated (AH22).
  And a reader given no authenticator refuses ANY non-empty REQUIRED
  history -- an anchored read whose tail is empty included, because that is
  exactly a writer's own head check: the same draft let a writer holding an
  anchor extend a REQUIRED history without ever authenticating its head
  (found the same way; AH23).
* THE INDEPENDENT READER restates the profile in its own code (literals, not
  imports): with the primitive's gate switched off, it still folds nothing of
  a REQUIRED history it was given no authenticator for, and refuses a
  misplaced declaration. `history.security_profile` is a registered action
  and an inventoried one: 39 of 39 durable actions independently read.
* COVERAGE: `principals.READER_COVERAGE`, reader by reader. Fourteen readers
  in `qta_agent` are GATED (two also give their own per-event verdict);
  three tools that open their own log can only REFUSE a REQUIRED history;
  the hypothesis lifecycle is NOT_BUILT. A structural test holds that every
  module reading the log is in the table -- and `tools/verified_read_guard.py`
  already holds that none reads it any other way.

**TEST.** `tests/test_authenticated_history.py` (new, 38): the reference
conforms and is refused for production; a provider saying VETTED that
accepts forgeries, raises, or signs what the RFC does not is refused by name;
a conforming stand-in is accepted; production refuses without a registry and
without a vetted provider; the authenticator's verdict is its provider's. A
legacy history reads unchanged; a REQUIRED one reads nothing without an
authenticator and everything with one; an unattested event ends the read and
blocks the next append; declarations are genesis-only and known, on write and
on read (forged past the write guard); the anchored read is gated, also for
a reader that saw the history before its genesis; a writer with no
authenticator cannot extend a REQUIRED history, its own head included; a pinned
deployment refuses a downgrade. Eleven readers refuse without and read with;
three tools refuse; the independent reader refuses on its own.

**MUTATIONS.** `authenticated_history.json` (new) 23/23;
`actor_authentication.json` re-run against the provider seam, 19/19.

**NOT DONE, and not claimed.**
* No VETTED provider is configured: choosing and declaring one
  (`cryptography`, libsodium) is a supply-chain decision for the deployment.
  EXTERNALLY_BLOCKED, with the seam and its gate ready.
* No production key or registry exists (D-2026-89): EXTERNALLY_BLOCKED.
* The governed writers do not sign: every subsystem appends with the plain
  `append`, so today a REQUIRED history is written only through
  `signed_append`, and an unsigned append to one is caught by the next read,
  not refused at the write. The attestation is written after the event,
  leaving a crash window. Both are the next commit's (directive 6, s.36).
* The three tools that open their own log cannot yet be given an
  authenticator; the registry they would need is not yet pinned (s.39).
* A downgrade is refused only where the deployment pins the profile.

## D-2026-93 — a signed append was two unordered writes, the registry was any file, and a key had no life

**CLASS** — `GAP` and `CRASH_SEMANTICS`, `qta_agent/principals.py`,
`qta_agent/events.py`, `tools/fuzz_substrate.py`. Directive 6, sections 36,
39, 40 and 42.

1. **A signed append was two appends in the wrong order.** The event was
   written, then its attestation. A crash between them left a durable,
   unattested event: an AUTHENTICATED_REQUIRED history that failed closed on
   every read afterwards, with no way to tell a crash from a forgery, and a
   repair -- attesting it afterwards -- that would sign whatever a writer had
   put there. And an unsigned append to a REQUIRED history was caught only
   by the next reader.
2. **The registry was trusted because it parsed.** An external file is not
   a trusted registry: a file beside the log is as writable as the log.
3. **A key had no life.** Rotation, revocation and compromise had no
   representation; a key registered was a key valid for every event ever,
   and removing it would have unauthenticated history it had rightly signed.
4. **Neither parser had been fuzzed.** Run against the parsers as they stood before this change -- a scratch export of the CB tree, 5000 cases each, seed 20260927 -- the fuzzer found both wanting: the attestation reader authenticated three files whose only difference from the genuine one was an uppercase hex digit in a signature (the same signature bytes, a record no signer wrote), and the registry reader crashed with `TypeError` on four documents whose `keys` was not a list. The seven inputs are in the regression corpus, each refused now.

**REPAIR.**

* PREPARE FIRST. `EventLog.append` and `append_decided` take `before_write`,
  called with the exact record -- seq, hash and all -- under the writer lock
  and before its bytes are written; if it raises, nothing is written.
  `signed_append` and `begin_history` use it to make the attestation durable
  first (`Attestations.prepare`). A crash therefore leaves one of four states,
  each with one meaning: nothing; a torn final attestation line (a prepare
  that did not finish -- truncated by the next prepare); a complete
  attestation one past the history (a prepare whose record never landed --
  PENDING, not a finding, and ABORTED by the next prepare with an explicit
  abort record); or the record and its attestation. What cannot arise is a
  durable record without its attestation. Two appends are not atomic and
  nothing here claims they are; the order is what is arranged.
* NO FALSE AUTHENTICATION. A pending prepare authenticates nothing but the
  record it signed; two prepares cannot both be pending; an abort naming a
  COMMITTED event is itself a finding (ABORTED_COMMITTED); a torn line that is
  not the last is MALFORMED; every attestation field is typed and bounded
  before it is used.
* ONE ENCODING. Every hex field -- an attestation's signature and event
  hash, an abort's hash, a registry key -- is exactly its length in
  LOWERCASE hex, the one encoding a signer writes. `bytes.fromhex` also
  takes uppercase (and spaces), so one signature had many records, and a
  record no signer wrote authenticated as though one had. Not a forgery; a
  second encoding of one, which a record of who signed what must not have.
* NO CRASH ON NESTING. A line nested deep enough to exhaust the JSON parser
  raised `RecursionError` out of the attestation reader -- a crash of every
  authenticating reader from one line of an attacker-writable file. It is a
  MALFORMED line now. Found by review, not by the fuzzer, whose mutators had
  not produced such depth; it is a seed of both campaigns now.
* UNSIGNED WRITES REFUSED. An append to a history whose profile is
  AUTHENTICATED_REQUIRED -- declared, or pinned by the deployment -- without a
  prepared attestation is refused before anything is written, and so is an
  unsigned declaration of that profile.
* THE REGISTRY IS THE ONE THE DEPLOYMENT PINNED. `registry_digest` is the
  sha256 of the document's canonical form; `from_document(pinned_digest=)`
  refuses a document that differs before believing a single entry; production
  needs `PRODUCTION_REGISTRY_DIGEST` as well as `PRODUCTION_KEYS`, and neither
  is configured. `from_bytes` maps every decoding failure -- nesting deep
  enough to exhaust the parser included -- to a refusal. A key "registered"
  in the log's own events authenticates nothing: the history does not choose
  its registry.
* A KEY'S LIFE, IN LOG POSITIONS. Registry v2 entries carry
  `valid_from_seq`, `valid_until_seq`, `revoked_at_seq` with
  `revocation_reason`, `compromised_from_seq` and `replacement_key`, and a
  `status` that must agree with them. Positions, not wall time, so every
  replay agrees: a rotation hands over at a position; a revocation refuses the
  key from its position on and leaves earlier authorship standing; only a
  declared compromise reaches back, and only as far as declared; a
  replacement must be a key of the same principal. v1 documents still read:
  every key valid from genesis, for good.
* FUZZED. `attestations` (against a fixed genuine history; the oracle is that
  nothing authenticates without its genuine attestation) and `key_registry`
  (a document is refused, or every key id derives from its key), both in the
  bounded CI campaign and required by the fuzz test; the findings from before
  the fix are committed to the regression corpus.

**TEST.** `tests/test_signed_append_lifecycle.py` (new, 51): the attestation is on
disk before the record; unsigned appends and declarations refused; each crash
point -- before the prepare, during it, between the writes, during the record,
after both -- leaves a history that authenticates, or fails only on the log's
own torn tail, and the next append repairs it; nothing false is authenticated;
replay is deterministic; ill-typed lines, a signature or key in another
encoding, and nesting that exhausts the parser are malformed or refused, not
crashes; registry bytes that are not a document are refused. Rotation,
expiry, not-yet-valid, revocation and compromise each refused or kept at the
right position; incoherent lifecycles refused; the registry round-trips at its
pin, refuses tampering, and production needs the pin. Three CB tests change
with the premises they tested: production authentication needs the pin as
well as the file; an unattested event in a REQUIRED history is now written
past the write-side guard, as a holder of the file could; and
`Attestations.read` returns an `AttestationFile` (by hash, malformed,
aborted, torn tail) rather than a pair.

**MUTATIONS.** `signed_append_lifecycle.json` (new) 27/27.

**NOT DONE, and not claimed.** No production key, registry, pin or vetted
provider exists (EXTERNALLY_BLOCKED). The governed writers do not sign yet,
so no governed history is AUTHENTICATED_REQUIRED today. A revocation is only
as timely as the deployment's registry update. Multi-host writers are outside
this (the lock is local; open item 71).

## D-2026-94 — the run identity read the backend from metadata, and reused what it could not name

**CLASS** — `GAP`, `scientific/run_identity.py`, `scientific/backend_probe.py`
(new), `scientific/model.py` and the three models. Directive 6, sections
43-50. Not R59 itself: this changes what an identity records and when a run
is reused, and no number, tolerance or canonical output.

What D-2026-90 left the identity unable to say:

1. **The native build was RECORD, not bytes.** Each distribution's compiled
   code was identified by the digests its wheel's RECORD holds. A library
   replaced after installation leaves RECORD unchanged, so the identity
   called the replaced build the same computation; and a file that could not
   be read was indistinguishable from one that matched.
2. **Nothing was asked of the running stack.** Which SIMD loops NumPy
   dispatched to on this CPU, which kernel each bundled OpenBLAS selected,
   and which C, math and loader libraries the process runs on are runtime
   decisions -- R59 is one of them -- and metadata cannot see them.
3. **An unknown backend was reusable.** Nothing distinguished "the backend is
   known and identical" from "neither run could say what it ran on"; two
   identities agreeing only in not knowing were reused as the same.

**REPAIR.**

* INSTALLED BYTES. `native_record` hashes every native file's current bytes
  beside RECORD's digest (cached per path, size, mtime and inode within a
  process), and states the result: `RESOLVED`; `MODIFIED` naming the files
  whose bytes no longer match RECORD; or `UNRESOLVED` naming the files that
  could not be read.
* THE RUNTIME PROBE (`scientific/backend_probe.py`, new). In the WORKER that
  runs the model -- NumPy is imported inside `runtime_record`, never at
  import time, so the top-level scientific interfaces still import no NumPy
  (shown in a fresh interpreter) -- the probe records NumPy's dispatched CPU
  features and SIMD configuration; every OpenBLAS the numeric distributions
  bundle, found through their own RECORD (NumPy's ILP64 build and SciPy's
  LP64 one: `scipy.linalg` runs on the second), loaded and asked its selected
  kernel and build configuration, and hashed; the C and math runtimes the
  loader resolves, the OpenMP and Fortran runtimes where present, and the
  dynamic loader, each by its bytes. It is ORDER-INDEPENDENT: an identity is
  computed in one process and a run in another, and the first draft, which
  read the process's own mappings, named SciPy's OpenBLAS in a process that
  had imported SciPy and NumPy's in one that had not. The test runs it in
  the same process before and after importing SciPy, and in fresh processes
  that load SciPy first and not at all. Anything it cannot determine makes
  the runtime `UNRESOLVED` and is named; a process with no NumPy installed
  has no NumPy backend and says so. What it can IDENTIFY is a BLAS its
  distribution bundles and records; a NumPy built against MKL, Accelerate
  or a system BLAS or LAPACK, and a SciPy with no bundled BLAS, leave the
  runtime UNRESOLVED -- the linear algebra would otherwise run on a library
  nobody named. (A later draft still resolved those: it flagged only
  "OpenBLAS said, none bundled". Found writing this tranche's report,
  before the commit; BK25, BK26.)
* BACKEND STATUS. `environment_record(runtime=...)` carries the probe and a
  `backend_status` -- `RESOLVED` only when every native build is read and
  unmodified AND the runtime was probed and resolved. `RunIdentity` carries
  it and digests it. `run_identity_for` and the three models' bundles both
  take their environment from `backend_probe.run_environment()`, so the
  identity and the bundle it describes cannot disagree.
* REUSE. `may_reuse` refuses, before comparing anything, when either run's
  backend is not `RESOLVED`: an unresolved backend is recomputed, never
  reused, even against an identical identity. A resolved, identical backend
  may be reused (with intact evidence, as before); a different one is
  recomputed, naming the difference. This is COMPUTATION reuse. Authority
  is unchanged: a reused result is still admitted only on its evidence,
  re-derived intact.
* FUZZED. `cpuinfo` (the record lists only SIMD features the text lists, and
  canonicalises) and `proc_maps` (an answer is an absolute path the text
  names, of exactly that soname), in the bounded CI campaign and required by
  the fuzz test; the fuzzer's coverage now counts lines in `scientific/` as
  well as `qta_agent/`. 5000 cases on each: no findings.

**TEST.** `tests/test_backend_identity.py` (new): installed bytes resolved,
a replaced library MODIFIED with RECORD unchanged, an unreadable one
UNRESOLVED, the byte cache re-reading a rewritten file; the probe resolved
here, NumPy's and SciPy's OpenBLAS both recorded, the same record whatever
the process loaded first (in process and in fresh interpreters); no NumPy
imported by importing the interfaces; each runtime part -- a dispatched
feature, a BLAS kernel, a BLAS library's bytes, libm's bytes -- changes the
environment digest; a BLAS that will not name its kernel, an unreadable BLAS,
a missing libm and a missing loader each UNRESOLVED; MKL, Accelerate, a
system BLAS, an unbundled OpenBLAS and an unbundled SciPy BLAS each
UNRESOLVED, and a NumPy with no external BLAS resolved; no NumPy installed is a
resolved backend without one; maps lines that are not absolute mappings of
that soname are skipped; unresolved status without a probe, resolved with
one, unresolved with a modified build; reuse refused for an unresolved prior,
current or both; a resolved identical pair reused and a different one not;
backend status digested; a governed identity resolved, and its bundle's
environment the same. Existing tests that built identities by hand now state
`backend_status="RESOLVED"` where they test something else, and the
interfaces test adds that the default is UNRESOLVED and refused.

**MUTATIONS.** `backend_probe.json` (new) 26/26. Three mutations whose anchored lines CD rewrote are re-anchored to the same meaning: governed_model_reuse GR19 (the environment left out of the identity), run_identity_backend RB5 (the native build unrecorded) and RB6 (native hashes ignored). Regression, each killed: run_identity_backend 9/9, scientific_interfaces 27/27, fuzz_harness 9/9, governed_model_reuse 20/20, scientific_thermal_1d 12/12, scientific_thermal_2d 15/15, surface_adsorption 25/25, scientific_authority_replay 25/25.

**NOT DONE, and not claimed.** Reuse across backends is refused, never
reconciled: there is still no equivalence policy saying when two backends
agree well enough for a model (D-2026-90, directive 11). A resolved backend
is resolved on Linux: the probe reads `/proc/self/maps` and the loader's
search, and elsewhere it would be UNRESOLVED -- recomputed, not guessed. The
kernel an OpenBLAS reports is the one it selected for its own calls; a
library that dispatches per call below that is not seen. A backend variable
set AFTER the probe ran is not seen either; the governed worker's
environment is fixed before it starts.

## D-2026-95 — the governed-identity test expected the test process's backend, not the worker's

**CLASS** — `TEST_DEFECT`, `tests/test_governed_model_reuse.py`. Introduced
by D-2026-94 (`90bfe7a`); not R59, and no production code is wrong.

**DISCOVERED BY.** Hosted CI at `ac427a7`, run 36366385733, job
`dispatch-sensitivity` 108753534811: the full suite under
`OPENBLAS_CORETYPE=Haswell` and `NPY_DISABLE_CPU_FEATURES=X86_V4 AVX512_ICL
AVX512_SPR` failed one test,
`test_the_identity_is_this_model_these_parameters_this_environment`
(environment digests `48ef3cb9...` recorded, `cf6fe912...` expected).
Reproduced locally with the same two variables; green without them.

**CAUSE.** The test recomputed the expected environment by probing the
runtime in the TEST process, passing only the tool's environment variables
as data. The governed tool runs with exactly the allowlisted environment,
which by design does not carry dispatch overrides; the test process in that
job does. The worker's identity named the backend the worker ran on --
SkylakeX, AVX-512 -- correctly; the expectation named the test process's,
Haswell without AVX-512. The comment above the assertion ("the probe
answers the same in any process on this host") was true for processes that
share an environment and false here.

**REPAIR.** The expectation is computed in a subprocess launched with
exactly the tool's environment, so it probes what the worker probes. Green
under the dispatch override and without it.

**NOT CHANGED.** The probe, the identity and the tool's environment
allowlist: a dispatch override in the supervisor does not reach the
governed tool, and the identity says what the tool actually ran on.

## D-2026-96 — a key that could no longer sign was refused by every reader, and never by the writer

**CLASS** — `TRUST_BOUNDARY_GAP`, `qta_agent/principals.py`. Found by the
directive-7 inventory of D-2026-93's key lifecycle; not R59.

**WHAT WAS THERE.** D-2026-93 gave a key a life in log positions:
`RegisteredKey.refusal_at(seq)` answers "was this key good for event N", and
every authenticating reader asks it of every event. That answer is
historical and stays put -- a key revoked at seq 10 still authenticates
seq 5. What nothing asked was the WRITER's question, "may this key sign the
NEXT event": `signed_append` took no registry at all, and a test said so in
as many words ("the WRITER does not police the registry"). A revoked,
expired, not-yet-valid or compromised key signed, the attestation was
prepared, the record written, and only the next reader refused it -- in an
AUTHENTICATED_REQUIRED history, a record that makes every later read of the
history fail.

**REPAIR.** `may_sign(registry, signer, seq)` asks the writer's question and
`signed_append` and `begin_history` ask it under the writer lock, from the
record's own seq, before the attestation is prepared or the record written:
a refused key leaves no record and no prepared attestation. The registry is
the one named at the call or the one the log's authenticator reads with; a
write with neither is refused, because nothing could say whether the key may
sign. The two questions are now distinct functions answering from the same
lifecycle: `refusal_at(N)` for authorship of N, `may_sign(..., head + 1)`
for the next event.

**EVIDENCE.** `tests/test_signed_append_lifecycle.py`: revoked, compromised
and expired keys refused at the write with the files byte-unchanged and the
earlier events still authenticating; a not-yet-valid key, a key the registry
does not hold and a key registered for another principal refused; a write
with no registry refused; an explicitly named registry is the one asked.
Mutations KW1-KW6 in `tools/mutations/signed_append_lifecycle.json`.

**NOT CHANGED.** The readers, the lifecycle semantics and the attestation
format. No governed writer signs yet (plan item 79), so no production path
changes behaviour; the refusal is where a signing writer will meet it.

## D-2026-97 — the probe named the CPU features NumPy saw, not the loops it chose, and never asked the BLAS how many threads it runs

**CLASS** — `IDENTITY_INCOMPLETE`, `scientific/backend_probe.py`,
`scientific/run_identity.py`. Found by the directive-7 inventory of
D-2026-94; part of R59-A's evidence base, not a repair of R59.

**WHAT WAS THERE.** The runtime record carried `numpy.__cpu_features__` --
what the CPU offers -- and each bundled OpenBLAS's kernel and build string.
Measured here: with `NPY_DISABLE_CPU_FEATURES=X86_V4 AVX512_ICL AVX512_SPR`
the feature table still lists AVX512F, AVX512BW and the rest as present;
only the group tokens change. What changed the bytes in R59 is what the
dispatcher SELECTED, function by function -- 477 selections on this host,
244 of them X86_V3, 159 X86_V4, 20 AVX512_SPR, 54 baseline -- and nothing
recorded that. Nor was the BLAS thread count recorded as the library
answers it: the identity held the variables that request a count and the
host's CPU count, which is not what a library with a compiled-in maximum or
a pinned count actually runs. And `GLIBC_TUNABLES`, which can mask the
features glibc's libm IFUNCs select FMA and AVX2 variants of exp, log and
pow by, was not a backend variable.

**REPAIR.** The record carries `numpy.dispatch`: the count of selections,
the count per selected target, and a digest over every (function,
signature, selected target) -- read from `numpy.lib.introspect.
opt_func_info`, and `UNRESOLVED` when NumPy cannot answer or answers with
an empty table. Each bundled OpenBLAS is asked `get_num_threads` and
`get_parallel`; an unanswered one is `UNRESOLVED`. `GLIBC_TUNABLES` and
`GOTO_NUM_THREADS` are backend variables.

**EVIDENCE.** `tests/test_backend_identity.py`: the dispatch record follows
the runtime choice (every selected target disabled -> every selection
baseline, a different digest) while the CPU is unchanged; the digest is
over each choice, not the function names; unanswered dispatch and thread
count are unresolved; `OPENBLAS_NUM_THREADS=1` is read back as 1 from the
library itself; each new part reaches the environment digest. Mutations
BK27-BK33.

**NOT CHANGED.** Which runs may be reused: an unresolved or different
backend is recomputed exactly as before; a run identity simply names more of
what executed.

## D-2026-98 — three quantities crossed zero between backends with nothing in the file saying what their method resolves, and one of them was called declared

**CLASS** — `RESOLUTION_UNDECLARED` (D-2026-53's class), three producers;
`INSTRUMENT_PROXY` in `tools/cross_env_semantics.py`. Found by the
directive-7 remeasurement of R59; part of R59-B's evidence, not of R59-C.

**MEASURED.** The full canonical corpus (88 compared, 1 exempt) regenerated
on this host under seven dispatch configurations (OpenBLAS Haswell or
Nehalem kernel, NumPy with AVX-512 or with every dispatch target disabled,
one thread or the default), each probed in the regenerating process, and
every differing leaf classified. The existing comparator counted 27 zero
crossings in the Haswell + AVX2 configuration, 26 "declared by the file they
are in" and 1 bare. Asked quantity by quantity, the 27 are:

* 25 bound to a class of their own -- 18 in `gas_transport_profile.csv`,
  1 in `gas_transport_metrics.csv`, and the Mode C/D methane residuals, 2
  each in `coupled_mode_recovery_metrics.csv`, `multiphysics_summary.json`
  and `coupled_mode_state_summary.json` `.metrics` -- all BELOW_RESOLUTION
  on both sides;
* `coupled_mode_state_summary.json` `.state.gasC_sample.CH4`, 0.0 against
  4.0e-09: **bare**. The same physical number as `.metrics.Mode_C_cleanup_
  residual_CH4_m3`, which the file does classify; the comparator called the
  state copy declared because the FILE contains `_resolution` somewhere
  (`declares_resolution` read the text);
* `energy_ledger_cumulative_3d.csv` `cumulative_dU_J` after cycle 1 MODE_C,
  1.615587134e-27 against 0.0: **bare**. +9.114286125e-12 J stored in MODE B
  and released in MODE C, cancelling to one ulp on one backend and to
  nothing on another -- to two ulp (3.23e-27) with the Haswell kernel
  alone, which is why no rounding bound on the sum can be the floor: the
  inputs themselves differ.

The configuration with no NumPy dispatch at all added a third:
`convergence_report_3d.json` `.time_integration_check.rel_change`, 0.0
against 1.29e-16 -- one ulp of a 13.73 K probe, between two solves
integrated to 1e-6 and 1e-7.

**REPAIR, in the producers.** None rounds, clips or edits an artefact; each
class is computed by the code that produced the number, against a floor
that code establishes, and the canonical copies are that code's output on
the witness backend.

* The energy ledger publishes `phase_resolution_floor_J` and
  `cumulative_resolution_floor_J` -- the energy its own balance leaves
  unexplained (`|source - sink - dU|`, summed in magnitude across phases)
  plus the first-order rounding bound of the sums -- and a class beside
  every energy term, per phase and cumulatively (`campaign_state_3d.
  ledger_floor`). After a full B->C cycle the stored energy is
  BELOW_RESOLUTION against ~2e-13 J; so is the end-of-campaign dU in
  `campaign_state_3d.json`. MODE_C's 2.8e-25 J laser source is
  BELOW_RESOLUTION against its phase's 1.9e-13 J closure.
* The coupled-mode state publishes `gasB_sample_resolution`,
  `gasC_sample_resolution`, `thetaB_resolution` and `thetaC_resolution`,
  species by species, from the solves that produced them.
* The 3D convergence report publishes the class of each relative change
  against the tightest tolerance of the solves compared
  (`convergence_3d.rel_change_resolution`).

**REPAIR, in the binding.** `docs/resolution_inventory.json` binds classes
to quantities one at a time: `floor_from` on a wide column, `row_bindings`
for a long-format table, `json_bindings` (with `.*` for a parallel
resolution object) for JSON. `tools/resolution_inventory.py` reconciles
every binding against the committed artefact -- the path exists, the value
is a number, the carrier holds a class, parallel objects have the same
members, `floor_from` names a declared FLOOR. The ledger's twelve
formerly-bare columns are now CARRIER or FLOOR; rule 4 (no bare column in an
artefact that classifies another) holds for it. `package_consistency_check
.py` re-derives every row-floor class from the value and the floor in the
same row.

**EVIDENCE.** `tests/test_quantity_resolution.py`; mutations QR1-QR16. The
regenerated corpus differs from the committed one in exactly the four
files the producers write (and the by-design-exempt readiness stub).

**NOT CLAIMED.** The floors bound what each method RESOLVES; they are not
error bars, and a BELOW_RESOLUTION class is not a statement that the
quantity is zero. Nothing here establishes scientific equivalence between
backends (R59-C).

## D-2026-99 — which sixteen cells the NV-plane coverage table lists is chosen by roundoff

**CLASS** — `SELECTION_BELOW_RESOLUTION`, `qta_multiphysics/
surface_coverage_3d.nv_plane_coverage_rows`. Found by the directive-7
remeasurement; OPEN.

**MEASURED.** With the OpenBLAS Nehalem kernel and every NumPy dispatch
target disabled, `surface_coverage_3d_summary.csv` lists a DIFFERENT SET of
cells: the table is "the 16 hottest cells of the NV plane at Mode-D entry
plus the beam-axis cell", chosen by `argsort` of a plane whose temperatures
agree to ten significant digits (1.000000000e-02 K), so the order is decided
in the last bits and moves with the backend. The old comparator reported 17
SIGN_FLIPs in the x/y coordinates and passed; the rows describe different
cells.

**WHY NOT REPAIRED HERE.** A defensible tie-break needs the resolution of
the 3D thermal solve's temperatures, and that method declares no floor
(its columns are NO_FLOOR_DEFINED). Inventing one to make the selection
stable is the fabricated floor D-2026-66 refuses. Until a floor exists the
portable comparison refuses this artefact wherever the selection moves --
as STRUCTURAL, because the rows no longer name the same cells. Observed only
in the configuration with no SIMD dispatch at all. The native AVX-512
configuration and Haswell + AVX2 -- the one that reproduced the non-AVX-512
hosted runner's divergence file for file -- select the committed cells.

## D-2026-100 — the cross-environment comparator stripped digits from text, printed structure changes it should refuse, and asked files what their quantities resolve

**CLASS** — `INSTRUMENT_PROXY`, `tools/cross_env_semantics.py`. Found by the
directive-7 review of the instrument R59 was to fall back on; not R59 itself.

**WHAT WAS THERE.** A differing leaf was classified by deleting every digit
from both sides with a regex and comparing what was left: the residue
matched, so every number was a number. `model_v2` -> `model_v3` was a
precision event; so was any count that moved by one. A key present on one
side only was printed and not refused. A zero crossing or a sign flip was
never refused at all -- counted, and split DECLARED/BARE by whether the FILE
mentioned a resolution anywhere, which called `.state.gasC_sample.CH4`
declared (D-2026-98). Its scope was "every file in the other tree with a
committed counterpart", so a differing artefact it did not parse -- the
`.mmd` diagram -- was silently skipped, and a missing one never noticed.
The only refusal was a changed non-numeric residue.

**REPAIR.** Rewritten type-aware. JSON is parsed (duplicate keys refused)
and walked; CSV headers, row counts and row lengths must agree; a cell that
is a whole numeric token is a number, a cell that parses as a JSON or Python
literal container is recursed, anything else is text compared whole. Refused:
STRUCTURAL (keys, lengths, headers, rows, types, missing or foreign
artefacts), DECISION (any text, boolean, null, label, unit, class), DISCRETE
(an integer that moved; any change in a COORDINATE, EXACT_BY_CONSTRUCTION or
INPUT_CONSTANT column), NONFINITE, UNCLASSIFIED (an unparsed or unparseable
differing artefact, an equal value in changed text), and a zero crossing or
sign flip that is not bound -- through `docs/resolution_inventory.json`, per
quantity -- to BELOW_RESOLUTION on BOTH sides and, where a floor is bound,
inside it. Permitted and reported: PRECISION and bound crossings. The scope
is the declared canonical set minus exact exemptions, both passed in by the
caller (the CLI parses them from `package_consistency_check.py`). Every
report carries `CROSS_ENV_STATUS` and `scientific_equivalence:
NOT_ESTABLISHED`.

**MEASURED WITH IT.** On the seven pre-repair regenerations it finds exactly
the bare crossings D-2026-98 names, the 17 coordinate sign flips of
D-2026-99 in the no-SIMD configuration, and no structural, decision,
discrete, non-finite or unclassified difference in any.

**EVIDENCE.** `tests/test_cross_env_semantics.py` (hostile fixtures:
version labels, mode labels, text carrying a number, integer counts,
coordinates, NaN/Inf, header/row/key/type changes, unparsed artefacts,
duplicate keys, wide/long/JSON bindings, CASES 18-20, precedence, scope).
Mutations X1-X28 in `tools/mutations/cross_environment.json`, replacing
E12-E20, which anchored the code this replaces.

## D-2026-101 — a required job was red by design, and a different machine's digits were reported as stale outputs

**CLASS** — `CI_CLASSIFICATION`, `.github/workflows/agent-substrate.yml`
(full-suite), `package_consistency_check.py` Step 2b, `.github/workflows/
release.yml`. R59-B.

**WHAT WAS THERE.** `full-suite` ran `package_consistency_check.py`, whose
Step 2b failed every byte difference between the regenerated corpus and the
committed copies as "N stale root copies", and the step's comment said
"Expected RED on a runner whose dispatch differs from the committed
outputs'". A REQUIRED job red by design: red for a legitimate CPU
assignment and red for a regression look the same, so the second hides in
the first -- which is what R59 was classified as, run after run. The
checker's strict question -- did this machine reproduce the canonical
bytes? -- was being asked of whatever machine GitHub assigned, as though
the runner pool were one numerical environment; it is not (remeasured here:
OpenBLAS's runtime kernel and NumPy's runtime dispatch each move 20 of 88
files on their own, 23 together). The release workflow asked the same
question of an arbitrary `ubuntu-latest` and would have spent the job to
report a different CPU's digits as stale outputs.

**REPAIR.** Four questions, not one boolean (`scientific/reproduction.py`):
package integrity (unchanged), byte reproduction, cross-environment decision
stability, scientific equivalence -- and two policies in the checker.

* `--policy strict-reproduction` (the default; the release; the legacy
  Snakemake rule): exact bytes, and only on a backend the witness profile
  saw reproduce this corpus. Any other backend is refused BEFORE anything is
  regenerated, as `CANONICAL_REPRODUCTION_ENVIRONMENT_REQUIRED`; the release
  runs `tools/reproduction_witness.py require-reference` as its first
  legacy step for the same reason.
* `--policy ci` (full-suite, dispatch-sensitivity, the container): exact
  bytes pass as `BYTE_IDENTICAL`; ANY byte difference on a witnessed backend
  fails as `BYTE_DRIFT_COMPARABLE_BACKEND`, with no semantic fallback; a
  resolved backend the profile never witnessed passes only as
  `DIFFERENT_RESOLVED_BACKEND` with `DECISION_STABLE_WITH_NUMERIC_DRIFT`
  from the hardened comparator (D-2026-100), and says scientific
  equivalence is `NOT_ESTABLISHED`; an unresolved backend, an invalid
  profile or one that no longer applies fails.

The identity the verdict reads is the REGENERATING process's own:
`tools/regenerate_instrumented.py` runs the generator in-process, measures
its input closure with an audit hook and the modules it loaded, and probes
the backend in that process afterwards. `--verify-existing` classifies a
supplied tree only with a generation record that names exactly its bytes;
it never borrows the verifying process's identity. "Stale" is now said only
of drift on a witnessed backend.

**THE WITNESS.** `docs/byte_reproduction_profile.json`, written only by
`tools/reproduction_witness.py establish` from an observed regeneration
(exact declared set, all 88 non-exempt outputs byte-identical, backend
RESOLVED), binds the corpus, the exemptions, the generator's measured
closure (98 files), the lock and the witnessed backend's identity; every
digest in it is recomputed on read. It is not the corpus's historical
provenance and not evidence the numbers are right. The agent-substrate job
checks in seconds that it still applies to the tree.

**MEASURED, on this tree.** Native (the witness): `BYTE_IDENTICAL` under both
policies. `OPENBLAS_CORETYPE=Haswell` + NumPy without AVX-512: ci ->
`DIFFERENT_RESOLVED_BACKEND`, 23 of 88 files differ, 5411 leaves compared, 27
bound zero crossings, 195 precision differences, 0 structural / decision /
discrete / non-finite / unclassified / bare -> `DECISION_STABLE_WITH_
NUMERIC_DRIFT`; strict -> refused before regenerating.

**EVIDENCE.** `tests/test_reproduction_verdict.py` (directive CASES 1-17),
`tests/test_checker_reproduction_policy.py`, `tests/test_regenerate_
instrumented.py`; mutations RP1-RP29, BK34-BK35. The dispatch-sensitivity
job now runs the different-backend path on every commit and asserts it
compared something (the backend differed, a file differed, leaves were
compared, nothing but digits did).

**NOT CLAIMED.** Decision stability is not scientific equivalence (R59-C
stays open). A hosted runner that is not a witnessed backend cannot answer
the strict question: strict hosted reproduction is EXTERNALLY_BLOCKED
(R59-D).

## D-2026-102 — the physical CPU chose the arithmetic that defines the canonical bytes, and nothing existed that it could not choose

**CLASS** — `REFERENCE_ENVIRONMENT_ABSENT`, R59-E. `reference_backend/`,
`tools/reference_backend.py`, `.github/workflows/reference-backend.yml`.

**WHAT WAS THERE.** The canonical corpus is reproduced by one numerical
backend: the locked wheels on a host whose CPU has AVX-512, where OpenBLAS's
DYNAMIC_ARCH picks the SkylakeX kernel and NumPy's dispatcher picks its
AVX-512 loops (the witness, D-2026-101). Both choices are made at load time
from the CPU's flags, and glibc's libm makes a third the same way (IFUNCs:
FMA variants of exp, log, pow, sin where the CPU has FMA). So the host CPU
selects the arithmetic path, a runner without AVX-512 cannot produce the
canonical bytes, and forcing the SkylakeX kernel there dies on an illegal
instruction. No environment existed in which the arithmetic was fixed
independently of the machine running it.

**REPAIR — STAGED, not a migration.** A canonical reference backend, declared
in `reference_backend/spec.json` and checked by `tools/reference_backend.py
recipe` in the required agent-substrate job:

* OpenBLAS 0.3.31 built for ONE target (`TARGET=NEHALEM`, `DYNAMIC_ARCH=0`),
  `USE_THREAD=0`, `NUM_THREADS=1`: one set of kernels, chosen at build time.
  One pinned patch (`reference_backend/patches/`, sha256 in the spec):
  0.3.31 exports `openblas_set_threads_callback_function` from every build
  but compiles it only for threaded ones, so a serial shared library fails
  its own link test; the patch compiles the setter -- a store nothing in a
  serial library reads -- into the serial build. No arithmetic changes.
* NumPy 2.4.4 built with `cpu-baseline=X86_V2`, `cpu-dispatch=none`: no
  function has a second implementation for a dispatcher to prefer. SciPy
  1.17.1 on the same OpenBLAS. Wheels repaired by auditwheel, so the
  OpenBLAS and Fortran runtime they run on travel inside them, by bytes.
* `-O2 -fno-fast-math -ffp-contract=off` everywhere; no `-march=native`.
* A pinned interpreter (cpython 3.12.11 standalone, binary sha256), a
  pinned root filesystem (ubuntu-base 24.04.3, sha256) supplying libc,
  libm and the loader, one thread in every runtime.
* RUN on a pinned SOFTWARE CPU: `reference_backend/run.sh` executes under
  qemu-user 8.2.2 with `-cpu Nehalem-v1` and `-L rootfs`, environment
  emptied. qemu-user translates every instruction, so the CPUID the
  dispatchers and libm's IFUNCs see is the declared model's, whatever the
  physical CPU is.

`tools/reference_backend.py verify`, run INSIDE the reference runtime,
refuses anything that is not the declaration: NumPy dispatching outside its
baseline or built with dispatch targets, a baseline other than X86_V2, a CPU
offering AVX/AVX2/FMA/AVX-512, an OpenBLAS kernel other than Nehalem or with
more than one thread or built with DYNAMIC_ARCH, a thread variable that is
not 1 (or not recorded), another interpreter, a libc/libm/loader that is not
the root filesystem's by bytes (or no build record to compare against),
flushed subnormals, a rounding mode other than ties-to-even. The reference
identity it prints keeps what the dispatchers saw (NumPy's features and
dispatch, the BLAS, the libraries by bytes, the interpreter, the native
builds, the spec) and leaves out /proc/cpuinfo, which under qemu-user is the
physical host's.

**MEASURED.** On one host (GenuineIntel family 6 model 207: AVX-512, AMX), with
the runtime built from the recipe:

* `verify` under qemu-user 8.2.2 (binary sha256 90e3ce3f...) `-cpu
  Nehalem-v1`: conformance clean. NumPy sees CX16, LAHF, MMX, POPCNT and
  SSE through SSE4.2, and nothing else; all 477 of its dispatchable
  functions run their `baseline(X86_V2)` loop; NumPy and SciPy bundle the
  same OpenBLAS, which reports kernel `NEHALEM`, one thread,
  `SINGLE_THREADED`; libc, libm and the loader are the root filesystem's by
  bytes; the interpreter is the pinned binary. Reference identity
  `d2907025758c88d7...`.
* The same `verify` on the same userspace run NATIVELY is refused: the CPU
  offers AVX, AVX2, FMA3 and AVX512F. (A first `verify` was refused for a
  declaration error of this entry's own: the spec expected the kernel name
  a DYNAMIC_ARCH build reports, "Nehalem"; a TARGET=NEHALEM build reports
  "NEHALEM". The check stays exact; the declaration was corrected.)
* The full canonical generator on the reference: 3866 s under the
  software CPU (194 s natively), 89 outputs.
* THE HIDDEN INPUT THE SOFTWARE CPU REMOVES, measured, not assumed: the
  same userspace -- OpenBLAS fixed, NumPy without dispatch, the root
  filesystem's libm -- run natively differs from the software-CPU run in
  19 of 88 files. Run natively again with `GLIBC_TUNABLES=glibc.cpu.hwcaps=
  -AVX,-AVX2,-FMA,-FMA4,-AVX512F,...` it differs in 0 of 88. With the BLAS
  and NumPy paths fixed at build time, the arithmetic the physical CPU
  still chose was glibc libm's IFUNC selection -- which only a CPU the host
  cannot supply, or a mask the host could forget, takes away. The run also
  shows qemu's emulated SSE arithmetic agreeing bit for bit with this
  physical CPU's on these paths; that is one host, not the two-host proof.
* Against the committed corpus (s.35 step 4; nothing migrated): 24 of 88
  files differ; 5598 leaves compared; 0 structural, decision, discrete,
  non-finite or unclassified differences, 0 bare zero crossings, 3 bound
  zero crossings, 198 precision differences -- and 17 bare sign flips, all
  in `surface_coverage_3d_summary.csv`: the sixteen cells the NV-plane
  coverage table lists are chosen by roundoff (D-2026-99), so the reference
  lists different cells. The comparator refuses it
  (`RESOLUTION_AMBIGUITY`). A migration of the corpus to this backend
  would therefore change which cells that table names; D-2026-99 must be
  resolved, and the owner must authorize the migration, before any
  canonical byte moves.

**THE TWO-HOST TEST.** `docs/reference_backend_host_a.json` records host A: its physical
CPU class, the emulator's bytes, the recipe and generator digests, the
reference identity and the sha256 of all 89 outputs.
`.github/workflows/reference-backend.yml` is host B: it runs when the
recipe changes, builds the same recipe on a GitHub-hosted runner, runs the
same generator on the same software CPU, and asks `two-host`, which passes
only as `TWO_HOST_BYTE_IDENTICAL` -- different physical CPU classes, equal
reference identities, every non-exempt output byte-equal; one CPU class
twice is `NOT_TWO_HOSTS`, and a differing byte is `BYTES_DIFFER`, never
normalised. Its result is reported with the tranche; until a hosted run on
a materially different CPU class returns TWO_HOST_BYTE_IDENTICAL, R59-E
stays open, and if the hosted runner cannot build or run it, the proof is
EXTERNALLY_BLOCKED.

**EVIDENCE.** `tests/test_reference_backend.py`; mutations RB1-RB46,
RBS1-RBS16 (the recipe itself: DYNAMIC_ARCH enabled, a host target, NumPy
dispatch, a native baseline, threads raised, a passthrough CPU, an
unresolved FP policy, FMA contraction, build.sh ignoring the spec or
trusting what it fetches or patches, run.sh on the host CPU, host
userspace or inherited environment), RBT1-RBT13 (the two-host verdict, the generator it compares).

**NOT CLAIMED.** The reference is reproducible, not correct: it is not
physically validated, not experimentally verified, not ground truth. It
does NOT define the canonical corpus: migrating the corpus to it needs
separate owner authorization (directive 7 s.35), and the corpus is
untouched. A generator that started processes would run them on the
physical CPU (qemu-user emulates only the process it starts); the current
closure starts none, and a test keeps it so.

**THE TWO-HOST RESULT** (recorded with the tranche report, section 16 of
the plan). `reference-backend` run 36513496040, job 109230591297, at
`f6b24ab`: host B, a GitHub-hosted ubuntu-24.04 runner on an AMD EPYC 9V45
(AuthenticAMD, 4 vCPUs), installed the pinned toolchain, built the recipe
in 10 minutes with every source, patch and the interpreter verified, and
`verify` printed reference identity `d2907025758c88d7...` -- host A's,
exactly, so the reference builds were byte-reproducible across the hosts.
It regenerated the corpus in 2079 s on the software CPU. `two-host`
against host A (GenuineIntel family 6 model 207, AVX-512/AMX):
**`TWO_HOST_BYTE_IDENTICAL`** -- all 88 non-exempt canonical outputs
byte-equal, on two CPU classes that differ in vendor and feature set.
R59-E closes for the STAGED reference. Both hosts are virtual machines,
so "physical CPU" is the class each guest sees; it is one pair of hosts;
it proves reproducibility across them, not that any number is right; and
the canonical corpus is still the witnessed native backend's.

## D-2026-103 — a learned record could take every generic authority edge, and nothing existed that could state what a learned model is, trained on, or may claim

**CLASS** — `LEARNED_SUBSTRATE_ABSENT`, NF-1T. `scientific_ai/neural/`,
`qta_agent/learned_rules.py`, `qta_agent/learned_lifecycle.py`,
`qta_agent/store.py`, `qta_agent/reconstruct.py`,
`tools/neural.py`, `tools/neural_ledger.py`, `tools/neural_legacy_audit.py`.

**WHAT WAS THERE** (`docs/neural/nf1t_inventory.json`, recorded at
`f303a73` before any change). No generic neural, training, checkpoint or
distributed code; no framework installed; the one learned layer in the
repository (`qta_multiphysics/deep_expdesign/`) is legacy, bound to the
apparatus's design space, and stays unimported. The authority store had
content rules for scientific results and none for anything learned: a
record of any kind could take PROPOSED -> UNDER_REVIEW -> VERIFIED ->
PROMOTED, so a learned prediction registered as a record could have been
verified by evidence about itself. Units were strings with no parser from a
unit to its dimension, and nothing could say how many parameters a
configuration has, how many a token uses, or whether a large configuration
had been allocated, trained, or only described.

**REPAIR.**

* **The boundary first.** `qta_agent/learned_rules.py`: every kind prefixed
  `learned_` is refused VERIFIED and PROMOTED -- in `AuthorityStore`
  before the append (a live transition never reaches the log) and again on
  replay (`_admit`), so a log written by anything else cannot carry one in.
  The second reader (`qta_agent/reconstruct.py`) restates the rule
  independently and reports such a transition as unauthorized. No admission
  policy for learned models exists; every learned output is
  `LEARNED_PREDICTION`, `NON_AUTHORITATIVE`,
  `REQUIRES_EXTERNAL_VERIFICATION`. `learned_lifecycle.py` registers the
  seven document kinds through the EXISTING `record.create` /
  `record.depend` actions (no new action class) and refuses a document
  whose upstream is REVOKED, REJECTED or STALE, or whose digests do not
  link. Only `tools/neural_ledger.py` drives it; `tools/neural.py`, which
  generates data and trains, runs that tool as a separate process and
  imports nothing from `qta_agent` (a test pins it), so no process that
  computes a dataset or a model loads the authority substrate.
* **A family, not a file name.** `scientific_ai/neural/config.py`,
  `accounting.py`, `solver.py`, `family.py`: one configuration-driven
  feature-token MoE family. The parameter count is exact arithmetic over the
  configuration by fourteen categories; active-per-token counts the embedding
  rows a token reads and `top_k` of the experts, never all of them; the
  batch-touched count is a separate, bounded quantity. The budget solver
  resolves the expert width and `top_k` against a stated target and range and
  records every candidate it evaluated.
* **Meta validation is not allocation.** `model/meta.py` traces the real
  `init` and `apply` with `jax.eval_shape`; the abstract tensors are
  counted category by category against the arithmetic. A real allocation
  above 2 GiB is refused twice, independently -- `meta.materialize` and
  `network.init` -- unless `allow_large=True` AND
  `QTA_NEURAL_ALLOW_LARGE_ALLOCATION=1`, which nothing in the repository
  sets.
* **Data with provenance.** `source_surface_adsorption.py` builds every
  sample by one run of the admitted `surface.langmuir_capture@1.0.0` model
  through `scientific.model.run_model`, carrying the bundle, parameter and
  implementation digests and the backend identity; splits are a hash of the
  provenance family; normalisation is fitted on the training split only; the
  out-of-distribution split is a separate temperature region; leakage checks
  and their remaining risks are written into the dataset manifest.
* **Tokens that carry their units.** `units.py` parses a unit string to
  seven SI exponents and a class (K and Pa are different dimensions; PER_ROW
  is refused); `tokens.py` and `features.py` encode identity, a
  standardised value, sign, log-magnitude and exact zero, the dimension, the
  role and MISSING as its own state. NaN and infinity are refused with a
  reason, never clipped.
* **Checkpoints without pickle.** `model/checkpoint.py` writes the
  safetensors layout with NumPy, decodes strictly, identifies a checkpoint by
  the sha256 of its bytes and refuses a load whose digest, configuration,
  tensor set or shapes differ. Resume semantics are one of five, and only
  EXACT_RESUME continues a run's lineage.
* **Claims that need their evidence.** `claims.py` evaluates nine claims
  from documents, by digest, with a scope (SUBJECT, FAMILY_MEMBER, FAMILY);
  `LARGE_MODEL_TRAINED` and `DISTRIBUTED_HARDWARE_VALIDATED` cannot hold
  from anything this repository produces, and a simulated device profile
  cannot record a hardware execution.
* **No legacy semantics.** `tools/neural_legacy_audit.py` classifies every
  hit of the hardware-era mode, routing and sequence vocabulary; the result
  is gated in CI at zero `ACTIVE_NEURAL_SEMANTIC_LEAK`.
* **Dependencies.** `jax`, `jaxlib` 0.11.2 and `ml-dtypes` 0.6.0 (all
  Apache-2.0) as the optional extra `neural`, locked; `opt-einsum` 3.4.0
  (MIT) comes in transitively. No existing locked version moved, and the
  reference-backend export (`--all-groups`, no extras) is byte-identical
  before and after. PyTorch was not chosen: `download.pytorch.org` is
  refused by the network policy, and the PyPI x86-64 wheel is the CUDA
  build.

**MEASURED** (dry run before the commit; the committed evidence is
regenerated at the commit and recorded with it). The flagship member --
d 8192, 64 layers, 64 heads, 64 routed SwiGLU experts of width 9,728,
`top_k` 12 -- has 996,509,217,800 trainable parameters (-0.349 % of the
1T target), 8,200 non-trainable buffers, and 200,832,889,864 active per
token (+0.416 % of 200B). `jax.eval_shape` resolved it with the abstract
count equal in every category, all 64 MoE layers traced with legal routing,
zero live arrays before and after, 16,646,144 bytes of peak-RSS growth, and
the real allocation (1,993,018,468,400 bytes) refused.

**EVIDENCE.** `tests/test_neural_{config,accounting,features,data,moe,model,
meta,provenance,boundary,training,distributed}.py`; mutations
NA1-NA20 (accounting, configuration, solver), NT1-NT20 (tokens, units,
datasets, OOD, constraints), NR1-NR14 (router and MoE), NU1-NU22 (meta
validation, claims, checkpoints, lineage, the store and second-reader
refusal), NX1-NX7 (training and resume), NP1-NP5 (parallel plans).

**NOT CLAIMED.** The flagship has never been allocated, trained or run on any
accelerator: it is parameterised and structurally validated. The development
member's results are its own and say nothing about the flagship's. No learned
output is verified, promoted or scientifically validated; the training data
are simulator outputs whose own status is INVARIANTS_HOLD, not
authority-verified. Distributed readiness is software on simulated CPU
devices only. The estimates are arithmetic, not measurements.

**THE COMMITTED RESULT** (recorded with the tranche report, section 17 of
the plan). At `0a0d43e`, `docs/neural/` holds what `tools/neural.py all`
wrote at the clean commit `8ee1e5b`, which every manifest names as its
source: 3,456 governed samples (digest `5b7332a5...`, 0 rejected, every
leakage check passed); 4,000 training steps with fresh runs equal tensor for
tensor and an EXACT_RESUME bit-identical to the uninterrupted run; the
final checkpoint `74de5740...` reloaded to identical outputs; and an
evaluation that reports what the development model gets wrong -- its
predictions violate declared invariants of the source model on part of the
test split, its impingement-flux intervals undercover (65.5 % at 90 %
nominal), and out of distribution its coverage collapses -- without
clipping any of it. Claims, recomputed in a fresh history: the flagship
holds ARCHITECTURE_DEFINED, ARCHITECTURE_PARAMETER_VERIFIED and
ARCHITECTURE_META_VALIDATED in its own right, training and reload only in
FAMILY_MEMBER scope, and the attempt to accept the evaluation was refused
by the store. Hosted: every job green at `8ee1e5b` (agent-substrate
37566755617 and 37566759142; stack-verify 37566755602 and 37566759195) and at `0a0d43e` (agent-substrate 37574169210 and 37574174428; stack-verify 37574174415), the full suite run with the
`neural` extra and `QTA_NEURAL_REQUIRED=1`, and all 78 mutation specs in
8 shards.

## D-2026-104 — the current programme read as "PASS = 0", a trained model was said to be unallocated, and simulated or witnessed results were worded as more than they are

**CLASS** — `CLAIM_HYGIENE`, NF-1T closure. `scientific_ai/neural/documents.py`,
`scientific_ai/neural/claims.py`, `scientific_ai/neural/status.py`,
`tools/neural.py`, `tools/neural_ledger.py`, `tools/pass_semantics_audit.py`,
and the current-harness text named below.

**WHAT WAS THERE** (`eb716ad`, the NF-1T report commit, every hosted job
green). Four reporting defects, none in a computed number:

* **The legacy gate count stood in for the current status.** The QTA
  hardware-forecast gate table's `PASS_count = 0` is a fact about that
  forecast -- no gate could pass because nothing was measured. Nine
  statements in seven files of the current harness (the agent-substrate
  workflow's header, the gates authority in `authorities.json`,
  `conftest.py`, the completion matrix's `does_not_mean` and its R55
  detail, a policy description in `qta_agent/governed_stage10.py`, the
  reproduction profile's `DOES_NOT_MEAN`, `tools/test_isolation.py`) said
  "PASS remains 0" or its equivalents without saying whose PASS it was, and
  the PR description put it at the top as the branch's status. Read
  without context it says the software passes nothing.
* **A trained configuration was said to be unallocated.** The development
  manifest's `plain_language` read "...structurally validated by
  zero-allocation (abstract) construction; its weights have not been
  allocated. This configuration has been trained end to end..." -- two
  facts about different moments joined as if simultaneous, the first false
  of the configuration as it stands. `plain_language` appended the meta
  sentence whenever ARCHITECTURE_META_VALIDATED held, whatever else did.
* **Simulated execution was worded as distributed validation.** The claim
  reason was "every parallel check passed on SIMULATED_MULTI_DEVICE"; that
  the run was software-path validation only, that `hardware_executed` was
  false, and that tensor and pipeline parallelism were never executed were
  each true and each said elsewhere, not where the claim is read.
* **A stored witness's byte identity read as the tree's.** Section 17's
  validation table gave the witness's full-corpus byte identity with no
  backend, beside hosted runs whose own verdict at the same SHA was
  `REPRODUCTION_STATUS=DIFFERENT_RESOLVED_BACKEND`,
  `CROSS_ENV_STATUS=DECISION_STABLE_WITH_NUMERIC_DRIFT` (23 of 88 files
  differing in their digits).

**REPAIR.**

* **Whose PASS it is, checked.** `tools/pass_semantics_audit.py` finds
  every occurrence of the legacy gate-table vocabulary (nine patterns) in
  every tracked or untracked-unignored file and classes each file by an
  ordered rule table: LEGACY_QTA_CANONICAL, LEGACY_COMPATIBILITY_GUARD,
  DOCUMENTATION_HISTORY, TOOLING_REFERENCE, FALSE_POSITIVE. In the current
  harness every occurrence is judged on its own: within two lines it must
  say whose gates these are (legacy, historical, hardware-era, hardware
  forecast, QTA), and `automatic_gate_effect` must be NONE; anything else
  is a CURRENT_AI_SEMANTIC_LEAK, and a file no rule covers is UNCLASSIFIED.
  The nine statements now say that the count belongs to the legacy QTA
  hardware forecast and is not a measure of the code they describe. The
  check (`--check`) is a CI step and requires zero of each; the report is
  committed.
* **A current status that does not read the legacy table.**
  `scientific_ai/neural/status.py` builds `docs/neural/current_status.json`
  and `SCIENTIFIC_AI_STATUS.md` from the committed learned-model evidence,
  the claims, the distributed report and the stored witness; the legacy
  gate statuses are a separate argument that only the section labelled
  LEGACY_QTA_ONLY reads (a test changes them and requires every other
  section to stay the same). The programme verdict is printed as DECLARED by
  the closure report, not derived. `tools/neural.py status` writes both;
  `verify` and a test recompute them.
* **Plain language from the claims that hold.** A subject with its own
  completed training is described as validated abstractly first and then
  allocated and trained; "real weights have not been allocated" is said
  only of a subject with no subject-scoped training. The manifests'
  `claim_status` -- derived state -- was re-derived from the unchanged
  evidence (`tools/neural.py rederive`, which refuses if any other field
  would change); `source_commit` stays `8ee1e5b`, and no weight, checkpoint
  or evaluation byte moved.
* **Simulated is software-path only.** A non-hardware run's reason now
  reads "<profile> SOFTWARE-PATH VALIDATION ONLY ... hardware_executed is
  not true, so this is not distributed hardware validation", and names the
  parallel axes no executed check covered as PLAN_ONLY, derived from the
  check names. The status reports `hardware: NOT_VALIDATED` unless a
  non-simulated profile executed on hardware.
* **Three reproduction facts, kept apart.** The status and section 18 state
  separately: the stored witness (88/88 on backend `97b7772c...`, observed
  at `f303a73`), what a generic hosted runner reports (decision stability
  under numeric drift, not byte identity), and
  `SCIENTIFIC_EQUIVALENCE_STATUS=NOT_ESTABLISHED`. A test requires every
  88/88 byte-identity line in the current documents to name the witnessed
  backend, and no decision-stability line to claim identity or
  equivalence.
* **Boundaries.** EB15-EB17 in the claims boundary: the legacy QTA
  gate-table PASS count is not a status of the current harness; simulated
  distributed execution is not distributed hardware validation; decision
  stability under numeric drift is not byte identity.

**EVIDENCE.** `tests/test_claim_hygiene.py`; `tests/test_neural_meta.py`
(a trained subject is never said to be unallocated; the flagship still
is); `tests/test_neural_accounting.py` (the locked architecture's exact
counts, 4,096 expert modules, 768 expert selections per token across
depth); `tests/test_neural_provenance.py` (the refusal is exactly VERIFIED
and PROMOTED for every learned kind); `tests/test_mutation_harness.py`
(every committed spec passes the harness's own validation); mutations
CH1-CH20 (`tools/mutations/claim_hygiene.json`) and NU23-NU25.

**NOT CLAIMED.** Nothing here changes a model, a weight, a dataset, a
canonical output, the legacy gate table or any historical result; the
stored witness profile is unchanged and keeps the wording it was written
with. No large configuration was allocated or trained; no learned output
was admitted. The broader semantic clean-up of the hardware-era vocabulary
(tranche E) is not begun.

## D-2026-105 — a proposer could take its own claim into review, and the reviewer then stalled on finding it there

**CLASS** — `AUTHORITY`, harness completion programme. `qta_agent/authority.py`,
`qta_agent/reconstruct.py`, `qta_agent/governed_model.py`.

**WHAT WAS THERE** (`b7807bb`). Separation of duties held at the verdict
(UNDER_REVIEW to VERIFIED or REJECTED requires an actor other than the
proposer) and at promotion, but not at the pickup: PROPOSED to
UNDER_REVIEW, and STALE to UNDER_REVIEW, needed only the VERIFIER role. An
agent holding both roles could move its own claim into review. Nothing
became VERIFIED that way -- the verdict edge still refused it -- but the
record was then in a state the real reviewer's `decide` did not expect,
and `decide` failed on a pickup that had already happened instead of going
on to the verdict. Found by the proposal-ingress tests, when an AI proposer
was given the governed path end to end.

**REPAIR.** Both edges into UNDER_REVIEW require a distinct actor, in the
gate (`requires_distinct_actor=True`) and, restated, in the second
reader's edge table, so a pickup appended straight into the log past the
store is refused on replay. `decide` resumes a record that is already
UNDER_REVIEW and goes on to the verdict, so a crash between pickup and
verdict is recoverable.

**EVIDENCE.** `tests/test_proposal_ingress.py` (the store refuses the
proposer's pickup; the second reader refuses one forged into the log; an
interrupted decision resumes); mutations AU1 (the gate) and AU2 (the
second reader) in `tools/mutations/proposal_ingress.json`, and R43 re-anchored on the
amended edge table (`tools/mutations/agent_second_reader.json`).

**NOT CLAIMED.** No record in any committed history took this path; the
replayed stage-10 logs reconstruct unchanged.

## D-2026-106 — the internal RO-Crate validator accepted a crate that conforms to no specification, and the crate lacked a REQUIRED property

**CLASS** — `CONFORMANCE`, harness completion programme. `ro_crate_tools.py`,
`ro-crate/ro-crate-metadata.json`.

**WHAT WAS THERE** (`b7807bb`). `ro_crate_tools.py validate` checked
entity identity, the root dataset and file coverage, and reported VALID for
a crate whose metadata descriptor had no `conformsTo` (RO-Crate 1.1 s.4.1
REQUIRES it, and it is how a reader knows what the crate claims to be) or
was not `about` the root. The committed crate also lacked the root
dataset's `datePublished`, which RO-Crate 1.1 REQUIRES. Found when the
maintained external validator (roc-validator, pinned and hashed in
`integrations/ro_crate/validator.lock`) was run with negative controls
beside the internal one, and the two disagreed.

**REPAIR.** The internal validator checks the descriptor (present, about
the root, `conformsTo` an RO-Crate 1.x specification) and the root's ISO
8601 `datePublished`; the crate carries a declared publication date, not a
clock reading. `tools/ro_crate_conformance.py` runs both validators over
the real crate and four broken controls and requires them to agree.

**EVIDENCE.** `tests/test_ro_crate_conformance.py`; mutations RC1-RC5.

**NOT CLAIMED.** Conformance to the RO-Crate structure is not a claim that
any packaged result is correct.

## D-2026-107 — a performance baseline gated a timed ratio that had stopped being timed

**CLASS** — `MEASUREMENT`, harness completion programme.
`docs/performance_baseline.json`, `tests/test_agent_performance.py`,
`tools/performance_baseline.py`.

**WHAT WAS THERE** (`b7807bb`). `governed_operation_vs_history` was
recorded as a TIMED ratio under a 4.0 ceiling (two observations, 1.21 and
1.10). The guard had since become an exact re-hash count whose property is
equality, and the suite records 1.0 -- yet the baseline still held the
timed series and the test still pinned 4.0. A count and a timing were
being compared as one quantity, and nothing distinguished work that is
deterministic (a count, gated) from timing that depends on the host
(telemetry, never a gate on a shared runner).

**REPAIR.** Every measured guard declares a kind: DETERMINISTIC_WORK (an
inclusive maximum, gated on every run) or ENVIRONMENT_SENSITIVE_TIMING (an
exclusive gross bound, reported as telemetry with the host). The timed
series is retired -- kept, with its reason, and refused by `--record` --
and the guard is published as DETERMINISTIC_WORK with a maximum of 1.0.

**EVIDENCE.** `tests/test_agent_performance.py`,
`tests/test_performance_telemetry.py`; mutations
`tools/mutations/performance_telemetry.json`.

**NOT CLAIMED.** Timing on a shared hosted runner says nothing about a
user's machine; it is reported, not gated.

## D-2026-108 — the completion-matrix validator crashed on the malformed boundary it exists to report

**CLASS** — `VALIDATION`, harness completion programme.
`tools/completion_matrix.py`.

**WHAT WAS THERE** (`3b82594`). A boundary's `limit` that was not a string
(None, a number, a list) was reported as not substantive -- and then
reached the restatement check, which evaluated `detail in limit` and raised
TypeError. The validator crashed instead of returning the finding. Found
by the current-active type check (`tools/typecheck_scope.py`), not by a
test.

**REPAIR.** A non-string limit is reported and then compared as empty
text; the restatement check runs only on a non-empty limit.

**EVIDENCE.** `tests/test_completion_matrix.py`
(`test_a_limit_that_is_not_a_sentence_is_a_finding_not_a_crash`, four
cases); C5 re-anchored on the amended line.

**NOT CLAIMED.** No committed boundary had a non-string limit; the shipped
matrix validated before and after.

## D-2026-109 — a foreign checkpoint was classed as records removed whenever the other log's records happened to be longer

**CLASS** — `RECOVERY`, harness completion programme (R41).
`qta_agent/checkpoint.py`.

**WHAT WAS THERE** (`3b82594`, and the classified audit added in this
programme). `check_against` compared the checkpoint's end offset with the
log's size before anything else, and an offset past the end raised
CheckpointAheadOfLog -- "records the checkpoint covered have been removed".
A checkpoint of a DIFFERENT log whose records are a few bytes longer
(ids, timestamps) also ends past this log's end, so it was classed
AHEAD_OF_LOG instead of FOREIGN_LOG. Both classes are unusable, so no
recovery restored the wrong state; but the audit's account of why was
wrong, and `tests/test_checkpoint_recovery.py` failed about one run in six.
Found when the mutation harness's null control went red on it: a test that
fails without any change cannot tell a mutation from noise.

**REPAIR.** When the log is short in bytes, the log's own last complete
record decides: if it reaches the checkpoint's seq, the checkpoint's
offsets belong to another log (CheckpointMismatch, FOREIGN_LOG); otherwise
records are gone (AHEAD_OF_LOG). A torn final line is not a record. The
read happens only on that failure path, so the cheap check stays cheap.

**EVIDENCE.** `tests/test_checkpoint_recovery.py`: a foreign checkpoint
built to be longer in bytes is FOREIGN_LOG on every run; a log truncated
in place, its head witness untouched, is AHEAD_OF_LOG; the last-record
reader on empty, clean, torn and malformed tails. Mutations CR9 and CR10
(`tools/mutations/checkpoint_recovery.json`); 25 consecutive runs of the
file green after the repair.

**NOT CLAIMED.** Nothing about which checkpoint recovery uses changed:
both classes were and are unusable.

## D-2026-110 — a durable proposal stranded by a crash could never reach a decision

**CLASS** — `RECOVERY`, harness completion programme (section 44).
`qta_agent/governed_stage10.py`, `qta_agent/governed_model.py`.

**WHAT WAS THERE** (`051acf7`). A submission bound to an idempotency key
(the proposal ingress passes the proposal's id) was durable, and a
resubmission found the bound task -- and reported its state and stopped,
whatever that state was. `recover()` returned stranded work to the queue and
nothing took it from there: `run()` only drove a task it had just created.
A task a dead supervisor left QUEUED, CREATED, VALIDATED, or COMPLETED with
no verdict stayed there, and every resubmission said so. Separately,
`decide()` raised I2 when resumed after its verdict was already appended,
so a crash between the verdict and its caller hearing failed the resumed
path on its own decision. Nothing was ever duplicated or skipped; the
proposal was durable and stuck. Found by cutting the real
proposal-to-decision log after every append and resubmitting
(`tests/test_proposal_crash_recovery.py`): 13 of 20 cuts could not finish.

**REPAIR.** A resubmission takes up the bound task when finishing it repeats
nothing (`GovernedStage10._resume`): CREATED and VALIDATED are admitted and
queued; QUEUED with a READY job is a new attempt under a new lease, counted
against the retry budget; QUEUED with the job still dispatched to THIS
worker identity under a live lease resumes that attempt -- the task edge
into LEASED admits one taker; COMPLETED is verified by the verifier from the
attempt's own records, after re-checking that the execution record and the
capture agree. The queue is reconciled first and the policy is asked again.
Anything terminal, anything LEASED or EXECUTING, any tool declaring
EXTERNAL effects, and any job waiting, backing off, blocked or failed is
reported as before. `decide()` returns a decision already in the log when
it was made on the same check's report, and refuses one made on another.
`GovernedRun.resumed_from` says which state a call took up.

**EVIDENCE.** `tests/test_proposal_crash_recovery.py`: 20 cuts, each
rebuilt from the log alone with the lease holder's process gone, each
finishing at the uncrashed verdict with one receipt, one executed model
task, one record, one pickup, one verdict and no second-reader anomaly;
another worker identity refused a live lease; a completion whose records
disagree never verified; a policy published after the crash governing the
resumed attempt; a lapsed lease taken up as a second attempt; a crash while
checkpointing the projection recovered by full replay or a whole
checkpoint; a decision never returned for another check's report.
`tests/test_agent_idempotency.py`: a stranded EXTERNAL task is UNCERTAIN and
never runs. Mutations PCR1-PCR12 (`tools/mutations/proposal_crash_recovery.json`).

**NOT CLAIMED.** Exactly-once execution against anything outside this
system: an EXTERNAL tool is still never resumed. A task stranded between
the scheduler's dispatch and its own lease by a supervisor of ANOTHER worker
identity waits for that lease to lapse.

## D-2026-111 — the RO-Crate's scripts were data entities typed neither File nor Dataset

**CLASS** — `CONFORMANCE`, harness completion programme (R65).
`ro_crate_tools.py`, `tools/ro_crate_conformance.py`.

**WHAT WAS THERE** (`051acf7`). Seven data entities in the root's `hasPart`
-- the runners, the Snakefile and the three checkers -- were typed
`SoftwareSourceCode` alone. RO-Crate 1.1 requires every data entity to be a
File or a Dataset, whatever else it also is. The internal validator did not
check types and accepted the crate; the conformance packager copied only
File-typed entities, so those seven files were not even in the package the
community validator was shown. The hosted `ro-crate` job (run 37884676618)
refused the committed crate: internal ACCEPTED, external REFUSED, DISAGREE.
The tool did not print the external validator's reasons, so they are not in
the log; the cause was identified by reading the profile's REQUIRED shapes
against the crate.

**REPAIR.** The seven are typed `["File", "SoftwareSourceCode"]`; the
internal validator refuses a data entity with neither type; the packager
copies every data entity the root has, whatever its type; a fifth negative
control (`data_entity_not_a_file`) must be refused by both validators; and
a disagreement now prints both validators' reasons into the log.

**EVIDENCE.** `tests/test_ro_crate_conformance.py`. The external
validator's verdict on the repaired crate is the next hosted `ro-crate`
run's to give; it cannot be measured here (w3id.org is not reachable).

## D-2026-112 — FMI acceptance with a relative work directory asked the runtime for a file it could not see

**CLASS** — `INTEGRATION`, harness completion programme (R63).
`scientific/checks/fmu_rc2.py`, `tools/fmi_acceptance.py`.

**WHAT WAS THERE** (`051acf7`). The hosted `fmi` job ran
`fmi_acceptance.py --work fmi_work`; the FMU's path stayed relative and the
runner, which works in a scratch directory of its own, answered
FileNotFoundError from inside fmpy. Every local run had passed an absolute
path. The governed path was not affected: it resolves the FMU inside the
workspace first.

**REPAIR.** `run_runner` refuses a relative FMU path by name before any
runtime is asked; the acceptance tool resolves its work directory.

**EVIDENCE.** `tests/test_harness_integrations.py`; the acceptance campaign
run locally with `--work fmi_work`: 7/7 criteria, 9/9 controls rejected.

## D-2026-113 — the H1 push's own integration defects, found by its hosted runs

**CLASS** — `CI`, harness completion programme.
`Snakefile`, `.github/workflows/harness-integrations.yml`,
`.github/workflows/agent-substrate.yml`, `tests/test_ro_crate_conformance.py`.

**WHAT WAS THERE** (`051acf7`), four defects no local run reached:

* the `s10_rust_parity` rule asserted the per-kernel `backend_in_force`
  key the Rust resolution had removed from the status report (KeyError in
  stack-verify core and full, and in end-to-end) -- the default target was
  never run end to end before the push;
* the end-to-end job ran `snakemake --cores 1 --forcerun harness_demo
  harness_demo`: `--forcerun` took both words, so the demonstration's job
  ran the whole default target instead of its rule;
* the bare Python 3.13 job installs no h5py, and the fuzz campaign builds
  its targets together, the HDF5 bundle reader among them;
* under mutant RC5 every conformance test judging from the repository root
  overwrote the committed validation report, and the mutation harness
  refused the shard for a tracked file left changed (shard 6 and the
  aggregate).

**REPAIR.** The rule asserts the committed decisions; the target precedes
the options; h5py is installed in the bare job with its reason stated; the
conformance tests restore the committed report after each test.

**EVIDENCE.** The rule run locally; the dry run schedules only
`harness_demo`; the agent suites in a bare 3.13 environment with h5py; the
conformance spec rerun locally. The hosted runs of the next push are the
measurement.

## D-2026-114 — three tests assumed the host they ran on

**CLASS** — `TEST`, harness completion programme (R64).
`tests/test_agent_execution.py`, `tests/test_agent_collusion.py`,
`tests/test_reference_backend.py`.

**WHAT WAS THERE** (`051acf7`, the first container-verify run on push). One
test read the process table through `ps`, which the image does not ship;
one asserted that `ROOT.parent.parent/etc/passwd` does not exist, which is
the system's own file wherever the checkout sits two levels below `/`, as
it does in the container; one required the backend probe's interpreter
record to carry exactly `name` and `sha256`, and the probe correctly adds
`libpython` when the interpreter runs from a shared one, as the container's
does. None was a defect in what the tests guard.

**REPAIR.** The process table is read from `/proc`, and the scan must see
the test's own process; the escaping write is judged by comparing the
target before and after; `libpython` is optional at the interpreter record
and shaped when present.

**EVIDENCE.** The three tests locally; container-verify on the next push.

## D-2026-115 — task moves and proposal receipts were read, checked and appended with the lock around the append alone

**CLASS** — `CONCURRENCY`, harness completion programme (section 45).
`qta_agent/governed_stage10.py`, `qta_agent/proposals.py`.

**WHAT WAS THERE** (`051acf7`). `GovernedStage10._move` read the task, checked
the transition and appended, holding the writer lock for the append only;
`ProposalIngress.receive` looked the proposal up and appended the same way.
The scheduler and the authority store had learned this shape
(`append_decided`); the task machine and the ingress had not. Two processes
moving one task -- two supervisors recovering it on start, two resubmissions
taking up one stranded dispatch -- each appended a move out of the same
state, and the second record moves the task from a state the replay has
already left: a well-formed chain nobody can rebuild. Four ingress
processes receiving one proposal appended two receipts (measured on the
pre-fix tree: `[False, False, True, True]`).

**REPAIR.** Both decide under the writer lock against the head they are
written onto (`EventLog.append_decided`); a task that moved since its caller
read it is refused with nothing written, and `recover()` treats losing that
race as somebody else's recovery. The task a move returns is its one record
folded onto the projection it was decided against, by the replay's own
rules, so a governed run makes no more whole-log passes than before
(`MAX_FULL_LOG_PASSES_PER_GOVERNED_RUN` unchanged).

**EVIDENCE.** `tests/test_proposal_concurrency.py`: four processes receive
one proposal (one receipt); four resubmit one stranded proposal (the model
runs once; one record, one pickup, one verdict; every other caller refused
by name); four submit one received proposal for the first time (one bound
task runs, the orphans are cancelled before anything is queued). Mutation
PI7 re-anchored to the decided check; V9 to the tolerant recovery.

## D-2026-116 — a checkpoint's snapshot and its anchor were taken at two different heads

**CLASS** — `CONCURRENCY` / `RECOVERY`, harness completion programme (R41,
section 45). `qta_agent/store.py`.

**WHAT WAS THERE** (`051acf7`). `AuthorityStore.checkpoint` snapshotted the
projection, then created the checkpoint at the CURRENT head, then appended
the claim -- three steps with nothing holding the log still. A process
checkpointing beside another snapshotted seq 57 and anchored at 58 or 60,
the other's claim having landed between. The restart then raised
StoreError from `load_from` instead of recovering: a checkpoint the audit
classed USABLE crashed the recovery it was meant to speed up (measured on
the pre-fix tree).

**REPAIR.** Snapshot, checkpoint and claim are decided under the writer
lock at one head; and `recover()` treats a checkpoint `load_from` refuses
as unusable -- a full replay, reported unhealthy with the refusal -- rather
than failing to start.

**EVIDENCE.** `tests/test_proposal_concurrency.py`: four processes
checkpoint at once; every checkpoint whole, the restart CHECKPOINT_ASSISTED
and equal to a full replay. Mutations C32 and C33 re-anchored.

## D-2026-117 — grants, roots and bindings decided from a head or a ledger read before the append

**CLASS** — `CONCURRENCY`, harness completion programme (section 45).
`qta_agent/capability.py`, `qta_agent/netauth.py`, `qta_agent/secrets.py`,
`qta_agent/idempotency.py`, `qta_agent/governed_stage10.py`,
`qta_agent/governed_model.py`.

**WHAT WAS THERE** (`051acf7`), one shape in five places:

* capability, egress and secret grants were stamped `issued_seq = head + 1`
  from a head read before the append. Any writer landing between put the
  grant after the start it claimed, and replay refuses a backdated grant on
  EVERY load -- one benign race left a log no governed runner could open.
  Found when the concurrent resubmission test's winner died at its read
  grant; measured 3 runs out of 3 on the pre-fix tree with four processes
  interleaving grants (capability and egress both);
* `anoint` could write a second root the same way;
* the idempotency ledger was loaded once per runner and every lookup and
  bind answered from it: a second runner -- or a second process -- found a
  key another had bound since, free, created its own task, bound the key
  again (a rebinding replay refuses), and ran the work twice (measured on
  the pre-fix tree);
* the independent check of a model run had no key at all, so a crash during
  it left an orphan and concurrent requests ran several checks into one
  directory.

**REPAIR.** Grants are stamped under the writer lock at the position they
are written; the root is decided there against the log; the ledger catches
up in O(new) before a lookup and decides a binding under the lock, and a
submission that loses the bind cancels its own orphan before anything is
queued and answers with the bound task; a check is keyed by the record, the
check and its directory.

**EVIDENCE.** `tests/test_proposal_concurrency.py` (four processes, 96
interleaved grants, every one starting where it sits);
`tests/test_agent_idempotency.py` (a runner built before another's binding
finds it). Mutations X23, L4, E39 and S27 re-anchored to the decided stamps;
I11 and I13 to the decided bind.

**THE EXPIRY HALF, found reviewing this repair before it was committed.**
Stamping the start under the lock left the expiry where the caller
computed it, from the earlier head. The governed runner's compensation and
re-verification grants expire four positions after that head, so five
records from another writer moved the stamped start past the end: a grant
that was never valid, appended, and refused by every replay -- the same
unopenable log by another route (measured: the record was appended, the
writer's own fold raised, and every later load refused the log). Every stamped grant is now rebuilt by replay's own
constructor inside the locked decision, so one replay would refuse is
refused with nothing written; and the two one-action grants carry a
lifetime counted from their stamp (`lifetime_seqs=4`) instead of an
absolute expiry, so contention cannot leave them dead on arrival. A grant
tied to a lease keeps its absolute expiry -- outliving the lease is what
the bound exists to prevent -- and is refused, unwritten, if the lease has
ended by the time it is stamped. Tests:
`test_a_grant_the_stamp_moves_past_its_expiry_is_refused_unwritten`
(capability, egress, secret) and
`test_a_one_action_grant_lives_from_where_it_is_stamped`; mutations
DA9-DA12. G15 re-anchored to the lifetime.

## D-2026-118 — the undeclared-write sweep charged concurrent supervisors' appends to the running tool

**CLASS** — `CONCURRENCY`, harness completion programme (section 45).
`qta_agent/execution.py`, `qta_agent/governed_stage10.py`.

**WHAT WAS THERE** (`051acf7`). The sweep inventories a tool's writable
scope before and after the run and reports every change it did not declare.
The authority log, its witness and the evidence store sit inside that scope
on the governed path, so another supervisor appending while a tool ran was
reported as the tool's undeclared write, and the run refused FAILED --
fail-closed, never a false verdict, but no concurrent supervisor could
complete work. The sweep's own docstring already said a concurrent
process's change is not evidence against the tool; it had only excluded
changes outside the scope.

**REPAIR.** The runner's own durable stores are passed to the executor by
exact path and are not attributed to the tool. A write to them is judged by
the hash chain, the head witness and the separate-process verification.

**EVIDENCE.** The concurrent resubmission and first-submission tests, whose
winners now finish.

**NOT CLAIMED.** Anything about a tool writing the authority log on
purpose beyond what the chain, the witness and the separate verification
detect: the sweep never was, and is not now, the defence for that.

## D-2026-119 — every proposal receipt re-read and re-verified the whole log

**CLASS** — `PERFORMANCE` (quadratic in history), harness completion
programme (section 46). `qta_agent/proposals.py`.

**WHAT WAS THERE** (`051acf7`). `ProposalIngress.receive` asked whether its
proposal was already in the log by calling `received(self.log)`: a whole
verified read of the history, per receipt. `pending()` and `submit()` did
the same. Each call is linear in the log, so an ingress running beside the
rest of the system for a long history is quadratic -- the class of defect
that checkpointing and the event log's append each had before
(`tests/test_agent_long_horizon.py`'s module docstring). No short test
could see it. It was found by reading this path while adding proposals to
the long-horizon campaign, not by the campaign failing. Measured on the
pre-fix tree: one ingress beside another writer made 44 whole-log reads
for 44 receipts, after warm-up. On the repaired tree it makes 0.

**REPAIR.** The ingress keeps an anchored receipt view, as the scheduler,
the store and the idempotency ledger already do. It catches up in O(new)
since it last looked, and falls back to one whole verified read when it
has no anchor or the anchor no longer describes the bytes at its offset.
`received(log)` and the catch-up fold through the same function. The
receipt is still decided under the writer lock against the head it is
written onto (D-2026-115).

**EVIDENCE.** `tests/test_proposal_ingress.py`:
`test_a_running_ingress_reads_the_history_once_not_once_per_receipt` (0
whole-log reads across 44 receipts interleaved with another writer, every
receipt visible to both) and its anti-vacuity twin (a fresh ingress per
receipt is counted at one or more each). Mutations PI17 (the anchor is
ignored) and PI18 (the catch-up drops a receipt another writer appended).
The long-horizon campaign now keeps ONE ingress for the life of each
process and receives a proposal on every cycle.

**NOT CLAIMED.** That a fresh ingress per receipt is cheap. It is not, and
the campaign does not build one per receipt.

## D-2026-120 — recovery swallowed the state machine's refusal along with the race it meant to tolerate

**CLASS** — `DEFECT` (a defence weakened by its own repair), harness
completion programme (section 45). `qta_agent/governed_stage10.py`.

**WHAT WAS THERE** (`9cc4a10`). D-2026-115 made `recover()` step aside
when another supervisor had moved a stranded task first, by catching
`TaskTransitionError` around the move. That class is also what the task
state machine raises for a move it FORBIDS. The catch swallowed both. A
recovery that tried an illegal move -- dragging a COMPLETED task, which
waits for an independent verifier, back to the queue -- reported nothing
and carried on, instead of failing. Found by mutation V10 surviving
(13/14) in the run of `tools/mutations/agent_recovery.json` on that
commit's tree. Before `9cc4a10` the illegal move raised and V10 was
killed.

**REPAIR.** Losing the race is its own class, `TaskMovedUnderWriter`, a
subclass of `TaskTransitionError` raised only where the move finds the task
moved since its caller read it. `recover()` catches that and nothing else.

**EVIDENCE.** V10 is killed again, by
`test_a_completed_task_is_reported_not_resolved`. The race itself is now
tested deterministically:
`test_two_supervisors_recovering_one_task_requeue_it_once` (the second
supervisor steps aside, one requeue record). Mutation V15 makes a lost
race an error, and that test kills it: `agent_recovery` 15/15.

## D-2026-121 — two runtime builders refused on stdout, which their callers send to a file

**CLASS** — `DEFECT` (a refusal nobody can read), harness completion
programme. `tools/fenicsx_env.py`, `tools/isolated_runtime.py`,
`.github/workflows/harness-integrations.yml`.

**WHAT WAS THERE** (`9cc4a10`). Both tools print their JSON record on
stdout and, on failure, print `... REFUSED: <reason>` on stdout as well.
The end-to-end job redirects stdout into `fenicsx_environment.json`. Its
environment step failed in both hosted runs of `9cc4a10`, and the log ends
at the micromamba post-link warning and `exit code 1`, with no reason. The
reason is in a file the job uploads as an artifact. The fenicsx job runs
the same command through `tee` and passed in the same push. The FEniCSx
import probe also discarded its own stderr, so a failed import could only
say "returned non-zero exit status".

**REPAIR.** Refusals go to stderr. The import probe's stderr is carried
into the refusal. The end-to-end job tees both records, as the fenicsx job
already did.

**NOT CLAIMED.** The cause of that end-to-end failure. It was not visible in
the log of the commit it happened on. The next hosted run of the job either
passes or says why it fails.

## D-2026-122 — the RO-Crate used a key its JSON-LD context does not define, and the internal validator passed it

**CLASS** — `WRONG_SPECIFICATION` (the internal validator held a weaker
rule than the specification), harness completion programme (R65).
`ro_crate_tools.py`, `tools/ro_crate_conformance.py`.

**WHAT WAS THERE** (`9cc4a10`). Every data entity carried a `sha256` key,
and the crate's `@context` was the RO-Crate 1.1 context alone, which does
not define `sha256`. RO-Crate 1.1 requires the descriptor in compacted
JSON-LD, which admits only defined terms. The community validator refused
the crate on hosted CI: "The 25 occurrences of the JSON-LD key "sha256"
are not allowed in the compacted format" (check `ro-crate-1.1_3.1`,
REQUIRED). It is the next refusal after D-2026-111: with the types fixed,
the check that had been reached second was reached first. The internal
validator never looked at keys, so the two validators disagreed on the
committed crate.

**REPAIR.** The crate defines the term in its own `@context`, mapped to
the workflow-run vocabulary's `sha256`
(`https://w3id.org/ro/terms/workflow-run#sha256`). The community
validator's Process Run Crate profile uses that same IRI for a checksum.
The internal validator now refuses any key that is neither one of the
RO-Crate context terms it knows the crate uses nor defined in the crate's
own context. That list is short and known, so an unlisted real term is
refused rather than an undefined one accepted. The conformance tool gains
a negative control, `undefined_term`: the crate with the term's
definition removed.

**EVIDENCE.** `tests/test_ro_crate_conformance.py`:
`test_every_key_is_a_term_some_context_defines`; mutation RC6. The
external verdict on the repaired crate is the next hosted ro-crate run's.
The validator cannot fetch the RO-Crate context here (w3id.org is not
reachable from this sandbox).

## D-2026-123 — the sweep still charged a concurrent append's witness temp file to the running tool

**CLASS** — `CONCURRENCY` (an incomplete repair), harness completion
programme (section 45). `qta_agent/execution.py`,
`qta_agent/governed_stage10.py`, `qta_agent/governed_model.py`.

**WHAT WAS THERE** (`9cc4a10`). D-2026-118 stopped attributing writes to
the log, its head witness, its lock and the evidence store to the running
tool. But the witness is replaced atomically, through a temp file
`.head-*.tmp` beside it, so another supervisor's append leaves one in the
directory for an instant. Inventoried before a tool ran and gone after, it
was reported as `deleted`. The winning run of a four-process first
submission was refused FAILED for a file it never touched. This was found
by repeating `tests/test_proposal_concurrency.py` under load: 1 failure in
6 runs, `verification/stage10/_pytest_proposal_crash/.head-1tmb2_62.tmp:
deleted`. The deterministic test below fails on `9cc4a10` with the run
FAILED. It fails closed, never a false verdict, but concurrent supervisors
could still fail each other's runs.

**REPAIR.** The exclusion list takes name PATTERNS at their own depth, and
the runner names `<log dir>/.head-*.tmp`. A pattern never covers a
subdirectory, so a tool cannot hide a file under a matching directory name.
A governed model runner also names the checkpoint stores it restarts from
or writes to, its other supervisor store that can sit inside a tool's
scope.

**EVIDENCE.** `tests/test_decided_appends.py`:
`test_a_concurrent_appends_witness_temp_file_is_not_the_tools` (a temp file
there when the tool starts and gone when it ends, and another the other
way round; the run is VERIFIED with no undeclared writes) and
`test_a_supervisor_name_pattern_does_not_cover_a_subdirectory`. Mutations
DA13 (a pattern covers subdirectories) and DA14 (the runner does not name
the temp file). DA8 is re-anchored.

**NOT CLAIMED.** A store a runner is not told about, written by another
supervisor inside a tool's scope, is still inventoried and charged to the
tool, which fails closed. The way to avoid that is to keep stores out of
tool scopes or to name them in `supervisor_stores`.

## D-2026-124 — the FEniCSx runs let MPI probe the host's network, and on some hosted runners MPI_Init aborted

**CLASS** — `ENVIRONMENT` (a dependence on the runner's hardware that the
check never needed), harness completion programme (R62, R69).
`scientific/checks/fenicsx_slab.py`, `tools/fenicsx_env.py`.

**WHAT WAS THERE** (`d432d9d`, and every commit since the FEniCSx check
landed). The pinned conda-forge MPICH initialises through UCX, and UCX
probes whatever transports the host offers, network devices included. The
check and the environment builder's import probe are each ONE process and
need no network, but they offered MPI everything. On some hosted runners,
MPI_Init aborted before any of the check ran:
`MPIDI_UCX_init_worker(86): ucx function returned with failed status ...
Input/output error`. The end-to-end job failed this way on all four of its
runs at `9cc4a10` and `d432d9d`, and the fenicsx job on one of four. The
reason was invisible on `9cc4a10` (D-2026-121) and visible on `d432d9d`.

**REPAIR.** Both run with `UCX_TLS=self,sm`: the process itself and shared
memory, which is all one process uses. The check states the constant, the
builder restates it (it imports nothing from the repository), and a test
holds the two equal. Measured here: dolfinx and petsc4py import with the
restriction, the acceptance passes (8/8 criteria, 3/3 controls rejected),
and the demonstration's tests pass with both runtimes REQUIRED. An
unusable transport forced through the same variable aborts MPI_Init here
too, so the variable does select the transport.

**EVIDENCE.** `tests/test_harness_integrations.py`: the two constants
equal; the check's subprocess gets them; the import probe gets them (a
stand-in interpreter reports its environment), and a probe that aborts
is refused with its own words. Mutations FE3 and FE4.

**NOT CLAIMED.** Which device on which runner made UCX fail. The failing
host is not reachable from here. What is shown is that the runs no longer
depend on it.

## D-2026-125 — D-2026-111 said the community validator refuses a data entity typed neither File nor Dataset; measured, it does not

**CLASS** — `WRONG_CLAIM`, harness completion programme (R65).
`tools/ro_crate_conformance.py`, `docs/DEFECT_LEDGER.md` (D-2026-111).

**WHAT WAS THERE** (`d432d9d`). D-2026-111 added the negative control
`data_entity_not_a_file` and said it "must be refused by both validators".
That was inferred by reading the profile, because the tool did not yet
print the external validator's reasons. The REQUIRED refusal actually
raised at the time was the undefined `sha256` keys (D-2026-122). Once
those were defined, hosted run 37963760144 measured the control on its own:
the community validator (roc-validator 0.12.2, ro-crate-1.1 profile,
REQUIRED level) ACCEPTED a data entity typed `SoftwareSourceCode` alone.
The internal validator refused it, as RO-Crate 1.1's File data entity rule
requires, so the job failed DISAGREE.

**REPAIR.** The internal rule stays. The conformance tool records this one
control as `INTERNAL_STRICTER`, measured and named with its reason in the
report and the log, not as agreement. It applies in one direction only. If
the internal validator accepted the control, the job fails whatever the
external says. Every other control must still agree, and the committed
crate must be accepted by both validators, which hosted run 37963760144
showed it now is.

**EVIDENCE.** `tests/test_ro_crate_conformance.py`:
`test_internal_stricter_is_measured_one_way_and_named` (the one control
passes as stricter; the same verdict on any other control fails; an
internal acceptance of it fails). Mutation RC7 (every disagreement called
stricter).

## D-2026-126 — the second reader's import guards could not see `from . import X`, and every list they checked was a deny-list

**CLASS** — `VERIFIER_INDEPENDENCE_DEFECT`, harness completion programme (§62 hostile
review: "a second reader imports the first"). `tests/test_agent_second_reader.py`,
`tests/test_agent_differential.py`, `tools/independent_verify.py`.

**WHAT WAS THERE** (`0dc60e1`). `qta_agent/reconstruct.py` is the second
reader, and it stays independent only if it imports none of the layers it
checks. Three AST tests guarded that. All three recorded `node.module` for
an `ImportFrom` and skipped the node when `node.module` was None. In
`from . import authority`, `node.module` is None and the module is in the
alias, so that spelling of the import passed all three. The call guard
looks for functions named `check`, so it would not have noticed
`authority.State` or `capability.CapabilityLedger(log).load()` either. The
names the guards checked against were a deny-list of ten layers. The
package now has some thirty-five modules, and first readers added since
(result_rules, learned_rules, proposals, invalidation, principals,
checkpoint) were on no list.

The runtime guard in `tools/independent_verify.py` had the same shape. It
refused the names in `FORBIDDEN` and admitted every other module.
`FORBIDDEN` never named `qta_agent.authority` or `qta_agent.tasks`, the two
gates the reader was decoupled from in D-2026-27. So the process built to
be the one place the shortcut is unavailable would have loaded either one
without a word.

Measured before the repair, against the guards at 1d14ae0:
`from . import authority` at module scope SURVIVED all three tests (R104).
`from .result_rules import record_problems` also SURVIVED (R106).
`from . import capability` inside `reconstruct_subsystems` was killed
(R105), but only by the runtime guard, because capability is in
`FORBIDDEN`. An unnamed module in that position would not have been.

**REPAIR.** No production behaviour changes. Today `reconstruct.py`
imports only `actions` and `events`, and the verifier process loads only
`qta_agent`, `actions`, `canonical`, `events` and `reconstruct`.

* The three AST guards now record the alias of `from . import X`.
* A new test holds `reconstruct.py` to an allow-list: the standard modules
  it uses, plus `qta_agent.actions` and `qta_agent.events`.
* `tools/independent_verify.py` refuses every `qta_agent` module outside
  `PERMITTED`, the five modules above, measured.
* `FORBIDDEN` stays as the named primaries, now including authority, tasks
  and the newer first readers.
* Each named primary, and two modules nobody named, is probed in its own
  process and must be refused.
* The set the verifier loads on a real run under its own guard must equal
  `PERMITTED`.

**EVIDENCE.** `tests/test_agent_second_reader.py`:
`test_the_second_reader_imports_only_the_log_and_the_action_names`.
`tests/test_agent_separate_verify.py`:
`test_every_named_primary_is_refused_including_the_two_gates`,
`test_a_module_nobody_named_is_refused_too`,
`test_the_permitted_modules_are_what_the_verifier_actually_loads`.

Mutations:

* R104 to R106 (`agent_second_reader`): the bare spelling, at module scope
  and in a function body, and a first reader the deny-list never named.
* S2 (`agent_separate_verify`): re-targeted to the deny-list the guard
  used to be.
* S12 and S13: `PERMITTED` admits the authority gate, or a module the
  verifier never loads.

## D-2026-127 — the release candidate's verify said MATCHES over an archive that had lost files, and never compared the archive with its own manifest

**CLASS** — `MISSING_ENFORCEMENT`, harness completion programme (§62
hostile review: "a signature checks bytes other than those in the
manifest"). `tools/supply_chain.py`, R65.

**WHAT WAS THERE** (`0dc60e1`). The signature binds `SHA256SUMS`, which
binds `source.zip`. `verify` compared each archive member with the commit's
blob of the same path and then reported `source_tree: MATCHES <commit>`. It
never compared the commit's tree with the members. The module docstring
says the archive holds "every tracked file at that commit", but nothing
checked that.

The manifest inside the archive (`final_manifest.json`, every tracked file
with its size and sha256, and `manifest_hash.txt` beside it) was compared
only by digest with `index.json`. Nothing compared it with the archive it
was packed in. So a signed archive could lack files its own manifest lists,
carry files it does not list, or carry a manifest describing other bytes,
and still verify. `index.files_in_source_zip` was written and never read.

Measured at 1d14ae0. A build whose archive dropped `qta_agent/authority.py`
and was otherwise consistent verified as:

```
{"accepted": true, ..., "source_tree": "MATCHES 1d14ae0ae07bd1e820128b26fd4d9c2a9575b4ca"}
```

That candidate would have been signed with the archive's own manifest
still listing the missing file.

**REPAIR.**

* `archive_against_manifest` runs with or without a checkout. It refuses an
  archive that:
  * names a member twice;
  * has a member count that differs from the index's;
  * has a detached hash that is not the hash of the manifest beside it;
  * has a manifest listing no files, which every per-file comparison
    would pass by examining nothing;
  * lacks a listed file;
  * contains an unlisted one (the two detached files aside);
  * has a member whose bytes or size are not the manifest's.
* With a checkout, the archive's member set must equal the commit's
  non-link files, in both directions. Then every member is compared with
  its blob as before.
* `source_zip` returns the names it packed rather than the names it
  listed, so the index count is a count of members.
* The report gains `manifest: DESCRIBES THE ARCHIVE (n files)`.

**EVIDENCE.** `tests/test_supply_chain.py`, using a repack helper that
recomputes every digest above the archive, so each forgery is consistent
everywhere except the layer under test:

* `test_an_archive_that_lost_a_tracked_file_is_refused` (manifest
  rewritten to match, so only the commit layer can refuse);
* `test_the_archive_is_what_its_own_manifest_says` (lost, changed,
  unlisted, detached hash, count, empty; with no checkout, so only the
  manifest layer can refuse);
* `test_a_member_named_twice_is_refused`;
* `test_a_member_whose_bytes_are_not_the_commits_is_refused`. The
  first mutation run of the new layering showed SC7 (the per-member blob
  comparison removed) SURVIVING, because the set comparison now refused
  every planted file first. This case keeps the member set and the
  manifest consistent, so only the blob comparison can refuse it;
* `test_the_unforged_archive_describes_itself_without_a_checkout`.

Mutations SC12 to SC20 (`supply_chain`): one per check, and the whole layer
removed.

## D-2026-128 — the FMI boundary admitted a unit by its name; what the FMU defined it to mean was never read

**CLASS** — `TRUE_DEFECT`, harness completion programme (§62 hostile
review: "an FMU import drops units"). `scientific/fmi_boundary.py`,
`tools/fuzz_substrate.py`, R63.

**WHAT WAS THERE** (`0dc60e1`). FMI 3.0 defines a unit in the model
description: `<Unit name="K"><BaseUnit K="1"/></Unit>`. The `BaseUnit`
gives SI exponents, a factor and an offset, so a value in the unit maps to
SI. The boundary read each unit's NAME and checked three things:

* every Float64 parameter, input and output has a unit;
* the name appears in `UnitDefinitions`;
* the name equals the contract's (FMI-P5).

The definition itself was never read. Measured at 0dc60e1 on the real
thermal_rc2 FMU, each of these was ADMITTED with an empty contract
difference:

* `K` redefined with `offset="273.15"` (Celsius values labelled kelvin);
* `W` redefined with the exponents of energy;
* `K` declared with no `BaseUnit` at all.

Float32, the other float type in FMI 3.0, carried no unit rule at all. The
fuzz oracle restated the same name-only rule, so it could not tell either.

**REPAIR.**

* The boundary reads each `UnitDefinitions/Unit` into (exponents, factor,
  offset), and refuses a name defined twice or a non-numeric attribute.
* For every Float64 and Float32 parameter, input and output, it requires
  that:
  * the unit has a `BaseUnit`;
  * the harness holds a definition of that name (`SI_DEFINITIONS`: s, K,
    J, W, J/K, W/K, the units the contract uses; extending it is a
    reviewed act);
  * the FMU's definition is exactly that one, with factor 1 and offset 0.
* The fuzz oracle now parses `UnitDefinitions` from the bytes itself and
  holds every accepted float variable to the same rule.

**EVIDENCE.** `tests/test_harness_integrations.py::test_the_boundary_refuses_a_tampered_fmu`
gains seven cases: a Celsius-offset K, an energy-dimensioned W, a
milli-scaled K, a K with no definition, a unit the harness does not hold
(mK), a unit defined twice, and a Float32 output with no unit.
`tests/test_agent_fuzz.py` covers the stronger oracle.

Mutations FB9 to FB16 (`harness_integrations`): the comparison skipped,
undefined or unknown units admitted, offset, factor or dimension not
compared, duplicate definitions allowed, and Float32 exempted.

## D-2026-129 — an XML declaration naming an unknown or multi-byte encoding crashed the FMI boundary instead of being refused

**CLASS** — `TRUE_DEFECT`, harness completion programme (R63). Found by the
hosted fuzz campaign. `scientific/fmi_boundary.py`.

**WHAT WAS THERE** (since the boundary was written). `_parse_xml` refused
a DOCTYPE or entity and converted `ET.ParseError` into `FmuRefused`. The
XML declaration's encoding went straight to the parser. An unknown codec
(`encoding="UTF-x"`) or a non-text one (`rot13`, `hex`) raises
`LookupError`. A multi-byte encoding (`utf-32`, `EUC-JP`) raises
`ValueError`, and `idna` or `punycode` raise `UnicodeError`. None of these
is a refusal, so `describe` crashed on them.

The agent-substrate job of pull_request run 37977178632 (at `d59c3c6`, job
113978304206) found it. Its fuzz campaign, seed 1103025227 with 2000 cases,
reported `CRASHED fmu_description: LookupError: unknown encoding: UTF-x`.
Every earlier campaign drew other seeds. The same seed reproduces it
locally, exactly. Separately, `ISO-8859-1`, which expat supports natively,
PARSED, though FMI 3.0 model descriptions are UTF-8.

**REPAIR.** The declaration's encoding is read before parsing. Anything
but UTF-8 is refused with the reason ("FMI 3.0 requires UTF-8"). Invalid
UTF-8 bytes stay expat's refusal. `ValueError` and `LookupError` from the
parser are converted to refusals as a backstop for a declaration the check
did not read. That backstop is defence in depth: with the check in place
no input is known to reach it, so no mutation measures it alone.

**EVIDENCE.** `tests/fuzz_corpus/fmu_description-crashed-09459953.json`
(the hosted finding's input, replayed on every run, with the checkout path
in its traceback replaced by `<repository>`).
`tests/test_harness_integrations.py::test_the_boundary_refuses_a_tampered_fmu`:
`unknown_encoding`, `multibyte_encoding`, `latin1_encoding`. Seed
1103025227 now reports no findings, and a fresh 4000-case campaign is
clean. Mutation FB17 (the encoding check disabled; ISO-8859-1 is then
admitted).
