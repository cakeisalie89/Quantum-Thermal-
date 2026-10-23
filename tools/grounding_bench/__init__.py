"""A benchmark of whether gating assertions on independently reconstructed
state reduces confident false statements (Tranche G, G5).

Scripted agents only: nothing here calls a model, and the real-model
condition is reported NOT_RUN. The protocol, its hypotheses and its results
are in docs/research/GROUNDING_BENCHMARK.md.

    claims  the claim contract, V(C, S, E)
    world   a governed history, the faults injected into it, the hidden oracle
    agents  the scripted error model and the conditions
    run     questions, scoring, the protocol
"""
