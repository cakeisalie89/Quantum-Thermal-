"""The deployment's filesystem_confinement check, run here (D-2026-157).

tools/confinement_probe.py is what a deployment runs and attests for RUN-3.
On this host it shows what THIS host does; these tests check that it reports
observations rather than the executor's own account, and that it fails when
the observations are not the ones confinement requires.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import confinement_probe as CP  # noqa: E402
import test_filesystem_confinement as FC  # noqa: E402

import agent_substrate.execution as X  # noqa: E402


@FC.needs_confinement
def test_the_probe_passes_where_confinement_is_in_force(tmp_path):
    out = CP.probe(tmp_path)
    assert out["result"] == "PASS", out["failures"]
    assert out["observed"] == CP.EXPECTED
    assert out["confinement"].startswith("landlock")
    assert out["check"] == "filesystem_confinement"


def test_the_probe_fails_where_the_host_cannot_confine(tmp_path, monkeypatch):
    """No namespaces: high_assurance does not run the probe tool, and the
    probe says FAIL with nothing observed -- not PASS on a refusal."""
    monkeypatch.setattr(X, "namespace_isolation", lambda: (
        (), {"namespaces": [], "detail": "unshare refused: simulated"}))
    out = CP.probe(tmp_path)
    assert out["result"] == "FAIL"
    assert out["observed"] == {}
    assert any("simulated" in f for f in out["failures"])


@FC.needs_confinement
def test_the_probe_fails_on_an_observation_confinement_forbids(
        tmp_path, monkeypatch):
    """A sentinel that opened is a FAIL, whatever the record says."""
    real = X.confinement_policy

    def granting_the_sentinel(**kw):
        pol = real(**kw)
        pol["read"].append(str(kw["cwd"] / "sentinel.txt"))
        return pol

    monkeypatch.setattr(X, "confinement_policy", granting_the_sentinel)
    out = CP.probe(tmp_path)
    assert out["result"] == "FAIL"
    assert any(f.startswith("sentinel:") for f in out["failures"]), \
        out["failures"]


def test_the_cli_exit_status_is_the_verdict(monkeypatch, capsys):
    monkeypatch.setattr(X, "namespace_isolation", lambda: (
        (), {"namespaces": [], "detail": "unshare refused: simulated"}))
    assert CP.main([]) == 1
    assert '"result": "FAIL"' in capsys.readouterr().out
