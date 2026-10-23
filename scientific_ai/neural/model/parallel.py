"""Distributed execution: plans, expert parallelism, and what they prove.

WHAT THIS IS

Software that a later hardware tranche can run unchanged: an execution
profile (generic, no provider, no host, no credential), a parallel plan over
a named device mesh (data, expert, tensor, pipeline) whose divisibility is
checked, the ownership of every expert, a sharding rule for every parameter
category, a deterministic per-rank partition of the training data, and an
EXPERT-PARALLEL mixture-of-experts layer (:func:`expert_parallel_moe`) in
which each device owns ``num_experts / ep`` experts and tokens travel to
them and back with two ``all_to_all`` collectives.

WHAT IT PROVES, AND WHAT IT DOES NOT

``tools/neural.py distributed`` runs it on SIMULATED devices -- eight CPU
devices of one host process (``--xla_force_host_platform_device_count``) --
and compares each distributed computation with its single-device original:
the routed outputs, a sharded training step, activation checkpointing,
gradient accumulation, a sharded checkpoint. Agreement there shows the
dispatch, gather and sharding LOGIC is right. It shows nothing about speed,
memory, interconnect, failures or scale on real hardware: that is
DISTRIBUTED_HARDWARE_VALIDATED, which needs a hardware execution and is not
claimed.

Capacity under expert parallelism is per SOURCE device (each device's
tokens get ``capacity`` slots per expert), the usual grouping; with
``capacity_factor=None`` nothing drops in either form and the outputs agree.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..config import ModelConfig
from . import network
from ._jax import require

PROFILE_KINDS = ("CPU_DEVELOPMENT", "SIMULATED_MULTI_DEVICE",
                 "SINGLE_ACCELERATOR", "MULTI_GPU_NODE", "B300_CLASS_NODE",
                 "GB300_CLASS_RACK", "DISTRIBUTED_CLUSTER")
MESH_AXES = ("data", "expert", "tensor", "pipeline")


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class ExecutionProfile:
    """Where a run executes, generically. Memory and interconnect are
    recorded from the provisioned system when there is one; a profile of
    hardware this repository has never run on carries None."""

    kind: str
    devices: int
    devices_per_node: int
    memory_per_device_bytes: int | None = None
    interconnect: str | None = None
    hardware_executed: bool = False

    def validate(self) -> "ExecutionProfile":
        if self.kind not in PROFILE_KINDS:
            raise PlanError(f"profile kind {self.kind!r}")
        for name in ("devices", "devices_per_node"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise PlanError(f"{name} must be a positive int")
        if self.devices % self.devices_per_node:
            raise PlanError("devices must be whole nodes")
        if self.kind in ("CPU_DEVELOPMENT", "SIMULATED_MULTI_DEVICE") and \
                self.hardware_executed:
            raise PlanError("a simulated profile is not a hardware "
                            "execution")
        return self

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class ParallelPlan:
    mesh: tuple          # ((axis, size), ...) over MESH_AXES

    @property
    def sizes(self) -> dict:
        d = dict.fromkeys(MESH_AXES, 1)
        d.update(dict(self.mesh))
        return d

    def validate(self, cfg: ModelConfig, profile: ExecutionProfile,
                 global_batch: int) -> dict:
        s = self.sizes
        if set(dict(self.mesh)) - set(MESH_AXES):
            raise PlanError(f"unknown mesh axes in {self.mesh}")
        if math.prod(s.values()) != profile.devices:
            raise PlanError(f"mesh {s} uses {math.prod(s.values())} devices,"
                            f" the profile has {profile.devices}")
        if global_batch % (s["data"] * s["expert"]):
            raise PlanError("the global batch must split evenly over the "
                            "data and expert axes")
        if cfg.moe is not None and cfg.moe.num_experts % s["expert"]:
            raise PlanError(f"{cfg.moe.num_experts} experts do not shard "
                            f"evenly over {s['expert']} devices")
        if s["tensor"] > 1:
            a = cfg.attention
            if a.num_heads % s["tensor"] or a.num_kv_heads % s["tensor"]:
                raise PlanError("attention heads must split over the "
                                "tensor axis")
            if cfg.moe is not None and cfg.moe.expert_hidden % s["tensor"]:
                raise PlanError("expert width must split over the tensor "
                                "axis")
        if cfg.num_layers % s["pipeline"]:
            raise PlanError("layers must split evenly into pipeline stages")
        owners = {}
        if cfg.moe is not None:
            per = cfg.moe.num_experts // s["expert"]
            owners = {str(e): e // per for e in range(cfg.moe.num_experts)}
        return {"mesh": s, "devices": profile.devices,
                "experts_per_expert_rank":
                    (cfg.moe.num_experts // s["expert"]) if cfg.moe else 0,
                "expert_owner": owners,
                "sharding": SHARDING_RULES,
                "layers_per_stage": cfg.num_layers // s["pipeline"]}


#: category -> how its tensors are sharded under a plan.
SHARDING_RULES = {
    "expert": "expert axis on the leading (expert) dimension; within an "
              "expert, the hidden dimension over the tensor axis",
    "shared_expert": "replicated over expert; hidden over tensor",
    "router": "replicated: every token is scored against every expert",
    "attention": "heads over the tensor axis; FSDP over data",
    "dense_ffn": "hidden over tensor; FSDP over data",
    "normalization": "replicated",
    "feature_embedding": "FSDP over data",
    "readout_embedding": "replicated",
    "context_embedding": "replicated",
    "dimensional_encoder": "replicated",
    "numerical_encoder": "replicated",
    "output_head": "replicated",
    "uncertainty_head": "replicated",
    "reconstruction_head": "replicated",
    "optimizer_state": "sharded exactly as its parameter (ZeRO-3 style)",
}


def rank_indices(n: int, rank: int, world: int, epoch_order) -> np.ndarray:
    """This data-parallel rank's share of one epoch's order: disjoint
    across ranks, together covering every whole batch, identical on
    every host that computes it."""
    if not 0 <= rank < world:
        raise PlanError(f"rank {rank} of {world}")
    usable = (n // world) * world
    return np.asarray(epoch_order[:usable]).reshape(-1, world)[:, rank]


def expert_parallel_moe(p, h, token_mask, cfg: ModelConfig, mesh, *,
                        axis: str = "expert"):
    """The MoE layer with experts sharded over ``axis`` of ``mesh``.

    ``h`` [T, d] and ``token_mask`` [T] are sharded over ``axis`` too
    (each device routes its own tokens); the router is replicated; expert
    weights [E, ...] are sharded on E. Returns ``y`` [T, d], sharded."""
    jax, jnp = require()
    P = jax.sharding.PartitionSpec
    ep = mesh.shape[axis]
    m = cfg.moe_block
    e = m.num_experts
    if e % ep:
        raise PlanError(f"{e} experts over {ep} devices")

    def local(router, gate, up, down, h_loc, mask_loc):
        t, d = h_loc.shape
        r = network.route(router, h_loc, mask_loc, cfg)
        k = r["top_k"]
        cap = network.capacity(cfg, t)
        slot, kept, _ = network.dispatch_positions(r["selected"], r["live"],
                                                   e, cap)
        flat_e = r["selected"].reshape(-1)
        flat_s = jnp.where(kept, slot, cap).reshape(-1)
        buf = jnp.zeros((e, cap, d), h_loc.dtype).at[flat_e, flat_s].add(
            jnp.repeat(h_loc, k, axis=0), mode="drop")
        # send expert block j to device j; receive every device's block
        # for the experts this device owns
        recv = jax.lax.all_to_all(buf, axis, 0, 0, tiled=True)
        recv = recv.reshape(ep, e // ep, cap, d).transpose(1, 0, 2, 3) \
            .reshape(e // ep, ep * cap, d)
        hid = jax.nn.silu(jnp.einsum("ecd,edf->ecf", recv, gate)) \
            * jnp.einsum("ecd,edf->ecf", recv, up)
        out = jnp.einsum("ecf,efd->ecd", hid, down)
        out = out.reshape(e // ep, ep, cap, d).transpose(1, 0, 2, 3) \
            .reshape(e, cap, d)
        back = jax.lax.all_to_all(out, axis, 0, 0, tiled=True)
        gathered = back.at[flat_e, flat_s].get(mode="fill", fill_value=0)
        w = (r["gates"] * kept.astype(jnp.float32)).reshape(-1, 1) \
            .astype(h_loc.dtype)
        y = jnp.sum((gathered * w).reshape(t, k, d), axis=1)
        return y * mask_loc.astype(h_loc.dtype)[:, None]

    ex = p["experts"]
    fn = jax.shard_map(
        local, mesh=mesh,
        in_specs=(P(), P(axis), P(axis), P(axis), P(axis), P(axis)),
        out_specs=P(axis))
    return fn(p["router"], ex["gate"], ex["up"], ex["down"], h, token_mask)
