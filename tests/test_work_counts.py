"""The work-count instrument (tools/work_counts.py) counts what it says.

It wraps the event log's read paths from outside, so the two things it must
get right are that it sees the reads and that it puts the log back. Each is
held here against a log small enough to count by hand.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import work_counts as WC  # noqa: E402
from agent_substrate.events import Event, EventLog  # noqa: E402
from agent_substrate.store import AuthorityStore  # noqa: E402


def _store(tmp_path, n):
    s = AuthorityStore(EventLog(tmp_path / "log.jsonl")).load()
    for i in range(n):
        s.create(record_id=f"r{i}", kind="claim", proposer="p")
    return tmp_path / "log.jsonl"


def test_a_full_load_and_a_catch_up_are_counted_where_they_happen(tmp_path):
    path = _store(tmp_path, 5)
    counts = WC.Counts()
    with WC.counting(counts):
        s = AuthorityStore(EventLog(path)).load()
        AuthorityStore(EventLog(path)).load().create(
            record_id="r5", kind="claim", proposer="p")
        s.catch_up(force=True)
    t = counts.totals()
    assert t["full_calls"] >= 2 and t["full_records"] >= 10
    assert t["tail_calls"] >= 1 and t["tail_records"] >= 1
    sites = {(r["kind"], r["site"]) for r in counts.by_site()}
    assert ("full", "store:load") in sites, sites
    assert t["hashed"] >= t["full_records"], (
        "every record a full read returns was hashed to verify it")


def test_the_read_paths_are_put_back(tmp_path):
    before = (EventLog.read_verified, EventLog.advance,
              EventLog.read_verified_from, EventLog.anchor_at,
              Event.recompute_hash)
    try:
        with WC.counting(WC.Counts()):
            raise RuntimeError("the run failed")
    except RuntimeError:
        pass
    assert (EventLog.read_verified, EventLog.advance,
            EventLog.read_verified_from, EventLog.anchor_at,
            Event.recompute_hash) == before
    # and nothing is counted once they are
    counts = WC.Counts()
    with WC.counting(counts):
        pass
    AuthorityStore(EventLog(_store(tmp_path, 2))).load()
    assert counts.totals()["full_calls"] == 0


def test_growth_is_the_ratio_to_the_run_before():
    def run(n, work):
        return {"decisions": n, "totals": {"full_records": work}}
    g = WC.growth([run(10, 100), run(20, 400), run(40, 800)])
    assert [x["ratios"]["full_records"] for x in g] == [4.0, 2.0]
    assert [x["size_ratio"] for x in g] == [2.0, 2.0]
    assert WC.growth([run(10, 0), run(20, 5)])[0]["ratios"][
        "full_records"] is None


def test_a_short_campaign_is_measured_end_to_end():
    doc = WC.report([2])
    (run,) = doc["runs"]
    assert run["decisions"] == 2
    assert run["totals"]["full_calls"] > 0
    assert run["by_site"], "a campaign that read nothing measured nothing"
    assert "SCRIPTED DECISIONS" in doc["what"]
