"""Integrations of the generic scientific layer with external tools.

Each module here connects an admitted ScientificModel, or the artefacts its
run produced, to one external tool, and each is a REPRESENTATION or an
EXPLORATION -- never a producer of scientific authority:

* :mod:`.vtk_export` -- a solved cell-centred field as a VTK XML
  RectilinearGrid for ParaView, unchanged and unresampled;
* :mod:`.usd_export` -- the solved domain and its probe points as an
  OpenUSD stage, validated by ``usd-core`` where it is installed;
* :mod:`.openmdao_doe` -- a design of experiments over a model's declared
  parameters run through OpenMDAO, every case cross-checked against a
  direct run of the same model.

Global sensitivity (SALib) is :mod:`scientific.sensitivity`; FEniCSx and FMI
are independent checks under :mod:`scientific.checks`; HDF5 is
:mod:`scientific.hdf5_bundle`. One implementation per primitive.

These functions return text, bytes and reports. Writing them anywhere is the
caller's business, and in this repository the caller writes through the
governed workspace guard (``scientific.workspace``).
"""
