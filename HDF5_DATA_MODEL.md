# HDF5 data model

HDF5 is a structured REPRESENTATION of results that already exist -- not new
scientific evidence, and not authority. The authority for what a result is,
and whether it is admitted, is the event log and the authority store; an
HDF5 file is an export of their artifacts.

What any HDF5 export here must carry, whatever it represents:

* **units** on every numeric dataset, taken from the source's declared units
  and never invented (an unknown unit is the literal string `unresolved`);
* **schema version** and **dataset provenance**: the digests of the source
  artifacts, the model and run identity that produced them;
* **content digest** of every source, so the export can be checked against
  what it claims to represent;
* **numerical-resolution metadata** where the source states it;
* **deterministic writing**: sorted creation order, `track_times=False`,
  fixed compression, no timestamps, no absolute paths -- byte identity is
  claimed only within the locked environment;
* **equivalence checks**: exact parsed-value equality against the sources,
  with no tolerance.

The generic model represents scientific `ResultBundle` artifacts. The HDF5
file the repository ships today, `qta_scientific_results.h5`, represents the
legacy QTA pipeline's 88 governed outputs; its contract is
`docs/legacy/qta/HDF5_DATA_MODEL.md`, still built and checked as documented
there, and it retires with that payload.
