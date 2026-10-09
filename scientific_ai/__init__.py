"""Learned scientific models: a substrate that predicts and never decides.

Tranche NF-1T. ``scientific_ai.neural`` holds one architecture family -- a
scientific feature-token transformer with mixture-of-experts feed-forward
layers -- from a development model that trains on a CPU in minutes to a
configuration of about one trillion parameters that is parameterised and
structurally validated but NOT trained.

Where it sits on the governed line

    AI proposes -> governed deterministic computation executes
      -> independent verification produces evidence -> authority decides

A learned prediction is none of those four. It is a LEARNED_PREDICTION,
NON_AUTHORITATIVE, and REQUIRES_EXTERNAL_VERIFICATION: nothing in this
package can mark its own output verified, accept a model, or replace a
governed calculation, and the authority store refuses every edge into
VERIFIED or PROMOTED for a learned record (``qta_agent.learned_rules``).

It does not know the agent substrate and it does not know the QTA apparatus:
no machine mode, gas species routing or gate is a concept here, and
``tests/test_neural_boundary.py`` fails if one is introduced.
"""
