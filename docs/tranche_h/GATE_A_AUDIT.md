# Tranche H, Gate A: audit, threat model and gated plan

Starting reference `9e5ac06`. This is an audit and a plan. **No gate after A
has started.**

- **Model calls:** none. No model was called, no autonomous decision was
  made and no model credential was used.
- **Code:** no implementation file changed. The one repository change beyond
  these documents attaches existing hosted evidence to one readiness row
  (SUB-2); see F-H1 below.

The machine-readable capability matrix is
[`capability_matrix.json`](capability_matrix.json): 41 rows, held to the
tree by `tests/test_tranche_h_audit.py`. This document summarises it, adds
the threat model and the interface map, and sets out the gated plan.

## 1. Starting state, verified

- Branch `claude/scientific-python-stack-7rsq3m` at `9e5ac06`, both locally
  and on origin, with a clean working tree. PR #17 is open and unmerged.
- All 44 hosted check runs at `9e5ac06` succeeded.
- **Software completion:** `COMPLETE_TO_CURRENT_TECHNICALLY_DEFENSIBLE_LIMIT`,
  with 0 unsatisfied conditions.
- **Production readiness:** NOT MET. 10 rows are hosted-tested after F-H1;
  3 need a deployment's attestation, 5 are blocked on owner actions and 2
  are unverified.
- **Scientific and model evidence:**
  - the scripted grounding benchmark (Tranche G); its real-model condition
    was NOT_RUN;
  - NF-1T's computed claims: the flagship is untrained, and a
    431,784-parameter development member was trained.
- **Model-provider credentials.** None is provisioned for the harness. The
  environment this audit ran in carries the coding session's own
  infrastructure credentials. They are not the harness's, and they were not
  used: using them would be paid inference without authorization (OD-1).

## 2. What exists, and what does not

| State | Rows | What they are |
|---|---|---|
| IMPLEMENTED | 20 | the substrate: event log, decided appends, policy, capabilities, scheduler, the governed model path, memory and its trust audit, checkpoints, the second reader, confinement, the release engine, local retrieval, the scientific catalogue and its checks, NF-1T as stated, and the scripted benchmark |
| PARTIAL | 10 | adapters with no bounds; egress with no kernel restriction for a network-enabled process; context built for tool runs but never for a model; the action set; human escalation with no human; authentication with test keys only; single-host coordination; units without symbolic maths; hostile inputs with no model in the loop; the third status system |
| ABSENT | 7 | the inference gateway; objectives and hypotheses; the decision controller; swarm roles; formal proof checking; external retrieval; token and cost accounting |
| EXTERNALLY_BLOCKED | 3 | live inference (OD-1); the real-model grounding experiment (OD-6); CI authority on the default branch (O-1 to O-9) |
| DEPLOYMENT_REQUIRED | 1 | tool quotas (readiness RUN-4) |

**The gaps, reproduced at `9e5ac06` rather than asserted** (the script's
output is in the Gate A commit message):

| | Observed |
|---|---|
| R1 | `ExternalClientAdapter(respond)` takes a callable and nothing else. It has no timeout, retry, output bound, budget or token accounting. |
| R2 | `ProposalIngress.submit` refuses every kind but MODEL_RUN: "only MODEL_RUN is executed, the rest is recorded for a person". |
| R3 | The rationale is kept by digest only. Its text is not in the log. |
| R4 | The action registry has no `inference.*`, `objective.*` or `hypothesis.*` action. |
| R5 | A proposal's context binds `{digest, retrieved}`. That is retrieval only: no authority head, task state, policy or context manifest. |
| R6 | `principals.READER_COVERAGE["hypothesis lifecycle"]` is `NOT_BUILT`. |
| R7 | No symbolic engine (`sympy`) and no proof checker (`z3`, Lean) is installed. |
| R8 | The provider and model a response claims are recorded unchecked. This is correct, and must stay so: a claimed identity is never proof. |

**F-H1, an evidence gap repaired here.** Readiness row SUB-2 (the T-2
closure) stayed IMPLEMENTED because the Tranche G evidence commit `9e5ac06`
attached the five rows that had been hosted before and missed this one. Its
five tests and two mutation specifications ran green in both hosted
agent-substrate runs at `1586322`. They are now attached, each run re-checked
against the API and the implementation digest taken from that commit's tree,
by `tools/readiness.py attach`. Nothing about the code changed. Recorded as
D-2026-161.

**Not to be confused** (H1): software-contract completion is not production
deployment, and neither is scientific validity or autonomous-agent
capability. The matrix's IMPLEMENTED means *the code exists and its tests and
mutations pass in hosted CI*. It says nothing about any model's behaviour.

## 3. Threat model for real-model integration

### Trust zones

| Zone | What runs there | Network | Credentials | Writes to the log |
|---|---|---|---|---|
| Z0 owner | the person; out of band | — | production keys (none exist yet), the provider key, the human principal | publishes policy; answers escalations |
| Z1 control plane | the substrate: log writer, policy, capabilities, scheduler, ingress, and the future controller | **none** | none | yes, under its identities |
| Z2 model gateway (new) | one process per provider: holds the key, calls the provider | the provider's host only | the provider key, revealed in this process only | **no**: it returns bytes to Z1, which records them |
| Z3 workers | governed tool processes | **none** (network namespace) | none (scrubbed environment) | no |
| Z4 verifiers | the second reader in another process; independent checks | none | none | their own verdict records |
| Z5 outside | the provider, retrieved sources, everything else | — | — | — |

**Everything from Z2 and Z5 is untrusted input**: model output, retrieved
documents, tool output, and messages between agents. The boundary the system
rests on is unchanged. A model's output can only become a proposal, and a
proposal can only be decided by policy, capability and a separate reviewer.

### Assets

1. The integrity of the event log and of the authority states
   (VERIFIED/PROMOTED).
2. The evidence store.
3. The provider credential.
4. The spending budget.
5. Policy documents.
6. The human escalation channel.
7. The confidentiality of whatever the context must not carry.

### Threats

| | Threat | Exists today | Needed |
|---|---|---|---|
| T-H1 | Malicious or mistaken model output: authority keys, paths, oversized records, a forged verdict | the envelope's closed schema, forbidden keys, credential refusal, content-addressed id | schemas for each new action kind; mutations |
| T-H2 | Prompt injection through retrieved text, tool output or another agent's message, steering the model | retrieved spans stamped NOT_EVIDENCE; proposals have no authority | context tiers that label untrusted content; a campaign showing an injected instruction never yields an unauthorized action, with a control |
| T-H3 | Credential exfiltration: the key in a prompt, a log line, a tool environment or an error | `SecretRef`, the redactor, egress-bound grants, credential refusal in proposals | the key revealed only in Z2; tests that no context, log, proposal or tool environment ever holds it |
| T-H4 | Runaway spend, retry storms, infinite loops | none | a budget ledger decided under the writer lock; step and time caps; rate limits; stop on exhaustion |
| T-H5 | Provider spoofing or a forged response | claimed identity recorded unchecked | response digests and transport facts recorded; never relied on |
| T-H6 | A compromised gateway pivoting to the network | — | gateway process separation; no log authority in Z2; kernel egress restriction is a deployment requirement |
| T-H7 | Self-verification and collusion among model agents, which may share one underlying model | separation of duties in the store; the collusion tests | verification stays deterministic and independent; an AI reviewer proposes criticism, never verification; model identity recorded per role |
| T-H8 | Stale or poisoned memory and summaries steering decisions | non-authoritative memory; the trust audit; omissions in manifests | a test where a poisoned summary does not steer an authorized action |
| T-H9 | Duplicate delivery, or a crash between call and record, causing duplicate side effects | idempotency keyed by proposal id; decided appends | idempotent inference records by request id; reconciling orphaned requests |
| T-H10 | Evaluation leakage in the real-model experiment | the hidden-oracle discipline of Tranche G | the same discipline, re-tested |
| T-H11 | A forged human decision | only a registered HUMAN principal may answer, and none is registered here | the owner registers one out of band (OD-4) |
| T-H12 | A log truncated together with its head witness (M-3) | accepted limitation, documented | an external head anchor (deployment) |
| T-H13 | Writers on more than one host | none: flock on one host | out of scope until OD-7 |
| T-H14 | Nondeterministic model output | `context.py` separates the three meanings of "reproducible" | record outputs; reproduce recorded decisions, never claim reproducible tokens |

## 4. Interface map

The path one model decision will take. **NEW** marks what does not exist yet;
everything else is reused as it is.

```
objective (NEW research record, Z1)
  -> controller step (NEW, Z1): reconstruct state       reconstruct.py, store.py
  -> context for the decision                           context.py ContextBuilder; record_context -> context.build
  -> budget check (NEW ledger, decided under the lock)
  -> inference.request recorded (NEW action)
  -> gateway call (NEW, Z2)                             secrets.py reveal for the provider's host only;
                                                        netauth.py grant + socket_guard
  -> inference.result or inference.failure recorded     digests, usage, cost, latency, claimed identity
     (NEW actions)
  -> envelope(...)                                      proposals.py (schema /2 binds the context manifest
                                                        and the inference record)
  -> ProposalIngress.receive -> proposal.receive        NON_AUTHORITATIVE
  -> policy evaluate + capability check                 policy.py, capability.py
  -> Scheduler enqueue/dispatch under a lease           scheduler.py, tasks.py
  -> governed execution in Z3                           governed_model.py, governed_tasks.py, execution.py
  -> independent check in Z3/Z4                         scientific/checks/*, separate verifier identity
  -> decision by a reviewer who is none of them         store.py (a HUMAN principal for escalations: OD-4)
  -> research records updated (NEW): observation,
     hypothesis revised or retracted, missing-information record
  -> memory note (non-authoritative)                    memory.py
  -> second reader restates every NEW record            reconstruct.py
  -> next step, or stop / abstain / escalate
```

## 5. The gated plan

Each gate is a separate acceptance gate, run under the established
procedure:

1. **Before the commit:** local full suite, mutation specifications for every
   changed enforcement point, hosted CI, an evidence commit, and a ledger
   entry for each defect found.
2. **Order of work:** audit, reproduce, contract, implement, adversarial
   test, independent verification, evidence, derived status.

A gate does not start because its predecessor compiles.

### Gate B: real-model ingress (H2, part of H11 and H16)

| Stage | Work | Acceptance |
|---|---|---|
| B1 | The gateway contract, offline (`agent_substrate/inference.py`, provisional name): request and result records; a failure taxonomy (`OK`, `SCHEMA_REFUSED`, `PROVIDER_ERROR`, `TIMEOUT`, `RATE_LIMITED`, `BUDGET_EXHAUSTED`); bounded retries; a deterministic fixture provider; `inference.*` actions classified in the authority inventory and restated by the second reader | every call bounded and recorded, success or failure; both readers agree |
| B2 | Budget and rate ledger, decided under the writer lock | a call over budget is refused before it is sent; two processes cannot overspend between them |
| B3 | Context binding: proposal-envelope/2 cites the context manifest digest and the inference record id, and /1 receipts still parse; the rationale summary is kept as a bounded evidence blob cited by digest | a proposal's decision context is reconstructable from the log alone |
| B4 | One provider adapter, run in a separate gateway process: TLS, timeouts, a response-size cap, strict structured output; the key from `FileSecretProvider`, revealed only for the provider's host under an egress grant | offline tests prove CI needs no key and no network; the key appears in no context, log, proposal or tool environment |
| B5 | Hostile campaign and mutation specification: malformed or oversized output, schema violations, injected instructions, an echoed credential, a slow provider, rate limiting, budget exhaustion, duplicate requests, a crash between request and result | each refused or recorded as a failure, with a control |
| B6 | The third status system: a derived SCIENTIFIC_AND_MODEL_EVIDENCE registry (model evidence, benchmark results, NF-1T claims), unable to promote either of the other two | three registries, none changing another's verdict |
| B-live | One real model produces one valid proposal, which enters as NON_AUTHORITATIVE; a malformed one is refused before execution | **EXTERNALLY_BLOCKED on OD-1.** Until then the offline gateway is the deliverable, and the live result is NOT_RUN |

### Gate C: single-agent autonomy and recovery (H3, H4, part of H11)

| Stage | Work | Acceptance |
|---|---|---|
| C1 | A research-record subsystem, non-authoritative, with its own reducer and the second reader's restatement: objectives, subgoals, hypotheses with dependencies, observations, missing-information records; retraction, supersession and invalidation propagation | both readers agree; a retracted hypothesis cannot be cited as current |
| C2 | Action kinds for the loop: check request, retrieval, record or revise a hypothesis, request review, abstain, stop. Each maps to a registered tool contract or to a non-authoritative record. **None maps to a shell, a repository write or a release** | refused if it does not map; mutations |
| C3 | The controller: bounded steps, budget, stopping criteria, retries, escalation; durable progress, resumable from the log | a scripted model completes a multi-step objective with every action traceable to a proposal, a policy decision and an execution record |
| C4 | Reset and crash campaign at adversarially chosen points: between request and result, between receipt and submit, mid-tool, mid-verification; with corruption, stale memory, poisoned summaries, truncation, an altered head witness and invalidated sources | after a fresh-context restart the reconstructed task state is exact, and the next action relies on no invented prior work |
| C-live | The same with a real model | blocked on OD-1; escalation answers need OD-4 |

### Gate D: real-model grounding experiment (H8)

1. A new protocol, pre-registered and committed before any run. The Tranche
   G scripted benchmark and its 10.2% and 7.8% stay exactly as they are:
   scripted-only.
2. Conditions that isolate one intervention each. In particular, claim
   checking alone (no substitution of the state's answer) is separate from
   answering from the state, which Tranche G's review showed were confounded.
3. Claim-extraction errors are reported separately from verification errors.
4. Negative controls that must defeat the system.
5. Cost, latency and tokens are recorded.

**EXTERNALLY_BLOCKED on OD-1 and OD-6.** The real-model results stay NOT_RUN
until a model is actually called.

### Gate E: multi-agent swarm (H5)

The four roles (coordinator, researcher A, researcher B, adversary) as
`AgentDirectory` registrations, each with its own capabilities, context and
memory view. Coordination runs on the existing scheduler, leases, messages,
claims and resolutions.

Requirements:

- bounded delegation;
- duplicate suppression through idempotency;
- budget allocation and cancellation;
- cross-agent invalidation;
- a matched-budget comparison against one agent.

Agreement among agents is never verification. Agents that share one
underlying model are recorded as such, because they are not independent.
Acceptance is H5's: a disagreement resolved with no unauthorized promotion
and no lost history.

### Gate F: scientific research workflow (H6, H7, H10's roadmap)

- **Tool contracts:** model-facing contracts for the admitted models and
  checks (FEniCSx, FMI, the closed forms, OpenMDAO, SALib, HDF5, RO-Crate).
- **Symbolic and dimensional checks,** if adopted (OD-5).
- **Proof candidates:** machine-checked, or reported as unchecked (OD-5).
- **Simulations:** each records its assumptions, units, method, solver
  version, tolerances, convergence and error estimates.
- **Retrieval:** local first. External retrieval is designed but not built
  until OD-3.
- **NF-1T:** a scaling and integration roadmap (H10). It is a document, no
  compute is spent, and no current claim changes.

### Gate G: long-horizon operation (H9)

Separate counters for:

- events;
- governed operations;
- inference calls;
- genuine decisions;
- verified tasks;
- conclusions;
- resets;
- restarts.

The horizons are staged at 20–50, 100, 1,000, 3,000–5,000, and 10,000 or
more genuine decisions, and each needs its own budget (OD-6). Scripted
stress counts stay distinct from model decisions. The fault-injection matrix
is H9's.

### Gate H: operational hardening (H11 to H13)

Deployment attestations RUN-2 to RUN-4, production keys (SUB-5), the release
trust root and revocation (REL-4, REL-5), the owner actions O-1 to O-9, and
an operator status view derived from the log. Every one is the owner's to
authorize.

## 6. Owner decisions

Nothing below is assumed. Each gate stops at the boundary of the decisions it
needs.

| | Decision | Needed by |
|---|---|---|
| **OD-1** | Live inference: which provider and model; how the key is provisioned (a file outside the repository, read by `FileSecretProvider`, never in an environment a tool can see); a spending cap per gate; rate limits | live parts of B, C, D, E, F, G |
| OD-2 | Whether live-model runs may ever execute in hosted CI. Recommended: never. Live runs are made by hand and recorded as evidence bundles, and CI stays offline | B |
| OD-3 | External research retrieval: the source allowlist, the licence and usage-rights policy, bandwidth limits | F |
| OD-4 | A human principal for escalations and reviews, registered by the owner out of band (no agent may perform that bootstrap) | live C, E, F |
| OD-5 | A symbolic-mathematics dependency, and a formal proof checker or the decision to have none | F |
| OD-6 | Inference budgets for the grounding experiment and each long-horizon stage | D, G |
| OD-7 | Whether a multi-host deployment is intended | H |

The existing owner actions O-1 to O-9 (`docs/security/owner_actions.md`)
remain open. In particular, the default branch's agent workflow is still
ungated (CI-1).

## 7. What Gate A does not claim

- That any model can do anything in this system: no model has been
  connected.
- That an IMPLEMENTED row is production-ready, scientifically valid or
  autonomous.
- That the plan's estimates are measurements. It sets acceptance criteria;
  meeting them is the evidence.
- Anything about NF-1T beyond its computed claims. The flagship is a
  scientific-feature-token MoE design, neither allocated nor trained, and it
  is not a language model.

## 8. Gate A report (H18)

| | |
|---|---|
| Starting and ending commit | `9e5ac06`, then the Gate A commit |
| Files changed | this document; `capability_matrix.json`; `tests/test_tranche_h_audit.py`; readiness row SUB-2 (F-H1); ledger D-2026-161; derived files regenerated |
| Reused or new components | none built; all existing components audited |
| Model calls, autonomous decisions | 0, 0 |
| Governed task completions, verified results | none new |
| Security boundaries tested | none new (the existing suites ran unchanged) |
| Scientific claims | none supported or newly made |
| GPU, network, money | none |
| Status | **Software completion** reads INCOMPLETE (3 unsatisfied) at the Gate A commit. Adding two files moves the derived inventories that rows R70 and R71 name (the typed-file count and the content audit), so those rows' hosted evidence predates them. An evidence-only commit restores them after hosted CI at this commit, by the Tranche G procedure. **Readiness:** NOT MET, with 10 rows hosted-tested. **Real-model evidence:** NOT_RUN |
| External blockers | OD-1 to OD-7; O-1 to O-9 |
| **Next justified gate** | **B1–B6 offline**: the gateway contract, budget ledger, context binding, adapter, hostile campaign and evidence registry, needing no owner decision. B-live waits for OD-1 and OD-2 |
