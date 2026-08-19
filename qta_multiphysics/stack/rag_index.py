"""Read-only retrieval index over the project's own governed documents.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

"Read-only RAG" here means exactly what it says, and the constraints are the
point of the module:

* **Retrieval only — no generation.** This module returns verbatim spans of
  governed documents with citations. It never paraphrases, never summarises,
  and never calls a language model. The "G" in RAG is a human reading the
  cited span.
* **Read-only over the corpus.** The corpus is opened for reading and nothing
  else. The only thing written is a serialized index, and only into the
  guarded Stage-10 workspace.
* **Offline and deterministic.** Pure standard library: an Okapi BM25 index
  built in-process. No network client is imported, no embedding service is
  called, no model weights are loaded, and the same corpus always yields the
  same index bytes and the same ranking.
* **Provenance on every hit.** Each result carries the source path, the line
  range, and the SHA-256 of the source file at index time, so a retrieved
  claim can always be walked back to the governed document that made it — and
  a stale index is detectable rather than silently wrong.
* **Retrieved text is not evidence.** Everything in this corpus is
  forecast-only; retrieving a sentence about a threshold does not make the
  threshold measured. ``automatic_gate_effect = NONE``.

The corpus is the project's Markdown documentation plus its plain-text audit
files. Non-governed trees (``attic/``), binaries, and generated workspaces are
excluded by construction.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path

from . import AUTOMATIC_GATE_EFFECT, LABEL
from .workspace import (StrPath, guard_output_dir, repo_root, sha256_file,
                        write_json_deterministic)

INDEX_SCHEMA_VERSION = "1.0.0"
BM25_K1 = 1.5
BM25_B = 0.75
MAX_SNIPPET_CHARS = 600

# Non-governed or generated trees are never indexed.
EXCLUDED_DIRS = ("attic", "verification", ".git", ".venv", "outputs",
                 "release", "__pycache__")
CORPUS_GLOBS = ("*.md", "*.txt")

STOPWORDS = frozenset("""
a an and are as at be by for from has have in into is it its of on or
that the their then there these this to was were which with without not no
""".split())

_TOKEN = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, stopwords dropped, no stemming.

    Deliberately boring: a deterministic tokenizer keeps the index bytes and
    the ranking reproducible, which matters more here than recall.
    """
    return [t for t in _TOKEN.findall(text.lower())
            if len(t) > 1 and t not in STOPWORDS]


def corpus_files(root: StrPath | None = None) -> list[str]:
    """Governed text documents, as repo-relative POSIX paths, sorted."""
    root = Path(root) if root is not None else repo_root()
    out = []
    for pattern in CORPUS_GLOBS:
        for p in root.rglob(pattern):
            rel = p.relative_to(root)
            if any(part in EXCLUDED_DIRS for part in rel.parts):
                continue
            if not p.is_file():
                continue
            out.append(rel.as_posix())
    return sorted(set(out))


def split_chunks(text: str, path: str) -> list[dict]:
    """Split a document into heading-anchored chunks with 1-based line ranges.

    Markdown headings start a new chunk; blank-line-separated blocks inside a
    section are grouped up to a size bound. Chunks keep their line range so a
    hit cites ``file:start-end`` and a reader can open exactly that span.
    """
    lines = text.splitlines()
    chunks: list[dict] = []
    buf: list[str] = []
    start, heading = 1, ""

    def flush(end_line: int) -> None:
        body = "\n".join(buf).strip()
        if body:
            chunks.append({"path": path, "line_start": start,
                           "line_end": end_line, "heading": heading,
                           "text": body})

    for i, line in enumerate(lines, 1):
        is_heading = line.startswith("#")
        too_long = sum(len(b) for b in buf) > MAX_SNIPPET_CHARS * 2
        if (is_heading or too_long) and buf:
            flush(i - 1)
            buf, start = [], i
        if is_heading:
            heading = line.lstrip("#").strip()
        buf.append(line)
    flush(len(lines) if lines else 1)
    return chunks


class ReadOnlyIndex:
    """In-memory BM25 index. Construction reads; nothing here ever writes."""

    def __init__(self, chunks: Iterable[dict], file_digests: dict,
                 root: StrPath | None = None) -> None:
        self.root = Path(root) if root is not None else repo_root()
        self.chunks = list(chunks)
        self.file_digests = dict(file_digests)
        self.doc_tokens = [tokenize(c["text"]) for c in self.chunks]
        self.doc_len = [len(t) for t in self.doc_tokens]
        self.n_docs = len(self.chunks)
        self.avg_len = (sum(self.doc_len) / self.n_docs) if self.n_docs else 0.0
        self.term_freq = [Counter(t) for t in self.doc_tokens]
        df: Counter = Counter()
        for tf in self.term_freq:
            df.update(tf.keys())
        self.doc_freq: dict[str, int] = dict(df)

    # ---- scoring ----
    def idf(self, term: str) -> float:
        n_q = self.doc_freq.get(term, 0)
        if n_q == 0:
            return 0.0
        return math.log(1.0 + (self.n_docs - n_q + 0.5) / (n_q + 0.5))

    def score(self, query_terms: Sequence[str], doc_id: int) -> float:
        tf, dl = self.term_freq[doc_id], self.doc_len[doc_id]
        s = 0.0
        for term in query_terms:
            f = tf.get(term, 0)
            if not f:
                continue
            denom = f + BM25_K1 * (1 - BM25_B + BM25_B * dl /
                                   (self.avg_len or 1.0))
            s += self.idf(term) * f * (BM25_K1 + 1) / denom
        return s

    def search(self, query: str, k: int = 5, path_prefix: str | None = None
               ) -> list[dict]:
        """Top-``k`` verbatim spans for ``query``, each with full provenance.

        Ties break on document id so the ranking is stable; scores of zero are
        dropped rather than padded, because an empty result is a truthful
        answer and a padded one is not.
        """
        terms = tokenize(query)
        if not terms:
            return []
        scored: list[tuple[float, int]] = []
        for doc_id, chunk in enumerate(self.chunks):
            if path_prefix and not chunk["path"].startswith(path_prefix):
                continue
            s = self.score(terms, doc_id)
            if s > 0.0:
                scored.append((-s, doc_id))
        scored.sort()
        hits: list[dict] = []
        for neg_s, doc_id in scored[:int(k)]:
            c = self.chunks[doc_id]
            snippet = c["text"]
            truncated = len(snippet) > MAX_SNIPPET_CHARS
            hits.append({
                "rank": len(hits) + 1,
                "score": round(-neg_s, 9),
                "path": c["path"],
                "line_start": c["line_start"], "line_end": c["line_end"],
                "citation": f"{c['path']}:{c['line_start']}-{c['line_end']}",
                "heading": c["heading"],
                "source_sha256": self.file_digests.get(c["path"], ""),
                "text": snippet[:MAX_SNIPPET_CHARS],
                "truncated": truncated,
                "label": LABEL,
                "evidence_status": "RETRIEVED_TEXT_NOT_EVIDENCE",
            })
        return hits

    def stale_files(self) -> list[str]:
        """Corpus files whose bytes changed since the index was built."""
        bad: list[str] = []
        for rel, digest in sorted(self.file_digests.items()):
            p = self.root / rel
            if not p.exists() or sha256_file(p) != digest:
                bad.append(rel)
        return bad

    def to_dict(self) -> dict:
        return {
            "schema_version": INDEX_SCHEMA_VERSION,
            "label": LABEL,
            "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
            "producer": "qta_multiphysics.stack.rag_index",
            "retrieval": {"model": "Okapi BM25", "k1": BM25_K1, "b": BM25_B,
                          "tokenizer": "lowercase [a-z0-9_]+, stopwords "
                                       "dropped, no stemming"},
            "generation": "NONE — retrieval returns verbatim cited spans; no "
                          "language model is called and no text is synthesised",
            "network_access": "NONE — index and query run entirely offline",
            "write_access": "NONE over the corpus; the index itself is written "
                            "only into the Stage-10 workspace",
            "n_files": len(self.file_digests),
            "n_chunks": self.n_docs,
            "file_digests": dict(sorted(self.file_digests.items())),
            "chunks": [{"path": c["path"], "line_start": c["line_start"],
                        "line_end": c["line_end"], "heading": c["heading"],
                        "text": c["text"]} for c in self.chunks],
        }


def build_index(root: StrPath | None = None,
                paths: Iterable[str] | None = None) -> ReadOnlyIndex:
    """Build the index by reading the governed corpus (no writes at all)."""
    root = Path(root) if root is not None else repo_root()
    rels = list(paths) if paths is not None else corpus_files(root)
    chunks: list[dict] = []
    digests: dict[str, str] = {}
    for rel in rels:
        p = root / rel
        raw = p.read_bytes()
        digests[rel] = hashlib.sha256(raw).hexdigest()
        chunks.extend(split_chunks(raw.decode("utf-8", errors="replace"), rel))
    return ReadOnlyIndex(chunks, digests, root=root)


def load_index(path: StrPath,
               root: StrPath | None = None) -> ReadOnlyIndex:
    """Rebuild an index object from a persisted index document."""
    import json
    doc = json.loads(Path(path).read_text())
    if doc.get("schema_version") != INDEX_SCHEMA_VERSION:
        raise ValueError(
            f"index schema {doc.get('schema_version')!r} != "
            f"{INDEX_SCHEMA_VERSION!r}; rebuild the index")
    return ReadOnlyIndex(doc["chunks"], doc["file_digests"], root=root)


def write_index(out_dir: StrPath, root: StrPath | None = None,
                paths: Iterable[str] | None = None) -> dict:
    """Persist a deterministic index document into the guarded workspace."""
    out = guard_output_dir(out_dir)
    index = build_index(root=root, paths=paths)
    doc = index.to_dict()
    digest = write_json_deterministic(out / "rag_index.json", doc)
    return {"label": LABEL, "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
            "index_file": "rag_index.json", "sha256": digest,
            "n_files": doc["n_files"], "n_chunks": doc["n_chunks"],
            "schema_version": INDEX_SCHEMA_VERSION}


def retrieve(query: str, k: int = 5, root: StrPath | None = None,
             path_prefix: str | None = None) -> dict:
    """One-shot retrieval: build, query, and report — with the boundary stated.

    The returned document is deliberately shaped so it cannot be mistaken for
    an answer: it contains the query, the cited spans, and an explicit
    statement that retrieved text is not evidence and no generation occurred.
    """
    index = build_index(root=root)
    hits = index.search(query, k=k, path_prefix=path_prefix)
    return {
        "label": LABEL,
        "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
        "query": query,
        "n_hits": len(hits),
        "hits": hits,
        "corpus": {"n_files": len(index.file_digests),
                   "n_chunks": index.n_docs},
        "generation": "NONE",
        "disclaimer": "verbatim spans of forecast-only documents; retrieving "
                      "a statement does not make it measured, validated, or "
                      "gate evidence",
    }
