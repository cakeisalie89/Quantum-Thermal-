"""The verify-then-read defect class cannot silently re-enter.

``tests/test_agent_snapshot_coherence.py`` proves the reducers that exist, by
name. This proves the rule for the ones that do not exist yet: a static scan
of every production file (``tools/verified_read_guard.py``) and a runtime
refusal inside ``EventLog`` itself (D-2026-76). Every rule is exercised on a
source that breaks it and on a control that does not, so the guard cannot be
green by seeing nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import verified_read_guard as guard  # noqa: E402

# --- the real tree ----------------------------------------------------------


def test_the_tree_has_no_unverified_read():
    violations, allowed, n = guard.scan()
    assert n > 100, f"only {n} production files scanned"
    assert not violations, violations


def test_the_scan_sees_the_one_site_it_allows():
    """ANTI-VACUITY for the scope: the fuzz harness's parser target is in
    tools/, and the scan must reach it -- a scan that skipped tools/ would
    be green over a directory full of raw reads."""
    _, allowed, _ = guard.scan()
    assert {(h[0], h[1], h[2]) for h in allowed} == set(guard.ALLOWED)


def test_the_allowlist_is_pinned_and_reasoned():
    """Widening it is a reviewed change: the count is pinned here."""
    assert len(guard.ALLOWED) == 1
    for key, why in guard.ALLOWED.items():
        assert len(why) >= 40, f"{key}: say why, not that"


def test_a_stale_allowlist_entry_fails_the_verifier(monkeypatch):
    monkeypatch.setitem(guard.ALLOWED, ("qta_agent/store.py", "load",
                                        "RAW_READ"), "a site that no longer "
                        "exists, which an allowlist must not keep")
    assert guard.main() == 1


def test_a_violation_in_a_file_is_reported(tmp_path):
    """The whole path from file to verdict, not only the source scanner."""
    (tmp_path / "reducer.py").write_text(
        "def load(self):\n"
        "    self.log.verify().raise_if_bad()\n"
        "    for ev in self.log.read():\n"
        "        pass\n", encoding="utf-8")
    violations, allowed, n = guard.scan(root=tmp_path, files=["reducer.py"])
    assert n == 1 and not allowed
    assert [(v[1], v[2]) for v in violations] == [("load", "RAW_READ")]


# --- each rule, and its control -----------------------------------------------

@pytest.mark.parametrize("src,rule", [
    # THE shape: verify one read, fold another.
    ("def f(self):\n    self.log.verify().raise_if_bad()\n"
     "    for ev in self.log.read():\n        pass\n", "RAW_READ"),
    ("log.read()\n", "RAW_READ"),
    ("def f(p):\n    return EventLog(p).read()\n", "RAW_READ"),
    ("def f(self):\n    return self._log.read(strict=False)\n", "RAW_READ"),
    ("def f(self):\n    return self.event_log.read()\n", "RAW_READ"),
    # an alias in the same function
    ("def f(self):\n    x = self.log\n    return x.read()\n", "RAW_READ"),
    # iteration is the raw parse under another name
    ("def f(self):\n    for ev in self.log:\n        pass\n", "RAW_READ"),
    ("def f(self):\n    return [e for e in self.log]\n", "RAW_READ"),
    ("def f(self):\n    return list(self.log)\n", "RAW_READ"),
    # the tail parser verify_from used to be paired with
    ("def f(self, fh):\n    return self.log._read_tail(fh, 0, [])\n",
     "TAIL_PARSE"),
    ("def f(obj, fh):\n    return obj._read_tail(fh, 0, [])\n", "TAIL_PARSE"),
    # the removed primitive, resurrected
    ("class L:\n    def read_from(self, anchor):\n        pass\n",
     "READ_FROM"),
    ("def f(self, a):\n    return self.log.read_from(a)\n", "READ_FROM"),
    # stepping around the API
    ("def f(self):\n    return open(self.log.path).read()\n", "FILE_READ"),
    ("def f(self):\n    return self.log.path.read_bytes()\n", "FILE_READ"),
    ("def f(log):\n    return log.path.open('rb')\n", "FILE_READ"),
])
def test_each_dangerous_pattern_is_found(src, rule):
    found = guard.scan_source(src, "x.py")
    assert [h[2] for h in found] == [rule], found


@pytest.mark.parametrize("src", [
    "def f(fh):\n    return fh.read()\n",
    "def f(p):\n    return p.read_text()\n",
    "def f(self):\n    report, events = self.log.read_verified()\n",
    "def f(self, a):\n    return self.log.read_verified_from(a)\n",
    "def f(self):\n    return self.log.verify().head_seq\n",
    "def f(self):\n    return self.log.path.stat().st_size\n",
    "def f(self):\n    for x in self.logic:\n        pass\n",
    "def f(self):\n    return self.store.read(3)\n",
])
def test_the_controls_are_not_flagged(src):
    """A guard that flags every .read() would be switched off within a
    week; these must pass untouched."""
    assert guard.scan_source(src, "x.py") == []


# --- the runtime half -----------------------------------------------------------

def _log(tmp_path):
    from qta_agent.events import EventLog
    log = EventLog(tmp_path / "l.jsonl")
    log.append(actor="a", action="record.create", target="r",
               payload={"record_id": "r", "kind": "k", "proposer": "a"})
    return log


@pytest.mark.parametrize("code", [
    "log.read()", "[e for e in log]", "list(log)", "log.read(strict=False)"])
def test_the_authority_layer_cannot_read_unverified(tmp_path, code):
    """Decided by the calling frame's module, so an alias cannot evade it."""
    from qta_agent.events import UnverifiedReadRefused
    g = {"__name__": "qta_agent.a_future_reducer", "log": _log(tmp_path)}
    with pytest.raises(UnverifiedReadRefused, match="without verifying"):
        exec(code, g)


def test_the_authority_layer_can_read_verified(tmp_path):
    """Control: the verified path is open to the same caller."""
    g = {"__name__": "qta_agent.a_future_reducer", "log": _log(tmp_path)}
    exec("report, events = log.read_verified()", g)
    assert g["report"].ok and len(g["events"]) == 1


def test_a_caller_outside_the_authority_layer_may_look(tmp_path):
    """Control: tests, tools and diagnostics may parse the log; they fold
    nothing into authority state."""
    g = {"__name__": "tools.some_diagnostic", "log": _log(tmp_path)}
    exec("n = len(log.read())", g)
    assert g["n"] == 1


def test_the_refusal_is_not_an_event_log_error():
    """Projections catch EventLogError to fall back to a full read; this
    refusal must not be caught there and turned into a quiet fallback."""
    from qta_agent.events import EventLogError, UnverifiedReadRefused
    assert not issubclass(UnverifiedReadRefused, EventLogError)


@pytest.mark.parametrize("name", [None, 42])
def test_a_caller_with_no_module_name_is_not_crashed(tmp_path, name):
    """D-2026-79. Snakemake runs a rule body with ``__name__`` = None, and
    the guard called ``None.startswith`` -- the governed production rule
    crashed in hosted CI while every local suite passed. A caller with no
    module name is not the authority layer; it may look."""
    g = {"__name__": name, "log": _log(tmp_path)}
    exec("n = len(log.read())", g)
    assert g["n"] == 1
