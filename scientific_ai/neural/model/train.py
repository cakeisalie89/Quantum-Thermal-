"""Training: one loss, one optimizer, a data order that can be resumed.

THE OBJECTIVE (each term has one stated purpose)

* Gaussian negative log-likelihood of every target in its standardised
  transform space, ``0.5 (log 2 pi + log var + (y - mean)^2 / var)``,
  averaged over targets and samples: it fits the mean AND a per-sample
  variance, which is the model's aleatoric uncertainty;
* ``aux_loss_coef`` times the sum over MoE layers of the Switch
  load-balancing term ``E * sum_i f_i P_i``: its one purpose is to keep the
  router from collapsing onto a few experts.

Nothing else is added.

THE OPTIMIZER

Adam (beta1 0.9, beta2 0.999, eps 1e-8) with bias correction, written out
here so its arithmetic is in the repository; the global gradient norm is
clipped to ``clip_norm`` so one extreme batch cannot take one extreme step;
the learning rate warms up linearly, then decays by a cosine to
``min_lr_ratio`` of its peak. Master weights and moments are float32.

THE DATA ORDER

Epoch ``e`` visits the training split in the permutation drawn from
``PCG64(data_order_seed + e)``; a step is ``(epoch, batch_in_epoch)``. A
checkpoint records that position, the optimizer state and its step, so an
EXACT_RESUME continues the same sequence of batches and updates; on the
same backend it reproduces an uninterrupted run bit for bit, which
``tests/test_neural_training.py`` checks.

FAIL CLOSED

A step whose loss is not finite, or whose router produced a non-finite
logit, stops the run (``TrainingAborted``) with the step named; it is never
skipped and the run is never reported COMPLETED.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..config import ModelConfig
from . import network
from ._jax import require

LOG_2PI = math.log(2 * math.pi)


class TrainingAborted(RuntimeError):
    pass


@dataclass(frozen=True)
class TrainConfig:
    steps: int = 4000
    batch_size: int = 64
    learning_rate: float = 3e-3
    warmup_steps: int = 200
    min_lr_ratio: float = 0.1
    beta1: float = 0.9
    beta2: float = 0.999
    eps: float = 1e-8
    clip_norm: float = 1.0
    gradient_accumulation: int = 1
    init_seed: int = 0
    data_order_seed: int = 1
    checkpoint_interval: int = 1000
    log_interval: int = 50

    def validate(self) -> "TrainConfig":
        for name in ("steps", "batch_size", "gradient_accumulation",
                     "checkpoint_interval", "log_interval"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise ValueError(f"{name} must be a positive int: {v!r}")
        if self.batch_size % self.gradient_accumulation:
            raise ValueError("gradient_accumulation must divide batch_size")
        if not 0 <= self.warmup_steps < self.steps:
            raise ValueError("warmup_steps must lie in [0, steps)")
        for name in ("learning_rate", "eps", "clip_norm"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be > 0")
        if not (0 < self.beta1 < 1 and 0 < self.beta2 < 1
                and 0 <= self.min_lr_ratio <= 1):
            raise ValueError("beta1, beta2 in (0, 1); min_lr_ratio in "
                             "[0, 1]")
        return self

    def optimizer_dict(self) -> dict:
        return {"name": "adam", "beta1": self.beta1, "beta2": self.beta2,
                "eps": self.eps, "clip_global_norm": self.clip_norm,
                "weight_decay": 0.0, "master_dtype": "float32"}

    def scheduler_dict(self) -> dict:
        return {"name": "linear_warmup_cosine",
                "peak_learning_rate": self.learning_rate,
                "warmup_steps": self.warmup_steps,
                "min_lr_ratio": self.min_lr_ratio,
                "total_steps": self.steps}


def learning_rate(tc: TrainConfig, step):
    _, jnp = require()
    s = jnp.asarray(step, jnp.float32)
    warm = tc.learning_rate * (s + 1) / max(tc.warmup_steps, 1)
    frac = (s - tc.warmup_steps) / max(tc.steps - tc.warmup_steps, 1)
    cos = tc.min_lr_ratio + (1 - tc.min_lr_ratio) * 0.5 * (
        1 + jnp.cos(jnp.pi * jnp.clip(frac, 0.0, 1.0)))
    return jnp.where(s < tc.warmup_steps, warm, tc.learning_rate * cos)


def loss_terms(params, buffers, batch, y, cfg: ModelConfig):
    """``(loss, (nll, aux, outputs))`` for one batch."""
    _, jnp = require()
    out = network.apply(params, buffers, batch, cfg)
    mean, var = out["mean"], out["var"]
    nll = 0.5 * jnp.mean(LOG_2PI + jnp.log(var) + (y - mean) ** 2 / var)
    aux = jnp.zeros((), jnp.float32)
    for st in out["router"]:
        aux = aux + st["aux_loss"]
    coef = cfg.moe.aux_loss_coef if cfg.moe is not None else 0.0
    return nll + coef * aux, (nll, aux, out)


def init_optimizer(params) -> dict:
    jax, jnp = require()
    z = jax.tree_util.tree_map(lambda p: jnp.zeros(p.shape, jnp.float32),
                               params)
    return {"m": z, "v": jax.tree_util.tree_map(jnp.zeros_like, z),
            "step": 0}


def _router_summary(out) -> dict:
    _, jnp = require()
    r = out["router"]
    if not r:
        return {}
    return {"finite": jnp.all(jnp.stack([s["finite"] for s in r])),
            "dropped": jnp.stack([s["dropped"] for s in r]),
            "entropy": jnp.stack([s["entropy"] for s in r]),
            "max_load_fraction": jnp.stack([s["max_load_fraction"]
                                            for s in r]),
            "expert_counts": jnp.stack([s["expert_counts"] for s in r]),
            "aux": jnp.stack([s["aux_loss"] for s in r])}


def accumulated_gradient(cfg: ModelConfig, params, buffers, batch, y,
                         n_micro: int):
    """``(loss, nll, aux, router_summary, grads)`` of one batch split into
    ``n_micro`` equal micro-batches whose gradients are averaged -- the code
    every training step runs.

    For the likelihood term this equals the full-batch gradient: it is a
    mean over samples. The load-balancing term is NOT a mean over samples
    (``E * sum f_i P_i`` multiplies two batch statistics), so with
    ``aux_loss_coef > 0`` accumulation changes it by design; the
    distributed report measures how much."""
    jax, jnp = require()
    grad_fn = jax.value_and_grad(
        lambda p, b, x, t: loss_terms(p, b, x, t, cfg), has_aux=True)
    if n_micro == 1:
        (loss, (nll, aux, out)), g = grad_fn(params, buffers, batch, y)
        g = jax.tree_util.tree_map(lambda x: x.astype(jnp.float32), g)
        return loss, nll, aux, _router_summary(out), g

    def split(a):
        return a.reshape((n_micro, a.shape[0] // n_micro) + a.shape[1:])

    mb = jax.tree_util.tree_map(split, batch)
    my = split(y)
    g = jax.tree_util.tree_map(lambda p: jnp.zeros(p.shape, jnp.float32),
                               params)
    loss = nll = aux = jnp.zeros((), jnp.float32)
    rs = None
    for i in range(n_micro):
        bi = jax.tree_util.tree_map(lambda a: a[i], mb)
        (li, (ni, ai, oi)), gi = grad_fn(params, buffers, bi, my[i])
        g = jax.tree_util.tree_map(
            lambda acc, x: acc + x.astype(jnp.float32) / n_micro, g, gi)
        loss, nll, aux = (loss + li / n_micro, nll + ni / n_micro,
                          aux + ai / n_micro)
        rs = _router_summary(oi) if rs is None else rs
    return loss, nll, aux, rs, g


def make_step(cfg: ModelConfig, tc: TrainConfig):
    """A jitted ``(params, opt, buffers, batch, y) -> (params, opt,
    metrics)``; with gradient accumulation the batch is split into equal
    micro-batches whose gradients are averaged."""
    jax, jnp = require()
    n_micro = tc.gradient_accumulation

    def step(params, opt, buffers, batch, y):
        loss, nll, aux, rs, g = accumulated_gradient(cfg, params, buffers,
                                                     batch, y, n_micro)
        gnorm = jnp.sqrt(sum(jnp.sum(x * x)
                             for x in jax.tree_util.tree_leaves(g)))
        scale = jnp.minimum(1.0, tc.clip_norm / jnp.maximum(gnorm, 1e-12))
        g = jax.tree_util.tree_map(lambda x: x * scale, g)
        t = opt["step"] + 1
        lr = learning_rate(tc, opt["step"])
        m = jax.tree_util.tree_map(
            lambda m_, g_: tc.beta1 * m_ + (1 - tc.beta1) * g_, opt["m"], g)
        v = jax.tree_util.tree_map(
            lambda v_, g_: tc.beta2 * v_ + (1 - tc.beta2) * g_ * g_,
            opt["v"], g)
        bc1 = 1 - tc.beta1 ** t
        bc2 = 1 - tc.beta2 ** t

        def upd(p, m_, v_):
            new = p.astype(jnp.float32) - lr * (m_ / bc1) / (
                jnp.sqrt(v_ / bc2) + tc.eps)
            return new.astype(p.dtype)

        params = jax.tree_util.tree_map(upd, params, m, v)
        metrics = {"loss": loss, "nll": nll, "aux": aux, "grad_norm": gnorm,
                   "lr": lr, "router": rs}
        return params, {"m": m, "v": v, "step": t}, metrics

    return jax.jit(step)


def batch_order(n_train: int, tc: TrainConfig, epoch: int) -> np.ndarray:
    return np.random.Generator(np.random.PCG64(
        tc.data_order_seed + epoch)).permutation(n_train)


def position(step: int, n_train: int, tc: TrainConfig) -> dict:
    per_epoch = n_train // tc.batch_size
    if per_epoch < 1:
        raise ValueError("the training split is smaller than one batch")
    return {"step": step, "epoch": step // per_epoch,
            "batch_in_epoch": step % per_epoch,
            "batches_per_epoch": per_epoch}


def batch_for(step: int, tokens: dict, y: np.ndarray, tc: TrainConfig):
    n = y.shape[0]
    pos = position(step, n, tc)
    order = batch_order(n, tc, pos["epoch"])
    idx = order[pos["batch_in_epoch"] * tc.batch_size:
                (pos["batch_in_epoch"] + 1) * tc.batch_size]
    return {k: v[idx] for k, v in tokens.items()}, y[idx]


def run(cfg: ModelConfig, tc: TrainConfig, tokens: dict, y: np.ndarray, *,
        params, buffers, opt=None, start_step: int = 0, stop_step=None,
        on_checkpoint=None) -> dict:
    """Train from ``start_step`` to ``stop_step`` (default ``tc.steps``).

    ``on_checkpoint(step, params, opt)`` is called at every checkpoint
    interval and at the end. Returns the final state and the history."""
    jax, jnp = require()
    tc.validate()
    stop = tc.steps if stop_step is None else stop_step
    if opt is None:
        if start_step != 0:
            raise ValueError("a resumed run needs its optimizer state")
        opt = init_optimizer(params)
    if int(opt["step"]) != start_step:
        raise ValueError(f"optimizer step {opt['step']} is not the start "
                         f"step {start_step}")
    step_fn = make_step(cfg, tc)
    history, router_log = [], []
    for step in range(start_step, stop):
        bx, by = batch_for(step, tokens, y, tc)
        bx = {k: jnp.asarray(v) for k, v in bx.items()}
        params, opt, m = step_fn(params, opt, buffers, bx,
                                 jnp.asarray(by, jnp.float32))
        loss = float(m["loss"])
        rf = bool(m["router"]["finite"]) if m["router"] else True
        if not math.isfinite(loss) or not rf:
            raise TrainingAborted(
                f"step {step}: loss {loss}, router finite {rf}")
        if step % tc.log_interval == 0 or step == stop - 1:
            history.append({"step": step, "loss": loss,
                            "nll": float(m["nll"]), "aux": float(m["aux"]),
                            "grad_norm": float(m["grad_norm"]),
                            "lr": float(m["lr"])})
            if m["router"]:
                router_log.append({
                    "step": step,
                    "dropped": [float(x) for x in m["router"]["dropped"]],
                    "entropy": [float(x) for x in m["router"]["entropy"]],
                    "max_load_fraction": [float(x) for x in
                                          m["router"]["max_load_fraction"]],
                    "expert_counts": [[float(c) for c in row] for row in
                                      m["router"]["expert_counts"]]})
        done = step + 1
        if on_checkpoint is not None and (
                done % tc.checkpoint_interval == 0 or done == stop):
            on_checkpoint(done, params, opt)
    return {"params": params, "opt": opt, "history": history,
            "router_log": router_log}
