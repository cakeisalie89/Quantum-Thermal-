"""Checkpoints that can be trusted to be data, and named by their bytes.

FORMAT

The safetensors container layout, written and read here with NumPy: an
8-byte little-endian header length, a JSON header mapping each tensor name
to ``{"dtype", "shape", "data_offsets"}`` plus ``"__metadata__"`` (strings
only), then the raw little-endian tensor bytes. Nothing is pickled, so
LOADING A CHECKPOINT EXECUTES NO CODE -- the property ``torch.load`` and
``pickle`` do not have, and the reason this format was chosen.

The reader is strict, because a checkpoint is input: the header length must
fit the file; the header must parse with no duplicate key; every entry must
be exactly ``dtype``/``shape``/``data_offsets`` with a dtype from
:data:`DTYPES`; the offsets must tile the data region exactly -- contiguous
from 0, in order, no gap, no overlap, nothing after -- and each tensor's
byte length must equal its shape times its item size. The file's sha256
must equal the digest the manifest records BEFORE anything is parsed.

IDENTITY AND BINDING

A checkpoint's identity is ``sha256(file bytes)``. Its manifest
(``manifests.CHECKPOINT_MANIFEST``) binds that digest to the configuration
digest, the dataset digest, the training run and step, the parent
checkpoint, the data position and the seeds -- and :func:`load` refuses a
file whose tensors are not exactly the configuration's: a missing tensor,
an extra tensor, a different shape or dtype.

SHARDS

:func:`save_sharded` splits the tensors across ``n`` files by a stable
assignment (sorted names, round-robin by size class) and records each
shard's digest; the checkpoint digest is then the digest of the ordered
shard digests. :func:`load_sharded` verifies every shard first.
"""
from __future__ import annotations

import hashlib
import json
import struct
from typing import Any

import numpy as np

from scientific.identity import digest

from ..config import ModelConfig
from . import network
from ._jax import require

FORMAT = "safetensors-layout/1 (numpy writer; no pickle)"
DTYPES = {"F32": "<f4", "F64": "<f8", "I32": "<i4", "I64": "<i8",
          "U32": "<u4", "BF16": "bfloat16", "BOOL": "?"}
MAX_HEADER = 64 * 2 ** 20


class CheckpointError(ValueError):
    pass


def _np_dtype(code: str):
    if code == "BF16":
        import ml_dtypes   # a JAX dependency; part of the neural extra
        return np.dtype(ml_dtypes.bfloat16)
    return np.dtype(DTYPES[code])


def _code(arr: np.ndarray) -> str:
    name = arr.dtype.name
    table = {"float32": "F32", "float64": "F64", "int32": "I32",
             "int64": "I64", "uint32": "U32", "bfloat16": "BF16",
             "bool": "BOOL"}
    if name not in table:
        raise CheckpointError(f"dtype {name} cannot be checkpointed")
    return table[name]


def encode(tensors: dict, metadata: dict) -> bytes:
    """The container bytes for ``tensors`` (name -> array)."""
    for k, v in metadata.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise CheckpointError("metadata is str -> str")
    header: dict = {"__metadata__": dict(sorted(metadata.items()))}
    chunks, off = [], 0
    for name in sorted(tensors):
        # np.array(order="C") and not ascontiguousarray: the latter turns
        # a 0-d array into shape (1,), and a checkpoint reshapes nothing
        a = np.array(tensors[name], copy=True, order="C")
        code = _code(a)
        if code != "BF16" and a.dtype.byteorder == ">":
            a = a.astype(a.dtype.newbyteorder("<"))
        raw = a.tobytes()
        header[name] = {"dtype": code, "shape": list(a.shape),
                        "data_offsets": [off, off + len(raw)]}
        chunks.append(raw)
        off += len(raw)
    hb = json.dumps(header, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=True).encode()
    hb += b" " * (-len(hb) % 8)
    return struct.pack("<Q", len(hb)) + hb + b"".join(chunks)


def decode(raw: bytes) -> tuple:
    """``(tensors, metadata)`` from container bytes, strictly."""
    if len(raw) < 8:
        raise CheckpointError("shorter than its header length")
    (n,) = struct.unpack("<Q", raw[:8])
    if n > MAX_HEADER or 8 + n > len(raw):
        raise CheckpointError(f"header length {n} does not fit the file")

    def no_dupes(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise CheckpointError("duplicate key in the header")
        return dict(pairs)

    try:
        header = json.loads(raw[8:8 + n].decode("utf-8"),
                            object_pairs_hook=no_dupes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckpointError(f"header is not JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise CheckpointError("header is not an object")
    meta = header.pop("__metadata__", {})
    if not isinstance(meta, dict) or not all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in meta.items()):
        raise CheckpointError("metadata must be str -> str")
    data = raw[8 + n:]
    spans = []
    out = {}
    for name, ent in header.items():
        if not isinstance(ent, dict) or set(ent) != {"dtype", "shape",
                                                     "data_offsets"}:
            raise CheckpointError(f"{name}: malformed entry")
        if ent["dtype"] not in DTYPES:
            raise CheckpointError(f"{name}: dtype {ent['dtype']!r}")
        shape, offs = ent["shape"], ent["data_offsets"]
        if not isinstance(shape, list) or not all(
                isinstance(s, int) and not isinstance(s, bool) and s >= 0
                for s in shape):
            raise CheckpointError(f"{name}: shape {shape!r}")
        if not (isinstance(offs, list) and len(offs) == 2 and all(
                isinstance(o, int) and not isinstance(o, bool)
                for o in offs) and 0 <= offs[0] <= offs[1]):
            raise CheckpointError(f"{name}: data_offsets {offs!r}")
        dt = _np_dtype(ent["dtype"])
        count = 1
        for s in shape:
            count *= s
        if offs[1] - offs[0] != count * dt.itemsize:
            raise CheckpointError(f"{name}: {offs[1] - offs[0]} bytes for "
                                  f"shape {shape} of {ent['dtype']}")
        spans.append((offs[0], offs[1], name))
        out[name] = (dt, shape, offs)
    pos = 0
    for b, e, name in sorted(spans):
        if b != pos:
            raise CheckpointError(f"{name}: data does not tile the buffer "
                                  f"(gap or overlap at {pos})")
        pos = e
    if pos != len(data):
        raise CheckpointError(f"{len(data) - pos} bytes after the last "
                              "tensor")
    tensors = {name: np.frombuffer(data[o[0]:o[1]], dtype=dt).reshape(s)
               .copy() for name, (dt, s, o) in out.items()}
    return tensors, meta


# -- trees <-> named tensors ---------------------------------------------

def flatten(prefix: str, tree) -> dict:
    out = {}
    for path, leaf in network.leaves_with_paths(tree):
        out["/".join([prefix] + [str(p) for p in path])] = np.asarray(leaf)
    return out


def _expected(cfg: ModelConfig) -> dict:
    jax, _ = require()
    from .meta import abstract_init
    p, b = abstract_init(cfg)
    out = {}
    for prefix, tree in (("params", p), ("buffers", b)):
        for path, leaf in network.leaves_with_paths(tree):
            out["/".join([prefix] + [str(x) for x in path])] = leaf
    return out


def _unflatten(cfg: ModelConfig, tensors: dict, prefix: str, abstract):
    jax, jnp = require()
    leaves = []
    paths = network.leaves_with_paths(abstract)
    for path, leaf in paths:
        name = "/".join([prefix] + [str(x) for x in path])
        a = tensors[name]
        leaves.append(jnp.asarray(a, dtype=leaf.dtype))
    treedef = jax.tree_util.tree_structure(abstract)
    return jax.tree_util.tree_unflatten(treedef, leaves)


def save(cfg: ModelConfig, params, buffers, *, opt_state=None,
         metadata: dict | None = None) -> bytes:
    tensors = {**flatten("params", params), **flatten("buffers", buffers)}
    if opt_state is not None:
        tensors.update(flatten("opt/m", opt_state["m"]))
        tensors.update(flatten("opt/v", opt_state["v"]))
        tensors["opt/step"] = np.asarray(opt_state["step"], dtype=np.int64)
    meta = {"config_digest": cfg.digest(), "format": FORMAT}
    meta.update(metadata or {})
    return encode(tensors, meta)


def tensor_digest(params, buffers, opt_state=None) -> str:
    """sha256 of the tensors alone (no metadata): two states are the same
    state exactly when this agrees, whatever run or step wrote them."""
    tensors = {**flatten("params", params), **flatten("buffers", buffers)}
    if opt_state is not None:
        tensors.update(flatten("opt/m", opt_state["m"]))
        tensors.update(flatten("opt/v", opt_state["v"]))
        tensors["opt/step"] = np.asarray(opt_state["step"], dtype=np.int64)
    return hashlib.sha256(encode(tensors, {})).hexdigest()


def tensor_index(raw: bytes) -> list:
    tensors, _ = decode(raw)
    return [[n, _code(a), list(a.shape)] for n, a in sorted(tensors.items())]


def load(cfg: ModelConfig, raw: bytes, *, expected_digest: str,
         with_optimizer: bool = False):
    """``(params, buffers, opt_state or None, metadata)``, verified."""
    got = hashlib.sha256(raw).hexdigest()
    if got != expected_digest:
        raise CheckpointError(f"checkpoint bytes hash to {got}, the "
                              f"manifest records {expected_digest}")
    tensors, meta = decode(raw)
    if meta.get("config_digest") != cfg.digest():
        raise CheckpointError("the checkpoint was written for another "
                              "configuration")
    want = _expected(cfg)
    have = {k for k in tensors if not k.startswith("opt/")}
    if have != set(want):
        raise CheckpointError(f"tensors differ from the configuration's: "
                              f"missing {sorted(set(want) - have)[:5]}, "
                              f"extra {sorted(have - set(want))[:5]}")
    for name, leaf in want.items():
        a = tensors[name]
        if tuple(a.shape) != tuple(leaf.shape) or \
                a.dtype.name != np.dtype(leaf.dtype).name:
            raise CheckpointError(f"{name}: {a.dtype}{list(a.shape)} is not "
                                  f"{leaf.dtype}{list(leaf.shape)}")
    from .meta import abstract_init
    p_abs, b_abs = abstract_init(cfg)
    params = _unflatten(cfg, tensors, "params", p_abs)
    buffers = _unflatten(cfg, tensors, "buffers", b_abs)
    opt = None
    if with_optimizer:
        if "opt/step" not in tensors:
            raise CheckpointError("no optimizer state in this checkpoint")
        f32 = _as_f32(p_abs)
        opt = {"m": _unflatten(cfg, tensors, "opt/m", f32),
               "v": _unflatten(cfg, tensors, "opt/v", f32),
               "step": int(tensors["opt/step"])}
    return params, buffers, opt, meta


def _as_f32(tree):
    jax, jnp = require()
    return jax.tree_util.tree_map(
        lambda x: jax.ShapeDtypeStruct(x.shape, jnp.float32), tree)


def save_sharded(cfg: ModelConfig, params, buffers, n: int, *,
                 metadata: dict | None = None) -> tuple:
    """``(shards, checkpoint_digest)``: ``n`` container files and the digest
    of their ordered digests."""
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise CheckpointError(f"shard count {n!r}")
    tensors = {**flatten("params", params), **flatten("buffers", buffers)}
    names = sorted(tensors, key=lambda k: (-tensors[k].size, k))
    groups: list[dict[str, Any]] = [dict() for _ in range(n)]
    for i, name in enumerate(names):
        groups[i % n][name] = tensors[name]
    meta = {"config_digest": cfg.digest(), "format": FORMAT,
            "shard_count": str(n)}
    meta.update(metadata or {})
    shards = [encode(g, {**meta, "shard_index": str(i)})
              for i, g in enumerate(groups)]
    digests = [hashlib.sha256(s).hexdigest() for s in shards]
    return shards, digest(digests)


def load_sharded(cfg: ModelConfig, shards: list, *, shard_digests: list,
                 expected_digest: str):
    if digest(shard_digests) != expected_digest:
        raise CheckpointError("the shard list does not hash to the "
                              "checkpoint digest")
    if len(shards) != len(shard_digests):
        raise CheckpointError("shard count differs from the manifest")
    merged: dict[str, Any] = {}
    for raw, want in zip(shards, shard_digests):
        if hashlib.sha256(raw).hexdigest() != want:
            raise CheckpointError("a shard's bytes are not the ones "
                                  "recorded")
        t, meta = decode(raw)
        if meta.get("config_digest") != cfg.digest():
            raise CheckpointError("a shard belongs to another configuration")
        if set(t) & set(merged):
            raise CheckpointError("two shards hold the same tensor")
        merged.update(t)
    whole = encode(merged, {"config_digest": cfg.digest(),
                            "format": FORMAT})
    return load(cfg, whole, expected_digest=hashlib.sha256(whole)
                .hexdigest())[:2]
