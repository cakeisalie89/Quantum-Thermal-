"""The stack registry as a live authority, and the documents held to it.

``stack.json`` is validated by a strict model (``tools/stack_registry.py``);
``STACK.md`` and ``README.md`` state the same statuses in prose and tables,
and each document check is a function over TEXT run twice: on the shipped
document, which must pass, and on a stale control, which must fail. A
mutation of a governed document is refused first by the retrieval corpus
allowlist, which would be a kill for the wrong reason, so these carry their
own controls (D-2026-130).
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import stack_registry as REG  # noqa: E402

STACK_JSON = json.loads((ROOT / "stack.json").read_text())
STACK_MD = (ROOT / "STACK.md").read_text()
REGISTRY = REG.load_registry()


def _doc(**changes):
    doc = json.loads((ROOT / "stack.json").read_text())
    doc.update(changes)
    return doc


def test_registry_validates_against_its_strict_model():
    assert REGISTRY.schema_version == REG.REGISTRY_SCHEMA_VERSION
    assert REGISTRY.label == REG.LABEL
    assert REGISTRY.authority_effect == "NONE"
    assert len(REGISTRY.elements) == len(STACK_JSON["elements"])


@pytest.mark.parametrize("edit", [
    lambda d: d.update(label="anything else"),
    lambda d: d.update(authority_effect="ADVISORY"),
    lambda d: d.update(surprise="value"),
    lambda d: d["elements"][0].update(status="MOSTLY_ADOPTED"),
    lambda d: d["elements"][0].update(verification=""),
    lambda d: d["elements"].append(dict(d["elements"][0])),
    lambda d: d["adoption_levels"].pop("ADOPTED"),
    lambda d: next(e for e in d["elements"] if e["id"] == "fmi").update(
        status="STAGED", open_items=[]),
    lambda d: d["retired"].append({"id": d["elements"][0]["id"],
                                   "retired_at": "0000000",
                                   "why": "both at once"}),
])
def test_registry_rejects_edits_that_would_weaken_it(edit):
    from pydantic import ValidationError
    doc = _doc()
    edit(doc)
    with pytest.raises(ValidationError):
        REG.StackRegistry.model_validate(doc)


def test_every_declared_element_is_documented():
    for element in REGISTRY.elements:
        assert f"| {element.doc_key} |" in STACK_MD, element.id


def _ladder_mismatches(stack_md: str) -> list:
    lines = stack_md.splitlines()
    out = []
    for element in REGISTRY.elements:
        row = next((ln for ln in lines
                    if ln.startswith(f"| {element.doc_key} |")), None)
        if row is None:
            out.append((element.id, "no row"))
            continue
        stated = re.sub(r"[*¹²³]", "",
                        row.split("|")[2]).strip()
        if stated != element.status:
            out.append((element.id, stated, element.status))
    return out


def test_the_ladder_table_states_each_elements_registered_status():
    assert _ladder_mismatches(STACK_MD) == []
    row = next(ln for ln in STACK_MD.splitlines()
               if ln.startswith("| FEniCSx |"))
    stale = STACK_MD.replace(row, row.replace("ADOPTED", "STAGED", 1))
    assert stale != STACK_MD
    assert _ladder_mismatches(stale) == [("fenicsx", "STAGED", "ADOPTED")]


def _stated_status(text: str, name: str):
    m = re.search(re.escape(name) + r"[^()]*\((ADOPTED|RESOLVED|STAGED|"
                  r"DEFERRED|RETIRED)", text)
    return m.group(1) if m else None


@pytest.mark.parametrize("eid, name", [("fenicsx", "FEniCSx"),
                                       ("fmi", "FMI 3.0"),
                                       ("openmdao", "OpenMDAO")])
def test_the_readme_states_each_integrations_registered_status(eid, name):
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert _stated_status(readme, name) == REGISTRY.by_id(eid).status
    other = "DEFERRED" if REGISTRY.by_id(eid).status != "DEFERRED" \
        else "ADOPTED"
    assert _stated_status(f"the {name} element ({other}, said here)",
                          name) == other


def test_every_element_is_settled():
    """The software-only contract requires every element in an accepted
    state; an element that is not is visible here before it is visible in
    the derived status."""
    unsettled = [e.id for e in REGISTRY.elements
                 if e.status not in REG.SETTLED]
    assert unsettled == []


def test_every_owner_exists():
    """An owner that is neither a file of this tree nor an importable module
    is a registry row about nothing."""
    for element in REGISTRY.elements:
        owner = element.owner_module
        if (ROOT / owner).exists():
            continue
        assert re.fullmatch(r"[a-z_][a-z0-9_.]*", owner), owner
        importlib.import_module(owner)


def test_every_element_adopted_on_hosted_evidence_cites_its_run():
    for eid in ("containers", "fenicsx", "fmi", "slsa-sigstore"):
        assert re.search(r"run \d{8,}|hosted", REGISTRY.by_id(eid).boundary)


def test_registry_verification_targets_exist():
    """A verification command names a Snakemake target that exists, or a
    test file that exists."""
    snakefile = (ROOT / "Snakefile").read_text()
    for element in REGISTRY.elements:
        command = element.verification.split("#")[0].strip()
        if command.startswith("snakemake"):
            target = command.split()[-1]
            if target.startswith("verification/"):
                assert target.split("/")[-1] in snakefile, target
            else:
                assert f"rule {target}:" in snakefile, target
        for path in re.findall(r"tests/test_\w+\.py", command):
            assert (ROOT / path).is_file(), path


def test_the_retired_element_is_recorded_and_not_live():
    retired = {r.id: r for r in REGISTRY.retired}
    assert "rust-selective" in retired
    assert "REJECTED" in retired["rust-selective"].why
    assert "rust-selective" not in {e.id for e in REGISTRY.elements}
    assert not (ROOT / "rust").exists()


def test_the_container_claims_no_more_than_its_runs():
    c = REGISTRY.by_id("containers")
    assert c.status == "ADOPTED"
    assert "never a scientific result" in c.boundary
    assert "| Reproducible container | ADOPTED |" in STACK_MD
    assert "@sha256:" in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_fenicsx_is_a_verifier_never_a_producer():
    fx = REGISTRY.by_id("fenicsx")
    assert fx.status == "ADOPTED"
    assert "a verifier, never a producer" in fx.boundary
    assert fx.open_items, "the models it does not cover are outstanding"


@pytest.mark.parametrize("eid", ["paraview-vtk", "openusd", "openmdao"])
def test_the_reimplemented_integrations_name_their_new_owner(eid):
    """The three integrations rebuilt over the generic layer are owned by
    scientific.integrations, and each says what it is not."""
    e = REGISTRY.by_id(eid)
    assert e.owner_module.startswith("scientific.integrations."), eid
    assert "not" in e.boundary.lower()
