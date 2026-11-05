"""A ResultBundle as HDF5: a deterministic representation, never authority.

HDF5_DATA_MODEL.md promises a generic representation of scientific
``ResultBundle`` artefacts; this is it. One file holds one bundle:

    /                       attrs: schema, observation_kind, model identity,
                            the three identity digests, the bundle digest,
                            ``authority`` (always "NONE: representation")
    /bundle_record          the bundle's canonical JSON, exactly -- the
                            round trip is to these bytes, so nothing is lost
                            to a structured view
    /parameters/<name>      one scalar each, attrs ``unit`` (from the model's
                            declared schema, or the literal "unresolved")
    /outputs/<name>         a scalar with every Quantity field as an attr
                            (unit, resolution, its class and basis, reporting
                            digits, uncertainty class, exact, zero state); a
                            FAILED or UNDEFINED output is an empty group with
                            its status and reason
    /invariants/<id>        attrs holds, criterion, derivation; ``measured``
                            and ``threshold`` scalars with their units
    /artifacts/<name>       ``raw``: the artefact's bytes, digest-checked;
                            for a known layout also its decoded arrays with
                            units and a finite-validity mask
    /verification           digests of VerificationResults that cite this
                            bundle (links, nothing more: no state, no verdict)

Deterministic: datasets are created in sorted order with ``track_times`` off,
no compression, the earliest file format the library writes, and no
timestamp or absolute path anywhere -- so the same bundle gives the same
bytes with the same h5py/HDF5 build. Reading refuses a file whose structured
view disagrees with its own record, whose record is not its stated digest,
whose artefact bytes are not their digest, or whose schema is another one.

Representation only. Storing a bundle admits nothing; a VERIFIED or PROMOTED
state is never written here, and a link to a report is not the report's
verdict.
"""
from __future__ import annotations

import json
from pathlib import Path

from .identity import digest, digest_bytes
from .result import OutputStatus, ResultBundle

SCHEMA = "result-bundle-hdf5/1"
AUTHORITY = "NONE: representation only"
UNRESOLVED = "unresolved"
_QUANTITY_ATTRS = ("unit", "resolution", "resolution_class",
                   "resolution_basis", "reporting_digits",
                   "uncertainty_class", "exact", "zero_state")


class Hdf5Refused(ValueError):
    pass


def _h5():
    import h5py
    return h5py


def _attr(v):
    """An attribute value HDF5 stores exactly; None as the empty string is
    ambiguous, so it is written as JSON text."""
    if v is None or isinstance(v, (list, dict)):
        return json.dumps(v, sort_keys=True)
    return v


def _unattr(v):
    if isinstance(v, bytes):
        v = v.decode("utf-8")
    if hasattr(v, "item"):
        v = v.item()
    return v


def _decode_slab_field(raw: bytes) -> dict | None:
    """The slab model's QTSL1 layout: header line, then x and T as <f8."""
    import numpy as np
    if not raw.startswith(b"QTSL1\n"):
        return None
    head_end = raw.index(b"\n", 6)
    head = json.loads(raw[6:head_end])
    n = head["n"]
    body = raw[head_end + 1:]
    if len(body) != 16 * n:
        raise Hdf5Refused("QTSL1 field: payload length disagrees with its "
                          "header")
    xs = np.frombuffer(body[:8 * n], dtype="<f8")
    ts = np.frombuffer(body[8 * n:], dtype="<f8")
    return {"x": (xs, head["units"]["x"]), "T": (ts, head["units"]["T"])}


def write(path: Path | str, bundle: ResultBundle, *,
          artifacts: dict | None = None, parameter_units: dict | None = None,
          verification_links: tuple = ()) -> str:
    """Write ``bundle`` (and the artefact bytes it cites) to ``path``;
    return the file's SHA-256."""
    import numpy as np
    h5py = _h5()
    artifacts = dict(artifacts or {})
    units = dict(parameter_units or {})
    rec = bundle.to_record()
    refs = {a.name: a for a in bundle.artifacts}
    if set(artifacts) - set(refs):
        raise Hdf5Refused(f"artefacts the bundle does not cite: "
                          f"{sorted(set(artifacts) - set(refs))}")
    for name, raw in artifacts.items():
        if digest_bytes(raw) != refs[name].digest or \
                len(raw) != refs[name].size_bytes:
            raise Hdf5Refused(f"artefact {name}: bytes are not the ones the "
                              "bundle cites")
    for link in verification_links:
        if not isinstance(link, str) or len(link) != 64:
            raise Hdf5Refused("a verification link is a report digest")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    kw = {"track_times": False}
    with h5py.File(path, "w", libver="earliest", track_order=True) as f:
        for k, v in (("schema", SCHEMA), ("authority", AUTHORITY),
                     ("observation_kind", rec["observation_kind"]),
                     ("model_id", rec["model_id"]),
                     ("model_version", rec["model_version"]),
                     ("implementation_digest", rec["implementation_digest"]),
                     ("parameter_digest", rec["parameter_digest"]),
                     ("environment_digest", rec["environment_digest"]),
                     ("bundle_digest", bundle.digest())):
            f.attrs[k] = v
        text = json.dumps(rec, sort_keys=True, ensure_ascii=True,
                          separators=(",", ":"), allow_nan=False)
        f.create_dataset("bundle_record", data=np.frombuffer(
            text.encode("utf-8"), dtype=np.uint8), **kw)
        g = f.create_group("parameters", track_order=True)
        for name in sorted(rec["parameters"]):
            ds = g.create_dataset(name, data=rec["parameters"][name], **kw)
            ds.attrs["unit"] = units.get(name, UNRESOLVED)
        g = f.create_group("outputs", track_order=True)
        for o in sorted(rec["outputs"], key=lambda o: o["name"]):
            if o["status"] == OutputStatus.OK.value:
                q = o["quantity"]
                ds = g.create_dataset(o["name"], data=float(q["value"]),
                                      **kw)
                ds.attrs["status"] = o["status"]
                for a in _QUANTITY_ATTRS:
                    ds.attrs[a] = _attr(q[a])
            else:
                sub = g.create_group(o["name"])
                sub.attrs["status"] = o["status"]
                sub.attrs["reason"] = o["reason"]
        g = f.create_group("invariants", track_order=True)
        for inv in sorted(rec["invariants"],
                          key=lambda i: i["invariant_id"]):
            sub = g.create_group(inv["invariant_id"], track_order=True)
            sub.attrs["holds"] = bool(inv["holds"])
            sub.attrs["criterion"] = inv["criterion"]
            sub.attrs["derivation"] = inv["derivation"]
            for part in ("measured", "threshold"):
                q = inv[part]
                if q is None:
                    continue
                ds = sub.create_dataset(part, data=float(q["value"]), **kw)
                for a in _QUANTITY_ATTRS:
                    ds.attrs[a] = _attr(q[a])
        g = f.create_group("artifacts", track_order=True)
        for name in sorted(artifacts):
            raw = artifacts[name]
            sub = g.create_group(name, track_order=True)
            sub.attrs["digest"] = refs[name].digest
            sub.attrs["media_type"] = refs[name].media_type
            sub.create_dataset("raw", data=np.frombuffer(raw, dtype=np.uint8),
                               **kw)
            decoded = _decode_slab_field(raw)
            for col, (arr, unit) in sorted((decoded or {}).items()):
                ds = sub.create_dataset(col, data=arr, **kw)
                ds.attrs["unit"] = unit
                sub.create_dataset(f"{col}_valid",
                                   data=np.isfinite(arr), **kw)
        ds = f.create_dataset("verification", data=np.array(
            sorted(verification_links), dtype="S64"), **kw)
        ds.attrs["meaning"] = ("digests of VerificationResults citing this "
                               "bundle; a link, not a verdict")
    return digest_bytes(path.read_bytes())


def read(path: Path | str) -> tuple:
    """``(bundle, artifacts, verification_links)``, or refuse.

    Every way a file can fail to be one -- the library's own errors on
    corrupt metadata included (RuntimeError, OSError), found by fuzzing --
    is an Hdf5Refused naming it: the reader is the trust boundary, so
    nothing escapes it unnamed."""
    try:
        return _read(path)
    except Hdf5Refused:
        raise
    except (OSError, RuntimeError, KeyError, TypeError, ValueError,
            AttributeError, IndexError) as exc:
        raise Hdf5Refused(f"not a readable representation: "
                          f"{type(exc).__name__}: {exc}") from None


def _read(path: Path | str) -> tuple:
    h5py = _h5()
    with h5py.File(path, "r") as f:
        attrs = {k: _unattr(v) for k, v in f.attrs.items()}
        if attrs.get("schema") != SCHEMA:
            raise Hdf5Refused(f"schema {attrs.get('schema')!r} is not "
                              f"{SCHEMA}")
        if attrs.get("authority") != AUTHORITY:
            raise Hdf5Refused("a representation that claims authority")
        try:
            rec = json.loads(bytes(f["bundle_record"][()]).decode("utf-8"))
        except (KeyError, ValueError) as exc:
            raise Hdf5Refused(f"no readable bundle record: {exc}") from exc
        bundle = ResultBundle.from_record(rec)
        if bundle.digest() != attrs.get("bundle_digest"):
            raise Hdf5Refused("the record is not the bundle digest the file "
                              "states")
        for k in ("model_id", "model_version", "implementation_digest",
                  "parameter_digest", "environment_digest",
                  "observation_kind"):
            if attrs.get(k) != rec[k]:
                raise Hdf5Refused(f"attribute {k} disagrees with the record")
        problems = []
        params = f["parameters"]
        if sorted(params) != sorted(rec["parameters"]):
            problems.append("parameter names differ from the record")
        for name in sorted(set(params) & set(rec["parameters"])):
            if _unattr(params[name][()]) != rec["parameters"][name]:
                problems.append(f"parameter {name} differs from the record")
        outs = f["outputs"]
        by_name = {o["name"]: o for o in rec["outputs"]}
        if sorted(outs) != sorted(by_name):
            problems.append("output names differ from the record")
        for name in sorted(set(outs) & set(by_name)):
            o, node = by_name[name], outs[name]
            if _unattr(node.attrs.get("status")) != o["status"]:
                problems.append(f"output {name}: status differs")
                continue
            if o["status"] != OutputStatus.OK.value:
                continue
            q = o["quantity"]
            if float(node[()]) != q["value"]:
                problems.append(f"output {name}: value differs")
            for a in _QUANTITY_ATTRS:
                if _unattr(node.attrs.get(a)) != _attr(q[a]):
                    problems.append(f"output {name}: {a} differs")
        invs = f["invariants"]
        by_id = {i["invariant_id"]: i for i in rec["invariants"]}
        if sorted(invs) != sorted(by_id):
            problems.append("invariant ids differ from the record")
        for iid in sorted(set(invs) & set(by_id)):
            if bool(invs[iid].attrs["holds"]) != by_id[iid]["holds"]:
                problems.append(f"invariant {iid}: holds differs")
        refs = {a.name: a for a in bundle.artifacts}
        artifacts = {}
        for name in sorted(f["artifacts"]):
            if name not in refs:
                problems.append(f"artefact {name} is not cited by the bundle")
                continue
            raw = bytes(f["artifacts"][name]["raw"][()])
            if digest_bytes(raw) != refs[name].digest:
                problems.append(f"artefact {name}: bytes are not its digest")
            artifacts[name] = raw
        links = tuple(x.decode("ascii") for x in f["verification"][()])
        if problems:
            raise Hdf5Refused("; ".join(problems))
    return bundle, artifacts, links


def file_digest(path: Path | str) -> str:
    return digest_bytes(Path(path).read_bytes())


def manifest(path: Path | str) -> dict:
    """What a packager records about the file: its digest and what it
    represents -- never a claim that it was verified."""
    bundle, artifacts, links = read(path)
    return {"path": str(path), "sha256": file_digest(path),
            "schema": SCHEMA, "represents": bundle.digest(),
            "model": f"{bundle.model_id}@{bundle.model_version}",
            "artifacts": sorted(artifacts), "verification_links":
                list(links), "authority": AUTHORITY,
            "record_digest": digest(bundle.to_record())}
