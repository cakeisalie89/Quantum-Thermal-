# Evidence terms

Each check in this repository that answers a question about evidence answers
exactly one of the nine below, and no other. The table is generated from
`agent_substrate/evidence_terms.py` (`render_table`), and
`tests/test_evidence_terms.py` refuses it when it drifts.

| Property | Means | Does not mean |
|---|---|---|
| CITATION_EXISTS | a reference is present and well-formed | that anything was looked up |
| CITED_BYTES_VERIFY | the named bytes are held and hash to the reference | that they concern the claim, or support it |
| CITATION_RELEVANT | the cited bytes concern the claim's subject | that they support what the claim says |
| SEMANTICALLY_SUPPORTED | the cited content supports what the claim says | that it is true now, or scientifically verified |
| SCIENTIFICALLY_VERIFIED | a result passed its admission policy's verification | that the model it ran is right about the world |
| DECISION_AUTHORIZED | a transition passed the gates under the policy in force | that what it decided is true |
| CURRENT_WORLD | the claim holds of the external world now | (nothing here establishes it) |
| HISTORY_INTACT | the record chain and head witness verify | that the history is the true one: a history rewritten with its witness is intact |
| HISTORY_RECORDS | the verified history records the claimed state | that the world is in that state |

Only one implication holds: CITED_BYTES_VERIFY implies CITATION_EXISTS.

A check is DETERMINISTIC (a computation over bytes and records), ASSESSED (a
fallible judgement, a model's or a heuristic's) or OBSERVED (a measurement of
the world). Only a DETERMINISTIC check may be relied on by an authority
decision.

Some historical names say more than their checks do. They are kept, because
records and tests use them, and their meaning is fixed here:

- `memory_trust` `evidence_supported`: CITED_BYTES_VERIFY only.
- the grounding benchmark's `SUPPORTED` verdict: HISTORY_RECORDS only.

## Registry

<!-- BEGIN GENERATED: evidence_terms.render_table -->
| Check | Establishes | Strength | Requires | Not to be read as |
|---|---|---|---|---|
| `agent_substrate.proposals:ProposalEnvelope.parse` | CITATION_EXISTS | DETERMINISTIC | - | CITED_BYTES_VERIFY, SEMANTICALLY_SUPPORTED |
| `agent_substrate.proposals:ProposalIngress.receive` | CITED_BYTES_VERIFY | DETERMINISTIC | - | CITATION_RELEVANT, SEMANTICALLY_SUPPORTED |
| `agent_substrate.evidence:EvidenceStore.contains` | CITED_BYTES_VERIFY | DETERMINISTIC | - | CITATION_RELEVANT, SEMANTICALLY_SUPPORTED |
| `agent_substrate.evidence:EvidenceStore.get` | CITED_BYTES_VERIFY | DETERMINISTIC | - | CITATION_RELEVANT, SEMANTICALLY_SUPPORTED |
| `agent_substrate.memory_trust:assess#evidence_supported` | CITED_BYTES_VERIFY | DETERMINISTIC | - | CITATION_RELEVANT, SEMANTICALLY_SUPPORTED, CURRENT_WORLD |
| `agent_substrate.memory_trust:assess#hash_consistent` | HISTORY_INTACT | DETERMINISTIC | - | CURRENT_WORLD |
| `agent_substrate.memory_trust:assess#consistently_reconstructed` | HISTORY_RECORDS | DETERMINISTIC | HISTORY_INTACT | SEMANTICALLY_SUPPORTED, CURRENT_WORLD |
| `agent_substrate.events:EventLog.read_verified` | HISTORY_INTACT | DETERMINISTIC | - | HISTORY_RECORDS, CURRENT_WORLD |
| `agent_substrate.separate_verify:verify_in_separate_process` | HISTORY_RECORDS | DETERMINISTIC | HISTORY_INTACT | CURRENT_WORLD |
| `agent_substrate.authority:check` | DECISION_AUTHORIZED | DETERMINISTIC | CITED_BYTES_VERIFY | SCIENTIFICALLY_VERIFIED, SEMANTICALLY_SUPPORTED, CURRENT_WORLD |
| `agent_substrate.result_rules:admission_problems` | SCIENTIFICALLY_VERIFIED | DETERMINISTIC | CITED_BYTES_VERIFY | CURRENT_WORLD, SEMANTICALLY_SUPPORTED |
| `agent_substrate.inference:prompt_problems` | CITED_BYTES_VERIFY | DETERMINISTIC | - | CITATION_RELEVANT, SEMANTICALLY_SUPPORTED |
| `agent_substrate.inference:InferenceGateway.binding_problems` | HISTORY_RECORDS | DETERMINISTIC | CITED_BYTES_VERIFY | SEMANTICALLY_SUPPORTED, SCIENTIFICALLY_VERIFIED, CURRENT_WORLD |
| `tools.grounding_bench.claims:verify` | HISTORY_RECORDS | DETERMINISTIC | HISTORY_INTACT | SEMANTICALLY_SUPPORTED, CURRENT_WORLD |
| *(none)* | CITATION_RELEVANT | - | - | no check in this repository establishes it |
| *(none)* | SEMANTICALLY_SUPPORTED | - | - | no check in this repository establishes it |
| *(none)* | CURRENT_WORLD | - | - | no check in this repository establishes it |
<!-- END GENERATED -->
