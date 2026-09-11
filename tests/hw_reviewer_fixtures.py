"""Reviewer rosters for the hardware-governance suites. FIXTURES, NOT DATA.

WHY THESE EXIST AT ALL

Until D-2026-42, ``reviewer_id`` was any non-empty string and "human-only
review authoring" was enforced by refusing a review that set
``authored_by_tool``. Every positive test in the hardware-governance suites
therefore passed with a reviewer nobody had registered -- which is to say
they were testing record completeness and hash binding, and getting the
authority check for free because there wasn't one.

Now a reviewer must resolve against a declared roster, and the roster this
repository ships registers NOBODY (see ``hardware_reviewers.json``: no
hardware data exists and no reviewer has been registered out of band). So a
positive test has to say whose authority it is testing under, which is the
point: an admitted record now names a registered reviewer, and a test that
forgets to supply one fails rather than quietly proving less.

The names below are deliberately unusable as data. They are not people.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qta_multiphysics.hardware_governance_3d import (        # noqa: E402
    REVIEWER_BOOTSTRAP, load_reviewer_roster)

#: The reviewer the hardware suites' accepted fixtures are authored by.
FIXTURE_REVIEWER = "TEST_FIXTURE_NOT_DATA-REV-1"


def human(reviewer_id, registered_by=REVIEWER_BOOTSTRAP, **over):
    entry = {"reviewer_id": reviewer_id, "kind": "HUMAN",
             "registered_by": registered_by,
             "registered_on": "2026-01-01T00:00:00Z",
             "retired_on": None}
    entry.update(over)
    return entry


def roster_file(*entries, **doc):
    """Write a roster to a temp file and return its path.

    A FILE rather than a dict built in memory, because that is what
    production reads, and a fixture that skipped the file would be testing a
    code path the package does not take.
    """
    d = tempfile.mkdtemp(prefix="qta_roster_")
    p = os.path.join(d, "hardware_reviewers.json")
    with open(p, "w") as fh:
        json.dump(dict({"schema_version": "1.0",
                        "reviewers": list(entries)}, **doc), fh)
    return p


def roster(*entries, **doc):
    """A loaded roster registering ``entries`` (default: the fixture human)."""
    if not entries:
        entries = (human(FIXTURE_REVIEWER),)
    return load_reviewer_roster(roster_file(*entries, **doc))


def roster_empty():
    """A roster file that exists and registers nobody -- what ships here.

    Distinct from :func:`no_roster`: "the authority declares nobody" and
    "there is no authority" refuse the same reviews for different reasons,
    and a report that cannot tell them apart cannot say which state it is in.
    """
    return load_reviewer_roster(roster_file())


def no_roster():
    """A roster structure for "the authority file does not exist"."""
    return load_reviewer_roster(
        os.path.join(tempfile.mkdtemp(prefix="qta_noroster_"), "absent.json"))
