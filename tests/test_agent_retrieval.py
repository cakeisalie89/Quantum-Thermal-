"""The governed workspace guard and the read-only retrieval index.

Two generic pieces of the agent substrate, moved here with their tests when
the package they had lived in was retired:

* the WORKSPACE GUARD (``scientific.workspace``): an allowlist enforced
  at the point of the write -- inside the governed workspace and nowhere
  else, symlinks resolved, the governed-only subtrees written by the
  governed path only;
* RETRIEVAL (``agent_substrate.rag_index``): verbatim cited spans of reviewed
  documents, offline and deterministic, never generation and never evidence,
  over a corpus that is complete in both directions against what the
  repository tracks.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import rag_index as RAG  # noqa: E402
from scientific import workspace as WS  # noqa: E402

DET = settings(max_examples=25, deadline=None, derandomize=True,
               suppress_health_check=[HealthCheck.too_slow])
TEST_WS = "verification/stage10/pytest_retrieval"


def _declare_corpus(root):
    """Write the allowlist for a temporary corpus, as a commit would.

    Retrieval refuses a corpus nobody declared, so every test corpus has to
    declare itself: a test that could index an undeclared tree would be
    testing a build the production path cannot do."""
    import json
    doc = RAG.allowlist_document(root)
    out = root / RAG.CORPUS_ALLOWLIST
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc


@pytest.fixture
def ws(request):
    path = WS.repo_root() / TEST_WS / request.node.name.replace("/", "_")
    if path.exists():
        shutil.rmtree(path)
    yield path
    if path.exists():
        shutil.rmtree(path)


# ---- the workspace guard -------------------------------------------------

def test_the_repository_root_is_where_it_should_be():
    """The guard resolves the root from its own location; after the move it
    must still be the repository, not the package directory."""
    assert (WS.repo_root() / "pyproject.toml").is_file()
    assert (WS.repo_root() / "agent_substrate").is_dir()


def test_guard_refuses_everything_outside_the_workspace(tmp_path):
    with pytest.raises(ValueError):
        WS.guard_output_dir(WS.repo_root())
    with pytest.raises(ValueError):
        WS.guard_output_dir(".")
    for elsewhere in ("agent_substrate/x", "ro-crate/x", "tests/x",
                      "docs/x", ".github/x"):
        with pytest.raises(ValueError):
            WS.guard_output_dir(elsewhere)
    with pytest.raises(ValueError):
        WS.guard_output_dir(tmp_path)


def test_guard_accepts_the_workspace_and_creates_it(ws):
    resolved = WS.guard_output_dir(ws)
    assert resolved.is_dir()
    assert resolved.relative_to(WS.repo_root()).parts[0] == "verification"


def test_a_symlink_out_of_the_workspace_is_refused(ws, tmp_path):
    out = WS.guard_output_dir(ws)
    link = out / "escape"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        WS.write_text_deterministic(link / "x.txt", "no")


def test_deterministic_writers_are_byte_stable(ws):
    out = WS.guard_output_dir(ws)
    obj = {"b": 2, "a": [1, 2, 3], "nested": {"z": 1, "y": 2}}
    first = WS.write_json_deterministic(out / "a.json", obj)
    second = WS.write_json_deterministic(
        out / "b.json", dict(reversed(list(obj.items()))))
    assert first == second
    assert (out / "a.json").read_bytes().endswith(b"\n")


def test_the_governed_subtree_refuses_an_ungoverned_writer():
    target = WS.workspace_root() / "governed" / "out" / "x_probe.json"
    with pytest.raises(ValueError, match="governed"):
        WS.write_json_deterministic(target, {"x": 1})
    assert not target.exists()


def test_a_path_outside_the_repository_is_not_laundered(tmp_path):
    with pytest.raises(ValueError, match="outside the repository"):
        WS.relpath_in_repo(tmp_path / "secret.md")


# ---- retrieval -----------------------------------------------------------

def test_corpus_excludes_non_governed_trees():
    files = RAG.corpus_files()
    assert files == sorted(set(files))
    assert "README.md" in files and "CLAIMS_BOUNDARY.md" in files
    for rel in files:
        assert not any(part in RAG.EXCLUDED_DIRS
                       for part in pathlib.Path(rel).parts)


def test_a_virtualenv_is_excluded_whatever_it_is_called(tmp_path):
    """``pyvenv.cfg`` is what the interpreter writes when it creates a
    virtual environment: the definition, not a name."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "real.md").write_text("a governed document\n")
    for name in (".venv-alt", "env-for-py315", "whatever"):
        site = tmp_path / name / "lib" / "python9.9" / "site-packages" / "pkg"
        site.mkdir(parents=True)
        (tmp_path / name / "pyvenv.cfg").write_text("home = /usr\n")
        (site / "entry_points.txt").write_text("not a document\n")
    assert RAG.corpus_files(tmp_path) == ["docs/real.md"]
    plain = tmp_path / "notes"
    plain.mkdir()
    (plain / "kept.md").write_text("still a document\n")
    assert RAG.corpus_files(tmp_path) == ["docs/real.md", "notes/kept.md"]


def test_index_is_deterministic_and_hits_carry_provenance():
    a = RAG.build_index()
    b = RAG.build_index()
    assert a.to_dict() == b.to_dict()
    assert a.stale_files() == []
    hits = a.search("independent check authority store", k=3)
    assert hits, "expected a hit for a phrase the corpus contains"
    for h in hits:
        assert h["citation"].startswith(h["path"])
        assert h["line_start"] <= h["line_end"]
        assert len(h["source_sha256"]) == 64
        assert h["evidence_status"] == "RETRIEVED_TEXT_NOT_EVIDENCE"


def test_hits_are_verbatim_spans_of_the_cited_lines():
    index = RAG.build_index()
    hits = index.search("event log hash chain", k=3)
    assert hits
    for hit in hits:
        lines = (WS.repo_root() / hit["path"]).read_text().splitlines()
        span = "\n".join(lines[hit["line_start"] - 1:hit["line_end"]]).strip()
        assert hit["text"] == span[:RAG.MAX_SNIPPET_CHARS]


def test_empty_and_stopword_queries_return_nothing():
    index = RAG.build_index()
    assert index.search("") == []
    assert index.search("the and of to") == []


def test_retrieval_module_imports_no_network_or_model_client():
    source = (WS.repo_root() / "agent_substrate" / "rag_index.py"
              ).read_text()
    for token in ("import socket", "import urllib", "import requests",
                  "import http", "httpx", "openai", "anthropic",
                  "transformers", "sentence_transformers", "faiss",
                  "chromadb"):
        assert token not in source, f"unexpected client: {token}"


def test_stale_index_is_detected(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "doc.md").write_text("# Heading\nalpha beta gamma\n")
    _declare_corpus(root)
    index = RAG.build_index(root=root)
    assert index.stale_files() == []
    (root / "doc.md").write_text("# Heading\ndelta epsilon\n")
    assert index.stale_files() == ["doc.md"]


def test_retrieve_states_the_boundary():
    doc = RAG.retrieve("verification result", k=2)
    assert doc["generation"] == "NONE"
    assert "not evidence" in doc["disclaimer"]
    assert doc["authority_effect"] == "NONE"


@given(text=st.text(alphabet=st.characters(min_codepoint=32,
                                           max_codepoint=126),
                    min_size=0, max_size=200))
@DET
def test_tokenizer_is_total_and_drops_stopwords(text):
    tokens = RAG.tokenize(text)
    assert all(len(t) > 1 for t in tokens)
    assert not (set(tokens) & RAG.STOPWORDS)


def _governed_text_files():
    out = subprocess.run(["git", "-C", str(WS.repo_root()), "ls-files"],
                         capture_output=True, text=True, check=True).stdout
    tracked = [f for f in out.split() if f.endswith((".md", ".txt"))]
    return {f for f in tracked
            if not any(part in RAG.EXCLUDED_DIRS
                       for part in pathlib.Path(f).parts)
            and f not in RAG.EXCLUDED_FILES}


def test_rag_corpus_completeness_is_exact_in_both_directions():
    """Every governed document is retrievable, and nothing else is."""
    assert set(RAG.corpus_files()) == _governed_text_files()


def test_a_dot_directory_under_the_root_is_never_governed_text(tmp_path):
    """Tooling state that lives in the repository is not a document: a
    quarantined copy of a governed file once entered the allowlist this
    way."""
    for rel in (".mutation-quarantine/20260101T000000/AGENT_SUBSTRATE.md",
                ".git/hooks/README.md", ".scratch/notes.txt"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# not governed text\n", encoding="utf-8")
    (tmp_path / "REAL.md").write_text("# governed\n", encoding="utf-8")
    assert RAG.corpus_files(tmp_path) == ["REAL.md"]


def test_the_dot_rule_does_not_exclude_dotfiles_that_are_documents(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / ".hidden-but-a-document.md").write_text(
        "# governed\n", encoding="utf-8")
    assert RAG.corpus_files(tmp_path) == ["docs/.hidden-but-a-document.md"]
