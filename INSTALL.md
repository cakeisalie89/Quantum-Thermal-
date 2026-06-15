# INSTALL

## External system dependencies

This package's core consistency verification has minimal dependencies. The
optional full PDF text validation requires one external binary.

### Required

- **Python 3** (tested with 3.10+). No third-party Python packages are required
  by `qta_full_sim.py` or `package_consistency_check.py`; both use only the
  Python standard library.

### Optional

- **pdftotext** (from Poppler or Xpdf). Required only for Step 7 of
  `package_consistency_check.py`, which validates the extracted text content of
  `qta_manuscript_v4.pdf`. If `pdftotext` is not on `PATH`, the consistency
  checker explicitly reports

      [PASS] pdftotext not installed; PDF text validation skipped
             (install Poppler/Xpdf pdftotext for this optional check)

  and continues with all other checks. Core consistency verification (gate
  table, manifest hashes, taxonomy, bibliography hygiene, packaging hygiene,
  Step 8B/8C/8D) runs without `pdftotext`.

### Installing pdftotext

- **Windows**: download Poppler for Windows (e.g. from the `poppler-windows`
  GitHub release), extract, and add the `bin/` directory to your `PATH`.
  Alternatively use Xpdf command-line tools.
- **macOS**: `brew install poppler`
- **Ubuntu / Debian**: `sudo apt install poppler-utils`
- **Fedora / RHEL**: `sudo dnf install poppler-utils`

## Running

From the package root (where `qta_full_sim.py` lives):

    python qta_full_sim.py
    python package_consistency_check.py

Both should exit 0. The first regenerates all simulation outputs in
`outputs/`. The second runs all consistency checks and reports `[PASS]` /
`[FAIL]` per check.

## Reproducibility notes

- All Python source files declare UTF-8 explicitly via `reconfigure()` and a
  local `open()` wrapper that defaults text-mode opens to `encoding="utf-8"`.
  This stabilises behaviour on Windows where the default text-mode encoding
  is locale-dependent (e.g. `cp1252`).
- `subprocess.run(..., text=True)` calls also pass `encoding="utf-8",
  errors="replace"` to avoid mojibake when stdout contains non-ASCII.
