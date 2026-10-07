#!/usr/bin/env python3
"""The learned-model documents in an authority history (NF-1T).

The ONE place the learned-model tooling touches ``qta_agent``. It reads the
documents ``tools/neural.py`` has already written under ``docs/neural/``,
records each of them in a FRESH, throwaway authority history through
``qta_agent.learned_lifecycle`` (every link checked by digest before it is
written), walks the route a reviewer would take -- into review, then the
edge into VERIFIED, which the store refuses for a learned record -- and
evaluates the claims from what that history holds.

It computes no scientific result and trains nothing. ``tools/neural.py``,
which generates the governed dataset and trains, runs this as its OWN
process and reads its answer, so no process that computes a dataset or a
model ever loads the authority substrate: the direction
``tests/test_agent_substrate_isolation.py`` polices, made structural.

Usage::

    python tools/neural_ledger.py     # the claims document, as JSON
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from neural import F, read_json  # noqa: E402  the committed documents' paths

from scientific_ai.neural import claims, family, manifests, solver  # noqa


def ledger_claims() -> dict:
    """Record every document in a FRESH authority history and evaluate the
    claims from what the history holds."""
    from qta_agent.authority import Role, State
    from qta_agent.events import EventLog
    from qta_agent.evidence import EvidenceStore
    from qta_agent.learned_lifecycle import LearnedLedger
    from qta_agent.store import AuthorityStore, StoreError
    with tempfile.TemporaryDirectory() as tmp:
        log = EventLog(Path(tmp) / "authority.log")
        evid = EvidenceStore(Path(tmp) / "evidence")
        store = AuthorityStore(log, evidence=evid).load()
        led = LearnedLedger(store, evid)
        who = "nf1t-pipeline"
        ids = {}
        ids["dataset"] = led.register(read_json(F["dataset_manifest"]),
                                      actor=who)
        ids["dev_arch"] = led.register(read_json(F["dev_manifest"]),
                                       actor=who)
        ids["dev_meta"] = led.register(read_json(F["dev_meta"]), actor=who,
                                       depends_on=(ids["dev_arch"],))
        ids["flag_arch"] = led.register(read_json(F["flagship_manifest"]),
                                        actor=who)
        ids["flag_meta"] = led.register(read_json(F["flagship_meta"]),
                                        actor=who,
                                        depends_on=(ids["flag_arch"],))
        for name in ("training", "training_resume_a", "training_resume_b"):
            ids[name] = led.register(read_json(F[name]), actor=who,
                                     depends_on=(ids["dataset"],
                                                 ids["dev_arch"]))
        for ck, tr in (("checkpoint", "training"),
                       ("checkpoint_resume_a", "training_resume_a"),
                       ("checkpoint_resume_b", "training_resume_b")):
            ids[ck] = led.register(read_json(F[ck]), actor=who,
                                   depends_on=(ids[tr],))
        ids["evaluation"] = led.register(read_json(F["evaluation"]),
                                         actor=who,
                                         depends_on=(ids["checkpoint"],
                                                     ids["dataset"]))
        ids["distributed"] = led.register(read_json(F["distributed"]),
                                          actor=who)
        # the whole route a reviewer would take: into review (allowed for
        # any record), then the edge into VERIFIED -- refused for a learned
        # record by the store itself, citing real evidence
        store.transition(record_id=ids["evaluation"],
                         dst=State.UNDER_REVIEW, actor="a-reviewer",
                         role=Role.VERIFIER)
        try:
            store.transition(record_id=ids["evaluation"],
                             dst=State.VERIFIED, actor="a-reviewer",
                             role=Role.VERIFIER,
                             evidence={"verification_report": store.get(
                                 ids["evaluation"]).evidence["document"]})
            refused = None
        except StoreError as exc:
            refused = str(exc)
        docs = led.documents()
        report, events = log.read_verified()
        report.raise_if_bad()
        head = events[-1].hash
    dev = family.development().digest()
    flag = solver.config_from(family.solve_flagship()).digest()
    out = {}
    for name, subject, members in (("development", dev, ()),
                                   ("flagship", flag, (dev,))):
        table = claims.evaluate(subject, docs, family_members=members)
        out[name] = {"config_digest": subject, "claims": table,
                     "status_history": claims.derive_status(table),
                     "status": claims.current_status(table)}
    return {"schema": "scientific-moe-claims/1",
            "records": sorted(ids.values()),
            "history_head": head,
            "acceptance_attempt": {
                "record": ids["evaluation"], "dst": "VERIFIED",
                "refused": refused is not None, "reason": refused},
            "subjects": out,
            "semantics": list(manifests.PREDICTION_SEMANTICS)}


def main(argv=None) -> int:
    print(json.dumps(ledger_claims(), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
