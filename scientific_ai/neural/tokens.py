"""The token layout every part of the family agrees on.

One feature token carries five things, each embedded and summed:

* its IDENTITY        -- a row of the feature-identity table;
* its VALUE           -- ``VALUE_CHANNELS`` numbers through a small MLP;
* its DIMENSION       -- the seven SI exponents through a linear map, plus
                         a row for its dimension class;
* its CONTEXT         -- a row for its role in the record (``CONTEXTS``);
* its VALIDITY        -- a row for VALID or MISSING.

The value channels, in order:

``z``          the value in its declared transform (linear, or log10 of a
               positive quantity), standardised with TRAINING-split
               statistics only;
``sign``       -1, 0 or +1 of the SI value;
``log_mag``    log10(|v|) / LOG_MAG_SCALE of the SI value, 0 when v == 0;
``is_zero``    1.0 exactly when the SI value is exactly zero.

``z`` alone cannot tell 1e-30 from 1e-20 once both sit far below a
feature's training mean; ``sign`` and ``log_mag`` keep magnitude and sign
separable at any scale, and ``is_zero`` keeps an exact zero from
masquerading as a small number. A MISSING value has all four channels 0 and
validity MISSING: the model is told the value is absent instead of being
handed a number nobody measured.

Changing any of these numbers changes every parameter count and every
checkpoint: they are part of the family, and the accounting reads them from
here.
"""
from __future__ import annotations

VALUE_CHANNELS = ("z", "sign", "log_mag", "is_zero")
#: log10 range of float64 is about +-308; 32 maps 1e-32..1e32 onto +-1
#: without clipping anything outside it.
LOG_MAG_SCALE = 32.0
VALIDITY = ("VALID", "MISSING")
#: A feature's role in the record. INPUT: a parameter of the computation
#: that produced the targets. CONDITION: a fixed setting of it (an integer
#: count, a resolution). The two remaining rows are reserved and named so
#: that the table's size is a decision, not an accident.
CONTEXTS = ("INPUT", "CONDITION", "RESERVED_2", "RESERVED_3")
#: The number of SI base dimensions (``units.BASE``).
N_BASE_DIMS = 7
