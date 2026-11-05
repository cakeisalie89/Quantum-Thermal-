"""The scientific feature-token mixture-of-experts family (NF-1T).

What is here, and what each part may import

* ``config``     -- one member of the family, validated, with a digest;
* ``units``      -- the physical dimension of a unit as this repository
                    spells it (``docs/unit_inventory.json`` is the authority
                    on WHICH unit a quantity has; this only reads it);
* ``features``   -- the feature and target schemas and the tokenizer that
                    turns a governed scientific record into tokens;
* ``accounting`` -- the exact parameter count of a configuration, by
                    category, and what "active per token" means;
* ``solver``     -- the architecture that meets a parameter budget;
* ``estimates``  -- memory, FLOP and communication ESTIMATES, with their
                    assumptions, never benchmarks;
* ``claims``     -- the claim vocabulary and the model-status ladder;
* ``manifests``  -- the model, dataset, training and checkpoint documents;
* ``datasets``   -- governed samples, splits and leakage checks;
* ``model``      -- the JAX implementation: network, meta validation,
                    training, checkpoints, evaluation and parallel plans.

Everything but ``model`` is the standard library plus NumPy. ``model``
needs the ``neural`` extra (``uv sync --extra neural``) and refuses with a
named error when it is absent.
"""
