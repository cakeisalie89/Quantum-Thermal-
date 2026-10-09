#!/usr/bin/env python3
"""Generate and check docs/corpus_allowlist.json.

WHY THIS IS A TOOL AND NOT A BUILD STEP

The allowlist records which documents the retrieval layer is permitted to
quote. Regenerating it is how a new document JOINS the corpus, so running
this is a deliberate act whose result lands in a commit somebody reviews. A
build step that regenerated it automatically would restore exactly what the
allowlist replaced: membership as a side effect of creating a file.

    python3 tools/corpus_allowlist.py            # check; non-zero on drift
    python3 tools/corpus_allowlist.py --write    # regenerate after review
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_multiphysics.stack.rag_index import (  # noqa: E402
    CORPUS_ALLOWLIST, CorpusMembershipError, allowlist_document,
    assert_corpus_is_allowlisted,
)


def build(root: Path) -> dict:
    doc = allowlist_document(root)
    if not doc["entries"]:
        # A generator that writes an empty list would produce a file whose
        # every later check passes over nothing.
        raise SystemExit(
            f"refusing to write an empty allowlist: {root} contains no "
            "governed text documents, which is far more likely to be a wrong "
            "root than a real corpus")
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="regenerate the allowlist from the corpus on disk")
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args()
    root = Path(args.root)

    if args.write:
        doc = build(root)
        out = root / CORPUS_ALLOWLIST
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {CORPUS_ALLOWLIST}: {len(doc['entries'])} document(s), "
              f"entries_digest {doc['entries_digest'][:12]}")
        return 0

    try:
        allowed = assert_corpus_is_allowlisted(root)
    except CorpusMembershipError as exc:
        print(f"CORPUS ALLOWLIST DRIFT\n  {exc}", file=sys.stderr)
        print("\nIf the change is intended, run:\n"
              "  python3 tools/corpus_allowlist.py --write", file=sys.stderr)
        return 1
    print(f"corpus allowlist holds: {len(allowed)} document(s), all present "
          "and unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
