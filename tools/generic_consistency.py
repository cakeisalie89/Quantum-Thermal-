#!/usr/bin/env python3
"""The generic consistency verifier: a governed history, checked by its bytes.

The successor to the legacy QTA verifier, not a trimmed copy of it.
``package_consistency_check.py`` is the LEGACY_QTA_VERIFIER: it regenerates
the hardware-era output set and checks it against the 83-gate table, the
BOM, the mode semantics and the QTA release expectations, and it stays for as
long as that payload ships. This checks what the framework itself produces --
an event history, the evidence it cites, and the scientific results decided
from them -- and none of the QTA vocabulary: no gate table, no PASS count, no
machine FSM, no BOM, no Mode B/C/D. ``tests/test_generic_consistency.py``
holds that as well as the checks.

It answers, for one event log and its evidence store:

* EVENT_HISTORY -- does the hash chain verify?
* EVIDENCE -- does every stored object hash to its name, and does every
  digest an authority record cites resolve?
* RECONSTRUCTION -- does the independent reader (``qta_agent.reconstruct``),
  re-deciding every transition and every scientific admission from the
  evidence, refuse nothing and find nothing anomalous -- and does it agree
  with the live store's own projection, record by record?
* SCIENTIFIC_ARTIFACTS -- does every scientific result's bundle parse as a
  ResultBundle and its report as a VerificationResult, the report about that
  bundle?
* IDENTITY -- is every bundle's model admitted by the catalog at its version,
  and every report's check? Whether the bundle's implementation digest is
  this tree's code is reported, not judged: a history is not wrong for having
  been produced by older code.

What it does not answer yet, and where that lives today: signatures, SBOM
and release contents (``verify_release.py``); manifest completeness
(``generate_manifest.py --check``). It establishes consistency of the record,
not correctness of the science: a complete, admitted history over an
inadequate model is still complete and admitted.

Exit status, as the auditor's: 0 nothing found, 1 a finding, 2 the question
could not be asked.

    python tools/generic_consistency.py LOG --evidence DIR [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent import reconstruct as rc  # noqa: E402
from qta_agent import result_rules  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore, is_digest  # noqa: E402
from qta_agent.store import StoreError  # noqa: E402

OK, FINDING, CANNOT_ASK = 0, 1, 2
CHECKS = ("EVENT_HISTORY", "EVIDENCE", "RECONSTRUCTION",
          "SCIENTIFIC_ARTIFACTS", "IDENTITY")


def _live_store(log: EventLog, evidence: EvidenceStore):
    """The store's own projection of the log, with governed origin attached
    as production has it."""
    from qta_agent.governed_model import GovernedModelRuns
    return GovernedModelRuns(root=ROOT, log=log, evidence=evidence).authority


def _scientific(recon) -> dict:
    return {rid: r for rid, r in sorted(recon.records.items())
            if r["kind"] == result_rules.KIND}


def read_result(ev: dict, fetch) -> tuple:
    """``(bundle or None, report or None, findings)`` for one scientific
    result's cited evidence. Each document is parsed by the scientific
    package's own schema; a proposed result has no report yet."""
    from scientific.result import ResultBundle
    from scientific.verification import VerificationResult
    docs, found = {}, []
    for key, cls in (("result_bundle", ResultBundle),
                     ("verification_report", VerificationResult)):
        if key not in ev:
            continue
        try:
            docs[key] = cls.from_record(json.loads(fetch(ev[key])))
        except Exception as exc:                # noqa: BLE001 -- reported
            found.append(f"{key} does not parse as a {cls.__name__}: {exc}")
    if "result_bundle" not in ev:
        found.append("cites no result bundle")
    bundle, vr = docs.get("result_bundle"), docs.get("verification_report")
    if bundle is not None and vr is not None \
            and vr.subject_digest != bundle.digest():
        found.append("the report is not about the cited bundle")
    return bundle, vr, found


def identity_findings(bundle, vr, registry, admitted_check) -> list:
    found = []
    try:
        registry.lookup(bundle.model_id, bundle.model_version)
    except Exception:                           # noqa: BLE001 -- reported
        found.append(f"{bundle.model_id}@{bundle.model_version} is not an "
                     "admitted model")
    if vr is not None:
        try:
            admitted_check(vr.check_id)
        except KeyError:
            found.append(f"{vr.check_id} is not an admitted check")
    return found


def verify(log_path: Path, evidence_dir: Path) -> dict:
    """``{check: [finding, ...]}`` for every check, plus ``"notes"``."""
    from scientific import catalog

    out: dict = {c: [] for c in CHECKS}
    notes: list = []
    log = EventLog(log_path)
    evidence = EvidenceStore(evidence_dir)

    chain = log.verify()
    if not chain.ok:
        out["EVENT_HISTORY"].append(f"the hash chain does not verify: "
                                    f"{chain}")
        return {**out, "notes": notes}

    out["EVIDENCE"] += [f"store: {p}" for p in
                        evidence.verify_store().problems]

    recon = rc.reconstruct(log, evidence=evidence)
    for rid, r in sorted(recon.records.items()):
        for name, value in sorted(r["evidence"].items()):
            if isinstance(value, str) and is_digest(value):
                try:
                    evidence.get(value)
                except Exception as exc:        # noqa: BLE001 -- reported
                    out["EVIDENCE"].append(f"{rid}: {name} {value[:12]} "
                                           f"does not resolve: {exc}")
    out["RECONSTRUCTION"] += [f"refused: {u}" for u in recon.unauthorized]
    out["RECONSTRUCTION"] += [f"anomaly: {a}" for a in recon.anomalies]
    out["RECONSTRUCTION"] += [f"undecidable: {u}"
                              for u in recon.unverifiable]
    try:
        live = _live_store(log, evidence)
    except StoreError as exc:
        # the store refuses to project a history it cannot authorize --
        # e.g. a transition naming no admission policy it can decide under
        out["RECONSTRUCTION"].append(f"the live store refuses the history: "
                                     f"{exc}")
    else:
        for d in rc.compare(live, recon):
            out["RECONSTRUCTION"].append(f"the live store and the "
                                         f"independent reader disagree: {d}")

    registry = catalog.models()
    for rid, r in _scientific(recon).items():
        bundle, vr, found = read_result(r["evidence"], evidence.get)
        out["SCIENTIFIC_ARTIFACTS"] += [f"{rid}: {f}" for f in found]
        if bundle is None:
            continue
        out["IDENTITY"] += [f"{rid}: {f}" for f in identity_findings(
            bundle, vr, registry, catalog.check)]
        try:
            same = registry.lookup(bundle.model_id, bundle.model_version) \
                .implementation_digest() == bundle.implementation_digest
        except Exception:                       # noqa: BLE001 -- above
            same = False
        notes.append(f"{rid}: {bundle.model_id}@{bundle.model_version}, "
                     f"admission {r['admission']}, produced by "
                     f"{'this tree' if same else 'other'} code")
    return {**out, "notes": notes}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("log")
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not Path(args.log).is_file() or not Path(args.evidence).is_dir():
        print("no such log or evidence store", file=sys.stderr)
        return CANNOT_ASK
    result = verify(Path(args.log), Path(args.evidence))
    found = [f"{c}: {f}" for c in CHECKS for f in result[c]]
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True))
    else:
        for c in CHECKS:
            print(f"{c:<22} {'FINDING' if result[c] else 'consistent'}")
        for f in found:
            print(f"  {f}")
        for n in result["notes"]:
            print(f"  note: {n}")
    return FINDING if found else OK


if __name__ == "__main__":
    sys.exit(main())
