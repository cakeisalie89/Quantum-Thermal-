"""The JAX implementation of the family (needs the ``neural`` extra).

* ``_jax``       -- the one place JAX is imported, behind a named refusal;
* ``network``    -- parameters, the forward pass, routing and the MoE layer;
* ``meta``       -- zero-allocation validation and the allocation guard;
* ``train``      -- loss, optimiser, accumulation and the training loop;
* ``checkpoint`` -- the safetensors-layout codec, without pickle;
* ``evaluate``   -- per-split metrics, calibration, OOD and router health;
* ``pipeline``   -- the development run end to end, with its documents;
* ``parallel``   -- execution profiles, parallel plans and expert parallelism.

Importing this package imports none of them.
"""
