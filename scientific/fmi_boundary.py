"""The FMI boundary: what an FMU may bring into the harness, and how.

An FMU is a zip archive from outside -- a model description in XML and a
compiled binary. This module is the harness's own reading of one, written so
that crossing the FMI boundary changes nothing about what a result IS:

* **The archive** is read without extracting it blindly: every member name
  must be relative, free of ``..``, not a link, and the total uncompressed
  size bounded; ``modelDescription.xml`` must be present.
* **The XML** is refused if it declares a DOCTYPE or an entity (no external
  or expanding entities reach the parser), and must be FMI 3.0 with a
  Co-Simulation interface.
* **Units** are read through ``declaredType`` or the variable itself; every
  Float64 parameter, input and output must have one that the description
  defines. A unit the importer cannot see is a unit it would drop.
* **The claim boundary** (FMI-P4) is an annotation of type
  ``org.scientific-ai-harness.claim-boundary``: the FMU must say its
  observations are SIMULATION_RESULT and that it carries no authority
  (NON_AUTHORITATIVE). An FMU that says nothing, claims a measured kind, or
  claims authority is refused: an imported result does not become a
  measurement, or authoritative, because it crossed an FMI boundary.
* **The variable contract** (FMI-P5): every variable a model declares for
  export is compared by name, causality, variability and unit with what the
  description says; any difference is listed.

Nothing here runs the FMU. Running it is the FMI runtime's job
(``integrations/fmi/fmpy_runner.py``, fmpy, in its own environment); what
comes back is a SIMULATION_RESULT like any other model's, and enters the
same evidence and authority path.
"""
from __future__ import annotations

import hashlib
import io
import stat
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

CLAIM_ANNOTATION = "org.scientific-ai-harness.claim-boundary"
ALLOWED_KINDS = frozenset({"SIMULATION_RESULT"})
REQUIRED_AUTHORITY = "NON_AUTHORITATIVE"
MAX_MEMBERS = 64
MAX_TOTAL_BYTES = 64 * 1024 * 1024
PLATFORM = "x86_64-linux"


class FmuRefused(ValueError):
    """The FMU cannot cross the boundary; the message says why."""


@dataclass(frozen=True)
class Variable:
    name: str
    value_reference: int
    type: str
    causality: str
    variability: str
    unit: str | None


@dataclass(frozen=True)
class FmuDescription:
    archive_sha256: str
    binary_sha256: str
    model_identifier: str
    instantiation_token: str
    fmi_version: str
    variables: tuple
    units: tuple
    claim: dict = field(default_factory=dict)

    def variable(self, name: str) -> Variable:
        for v in self.variables:
            if v.name == name:
                return v
        raise KeyError(name)


def _safe_members(zf: zipfile.ZipFile) -> dict:
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise FmuRefused(f"{len(infos)} archive members (max {MAX_MEMBERS})")
    total = 0
    names = {}
    for info in infos:
        n = info.filename
        if n.startswith("/") or "\\" in n or ".." in n.split("/") or \
                (len(n) > 1 and n[1] == ":"):
            raise FmuRefused(f"archive member {n!r} escapes the archive")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise FmuRefused(f"archive member {n!r} is a link")
        if n in names:
            raise FmuRefused(f"archive member {n!r} appears twice")
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise FmuRefused("archive expands beyond the size bound")
        names[n] = info
    if "modelDescription.xml" not in names:
        raise FmuRefused("no modelDescription.xml at the archive root")
    return names


def _parse_xml(raw: bytes) -> ET.Element:
    head = raw[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in raw.upper():
        raise FmuRefused("the model description declares a DOCTYPE or an "
                         "entity; refused before parsing")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise FmuRefused(f"the model description is not XML: {exc}") \
            from None


_TYPES = ("Float64", "Float32", "Int8", "UInt8", "Int16", "UInt16", "Int32",
          "UInt32", "Int64", "UInt64", "Boolean", "String", "Binary",
          "Enumeration", "Clock")


def describe(path: Path | str) -> FmuDescription:
    """Read an FMU at the boundary; raise FmuRefused on anything outside it."""
    raw = Path(path).read_bytes()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise FmuRefused(f"not a zip archive: {exc}") from None
    with zf:
        names = _safe_members(zf)
        root = _parse_xml(zf.read("modelDescription.xml"))
        if root.tag != "fmiModelDescription":
            raise FmuRefused(f"root element {root.tag!r}")
        version = root.get("fmiVersion", "")
        if version != "3.0":
            raise FmuRefused(f"fmiVersion {version!r}; only 3.0 is admitted")
        cs = root.find("CoSimulation")
        ident = cs.get("modelIdentifier") if cs is not None else None
        if not ident:
            raise FmuRefused("no Co-Simulation interface")
        binary = f"binaries/{PLATFORM}/{ident}.so"
        if binary not in names:
            raise FmuRefused(f"no binary {binary}")
        binary_sha = hashlib.sha256(zf.read(binary)).hexdigest()
    units = tuple(sorted(u.get("name", "") for u in
                         root.findall("UnitDefinitions/Unit")))
    td = root.find("TypeDefinitions")
    declared = {t.get("name"): t.get("unit")
                for t in (list(td) if td is not None else [])}
    variables = []
    mv = root.find("ModelVariables")
    if mv is None:
        raise FmuRefused("no ModelVariables")
    for el in mv:
        if el.tag not in _TYPES:
            raise FmuRefused(f"unknown variable element {el.tag!r}")
        unit = el.get("unit") or declared.get(el.get("declaredType"))
        try:
            vr = int(el.get("valueReference", ""))
        except ValueError:
            raise FmuRefused(f"variable {el.get('name')!r}: value "
                             "reference is not an integer") from None
        variables.append(Variable(
            name=el.get("name", ""), value_reference=vr, type=el.tag,
            causality=el.get("causality", "local"),
            variability=el.get("variability", "continuous"), unit=unit))
    for v in variables:
        if v.type == "Float64" and v.causality in ("parameter", "input",
                                                   "output"):
            if not v.unit:
                raise FmuRefused(f"{v.name}: a {v.causality} with no unit")
            if v.unit not in units:
                raise FmuRefused(f"{v.name}: unit {v.unit!r} is not defined "
                                 "in UnitDefinitions")
    if len({v.value_reference for v in variables}) != len(variables):
        raise FmuRefused("two variables share a value reference")
    return FmuDescription(
        archive_sha256=hashlib.sha256(raw).hexdigest(),
        binary_sha256=binary_sha, model_identifier=ident,
        instantiation_token=root.get("instantiationToken", ""),
        fmi_version=version, variables=tuple(variables), units=units,
        claim=_claim(root))


def _claim(root: ET.Element) -> dict:
    found = [a for a in root.findall("Annotations/Annotation")
             if a.get("type") == CLAIM_ANNOTATION]
    if len(found) != 1:
        raise FmuRefused(f"{len(found)} claim-boundary annotations; exactly "
                         "one is required (FMI-P4)")
    cb = found[0].find("ClaimBoundary")
    if cb is None:
        raise FmuRefused("the claim-boundary annotation is empty")
    kind = cb.get("observationKind", "")
    auth = cb.get("authority", "")
    if kind not in ALLOWED_KINDS:
        raise FmuRefused(f"observationKind {kind!r}: an FMU result is a "
                         "SIMULATION_RESULT and nothing else")
    if auth != REQUIRED_AUTHORITY:
        raise FmuRefused(f"authority {auth!r}: an FMU carries no authority "
                         f"({REQUIRED_AUTHORITY})")
    model = cb.get("scientificModel", "")
    version = cb.get("scientificModelVersion", "")
    if not model or not version:
        raise FmuRefused("the claim boundary names no scientific model")
    return {"observation_kind": kind, "authority": auth,
            "scientific_model": model, "scientific_model_version": version,
            "integrator": cb.get("integrator", "")}


def contract_differences(desc: FmuDescription, contract: dict) -> list:
    """FMI-P5: every exported variable's causality, variability and unit
    against the model's declared table; [] when they all agree."""
    out = []
    for name, (causality, variability, unit) in sorted(contract.items()):
        try:
            v = desc.variable(name)
        except KeyError:
            out.append(f"{name}: not exported")
            continue
        for what, want, got in (("causality", causality, v.causality),
                                ("variability", variability, v.variability),
                                ("unit", unit, v.unit)):
            if want != got:
                out.append(f"{name}: {what} {got!r}, declared {want!r}")
    return out
