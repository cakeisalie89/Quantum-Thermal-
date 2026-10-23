"""The deployment isolation contract (Assurance I), held to the host.

Three things are checked:

* the contract names the eight dimensions, says for each whether the kernel,
  an application rule or nothing enforces it, and keeps the ones nothing
  enforces saying so;
* on this host, each dimension behaves as the contract says -- or, on a host
  without unprivileged namespaces, the high-assurance mode refuses to run a
  tool at all, which is what the contract promises there;
* each probe can see the boundary's ABSENCE: run with isolation off, the
  probes that observe a kernel boundary observe it gone. A probe that passed
  either way would be measuring nothing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import isolation_contract as IC  # noqa: E402

from agent_substrate.execution import namespace_isolation  # noqa: E402

CONTRACT = json.loads(IC.CONTRACT.read_text(encoding="utf-8"))
BY_ID = {d["id"]: d for d in CONTRACT["dimensions"]}
NAMESPACED = bool(namespace_isolation()[0])


def test_the_contract_names_every_dimension_and_what_enforces_it():
    assert tuple(BY_ID) == IC.DIMENSIONS
    for d in CONTRACT["dimensions"]:
        assert d["enforcement"] in IC.ENFORCEMENT, d["id"]
        for f in ("mechanism", "does_not_cover", "deployment_requirement"):
            assert d[f].strip(), (d["id"], f)


def test_what_nothing_enforces_is_not_labelled_as_if_something_did():
    """No syscall filter exists: the contract must say NOT_PROVIDED.
    An environment rule and a record are application rules, whatever
    kernel mechanism sits beside them."""
    assert BY_ID["syscalls"]["enforcement"] == "NOT_PROVIDED"
    assert BY_ID["credentials"]["enforcement"] == "APPLICATION"
    assert BY_ID["audit"]["enforcement"] == "APPLICATION"
    assert "not production attestation" in BY_ID["audit"]["does_not_cover"]
    assert CONTRACT["stronger_isolation"]["gvisor"] == "NOT_ADOPTED"


@pytest.mark.skipif(not NAMESPACED, reason="this host refuses unprivileged "
                    "namespaces; see the fail-closed test")
@pytest.mark.parametrize("dimension", IC.DIMENSIONS)
def test_each_dimension_holds_on_this_host(dimension, tmp_path):
    got = IC.PROBES[dimension](tmp_path)
    assert got["holds"], (dimension, got)


@pytest.mark.skipif(NAMESPACED, reason="this host has namespaces; the "
                    "probes above run instead")
def test_without_namespaces_high_assurance_runs_nothing(tmp_path):
    r = IC._run(tmp_path, "print('{}')")
    assert r.outcome.value == "DENIED", r.reason


@pytest.mark.skipif(not NAMESPACED, reason="the controls switch off a "
                    "boundary this host does not have")
@pytest.mark.parametrize("dimension", ["filesystem", "network", "namespaces",
                                       "process_tree", "credentials",
                                       "audit"])
def test_each_probe_sees_the_boundary_gone(dimension, tmp_path, monkeypatch):
    """Isolation off and confinement in research mode: the namespaces are
    not entered, Landlock has nothing to confine within, a grandchild in a
    session of its own outlives the process group killed on timeout, and
    every probe of a kernel boundary reports it absent. The credentials
    probe's KERNEL half goes with Landlock; its environment half (an
    application rule) stays, so that probe fails on the file alone."""
    monkeypatch.setattr(IC, "MODE", {"confine": "research", "isolate": False})
    got = IC.PROBES[dimension](tmp_path)
    assert not got["holds"], (dimension, got)
    if dimension == "credentials":
        assert got["observed"]["credential_file"] == "OPENED"
        assert "QT_ISOLATION_PROBE_TOKEN" not in got["observed"]["environment"]


@pytest.mark.skipif(not NAMESPACED, reason="the probe runs under "
                    "high_assurance, which this host cannot provide")
@pytest.mark.parametrize("quota, loosened, observed", [
    ("cpu_seconds", 60, ("cpu_bound_run", "TIMED_OUT")),
    ("address_space_bytes", 4 << 30,
     ("allocation_past_address_space", 1 << 30)),
    ("output_bytes", 64 << 20, ("file_past_size_limit_bytes", 2 << 20)),
])
def test_the_quota_probe_sees_each_quota_gone(quota, loosened, observed,
                                              tmp_path, monkeypatch):
    """Rlimits apply with or without namespaces, so switching isolation off
    does not remove them. Here ONE of the probe's requests is ignored at a
    time -- the others are honoured -- and the probe must say the quotas do
    not hold, for that quota alone. A short wall stands in for the missing
    CPU bound, so the run that should have been stopped by it ends on the
    wall instead."""
    real = IC._limits

    def one_loose(**over):
        if quota in over:
            over = {**over, quota: loosened}
            if quota == "cpu_seconds":
                over["wall_seconds"] = 3.0
        return real(**over)
    monkeypatch.setattr(IC, "_limits", one_loose)
    got = IC.probe_quotas(tmp_path)
    assert not got["holds"], got
    key, value = observed
    assert got["observed"][key] == value, got


def test_a_pass_is_reported_as_this_host_s_and_no_deployment_s():
    assert "nothing about a deployment" in CONTRACT["note"]
    assert "RUN-3" in BY_ID["filesystem"]["deployment_requirement"]
    for key in ("kernel", "machine", "namespaces"):
        assert key in IC.host()
