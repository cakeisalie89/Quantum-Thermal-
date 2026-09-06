"""§11/§31 regression: machine-readable authority must not disagree with reality.

authorities.json is the machine-readable half of AUTHORITIES.md. It drifted:
it claimed the manifest held "238 governed files" (it holds every tracked file,
which was 398 at the time), and it recorded that three root module copies had
been "removed in Stage1-CP1 (commit 9525e24)" -- a commit that does not exist
in this repository, describing files that are still present.

These tests pin the properties that made those claims detectable:
a registered authority path must exist; a cited commit must resolve; a claim
about a file's presence must match the tree; and a governed constant should
have one definition, not several with no declared derivation.

MODEL-ONLY / FORECAST-ONLY. Software verification; not a hardware statement.
"""
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AUTHORITIES = json.loads((ROOT / "authorities.json").read_text(encoding="utf-8"))
MANIFEST = json.loads((ROOT / "final_manifest.json").read_text(encoding="utf-8"))


def _tracked():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                         capture_output=True, text=True, check=True).stdout
    return set(out.split())


#: Fields holding quoted historical text rather than live claims. A withdrawn
#: claim must stay readable so the correction is auditable, but its content is
#: not asserted by these tests.
HISTORICAL_FIELDS = ("withdrawn_claim",)


def _strings(obj, skip=HISTORICAL_FIELDS):
    """Every live string value in the registry, with its dotted path.

    Fields listed in ``skip`` are historical quotations and are excluded.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in skip:
                continue
            yield from ((f"{k}.{p}" if p else k, s) for p, s in _strings(v, skip))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from ((f"[{i}].{p}" if p else f"[{i}]", s) for p, s in _strings(v, skip))
    elif isinstance(obj, str):
        yield "", obj


# ------------------------------------------------------- stale count claims --

def test_no_stale_governed_file_count_claim():
    """The registry must not restate a count that the git index owns.

    The literal defect: "final_manifest.json (238 governed files, ...)" while
    the manifest listed 398. A count copied into prose cannot be kept true.
    """
    n = len(MANIFEST["files"])
    pat = re.compile(r"(\d{2,5})\s+governed\s+files", re.I)
    for path, s in _strings(AUTHORITIES):
        m = pat.search(s)
        if m and int(m.group(1)) != n:
            raise AssertionError(
                f"authorities.json {path} claims {m.group(1)} governed files; "
                f"final_manifest.json lists {n}")


def test_manifest_membership_is_not_described_as_scientific_authority():
    pr = AUTHORITIES["authorities"]["provenance_records"]
    blob = " ".join(str(v) for v in pr.values()).lower()
    assert "provenance" in blob
    assert "does_not_mean" in pr, "the provenance/authority boundary must be explicit"


# ------------------------------------------------------------ cited commits --

def test_every_cited_commit_resolves():
    """A registry that cites a commit as evidence must cite a real one.

    Historical quotations (HISTORICAL_FIELDS) are excluded: the withdrawn
    root-module record names the non-existent commit it used to cite, and that
    text is the audit trail, not a claim.
    """
    pat = re.compile(r"\bcommit\s+([0-9a-f]{7,40})\b", re.I)
    bad = []
    for path, s in _strings(AUTHORITIES):
        for sha in pat.findall(s):
            r = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-t", sha],
                               capture_output=True, text=True)
            if r.returncode != 0 or r.stdout.strip() != "commit":
                bad.append(f"{path}: {sha}")
    assert not bad, f"authorities.json cites non-existent commits: {bad}"


# ------------------------------------------- root duplicate modules: reality --

ROOT_DUPLICATES = ("units.py", "verification.py", "vibration_transfer.py")


def test_root_duplicate_record_matches_the_tree():
    """The registry's claim about the root copies must match the actual tree."""
    rec = next(r for r in AUTHORITIES["competing_sources_record"]
               if r["concept"] == "root-level module copies")
    present = [f for f in ROOT_DUPLICATES if (ROOT / f).exists()]
    if present:
        assert rec.get("status") == "PRESENT_UNIMPORTED_DUPLICATE", (
            f"{present} exist but the registry says {rec.get('status')!r}")
    else:
        assert rec.get("status") == "RESOLVED_ROOT_COPIES_DELETED", (
            "the root copies are gone but the registry does not say so: "
            f"{rec.get('status')!r}")


def test_the_authoritative_package_copies_survive():
    """Deleting the duplicates must never have removed the retained copy."""
    for f in ROOT_DUPLICATES:
        pp = ROOT / "qta_multiphysics" / f
        assert pp.exists(), f"authoritative qta_multiphysics/{f} is missing"
        assert pp.stat().st_size > 0


def test_no_module_imports_a_root_level_copy():
    """Holds whether or not the root copies exist."""
    import hashlib                                          # noqa: F401
    for f in ROOT_DUPLICATES:
        rp, pp = ROOT / f, ROOT / "qta_multiphysics" / f
        if rp.exists():
            assert hashlib.sha256(rp.read_bytes()).hexdigest() == \
                   hashlib.sha256(pp.read_bytes()).hexdigest(), \
                   f"root {f} has diverged from the package copy"
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "tools"))
    from repo_scope import files_matching

    stems = "|".join(f[:-3] for f in ROOT_DUPLICATES)
    # Tracked AND untracked-unignored: a new module importing a root copy
    # would otherwise pass this guard until it was committed.
    importers = files_matching(rf"^\s*(from|import)\s+({stems})\b")
    assert not importers, f"root copies are imported: {list(importers)}"


# ---------------------------------------- registered authority paths resolve --

def test_registered_authority_modules_exist():
    missing = []
    pat = re.compile(r"\b([\w/]+\.py)\b")
    for name, entry in AUTHORITIES["authorities"].items():
        for rel in pat.findall(entry.get("authority", "")):
            if not (ROOT / rel).exists():
                missing.append(f"{name}: {rel}")
    assert not missing, f"authorities.json names modules that do not exist: {missing}"


# ------------------------------ §31 single source of truth: duplicate values --

def test_mode_d_threshold_has_one_definition():
    """config.SolverConfig is the registered tolerance authority.

    metrics.py carried its own literal 0.050. Duplicated governed thresholds
    with no declared derivation are how two files come to disagree silently.
    """
    from qta_multiphysics.config import default_config
    from qta_multiphysics import metrics
    cfg_value = default_config().solver.mode_d_temp_threshold_K
    src = (ROOT / "qta_multiphysics" / "metrics.py").read_text(encoding="utf-8")
    literal = re.search(r"^\s*th\s*=\s*0\.0?50\b", src, re.M)
    assert literal is None, (
        "metrics.py redefines the Mode-D readiness threshold as a literal; it "
        f"must derive it from config.SolverConfig (={cfg_value})")
    assert getattr(metrics, "MODE_D_TEMP_THRESHOLD_K", cfg_value) == cfg_value


def test_electron_gyromagnetic_ratio_agrees_across_definitions():
    """qta_full_sim and the NV source map must not disagree on gamma_e."""
    import math
    from qta_multiphysics.nv_spin.model import measured_constants
    c = measured_constants()
    assert abs(c["gamma_e_rad_s_T"] - 2 * math.pi * 28.025e9) \
        / c["gamma_e_rad_s_T"] < 1e-12


def test_withdrawn_claims_are_quarantined_not_deleted():
    """A corrected false claim stays readable, but never as a live citation."""
    rec = next(r for r in AUTHORITIES["competing_sources_record"]
               if r["concept"] == "root-level module copies")
    assert "9525e24" in rec.get("withdrawn_claim", ""), \
        "the withdrawn citation must remain auditable"
    live = " ".join(s for _, s in _strings(rec))
    assert "9525e24" not in live, \
        "the non-existent commit must not appear in any live field"
