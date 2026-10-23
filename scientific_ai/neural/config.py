"""One member of the scientific feature-token MoE family, validated.

A configuration is data: it builds nothing and allocates nothing. Its digest
(``scientific.identity.digest`` of :meth:`ModelConfig.to_dict`) is the name
every manifest, training run and checkpoint binds to, so "the 1T model" is
never a filename -- it is a digest, and a claim about it must cite one.

THE STRUCTURE A CONFIGURATION DESCRIBES

    feature tokens (one per schema feature, canonical order) + readout tokens
      -> num_layers pre-norm blocks:
             x += attention(rmsnorm(x))          every layer
             x += ffn(rmsnorm(x))                dense SwiGLU or MoE
      -> rmsnorm -> mean of the readout tokens -> heads

A layer is a mixture-of-experts layer when ``moe`` is set, its index is at
least ``moe_layer_start`` and ``(index - moe_layer_start)`` is a multiple of
``moe_layer_interval``; every other layer is a dense SwiGLU of width
``dense_ffn_hidden``.

WHAT IS REFUSED, AND WHY

Every dimension must be a positive int (a bool is not one). The attention
inner width ``num_heads * head_dim`` must equal ``hidden_size``: the output
projection maps it back onto the residual stream, and an implicit mismatch
is how a "valid" config hides a dimension nobody chose. ``num_kv_heads``
must divide ``num_heads`` (grouped-query attention shares each key/value
head across a whole group). ``top_k`` lies in ``[1, num_experts]``. A dense
width with no dense layer, or a shared-expert width with no shared expert,
is refused rather than ignored: a parameter nobody uses is a parameter
somebody will count. The router computes in float32 always -- a routing
decision taken in reduced precision can send a token to a different expert,
which is a different computation, not a rounding of the same one -- and FP8
is an ESTIMATION mode only (``estimates``): nothing here has been validated
against a higher-precision reference in FP8, so no model is built in it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from scientific.identity import digest

SCHEMA_VERSION = "scientific-moe-config/1"
FAMILY = "scientific-feature-token-moe"

#: Storage / compute dtypes a model may be BUILT in.
MODEL_DTYPES = ("float32", "bfloat16")
#: Head kinds. ``gaussian``: a mean and a variance for an unbounded scalar in
#: its transformed space. ``bounded_gaussian``: the mean is mapped into
#: ``[lower, upper]`` by a sigmoid -- a parametrisation that cannot leave the
#: interval, not a clip applied afterwards.
HEAD_KINDS = ("gaussian", "bounded_gaussian")
#: The auxiliary masked-feature reconstruction head: absent, its per-feature
#: decoder vectors TIED to the feature-identity embedding, or its own matrix.
RECONSTRUCTION_MODES = ("none", "tied", "untied")


class ConfigError(ValueError):
    """A configuration that does not describe a buildable member."""


def _pos_int(name: str, v) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise ConfigError(f"{name} must be a positive int, got {v!r}")
    return v


def _nonneg_int(name: str, v) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ConfigError(f"{name} must be an int >= 0, got {v!r}")
    return v


def _finite(name: str, v, *, positive: bool = False,
            nonneg: bool = False) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) \
            or not math.isfinite(v):
        raise ConfigError(f"{name} must be a finite number, got {v!r}")
    if positive and not v > 0:
        raise ConfigError(f"{name} must be > 0, got {v!r}")
    if nonneg and v < 0:
        raise ConfigError(f"{name} must be >= 0, got {v!r}")
    return float(v)


@dataclass(frozen=True)
class PrecisionProfile:
    param_dtype: str = "float32"
    compute_dtype: str = "float32"
    #: Routing is float32 by policy (see the module docstring).
    router_dtype: str = "float32"

    def validate(self) -> None:
        for name in ("param_dtype", "compute_dtype"):
            v = getattr(self, name)
            if v == "float8_e4m3":
                raise ConfigError(
                    f"{name}: FP8 is an estimation mode here, not a build "
                    "dtype -- no FP8 model has been validated against a "
                    "higher-precision reference")
            if v not in MODEL_DTYPES:
                raise ConfigError(f"{name}: {v!r} is not one of "
                                  f"{MODEL_DTYPES}")
        if self.router_dtype != "float32":
            raise ConfigError("router_dtype must be float32: a routing "
                              "decision in reduced precision is a different "
                              "computation")

    def to_dict(self) -> dict:
        return {"param_dtype": self.param_dtype,
                "compute_dtype": self.compute_dtype,
                "router_dtype": self.router_dtype}


@dataclass(frozen=True)
class AttentionConfig:
    num_heads: int
    head_dim: int
    #: ``num_heads`` for standard multi-head attention; fewer for
    #: grouped-query attention.
    num_kv_heads: int

    def validate(self, hidden_size: int) -> None:
        _pos_int("attention.num_heads", self.num_heads)
        _pos_int("attention.head_dim", self.head_dim)
        _pos_int("attention.num_kv_heads", self.num_kv_heads)
        if self.num_heads * self.head_dim != hidden_size:
            raise ConfigError(
                f"num_heads * head_dim = {self.num_heads * self.head_dim} "
                f"!= hidden_size {hidden_size}")
        if self.num_kv_heads > self.num_heads \
                or self.num_heads % self.num_kv_heads:
            raise ConfigError(
                f"num_kv_heads {self.num_kv_heads} must divide num_heads "
                f"{self.num_heads}")

    def to_dict(self) -> dict:
        return {"num_heads": self.num_heads, "head_dim": self.head_dim,
                "num_kv_heads": self.num_kv_heads}


@dataclass(frozen=True)
class MoEConfig:
    num_experts: int
    top_k: int
    expert_hidden: int
    num_shared_experts: int = 0
    shared_hidden: int = 0
    #: ``None``: every routed choice is computed (no token is dropped).
    #: Otherwise each expert takes at most
    #: ``ceil(capacity_factor * tokens * top_k / num_experts)`` choices and
    #: the rest are DROPPED -- counted, reported, never silent.
    capacity_factor: float | None = None
    router_temperature: float = 1.0
    #: Renormalise the selected experts' probabilities to sum to 1.
    normalize_top_k: bool = True
    #: Weight of the load-balancing loss E * sum_i f_i P_i (Switch form),
    #: whose one purpose is to keep the router from collapsing onto a few
    #: experts. 0 disables it.
    aux_loss_coef: float = 0.01
    #: Named inference-time routing profiles, ``((name, top_k), ...)``. When
    #: present, one is ``standard`` and equals ``top_k``.
    routing_profiles: tuple = ()

    def validate(self) -> None:
        e = _pos_int("moe.num_experts", self.num_experts)
        if e < 2:
            raise ConfigError("a mixture needs at least 2 experts")
        k = _pos_int("moe.top_k", self.top_k)
        if k > e:
            raise ConfigError(f"top_k {k} > num_experts {e}")
        _pos_int("moe.expert_hidden", self.expert_hidden)
        s = _nonneg_int("moe.num_shared_experts", self.num_shared_experts)
        sh = _nonneg_int("moe.shared_hidden", self.shared_hidden)
        if (s == 0) != (sh == 0):
            raise ConfigError("num_shared_experts and shared_hidden are "
                              "both zero or both positive")
        if self.capacity_factor is not None:
            _finite("moe.capacity_factor", self.capacity_factor,
                    positive=True)
        _finite("moe.router_temperature", self.router_temperature,
                positive=True)
        _finite("moe.aux_loss_coef", self.aux_loss_coef, nonneg=True)
        if not isinstance(self.normalize_top_k, bool):
            raise ConfigError("moe.normalize_top_k must be a bool")
        names = set()
        for item in self.routing_profiles:
            if not (isinstance(item, tuple) and len(item) == 2
                    and isinstance(item[0], str) and item[0]):
                raise ConfigError(f"routing profile {item!r} is not "
                                  "(name, top_k)")
            name, pk = item
            if name in names:
                raise ConfigError(f"routing profile {name!r} twice")
            names.add(name)
            _pos_int(f"routing profile {name}", pk)
            if pk > e:
                raise ConfigError(f"routing profile {name}: top_k {pk} > "
                                  f"num_experts {e}")
        if self.routing_profiles and \
                dict(self.routing_profiles).get("standard") != k:
            raise ConfigError("routing profiles must include 'standard' "
                              f"equal to top_k {k}")

    def to_dict(self) -> dict:
        return {"num_experts": self.num_experts, "top_k": self.top_k,
                "expert_hidden": self.expert_hidden,
                "num_shared_experts": self.num_shared_experts,
                "shared_hidden": self.shared_hidden,
                "capacity_factor": self.capacity_factor,
                "router_temperature": self.router_temperature,
                "normalize_top_k": self.normalize_top_k,
                "aux_loss_coef": self.aux_loss_coef,
                "routing_profiles": [list(p) for p in
                                     self.routing_profiles]}


@dataclass(frozen=True)
class HeadSpec:
    name: str
    kind: str = "gaussian"
    lower: float | None = None
    upper: float | None = None

    def validate(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ConfigError("a head needs a name")
        if self.kind not in HEAD_KINDS:
            raise ConfigError(f"head {self.name}: kind {self.kind!r} not in "
                              f"{HEAD_KINDS}")
        if self.kind == "bounded_gaussian":
            lo = _finite(f"head {self.name}.lower", self.lower)
            hi = _finite(f"head {self.name}.upper", self.upper)
            if not lo < hi:
                raise ConfigError(f"head {self.name}: lower {lo} must be < "
                                  f"upper {hi}")
        elif self.lower is not None or self.upper is not None:
            raise ConfigError(f"head {self.name}: bounds belong to a "
                              "bounded_gaussian head only")

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "lower": self.lower,
                "upper": self.upper}


@dataclass(frozen=True)
class ModelConfig:
    variant: str
    hidden_size: int
    num_layers: int
    attention: AttentionConfig
    feature_vocab_size: int
    value_encoder_hidden: int
    heads: tuple
    moe: MoEConfig | None = None
    dense_ffn_hidden: int = 0
    moe_layer_start: int = 0
    moe_layer_interval: int = 1
    context_vocab_size: int = 4
    num_readout_tokens: int = 1
    reconstruction_head: str = "none"
    precision: PrecisionProfile = field(default_factory=PrecisionProfile)

    # -- structure -------------------------------------------------------
    def is_moe_layer(self, index: int) -> bool:
        return (self.moe is not None and index >= self.moe_layer_start
                and (index - self.moe_layer_start)
                % self.moe_layer_interval == 0)

    @property
    def moe_block(self) -> MoEConfig:
        """The MoE block, for code that runs only where one exists: refused
        here, rather than met as ``None`` one attribute later."""
        if self.moe is None:
            raise ConfigError(f"{self.variant} has no moe block")
        return self.moe

    @property
    def moe_layer_indices(self) -> tuple:
        return tuple(i for i in range(self.num_layers)
                     if self.is_moe_layer(i))

    @property
    def dense_layer_indices(self) -> tuple:
        return tuple(i for i in range(self.num_layers)
                     if not self.is_moe_layer(i))

    # -- validation ------------------------------------------------------
    def validate(self) -> "ModelConfig":
        if not isinstance(self.variant, str) or not self.variant.strip():
            raise ConfigError("a configuration needs a variant name")
        d = _pos_int("hidden_size", self.hidden_size)
        _pos_int("num_layers", self.num_layers)
        if not isinstance(self.attention, AttentionConfig):
            raise ConfigError("attention must be an AttentionConfig")
        self.attention.validate(d)
        _pos_int("feature_vocab_size", self.feature_vocab_size)
        _pos_int("value_encoder_hidden", self.value_encoder_hidden)
        _pos_int("context_vocab_size", self.context_vocab_size)
        _pos_int("num_readout_tokens", self.num_readout_tokens)
        _nonneg_int("dense_ffn_hidden", self.dense_ffn_hidden)
        _nonneg_int("moe_layer_start", self.moe_layer_start)
        _pos_int("moe_layer_interval", self.moe_layer_interval)
        if self.moe is not None:
            if not isinstance(self.moe, MoEConfig):
                raise ConfigError("moe must be a MoEConfig or None")
            self.moe.validate()
            if not self.moe_layer_indices:
                raise ConfigError("moe is set but no layer is a "
                                  "mixture-of-experts layer")
        elif self.moe_layer_start or self.moe_layer_interval != 1:
            raise ConfigError("moe layer placement given without a moe")
        if self.dense_layer_indices and self.dense_ffn_hidden == 0:
            raise ConfigError(f"layers {list(self.dense_layer_indices)} are "
                              "dense and dense_ffn_hidden is 0")
        if not self.dense_layer_indices and self.dense_ffn_hidden:
            raise ConfigError("dense_ffn_hidden is set but no layer is dense")
        if not isinstance(self.heads, tuple) or not self.heads:
            raise ConfigError("at least one output head is required")
        names = set()
        for h in self.heads:
            if not isinstance(h, HeadSpec):
                raise ConfigError(f"{h!r} is not a HeadSpec")
            h.validate()
            if h.name in names:
                raise ConfigError(f"head {h.name!r} twice")
            names.add(h.name)
        if self.reconstruction_head not in RECONSTRUCTION_MODES:
            raise ConfigError(f"reconstruction_head "
                              f"{self.reconstruction_head!r} not in "
                              f"{RECONSTRUCTION_MODES}")
        if not isinstance(self.precision, PrecisionProfile):
            raise ConfigError("precision must be a PrecisionProfile")
        self.precision.validate()
        return self

    # -- identity --------------------------------------------------------
    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "family": FAMILY,
                "variant": self.variant, "hidden_size": self.hidden_size,
                "num_layers": self.num_layers,
                "attention": self.attention.to_dict(),
                "moe": None if self.moe is None else self.moe.to_dict(),
                "dense_ffn_hidden": self.dense_ffn_hidden,
                "moe_layer_start": self.moe_layer_start,
                "moe_layer_interval": self.moe_layer_interval,
                "feature_vocab_size": self.feature_vocab_size,
                "context_vocab_size": self.context_vocab_size,
                "value_encoder_hidden": self.value_encoder_hidden,
                "num_readout_tokens": self.num_readout_tokens,
                "heads": [h.to_dict() for h in self.heads],
                "reconstruction_head": self.reconstruction_head,
                "precision": self.precision.to_dict()}

    def digest(self) -> str:
        return digest(self.validate().to_dict())

    @classmethod
    def from_dict(cls, d: dict) -> "ModelConfig":
        """Strict: exactly the keys :meth:`to_dict` writes, this schema."""
        expected = set(cls(variant="x", hidden_size=1, num_layers=1,
                           attention=AttentionConfig(1, 1, 1),
                           feature_vocab_size=1, value_encoder_hidden=1,
                           heads=(HeadSpec("x"),)).to_dict())
        if not isinstance(d, dict) or set(d) != expected:
            raise ConfigError(f"configuration keys differ from "
                              f"{sorted(expected)}")
        if d["schema_version"] != SCHEMA_VERSION or d["family"] != FAMILY:
            raise ConfigError(f"unsupported schema {d['schema_version']!r} "
                              f"/ family {d['family']!r}")
        a = d["attention"]
        if not isinstance(a, dict) or set(a) != {"num_heads", "head_dim",
                                                 "num_kv_heads"}:
            raise ConfigError("attention keys")
        moe = d["moe"]
        if moe is not None:
            keys = set(MoEConfig(2, 1, 1).to_dict())
            if not isinstance(moe, dict) or set(moe) != keys:
                raise ConfigError("moe keys")
            moe = MoEConfig(**{**moe, "routing_profiles": tuple(
                tuple(p) for p in moe["routing_profiles"])})
        heads = []
        for h in d["heads"]:
            if not isinstance(h, dict) or set(h) != {"name", "kind",
                                                     "lower", "upper"}:
                raise ConfigError("head keys")
            heads.append(HeadSpec(**h))
        p = d["precision"]
        if not isinstance(p, dict) or set(p) != set(
                PrecisionProfile().to_dict()):
            raise ConfigError("precision keys")
        rest = {k: v for k, v in d.items() if k not in (
            "schema_version", "family", "attention", "moe", "heads",
            "precision")}
        return cls(attention=AttentionConfig(**a), moe=moe,
                   heads=tuple(heads), precision=PrecisionProfile(**p),
                   **rest).validate()

    def with_moe(self, **changes) -> "ModelConfig":
        """A copy whose MoE block has ``changes`` -- the solver's one edit."""
        from dataclasses import replace
        if self.moe is None:
            raise ConfigError("no moe block to change")
        return replace(self, moe=replace(self.moe, **changes))
