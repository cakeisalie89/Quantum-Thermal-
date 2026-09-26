"""Generic scientific-model interfaces (Phase 2 of the convergence plan).

The contract between a governed computation and the numerics it runs:

    AI proposes -> governed deterministic computation executes
      -> independent verification produces evidence -> authority decides

This package is the middle of that line, and only the middle:

* ``model``        -- what a ScientificModel declares and how it is run;
* ``registry``     -- which implementations are admitted, by digest;
* ``result``       -- the ResultBundle, the one thing that crosses from
                      computation into evidence;
* ``verification`` -- the VerificationResult: what a check establishes, and
                      what it does not;
* ``observation``  -- the six observation kinds; a simulation never becomes
                      a measurement;
* ``quantity``     -- a number with its unit and its resolution, so an exact
                      zero and a value below resolution are different results
                      (the generic form of D-2026-69);
* ``identity``     -- canonical digests and implementation digests;
* ``run_identity`` -- when a verified run may be reused instead of recomputed.

It does not know the agent substrate (``qta_agent``) and it does not know the
QTA apparatus: a model is identified by what it computes and by the digest of
the code that computes it, never by a machine mode or a gate. Proposing,
executing under governance, and deciding authority stay in ``qta_agent``. A
model's ``run`` writes no authority event and cannot certify its own output.

Importing any module here loads nothing outside the standard library.
"""
