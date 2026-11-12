"""No independent reader raises on a record's content (AUD-047).

reconstruct, reconstruct_tasks and reconstruct_subsystems are the second
implementation: plain dictionaries, read from the log alone. Each says a
hostile history produces findings, not an exception that hides the rest of
the log. The one test of that tried eight payloads against six actions.

A value of the wrong type somewhere deeper raised at 57 places across the
three: an id that is a list cannot be a key, a number cannot be iterated, a
list has no ``get``. Any actor that can append could write one, and every
consumer of the reader then got an exception where it wanted findings.

The claim is structural now (``reconstruct._never_raising``). These hold it
over every value of a genuine history, and over every key the module reads
under every registered action. The last test takes the guard away and
requires the sweep to find all three readers raising: a sweep that cannot
fail holds nothing.
"""
from __future__ import annotations

import copy
import dataclasses
import inspect
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import actions  # noqa: E402
from agent_substrate import reconstruct as RC  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.events import Event, EventLog  # noqa: E402
from agent_substrate.governed_tasks import GovernedTasks  # noqa: E402
from agent_substrate.tasks import TaskState  # noqa: E402

WS = "verification/stage10/_pytest_never_raise"

#: One value of each kind JSON has that a field of another kind may meet.
WRONG = ([], {"x": 1}, 7, None, "x")

READERS = ("reconstruct", "reconstruct_tasks", "reconstruct_subsystems")


@pytest.fixture()
def gov(request):
    name = request.node.name.replace("/", "_")[:60]
    base = ROOT / WS / name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    g = GovernedTasks(root=ROOT, log=EventLog(base / "log.jsonl"),
                      evidence=EvidenceStore(base / "evidence"))
    g.out_rel = f"{WS}/{name}/out"
    yield g
    if base.exists():
        shutil.rmtree(base)


class _History:
    """A log whose verified events are the ones given. It answers the
    readers' one read primitive from memory, so that a history can differ
    from a genuine one in a single value without being written out."""

    def __init__(self, report, events):
        self._report, self._events = report, events

    def read_verified(self, **kw):
        return self._report, list(self._events)


def _positions(value, depth=2, prefix=()):
    """Every position in a payload, two levels down."""
    if isinstance(value, dict) and depth:
        for k, v in value.items():
            yield prefix + (k,)
            yield from _positions(v, depth - 1, prefix + (k,))
    elif isinstance(value, list) and depth:
        for i, v in enumerate(value[:2]):
            yield prefix + (i,)
            yield from _positions(v, depth - 1, prefix + (i,))


def _replaced(payload, path, value):
    out = copy.deepcopy(payload)
    cur = out
    for k in path[:-1]:
        cur = cur[k]
    cur[path[-1]] = value
    return out


def _genuine(gov):
    run = gov.run(tool_id="stage10.emit_artifact", idempotency_key="k",
                  inputs={"out_dir": gov.out_rel, "name": "artifact.json",
                          "payload": {"label": "MODEL_ONLY", "value": 42}})
    assert run.state is TaskState.VERIFIED
    report, events = gov.log.read_verified()
    return report, events


def _sweep_history(report, events) -> tuple:
    """(histories tried, [(reader, action, position, value, error)])."""
    tried, raised = 0, []
    for name in READERS:
        reader = getattr(RC, name)
        for i, ev in enumerate(events):
            for path in _positions(ev.payload):
                for wrong in WRONG:
                    mutated = dataclasses.replace(
                        ev, payload=_replaced(ev.payload, path, wrong))
                    tried += 1
                    try:
                        reader(_History(
                            report, events[:i] + [mutated] + events[i + 1:]))
                    except Exception as exc:              # noqa: BLE001
                        raised.append((name, ev.action,
                                       ".".join(map(str, path)), wrong,
                                       f"{type(exc).__name__}: {exc}"))
    return tried, raised


def test_no_reader_raises_on_a_genuine_history_with_one_value_replaced(gov):
    """Every value in a governed run's history, replaced in turn by a value
    of each other kind, with every record after it kept."""
    report, events = _genuine(gov)
    tried, raised = _sweep_history(report, events)
    assert not raised, (len(raised), raised[:5])
    # Anti-vacuity: the run wrote a history worth walking.
    assert len({ev.action for ev in events}) >= 15
    assert tried > 3000, tried


#: Every key the module reads from a payload or from a dictionary inside
#: one, taken from its source: the names a wrong value can be put under.
KEYS = sorted(set(re.findall(r'\.get\(\s*"(\w+)"', inspect.getsource(RC)))
              | set(re.findall(r'\[\s*"(\w+)"\s*\]', inspect.getsource(RC))))


def _event(seq, action, payload) -> Event:
    return Event(seq=seq, event_id=f"e{seq}", wall_time=1.0 + seq,
                 actor="a", action=action, target="t", payload=payload,
                 prev_hash="0" * 64, hash=f"{seq:064x}")


def _sweep_actions(report) -> tuple:
    tried, raised = 0, []
    plain = {k: "x" for k in KEYS}
    for name in READERS:
        reader = getattr(RC, name)
        for action in sorted(actions.OWNERS):
            for wrong in WRONG[:3]:
                cases = [("every key", {k: wrong for k in KEYS})]
                cases += [(k, {**plain, k: wrong}) for k in KEYS]
                cases += [(k + " in a dictionary",
                           {**plain, k: {**plain, k2: wrong}})
                          for k in ("job", "identity", "entry", "grant",
                                    "document", "decision", "manifest",
                                    "envelope", "objective", "hypothesis",
                                    "step", "lease", "evidence")
                          for k2 in KEYS[::7]]
                for where, payload in cases:
                    tried += 1
                    try:
                        # twice: the second meets what the first left
                        reader(_History(report, [_event(0, action, payload),
                                                 _event(1, action, payload)]))
                    except Exception as exc:              # noqa: BLE001
                        raised.append((name, action, where, wrong,
                                       f"{type(exc).__name__}: {exc}"))
    return tried, raised


def test_no_reader_raises_on_any_registered_action(gov):
    """Every action a module of this package writes, with every key the
    readers read set to a plausible string and one of them, or all, to a
    value of another kind."""
    report, _ = gov.log.read_verified()
    assert len(KEYS) > 150 and "job_id" in KEYS and "record_id" in KEYS
    tried, raised = _sweep_actions(report)
    assert not raised, (len(raised), raised[:5])
    assert tried > 50_000, tried


def test_without_the_guard_the_sweeps_find_every_reader_raising(
        gov, monkeypatch):
    """CONTROL. The fold, called as it was before AUD-047: with nothing to
    set a record aside, each reader raises on some value, and the sweeps
    say which. A sweep that passed here would hold nothing above."""
    def unguarded(fold, log, **kw):
        return fold(log, _unreadable={}, _at=[None], **kw)

    report, events = _genuine(gov)
    monkeypatch.setattr(RC, "_never_raising", unguarded)
    _, in_history = _sweep_history(report, events)
    _, alone = _sweep_actions(report)
    # A governed task run writes no authority record, so its history has
    # nothing for reconstruct() to trip on; the second sweep reaches it.
    for name in READERS[1:]:
        assert any(r[0] == name for r in in_history), name
    for name in READERS:
        assert any(r[0] == name for r in alone), name
    kinds = {r[4].split(":")[0] for r in in_history + alone}
    assert {"TypeError", "AttributeError"} <= kinds, kinds
