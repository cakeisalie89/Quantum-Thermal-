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

Reading happens in a child process (AUD-053). The HDF5 library is native
code, and a crafted file makes it fault or loop: the first kills the process
it runs in and the second holds it, and neither can be caught where it
happens. The child does everything the library does and answers with what it
read or with its refusal; the parent names a child that died or did not
answer, and holds the answer to itself before it returns any of it.
"""
from __future__ import annotations

import base64
import json
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from .identity import digest, digest_bytes
from .result import OutputStatus, ResultBundle

SCHEMA = "result-bundle-hdf5/1"
AUTHORITY = "NONE: representation only"
UNRESOLVED = "unresolved"
_QUANTITY_ATTRS = ("unit", "resolution", "resolution_class",
                   "resolution_basis", "reporting_digits",
                   "uncertainty_class", "exact", "zero_state")


#: The longest one read may take. A file that makes the library loop is
#: refused when this runs out, where it would have held its reader for good.
READ_TIMEOUT_S = 60.0

#: The directory the ``scientific`` package is in: the one entry the child
#: adds to a path that isolated mode has emptied of everything but the
#: interpreter's own.
_ROOT = Path(__file__).resolve().parent.parent

#: What the child runs. Its first argument is ``_ROOT`` and its second the
#: file to read.
_CHILD = ("import sys; sys.path.insert(0, sys.argv[1]); "
          "from scientific import hdf5_bundle; "
          "raise SystemExit(hdf5_bundle._child(sys.argv[2]))")

#: What a library error on a file is called, in the child as it was before
#: there was one.
_LIBRARY_ERRORS = (OSError, RuntimeError, KeyError, TypeError, ValueError,
                   AttributeError, IndexError)


class Hdf5Refused(ValueError):
    pass


class Hdf5ReaderFault(RuntimeError):
    """The reader failed, and not because of anything it can say about the
    file: its child ended with a traceback, answered with what is not an
    answer, or contradicted itself. Not a refusal, so that it is never
    taken for one."""


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
    nothing escapes it unnamed. That includes what the library does with a
    file it does not survive. A child that dies by a signal, or has not
    answered in ``READ_TIMEOUT_S``, is a refusal of the file (AUD-053).

    The parent takes none of the answer on trust. It rebuilds the bundle
    from the record and holds it to the digest the child gave, each
    artefact to the digest the bundle cites, and each link to being text.
    An answer that fails that, or a child that ends any other way, is the
    reader's failure and not the file's: Hdf5ReaderFault."""
    answer = _ask(Path(path).absolute())
    try:
        return _accept(answer)
    except (KeyError, TypeError, ValueError, AttributeError,
            IndexError) as exc:
        raise Hdf5ReaderFault(
            f"the reader's child gave an answer that is not one: "
            f"{type(exc).__name__}: {exc}") from None


def _child_argv(path: Path) -> list:
    """The child's command line. Isolated mode: no environment variable, no
    user site and no current directory decides what the child imports. It
    also ignores where the parent was told to keep bytecode, so that is
    passed on in so many words -- a child compiling beside the source would
    reuse a stale cache the parent had been pointed away from."""
    argv = [sys.executable, "-I"]
    if sys.pycache_prefix:
        argv += ["-X", f"pycache_prefix={sys.pycache_prefix}"]
    if sys.dont_write_bytecode:
        argv.append("-B")
    return argv + ["-c", _CHILD, str(_ROOT), str(path)]


def _ask(path: Path) -> dict:
    """Run the child on ``path`` and return its answer, having named every
    other way it can end. The child stands in an empty directory of its
    own, removed when it ends, so that nothing it leaves -- the core file of
    a fault, where cores are kept -- lands where its caller stands."""
    try:
        with tempfile.TemporaryDirectory(prefix="hdf5-read-") as cwd:
            proc = subprocess.run(_child_argv(path), capture_output=True,
                                  timeout=READ_TIMEOUT_S, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise Hdf5Refused(f"not a readable representation: reading it did "
                          f"not end in {READ_TIMEOUT_S:g} s") from None
    except OSError as exc:
        raise Hdf5ReaderFault(f"no child to read with: {exc}") from None
    if proc.returncode < 0:
        try:
            name = signal.Signals(-proc.returncode).name
        except ValueError:
            name = f"signal {-proc.returncode}"
        raise Hdf5Refused(f"not a readable representation: the HDF5 "
                          f"library died reading it ({name})")
    if proc.returncode != 0:
        tail = proc.stderr[-800:].decode("utf-8", "replace")
        raise Hdf5ReaderFault(f"the reader's child failed (exit "
                              f"{proc.returncode}): {tail}")
    try:
        answer = json.loads(proc.stdout)
    except (ValueError, RecursionError) as exc:
        raise Hdf5ReaderFault(f"the reader's child did not answer in "
                              f"JSON: {type(exc).__name__}") from None
    if not isinstance(answer, dict):
        raise Hdf5ReaderFault("the reader's child answered with what is "
                              "not an object")
    if "refused" in answer:
        raise Hdf5Refused(str(answer["refused"]))
    return answer


def _accept(answer: dict) -> tuple:
    """The child's answer, held to itself. Nothing in it is returned
    because the child said so: the bundle is rebuilt here and must be the
    one the child named, and the artefacts must be the bytes it cites."""
    bundle = ResultBundle.from_record(answer["record"])
    if bundle.digest() != answer["digest"]:
        raise Hdf5ReaderFault("the reader's child named a bundle its own "
                              "record is not")
    refs = {a.name: a for a in bundle.artifacts}
    artifacts = {}
    for name in sorted(answer["artifacts"]):
        raw = base64.b64decode(answer["artifacts"][name], validate=True)
        if name not in refs or digest_bytes(raw) != refs[name].digest:
            raise Hdf5ReaderFault(f"the reader's child returned bytes for "
                                  f"{name} that the bundle does not cite")
        artifacts[name] = raw
    if not isinstance(answer["links"], list):
        raise Hdf5ReaderFault("the reader's child returned links that are "
                              "not a list")
    links = tuple(answer["links"])
    if not all(isinstance(link, str) for link in links):
        raise Hdf5ReaderFault("the reader's child returned a link that is "
                              "not text")
    return bundle, artifacts, links


def _child(path: str) -> int:
    """The child's whole job: read, and write one line that is the answer
    or the refusal. Whatever else it does -- a traceback, a death -- the
    parent names."""
    out: dict
    try:
        rec, bundle, artifacts, links = _read(path)
    except Hdf5Refused as exc:
        out = {"refused": str(exc)}
    except _LIBRARY_ERRORS as exc:
        out = {"refused": f"not a readable representation: "
                          f"{type(exc).__name__}: {exc}"}
    else:
        out = {"record": rec, "digest": bundle.digest(),
               "artifacts": {name: base64.b64encode(raw).decode("ascii")
                             for name, raw in artifacts.items()},
               "links": list(links)}
    sys.stdout.write(json.dumps(out, sort_keys=True))
    return 0


def _read(path: Path | str) -> tuple:
    """What the file holds, read with the library and checked against
    itself: ``(record, bundle, artifacts, links)``. Run in the child."""
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
    return rec, bundle, artifacts, links


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
