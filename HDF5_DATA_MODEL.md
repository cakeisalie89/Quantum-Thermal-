# HDF5 data model

HDF5 is a structured REPRESENTATION of results that already exist -- not new
scientific evidence, and not authority. The authority for what a result is,
and whether it is admitted, is the event log and the authority store; an
HDF5 file is an export of their artifacts.

`scientific/hdf5_bundle.py` writes a `ResultBundle` and reads it back, and
what it writes must carry:

* **units** on every numeric dataset, taken from the bundle's declared
  quantities and never invented;
* **schema version** and **provenance**: the model, its version and
  implementation digest, the parameter and environment digests and the run
  identity;
* **the bundle's own digest**, so the file can be checked against the
  record it claims to represent;
* **numerical-resolution metadata** where a quantity states it;
* **deterministic writing**: sorted creation order, `track_times=False`,
  fixed compression, no timestamps, no absolute paths -- byte identity is
  claimed only within the locked environment;
* **an exact read-back**: the bundle reconstructed from the file must be the
  bundle written, with no tolerance;
* **a reader that outlives the file**: the HDF5 library is native code, and
  a file can make it fault or loop, which no handler catches. It reads in a
  child process. A child that dies by a signal, or has not answered in time,
  is a refusal of the file, and what a child answers is checked again before
  any of it is returned (AUD-053).

`tests/test_hdf5_bundle.py` holds these, and `tools/mutations/hdf5_bundle.json`
removes each in turn.
