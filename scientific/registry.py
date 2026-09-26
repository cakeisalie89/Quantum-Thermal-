"""Which model implementations are admitted, keyed by id, version and code.

Default-deny: a model that is not registered cannot be looked up, so it
cannot be run through anything that resolves models here. Registration binds
``(model_id, model_version)`` to the implementation digest the model has at
that moment; a second registration of the same identity with DIFFERENT code
is refused, because two implementations answering to one name is how a
result gets attributed to code that did not produce it. Re-registering the
same code is idempotent. A model whose digest cannot be computed is refused:
an entry with no identity would match anything.
"""
from __future__ import annotations

from .identity import IdentityError
from .model import ScientificModel


class RegistryError(ValueError):
    pass


class ModelRegistry:
    def __init__(self):
        self._models: dict = {}
        self._digests: dict = {}

    def register(self, model) -> str:
        if not isinstance(model, ScientificModel):
            raise RegistryError(f"{model!r} is not a ScientificModel")
        key = (model.model_id, model.model_version)
        if not key[0] or not key[1]:
            raise RegistryError("a model needs an id and a version")
        try:
            d = model.implementation_digest()
        except IdentityError as exc:
            raise RegistryError(f"{key}: no implementation identity "
                                f"({exc})") from exc
        prior = self._digests.get(key)
        if prior is not None and prior != d:
            raise RegistryError(
                f"{key[0]}@{key[1]} is already registered with "
                f"implementation {prior[:12]}; refusing {d[:12]} under the "
                "same name -- bump the version")
        self._models.setdefault(key, model)
        self._digests[key] = d
        return d

    def lookup(self, model_id: str, model_version: str):
        """The registered model, after checking its code is still the code
        that was registered; a model whose source changed under its name is
        refused rather than run as if it were the registered one."""
        key = (model_id, model_version)
        if key not in self._models:
            raise RegistryError(f"{model_id}@{model_version} is not "
                                "registered")
        model = self._models[key]
        try:
            now = model.implementation_digest()
        except IdentityError as exc:
            raise RegistryError(f"{key}: identity lost ({exc})") from exc
        if now != self._digests[key]:
            raise RegistryError(f"{model_id}@{model_version}: implementation "
                                "changed since it was registered")
        return model

    def digest_of(self, model_id: str, model_version: str) -> str:
        self.lookup(model_id, model_version)
        return self._digests[(model_id, model_version)]

    def entries(self) -> list:
        return sorted((k[0], k[1], d) for k, d in self._digests.items())
