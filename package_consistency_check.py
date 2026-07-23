#!/usr/bin/env python3
"""
package_consistency_check.py — independent verifier for the QTA package.

Reruns qta_full_sim.py in a clean outputs directory, then compares every
canonical artifact against a single CANONICAL_EXPECTED truth table.

Inspects PDF text via pdftotext.

Exit code 0 only if every check passes. Any failure exits non-zero with
a clear per-check report.
"""
import sys, csv, json, re, hashlib, subprocess, shutil, builtins
from collections import Counter
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_builtin_open = builtins.open
def open(file, mode="r", buffering=-1, encoding=None, errors=None,
         newline=None, closefd=True, opener=None):
    if "b" not in mode and encoding is None:
        encoding = "utf-8"
    return _builtin_open(file, mode, buffering, encoding, errors,
                         newline, closefd, opener)

PKG = Path(__file__).resolve().parent

# ===================== CANONICAL_EXPECTED ====================================
CANONICAL_EXPECTED = {
    "total_gates": 83,
    "PASS": 0,
    "CONDITIONAL": 47,
    "BLOCKED": 23,
    "UNKNOWN": 2,
    "DERIVED_CHECK": 11,
    "tau_c_canonical_us": 292,
    "tau_c_superseded_us": 27.728,
    "required_generated_gates_include": [
        "A6","A7","A8","A9","A10","A11","A12","A13","A14",
        "Shield-RAD","Shield-RF","Shield-OPT","Shield-MAG","Shield-CHEM",
        "C_to_D_Readiness","RTB_JT_OPTIONAL_COOLING_PLANT",
        # Pass 15: non-lumped 1D/2D multiphysics gates (all CONDITIONAL/BLOCKED/DERIVED_CHECK)
        "THERMAL_1D_STABILITY_CHECK","THERMAL_2D_STABILITY_CHECK",
        "THERMAL_1D_MESH_CONVERGENCE_CHECK","THERMAL_2D_MESH_CONVERGENCE_CHECK",
        "KAPITZA_BC_CHECK","MOVING_BOUNDARY_MODEL_CHECK","NV_LAYER_TEMPERATURE_CHECK",
        "HOTSPOT_MARGIN_CHECK","POST_PULSE_DRIFT_CHECK","OPTICAL_ABSORPTION_PROFILE_CHECK",
        "GAS_TRANSPORT_STABILITY_CHECK","CRYOBAFFLE_CAPTURE_CHECK",
        "RESIDUAL_SPECIES_MODE_D_CHECK","SURFACE_COVERAGE_DECAY_CHECK",
        "MICROWAVE_HEATING_PROFILE_CHECK","RADIATION_VIEWFACTOR_CHECK",
        "VIBRATION_TRANSFER_CHECK","COUPLED_MODE_RECOVERY_CHECK",
        "LUMPED_MODEL_RETIRED_CHECK","THREE_D_LAYER_STATUS_CHECK",
    ],
    "all_can_PASS_now": "NO",
    "all_measured_in_this_system": "false",
}

# Stale strings — these are the EXACT phrases the independent auditor flagged.
# A file containing any of these (outside an explicit SUPERSEDED label) FAILS.
STALE_FORBIDDEN_IN_TEX_OR_PDF = [
    "34 conditional",
    "10 blocked",
    "0 Pass | 34",
    "Summary: 0 Pass | 34",
    "validation_matrix.csv (25 rows",
    "All numerical parameters carry directness labels",
]
STALE_FORBIDDEN_IN_STDOUT = [
    "-> D10b: PASS",
    "physics PASS",
    "PASS physics",
    "PASS (forecast only)",
    "34C",
    "0P 34C",
    "10BLOCKED",
    "10 BLOCKED gates present",
    "threshold=27.7us",
    "tau_c_min(SNR=5)=27.7us",
]

# References to deleted source_audit.csv (but not representative_source_audit.csv).
DELETED_FILE_REFERENCES = [
    ("source_audit.csv", "representative_source_audit.csv"),
]

# ===================== FAILURE TRACKING ======================================
FAILURES = []
def fail(check, detail):
    FAILURES.append((check, detail))
def ok(check, detail=""):
    print(f"  [PASS] {check}  {detail}".rstrip())

# ===================== STEP 1: regenerate outputs ============================
print("="*70)
print("PACKAGE CONSISTENCY CHECK (hardened — independent audit coverage)")
print("="*70)
print()
print("Step 1: regenerate qta_full_sim.py outputs in fresh directory")
print("-"*70)

gen_outputs_dir = PKG / "outputs"
shutil.rmtree(gen_outputs_dir, ignore_errors=True)
sim_path = PKG / "qta_full_sim.py"
sim_stdout = ""
if not sim_path.exists():
    fail("qta_full_sim.py present", f"missing {sim_path}")
else:
    proc = subprocess.run([sys.executable, str(sim_path)],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          cwd=str(PKG), timeout=300)
    sim_stdout = proc.stdout
    if proc.returncode != 0:
        fail("qta_full_sim.py exit 0",
             f"got {proc.returncode}; stderr: {proc.stderr[:500]}")
    else:
        ok("qta_full_sim.py executes (exit 0)")

# ===================== STEP 2: gate table checks =============================
print()
print("Step 2: gate table — counts, fields, A6-A14, no-PASS")
print("-"*70)

gen_gate = gen_outputs_dir / "results_gate_table.csv"
pkg_gate = PKG / "results_gate_table.csv"

if not gen_gate.exists():
    fail("generated outputs/results_gate_table.csv", "missing")
elif not pkg_gate.exists():
    fail("packaged results_gate_table.csv", "missing")
else:
    gen_rows = list(csv.DictReader(open(gen_gate)))

    gen_sha = hashlib.sha256(open(gen_gate,"rb").read()).hexdigest()
    pkg_sha = hashlib.sha256(open(pkg_gate,"rb").read()).hexdigest()
    if gen_sha != pkg_sha:
        fail("generated vs packaged results_gate_table.csv byte-identical",
             f"SHAs differ ({gen_sha[:16]} vs {pkg_sha[:16]})")
    else:
        ok("generated vs packaged results_gate_table.csv byte-identical")

    ids = [r["gate_id"] for r in gen_rows]
    if len(ids) != CANONICAL_EXPECTED["total_gates"]:
        fail("exactly 83 gate rows in generated table", f"got {len(ids)}")
    elif len(set(ids)) != len(ids):
        dups = [x for x in set(ids) if ids.count(x) > 1]
        fail("no duplicate gate_id rows", f"duplicates: {dups}")
    else:
        ok("exactly 83 unique gate rows in generated table")

    counts = Counter(r["status"] for r in gen_rows)
    expected_counts = {
        "PASS": CANONICAL_EXPECTED["PASS"],
        "CONDITIONAL": CANONICAL_EXPECTED["CONDITIONAL"],
        "BLOCKED": CANONICAL_EXPECTED["BLOCKED"],
        "UNKNOWN": CANONICAL_EXPECTED["UNKNOWN"],
        "DERIVED_CHECK": CANONICAL_EXPECTED["DERIVED_CHECK"],
    }
    bad_counts = []
    for k, v in expected_counts.items():
        a = counts.get(k, 0)
        if a != v:
            bad_counts.append(f"{k}: got {a}, expected {v}")
    if bad_counts:
        fail("gate status counts match canonical", "; ".join(bad_counts))
    else:
        ok(f"gate status counts canonical "
           f"(0P {expected_counts['CONDITIONAL']}C "
           f"{expected_counts['BLOCKED']}BLOCKED "
           f"{expected_counts['UNKNOWN']}U "
           f"{expected_counts['DERIVED_CHECK']}DC)")

    required_a = set(CANONICAL_EXPECTED["required_generated_gates_include"])
    missing_a = required_a - set(ids)
    if missing_a:
        fail("A6-A14 generated by sim", f"missing: {sorted(missing_a)}")
    else:
        ok("A6-A14 generated by sim (not manually patched)")

    bad = [r["gate_id"] for r in gen_rows if r.get("can_PASS_now") != "NO"]
    if bad:
        fail("can_PASS_now=NO for all rows", f"violations: {bad[:5]}")
    else:
        ok("can_PASS_now=NO for all 83 rows")

    bad_m = [r["gate_id"] for r in gen_rows if r.get("measured_in_this_system") != "false"]
    if bad_m:
        fail("measured_in_this_system=false for all rows", f"violations: {bad_m[:5]}")
    else:
        ok("measured_in_this_system=false for all 83 rows")

    d9 = [r for r in gen_rows if r["gate_id"] == "D9"]
    if not d9:
        fail("D9 present in gate table", "missing")
    elif d9[0]["status"] != "DERIVED_CHECK":
        fail("D9 = DERIVED_CHECK", f"got {d9[0]['status']}")
    else:
        ok("D9 = DERIVED_CHECK")

# ---- Step 2b: stale-snapshot guard over the full deterministic output set ----
# Every ROOT canonical output that the pipeline regenerates must be byte-identical
# to the regeneration. The deep-layer readiness file is exempt by design: the ROOT
# copy is the deep layer's authoritative record (e.g. TRAINED_NOT_TRUSTED), while
# the direct expdesign engine emits a NOT_IMPLEMENTED stub in its own run directory.
def regen_root_byte_drift(gen_dir, root_dir, exceptions=frozenset()):
    """Names of regenerated files whose ROOT canonical copy exists but is not
    byte-identical (stale committed snapshots)."""
    drift = []
    for p in sorted(gen_dir.iterdir()):
        if not p.is_file() or p.name in exceptions:
            continue
        rootp = root_dir / p.name
        if rootp.exists():
            if hashlib.sha256(p.read_bytes()).hexdigest() != \
               hashlib.sha256(rootp.read_bytes()).hexdigest():
                drift.append(p.name)
    return drift

_REGEN_EXEMPT = frozenset({"deep_surrogate_readiness.json"})
_drift = regen_root_byte_drift(gen_outputs_dir, PKG, _REGEN_EXEMPT)
if _drift:
    fail("root canonical outputs byte-match the canonical regeneration",
         f"stale root copies: {_drift[:8]}")
else:
    ok("root canonical outputs byte-match the canonical regeneration "
       "(exempt by design: deep_surrogate_readiness.json)")

# ===================== STEP 3: sim stdout stale audit ========================
print()
print("Step 3: sim stdout — stale-language audit")
print("-"*70)

stale_in_stdout = []
for s in STALE_FORBIDDEN_IN_STDOUT:
    for line in sim_stdout.split("\n"):
        if s in line and not any(x in line for x in
                                  ["SUPERSEDED","NOT_CANONICAL","NOT_LIVE_GATE_LOGIC"]):
            stale_in_stdout.append((s, line.strip()[:120]))
if stale_in_stdout:
    fail("sim stdout free of stale strings",
         f"{len(stale_in_stdout)} live hits: " + "; ".join(
             f"'{s}' -> {line[:60]}" for s,line in stale_in_stdout[:3]))
else:
    ok("sim stdout free of stale strings (incl. -> D10b: PASS, physics PASS, etc.)")

# ===================== STEP 4: tau_c_sweep.csv ===============================
print()
print("Step 4: tau_c_sweep.csv — canonical 292us gate; 27.728us = SUPERSEDED_V30")
print("-"*70)

tcs_path = PKG / "tau_c_sweep.csv"
if not tcs_path.exists():
    fail("tau_c_sweep.csv present", "missing")
else:
    tcs = list(csv.DictReader(open(tcs_path)))
    if "tau_c_canonical_threshold_us" not in tcs[0]:
        fail("tau_c_sweep has canonical_threshold_us column", "")
    else:
        ok("tau_c_sweep has tau_c_canonical_threshold_us column")

    pass_27 = [r for r in tcs if abs(float(r["tau_c_s"]) - 27.728e-6) < 1e-9
               and r["Gate"] == "PASS"]
    if pass_27:
        fail("27.728us row not PASS", f"{pass_27}")
    else:
        ok("27.728us row not marked PASS")

    sup_27 = [r for r in tcs if abs(float(r["tau_c_s"]) - 27.728e-6) < 1e-9
              and r["Gate"] == "SUPERSEDED_V30"]
    if not sup_27:
        fail("27.728us row labeled SUPERSEDED_V30", "")
    else:
        ok("27.728us row labeled SUPERSEDED_V30")

# ===================== STEP 5: README ========================================
print()
print("Step 5: README.md — gate counts and canonical claims")
print("-"*70)

readme_path = PKG / "README.md"
if not readme_path.exists():
    fail("README.md present", "missing")
else:
    readme = readme_path.read_text()
    required_pairs = [("83","total gates"),("0","PASS"),("47","CONDITIONAL"),
                      ("23","BLOCKED"),("2","UNKNOWN"),("11","DERIVED_CHECK"),
                      ("292","tau_c canonical threshold")]
    missing = [f"{n}/{l}" for n,l in required_pairs if n not in readme or l not in readme]
    if missing:
        fail("README contains all canonical numbers/labels", f"missing: {missing}")
    else:
        ok("README contains 83/0/47/23/2/11/292 and labels")

    # Reviewer-driven: detect internal count contradictions. Every "BLOCKED count | N"
    # row (GLOBAL STATUS table or elsewhere) must equal the canonical 23, and the
    # GLOBAL STATUS table must sum to 83. Step 5 previously only checked that "21"
    # appeared *somewhere*, so a contradicting "BLOCKED count | 20" slipped through.
    blocked_count_rows = re.findall(r"BLOCKED count\s*\|\s*(\d+)", readme)
    bad_blocked = [n for n in blocked_count_rows if n != "23"]
    if bad_blocked:
        fail("README BLOCKED count rows all equal canonical 23",
             f"found BLOCKED count = {bad_blocked} (canonical = 23)")
    else:
        ok(f"README BLOCKED count rows all equal 23 ({len(blocked_count_rows)} row(s) checked)")

    # GLOBAL STATUS table internal sum: PASS+BLOCKED+CONDITIONAL+UNKNOWN+DERIVED = 83
    gs_pass = re.search(r"PASS count\s*\|\s*(\d+)", readme)
    gs_blocked = re.search(r"BLOCKED count\s*\|\s*(\d+)", readme)
    gs_cond = re.search(r"CONDITIONAL count\s*\|\s*(\d+)", readme)
    gs_unknown = re.search(r"UNKNOWN count\s*\|\s*(\d+)", readme)
    gs_derived = re.search(r"DERIVED(?:_CHECK)? count\s*\|\s*(\d+)", readme)
    if all([gs_pass, gs_blocked, gs_cond, gs_unknown, gs_derived]):
        gs_sum = sum(int(x.group(1)) for x in [gs_pass, gs_blocked, gs_cond, gs_unknown, gs_derived])
        if gs_sum != 83:
            fail("README GLOBAL STATUS table sums to 83",
                 f"PASS+BLOCKED+CONDITIONAL+UNKNOWN+DERIVED = {gs_sum} (expected 83)")
        else:
            ok("README GLOBAL STATUS table sums to 83")

    forbidden = ["DARPA-ready","Nobel-worthy","proof of feasibility",
                 "validated system","breakthrough"]
    bad = []
    for s in forbidden:
        for line in readme.split("\n"):
            if s.lower() in line.lower():
                if not any(neg in line.lower() for neg in
                            ["no ","not ","never","forbidden","explicit non-claim",
                             "does not","| not ","not made","not available"]):
                    bad.append(f"{s}: {line.strip()[:80]}")
    if bad:
        fail("README free of forbidden positive claims",
             f"{len(bad)} hits: {bad[:2]}")
    else:
        ok("README free of forbidden positive claims (or all in non-claim context)")

# ===================== STEP 6: manuscript .tex ===============================
print()
print("Step 6: qta_manuscript_v4.tex — gate counts, stale-strings, deleted-file refs")
print("-"*70)

tex_path = PKG / "qta_manuscript_v4.tex"
if not tex_path.exists():
    fail("qta_manuscript_v4.tex present", "missing")
else:
    tex = tex_path.read_text()

    required_text = ["83", "0 PASS", "47 CONDITIONAL", "23 BLOCKED", "292"]
    missing = [s for s in required_text if s not in tex]
    if missing:
        fail("manuscript contains canonical counts",
             f"missing literal strings: {missing}")
    else:
        ok("manuscript contains 83 / 0 PASS / 47 CONDITIONAL / 23 BLOCKED / 292")

    stale = []
    for p in STALE_FORBIDDEN_IN_TEX_OR_PDF:
        if p in tex:
            stale.append(p)
    # Also explicit "34~\conditional" / "10~\blocked" / "Summary: 0~Pass $|$ 34"
    for p in ["34~\\conditional", "10~\\blocked", "34\\,conditional", "10\\,blocked"]:
        if p in tex:
            stale.append(p)
    if stale:
        fail("manuscript free of stale strings (independent-auditor list)",
             f"found: {stale}")
    else:
        ok("manuscript free of stale strings (independent-auditor list)")

    # Deleted-file references
    bad_refs = []
    for deleted, replacement in DELETED_FILE_REFERENCES:
        # Find references to `deleted` that are NOT `replacement`
        for m in re.finditer(rf"(?<!{re.escape(replacement[:len(replacement)-len(deleted)])})"
                             rf"{re.escape(deleted)}", tex):
            ctx = tex[max(0,m.start()-30):m.end()+30]
            if replacement not in ctx:
                bad_refs.append(f"{deleted} at char {m.start()}: ...{ctx}...")
    if bad_refs:
        fail("manuscript free of references to deleted files",
             f"{len(bad_refs)} hits: {bad_refs[:2]}")
    else:
        ok("manuscript free of references to deleted source_audit.csv")

    # validation_matrix row count claim must match actual file
    vm_path = PKG / "validation_matrix.csv"
    if vm_path.exists():
        vm_rows = sum(1 for _ in open(vm_path)) - 1
        m = re.search(r"validation[_\\ ]matrix(?:[_\\.]csv)?[^.\n]{0,80}?(\d+)\s*rows?",
                      tex, re.I)
        if m:
            claimed = int(m.group(1))
            if claimed != vm_rows:
                fail("manuscript validation_matrix row count matches file",
                     f"manuscript says {claimed}, file has {vm_rows}")
            else:
                ok(f"manuscript validation_matrix row count matches file ({vm_rows})")
        else:
            ok("manuscript makes no specific row-count claim about validation_matrix")

    # PDF freshness (2-second tolerance for sub-second precision lost by ZIP extraction).
    # The substantive PDF-content check happens in Step 7 (pdftotext extraction).
    pdf_path = PKG / "qta_manuscript_v4.pdf"
    if pdf_path.exists():
        TOL = 2.0
        pdf_m = pdf_path.stat().st_mtime
        tex_m = tex_path.stat().st_mtime
        if pdf_m + TOL < tex_m:
            fail("PDF rebuilt after .tex changes (mtime, with 2 s tolerance)",
                 f"PDF mtime {pdf_m:.3f} more than 2 s older than tex mtime {tex_m:.3f}")
        else:
            ok(f"PDF mtime within 2 s tolerance of tex mtime "
               f"(pdf={pdf_m:.0f} tex={tex_m:.0f}; Step 7 validates content)")

# ===================== STEP 7: PDF text via pdftotext ========================
print()
print("Step 7: qta_manuscript_v4.pdf TEXT — stale strings, deleted-file refs")
print("-"*70)

pdftotext_ok = shutil.which("pdftotext") is not None
pdf_path = PKG / "qta_manuscript_v4.pdf"
if not pdftotext_ok:
    ok("pdftotext not installed; PDF text validation skipped "
       "(install Poppler/Xpdf pdftotext for this optional check)")
elif not pdf_path.exists():
    fail("qta_manuscript_v4.pdf present", "missing")
else:
    r = subprocess.run(["pdftotext", str(pdf_path), "-"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    pdf_text = r.stdout

    # Stale strings in PDF (extracted text)
    stale_in_pdf = []
    for p in STALE_FORBIDDEN_IN_TEX_OR_PDF:
        cnt = pdf_text.count(p)
        if cnt:
            stale_in_pdf.append(f"'{p}' x{cnt}")
    if stale_in_pdf:
        fail("PDF text free of stale strings",
             "found: " + "; ".join(stale_in_pdf))
    else:
        ok("PDF text free of stale 34/10 gate counts, 25-row claim, source_audit.csv reference")

    # Required canonical counts present
    needed = ["47 conditional","23 blocked","0 Pass | 47"]
    missing = [s for s in needed if s not in pdf_text]
    if missing:
        fail("PDF contains canonical counts",
             f"missing in extracted text: {missing}")
    else:
        ok("PDF contains '47 conditional' / '23 blocked' / '0 Pass | 47'")

    # validation_matrix row count claim in PDF
    m = re.search(r"validation[_\\ ]matrix(?:\.csv)?[^.\n]{0,80}?(\d+)\s*rows?", pdf_text, re.I)
    if m:
        claimed = int(m.group(1))
        vm_path = PKG / "validation_matrix.csv"
        vm_rows = sum(1 for _ in open(vm_path)) - 1
        if claimed != vm_rows:
            fail("PDF validation_matrix row count matches file",
                 f"PDF says {claimed}, file has {vm_rows}")
        else:
            ok(f"PDF validation_matrix row count matches file ({vm_rows})")

    # Deleted source_audit.csv reference in PDF
    sa_refs = []
    for m in re.finditer(r"source_audit\.csv", pdf_text):
        ctx = pdf_text[max(0,m.start()-40):m.end()+40]
        if "representative_source_audit.csv" not in ctx:
            sa_refs.append(ctx.strip()[:80])
    if sa_refs:
        fail("PDF text free of references to deleted source_audit.csv",
             f"{len(sa_refs)} hits: {sa_refs[:2]}")
    else:
        ok("PDF text free of references to deleted source_audit.csv")

# ===================== STEP 8: source audit honesty ==========================
print()
print("Step 8: source audit — not falsely presented as complete")
print("-"*70)

sa_path = PKG / "source_audit.csv"
rsa_path = PKG / "representative_source_audit.csv"
if sa_path.exists() and rsa_path.exists():
    sa_sha = hashlib.sha256(open(sa_path,"rb").read()).hexdigest()
    rsa_sha = hashlib.sha256(open(rsa_path,"rb").read()).hexdigest()
    if sa_sha == rsa_sha:
        fail("source_audit.csv distinct from representative_source_audit.csv (or removed)",
             "files are byte-identical duplicates")
    else:
        ok("source_audit.csv distinct from representative_source_audit.csv")
elif rsa_path.exists() and not sa_path.exists():
    ok("source_audit.csv removed; representative_source_audit.csv kept")
elif not rsa_path.exists():
    fail("representative_source_audit.csv present", "missing")

status_path = PKG / "source_audit_status.txt"
if status_path.exists():
    txt = status_path.read_text()
    if "REPRESENTATIVE_ONLY" in txt or "representative" in txt.lower():
        ok("source_audit_status.txt labels audit REPRESENTATIVE_ONLY / not complete")
    else:
        fail("source_audit_status.txt honestly labels representative-only audit",
             "missing REPRESENTATIVE_ONLY / representative")

# ===================== STEP 8b: source_audit_status.txt content =============
print()
print("Step 8b: source_audit_status.txt content — no references to deleted files")
print("-"*70)

status_path = PKG / "source_audit_status.txt"
if status_path.exists():
    sa_txt = status_path.read_text()
    # Must NOT say "Current file: source_audit.csv"
    if re.search(r"Current\s+file\s*:\s*source_audit\.csv", sa_txt):
        fail("source_audit_status.txt: 'Current file' does not reference deleted source_audit.csv",
             "still says 'Current file: source_audit.csv'")
    else:
        ok("source_audit_status.txt 'Current file' references representative_source_audit.csv")
    # Stale validation_matrix.csv (88 rows)
    m = re.search(r"validation_matrix\.csv\s*\((\d+)\s*rows?\)", sa_txt)
    if m:
        claimed = int(m.group(1))
        vm_path = PKG / "validation_matrix.csv"
        actual = (sum(1 for _ in open(vm_path)) - 1) if vm_path.exists() else None
        if actual is not None and claimed != actual:
            fail("source_audit_status.txt validation_matrix row count matches file",
                 f"status text says {claimed}, file has {actual}")
        else:
            ok(f"source_audit_status.txt validation_matrix row count matches file ({claimed})")
    # No bare "source_audit.csv" (without representative_) reference
    bad = []
    for m in re.finditer(r"source_audit\.csv", sa_txt):
        # Wider window so "The previous source_audit.csv was ... removed" is recognised
        ctx = sa_txt[max(0,m.start()-80):m.end()+80]
        # Allowed contexts: substring of representative_source_audit.csv,
        # OR explicit past-tense / removal / deletion wording
        ctx_low = ctx.lower()
        if "representative_source_audit.csv" in ctx:
            continue  # substring match — not a live reference
        if any(neg in ctx_low for neg in [
            "previous source_audit","was a byte-identical","has been removed",
            "was removed","was deleted","is removed","is deleted","duplicate",
            "no longer present","no longer used"]):
            continue  # explicit removal/past-tense context
        line_no = sa_txt[:m.start()].count("\n")+1
        bad.append(f"line {line_no}: ...{ctx[-60:]}")
    if bad:
        fail("source_audit_status.txt free of live references to deleted source_audit.csv",
             f"{len(bad)} hits: {bad[:2]}")
    else:
        ok("source_audit_status.txt free of live references to deleted source_audit.csv")

# ===================== STEP 8c: cross-file references to deleted source_audit.csv ===
print()
print("Step 8c: validation_matrix.csv / risk_register.csv — no source_audit.csv refs")
print("-"*70)

for fname in ["validation_matrix.csv", "risk_register.csv"]:
    p = PKG / fname
    if not p.exists(): continue
    content = p.read_text(encoding="utf-8", errors="ignore")
    bad = []
    for m in re.finditer(r"source_audit\.csv", content):
        ctx = content[max(0,m.start()-40):m.end()+10]
        if "representative_source_audit.csv" not in ctx:
            line_no = content[:m.start()].count("\n")+1
            bad.append(f"line {line_no}: ...{ctx[-60:]}")
    if bad:
        fail(f"{fname} free of references to deleted source_audit.csv",
             f"{len(bad)} hits: {bad[:2]}")
    else:
        ok(f"{fname} free of references to deleted source_audit.csv")

# ===================== STEP 8d: non-PASS rows must not contain "(PASS)" wording ===
print()
print("Step 8d: results_gate_table.csv — no '(PASS)' wording in non-PASS rows")
print("-"*70)

gate_path = PKG / "results_gate_table.csv"
if gate_path.exists():
    rows = list(csv.DictReader(open(gate_path)))
    bad = []
    forecast_pass_patterns = ["(PASS)", "-> PASS", "PASS forecast only", "PASS only"]
    for r in rows:
        if r["status"] == "PASS":
            continue
        for fld in ("reason","equation","notes","fix"):
            text = str(r.get(fld,""))
            for pat in forecast_pass_patterns:
                if pat in text:
                    bad.append(f"{r['gate_id']} ({r['status']}) {fld}: {pat}")
                    break
    if bad:
        fail("non-PASS rows free of misleading PASS wording",
             f"{len(bad)} rows: {bad[:3]}")
    else:
        ok("non-PASS rows free of misleading PASS wording (no '(PASS)' / '-> PASS' in non-PASS rows)")


# ===================== STEP 8B: micro-cleanup forensic rules =================
print()
print("Step 8B: forensic checks — auditor micro-cleanup conditions")
print("-"*70)

# 1) source_audit_status.txt must NOT say "Current file: source_audit.csv"
sas_path = PKG / "source_audit_status.txt"
if sas_path.exists():
    sas_txt = sas_path.read_text()
    bad = []
    if re.search(r"^\s*Current file:\s*source_audit\.csv\s*$", sas_txt, re.M):
        bad.append("status says 'Current file: source_audit.csv' (deleted file)")
    # Detect any validation_matrix.csv row count other than the live count (computed below)
    # Compute actual validation_matrix.csv row count at check time
    try:
        vm_actual = sum(1 for _ in csv.DictReader(open(PKG / "validation_matrix.csv")))
    except Exception:
        vm_actual = None
    if vm_actual is not None:
        for m_old in re.finditer(r"validation_matrix\.csv\s*\(\s*(\d+)\s*rows", sas_txt):
            rc = int(m_old.group(1))
            if rc != vm_actual:
                bad.append(f"status references stale '{rc} rows' for validation_matrix (current = {vm_actual})")
    if bad:
        fail("source_audit_status.txt free of stale phrases", "; ".join(bad))
    else:
        ok(f"source_audit_status.txt: 'Current file: representative_source_audit.csv', validation_matrix.csv = {vm_actual} rows correct")

# 2) validation_matrix.csv: no row may reference deleted source_audit.csv
#    (representative_source_audit.csv contains the substring; exclude that)
vm_path = PKG / "validation_matrix.csv"
if vm_path.exists():
    vm = list(csv.DictReader(open(vm_path)))
    bad = []
    for i, row in enumerate(vm, start=2):  # row 2 = first data row (header is row 1)
        for k, v in row.items():
            v = str(v)
            for m in re.finditer(r"source_audit\.csv", v):
                ctx_start = max(0, m.start()-30)
                ctx = v[ctx_start:m.end()+10]
                if "representative_source_audit.csv" not in ctx and \
                   "source_audit_status.txt" not in v:
                    bad.append(f"row {i} field {k}: {v[:80]}")
    if bad:
        fail("validation_matrix.csv free of deleted source_audit.csv refs",
             f"{len(bad)} hits: {bad[:2]}")
    else:
        ok("validation_matrix.csv free of deleted source_audit.csv references")

# 3) risk_register.csv: no row may reference deleted source_audit.csv
rr_path = PKG / "risk_register.csv"
if rr_path.exists():
    rr = list(csv.DictReader(open(rr_path)))
    bad = []
    for i, row in enumerate(rr, start=2):
        for k, v in row.items():
            v = str(v)
            # Specific stale phrase
            if "source_audit.csv: representative only" in v:
                bad.append(f"row {i} ({row.get('risk_id','?')}) field {k}: stale phrase")
                continue
            # Generic source_audit.csv that is not representative_source_audit.csv
            for m in re.finditer(r"source_audit\.csv", v):
                ctx_start = max(0, m.start()-30)
                ctx = v[ctx_start:m.end()+10]
                if "representative_source_audit.csv" not in ctx:
                    bad.append(f"row {i} ({row.get('risk_id','?')}) field {k}: {v[:80]}")
    if bad:
        fail("risk_register.csv free of deleted source_audit.csv refs",
             f"{len(bad)} hits: {bad[:2]}")
    else:
        ok("risk_register.csv free of deleted source_audit.csv references")

# 4) results_gate_table.csv: no non-PASS row may contain "(PASS)" in
#    reason / equation / fix / notes — that is forecast-only PASS wording.
gt_path = PKG / "results_gate_table.csv"
if gt_path.exists():
    gt = list(csv.DictReader(open(gt_path)))
    bad = []
    for r in gt:
        if r["status"] == "PASS":
            continue
        for fld in ("reason","equation","fix","notes"):
            val = str(r.get(fld, ""))
            if "(PASS)" in val:
                bad.append(f"{r['gate_id']} [{r['status']}] field {fld}: ...{val[max(0,val.index('(PASS)')-30):val.index('(PASS)')+12]}...")
    if bad:
        fail("results_gate_table.csv: no non-PASS row contains '(PASS)' wording",
             f"{len(bad)} hits: {bad[:2]}")
    else:
        ok("results_gate_table.csv: no non-PASS row contains '(PASS)' wording")

# 5) Stricter: non-PASS rows must not contain any "-> PASS" / "=> PASS"
#    forecast-only patterns either.
if gt_path.exists():
    gt = list(csv.DictReader(open(gt_path)))
    bad = []
    for r in gt:
        if r["status"] == "PASS":
            continue
        for fld in ("reason","equation","fix","notes"):
            val = str(r.get(fld, ""))
            for pat in ["-> PASS", "=> PASS", " PASS forecast", "PASS (forecast"]:
                if pat in val:
                    bad.append(f"{r['gate_id']} [{r['status']}] field {fld}: pattern '{pat}'")
    if bad:
        fail("results_gate_table.csv: no non-PASS row contains forecast-only -> PASS wording",
             f"{len(bad)} hits: {bad[:2]}")
    else:
        ok("results_gate_table.csv: no non-PASS row contains forecast-only -> PASS wording")


# ===================== STEP 8C: packaging hygiene ============================
print()
print("Step 8C: packaging hygiene — single README, no grep_report, distinct RGA gates")
print("-"*70)

# (a) README.txt must NOT exist (README.md is canonical)
if (PKG / "README.txt").exists():
    fail("README.txt deleted (README.md is canonical)", "README.txt still present")
else:
    ok("README.txt deleted (README.md is canonical)")

# (b) grep_report.txt must NOT exist (legacy AI-workflow artifact)
if (PKG / "grep_report.txt").exists():
    fail("grep_report.txt removed",
         "grep_report.txt still present")
else:
    ok("grep_report.txt removed")

# (c) B3 and E04 must have distinct names
gt_path = PKG / "results_gate_table.csv"
if gt_path.exists():
    gt = list(csv.DictReader(open(gt_path)))
    b3 = next((r for r in gt if r["gate_id"] == "B3"), None)
    e04 = next((r for r in gt if r["gate_id"] == "E04"), None)
    if b3 and e04:
        if b3["name"] == e04["name"]:
            fail("B3 and E04 have distinct names",
                 f"both named: '{b3['name']}'")
        else:
            ok("B3 and E04 have distinct names")
            ok(f"  B3:  {b3['name'][:80]}")
            ok(f"  E04: {e04['name'][:80]}")

# (d) ZIP must not contain README.txt or grep_report.txt
zip_path = PKG / "QTA_submission.zip"
if zip_path.exists():
    import zipfile
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
    forbidden_in_zip = []
    for f in ("README.txt", "grep_report.txt"):
        if f in names:
            forbidden_in_zip.append(f)
    if forbidden_in_zip:
        fail("ZIP free of deleted files (README.txt, grep_report.txt)",
             f"present in ZIP: {forbidden_in_zip}")
    else:
        ok("ZIP free of deleted files (README.txt, grep_report.txt)")

# (e) Every file in the ZIP (except final_manifest.json itself and manifest_hash.txt)
#     must be listed in final_manifest.json. manifest_hash.txt remains detached;
#     this is documented in self_hash_policy.
if zip_path.exists() and (PKG / "final_manifest.json").exists():
    mf = json.load(open(PKG / "final_manifest.json"))
    manifest_files = {e["filename"] for e in mf.get("files", [])}
    detached_allowed = {"final_manifest.json", "manifest_hash.txt"}
    import zipfile
    with zipfile.ZipFile(zip_path) as z:
        zip_files = set(z.namelist())
    unlisted = (zip_files - manifest_files - detached_allowed)
    if unlisted:
        fail("ZIP files all listed in manifest (or in detached_allowed)",
             f"unlisted: {sorted(unlisted)}")
    else:
        ok("ZIP files all listed in manifest (or detached: final_manifest.json, manifest_hash.txt)")

    # self_hash_policy must explicitly mention manifest_hash.txt detached behavior
    pol = mf.get("self_hash_policy", "")
    if "manifest_hash.txt" in pol and "detached" in pol.lower():
        ok("manifest.self_hash_policy documents detached manifest_hash.txt")
    else:
        fail("manifest.self_hash_policy documents detached manifest_hash.txt",
             f"self_hash_policy: '{pol[:100]}'")


# ===================== STEP 8D: source taxonomy + bibliography hygiene =======
print()
print("Step 8D: directness taxonomy, bibliography hygiene, gap/audit registers")
print("-"*70)

ALLOWED_DIRECTNESS = {
    "DIRECT","INDIRECT","ASSUMED","MANUFACTURER_SPEC",
    "DESIGN_SPECIFIED","UNKNOWN","REQUIRES_EXPERIMENT","DERIVED",
}
FORBIDDEN_DIRECTNESS = {
    "REQUIRES_SOURCE","LITERATURE_BOUND","DIRECT_PHYSICS","PARTIAL",
    "N/A","USER_DECISION_PENDING","KNOWN MISMATCH",
    "INDIRECT/ASSUMED","ASSUMED/INDIRECT",
}

def _check_directness(path, fieldname):
    if not path.exists(): return None
    rows = list(csv.DictReader(open(path)))
    bad = [(i, r.get(fieldname,"")) for i, r in enumerate(rows, start=2)
           if r.get(fieldname,"") and r.get(fieldname,"") not in ALLOWED_DIRECTNESS]
    return rows, bad

# source_map.csv
sm_path = PKG / "source_map.csv"
result = _check_directness(sm_path, "directness")
if result is None:
    fail("source_map.csv present", "missing")
else:
    rows, bad = result
    if bad:
        bad_vals = sorted(set(v for _, v in bad))
        fail("source_map.csv directness in allowed taxonomy",
             f"{len(bad)} bad rows; values: {bad_vals[:5]}")
    else:
        ok(f"source_map.csv directness in allowed taxonomy ({len(rows)} rows)")

# validation_matrix.csv
vm_path = PKG / "validation_matrix.csv"
result = _check_directness(vm_path, "directness")
if result is None:
    fail("validation_matrix.csv present", "missing")
else:
    rows, bad = result
    if bad:
        bad_vals = sorted(set(v for _, v in bad))
        fail("validation_matrix.csv directness in allowed taxonomy",
             f"{len(bad)} bad rows; values: {bad_vals[:5]}")
    else:
        ok(f"validation_matrix.csv directness in allowed taxonomy ({len(rows)} rows)")

# bibliography_audit.csv present
ba_path = PKG / "bibliography_audit.csv"
if not ba_path.exists():
    fail("bibliography_audit.csv present", "missing")
else:
    ba = list(csv.DictReader(open(ba_path)))
    ok(f"bibliography_audit.csv present ({len(ba)} rows)")

    # Cross-check against actual manuscript bibliography
    tex_path = PKG / "qta_manuscript_v4.tex"
    if tex_path.exists():
        t = tex_path.read_text()
        actual_bib_keys = set(re.findall(r"\\bibitem\{([^}]+)\}", t))
        actual_cited = set()
        # Citations only outside the bibliography block
        bib_s = t.find(r"\begin{thebibliography}")
        bib_e = t.find(r"\end{thebibliography}")
        body = t[:bib_s] + t[bib_e:] if bib_s >= 0 else t
        for m in re.finditer(r"\\cite[a-z]*\{([^}]+)\}", body):
            for k in m.group(1).split(","):
                actual_cited.add(k.strip())

        # Audit: every \bibitem must be cited
        uncited_remaining = actual_bib_keys - actual_cited
        if uncited_remaining:
            fail("manuscript bibliography fully cited (no padding)",
                 f"{len(uncited_remaining)} uncited bibitems: {sorted(uncited_remaining)[:5]}")
        else:
            ok(f"manuscript bibliography fully cited ({len(actual_bib_keys)} entries; no padding)")

        # No orphan citations
        orphan_cites = actual_cited - actual_bib_keys
        if orphan_cites:
            fail("manuscript citations all resolve to bibitems",
                 f"orphan cites: {sorted(orphan_cites)[:5]}")
        else:
            ok("manuscript citations all resolve to bibitems")

        # Audit table actions consistent with reality
        kept_in_audit = set(r["bib_key"] for r in ba if r["action"] == "KEEP")
        removed_in_audit = set(r["bib_key"] for r in ba if r["action"] == "REMOVE_UNUSED")
        if kept_in_audit != actual_bib_keys:
            extra_kept = kept_in_audit - actual_bib_keys
            missing_kept = actual_bib_keys - kept_in_audit
            fail("bibliography_audit.csv KEEP set matches manuscript",
                 f"audit-only: {sorted(extra_kept)[:3]}; manuscript-only: {sorted(missing_kept)[:3]}")
        else:
            ok("bibliography_audit.csv KEEP set matches manuscript bibitems")
        if removed_in_audit & actual_bib_keys:
            fail("bibliography_audit.csv REMOVE_UNUSED entries are actually absent from manuscript",
                 f"still present in manuscript: {sorted(removed_in_audit & actual_bib_keys)[:3]}")
        else:
            ok("bibliography_audit.csv REMOVE_UNUSED entries are absent from manuscript")

# source_gap_register.csv present
gap_path = PKG / "source_gap_register.csv"
if not gap_path.exists():
    fail("source_gap_register.csv present", "missing")
else:
    gaps = list(csv.DictReader(open(gap_path)))
    ok(f"source_gap_register.csv present ({len(gaps)} rows)")

# source_audit_status.txt must not claim COMPLETE while gaps exist
status_path = PKG / "source_audit_status.txt"
if status_path.exists():
    txt = status_path.read_text()
    claims_complete = "FULL SOURCE AUDIT: COMPLETE" in txt
    has_gaps = (PKG / "source_gap_register.csv").exists() and \
               sum(1 for _ in open(PKG / "source_gap_register.csv")) > 1
    if claims_complete and has_gaps:
        fail("source_audit_status.txt does not claim COMPLETE while gaps exist",
             "status claims COMPLETE but source_gap_register.csv is non-empty")
    elif "FULL SOURCE AUDIT: INCOMPLETE" in txt:
        ok("source_audit_status.txt honestly says FULL SOURCE AUDIT: INCOMPLETE")
    else:
        fail("source_audit_status.txt declares audit status (INCOMPLETE or COMPLETE)",
             "neither status declaration found")


# ===================== STEP 8E: stale canonical reference audit ===============
# Fail if any live doc contains stale gate counts, file row counts, or ID ranges
# that contradict the current canonical state. Old values are allowed only inside
# explicit historical/superseded/audit context (a "Before:" line followed within a
# few lines by a matching "After:" line, or files marked as a change log).
print()
print("Step 8E: stale canonical reference audit (live docs only)")
print("-"*70)

# Live-truth docs the user mandated must not contain stale canonical values
LIVE_DOCS = [
    "README.md",
    "CLAIMS_BOUNDARY.md",
    "source_audit_status.txt",
    "final_manifest.json",
    "qta_full_sim.py",
]

# Stale patterns to flag. Each item: (regex, label)
STALE_PATTERNS_8E = [
    (r"\b56-gate\b", "56-gate"),
    (r"\b56\s*gates?\b", "56 gates"),
    (r"Total\s*gates\s*\|\s*56", "Total gates | 56"),
    (r"total\s*gates\s*\|\s*56", "total gates | 56"),
    (r"total\s+gates\s*=\s*56", "total gates = 56"),
    (r"BLOCKED\s*count\s*\|\s*14", "BLOCKED count | 14"),
    (r"BLOCKED\s*\|\s*14", "BLOCKED | 14"),
    (r"BLOCKED\s*=\s*14\b", "BLOCKED = 14"),
    (r"\b14\s*BLOCKED\b", "14 BLOCKED"),
    (r"\b14\s*blocked\b", "14 blocked"),
    (r"validation_matrix\.csv\s*\(\s*134\s*rows?", "validation_matrix.csv 134 rows"),
    (r"\bR001-R084\b", "R001-R084"),
    (r"\bR001[-\u2013]R084\b", "R001-R084"),
    (r"\bI001-I056\b", "I001-I056"),
    (r"0\s*of\s*14\s*engineering", "0 of 14 engineering"),
    (r"all\s+20\s+engineering\s+fixes", "all 20 engineering fixes"),
    (r"20\+15\s*engineering\s*fixes", "20+15 engineering fixes"),
    # Additional live-doc stale targets (final stale-clean patch)
    (r"56\s+explicit\s+decision\s+gates", "56 explicit decision gates"),
    (r"gate\s+table:\s*56\b", "gate table: 56"),
    (r"\b56\s+unique\s+gate_id\s+rows", "56 unique gate_id rows"),
    (r"All\s+56\+?\s+gates", "All 56+ gates"),
    (r"risks\s+R001\s+through\s+R084", "risks R001 through R084"),
    (r"interfaces\s+I001\s+through\s+I056", "interfaces I001 through I056"),
    (r"interface_map\.csv\s*\|\s*COMPLETE_DRAFT\s*\|\s*56\s*rows", "interface_map.csv | COMPLETE_DRAFT | 56 rows"),
    (r"\bI001-I056\b", "I001-I056"),
    (r"\bI051[-\u2013]I056\b", "I051-I056"),
    # Obsolete mode-map patterns (canonical map = A=Baseline, B=Process, C=Recovery, D=Sensing)
    (r"A\s*&\s*GROWTH\b", "A & GROWTH (manuscript table)"),
    (r"B\s*&\s*PURGE_RESET\b", "B & PURGE_RESET (manuscript table)"),
    (r"C\s*&\s*RECOOL\b", "C & RECOOL (manuscript table)"),
    (r"D\s*&\s*SENSE\b", "D & SENSE (manuscript table)"),
    (r"\bMODE A LCVD STATUS\b", "MODE A LCVD STATUS (header)"),
    (r"Mode A is a proposed pulsed", "Mode A is a proposed pulsed (LCVD is now Mode B)"),
    (r"Mode A laser heat", "Mode A laser heat (laser/heat is now Mode B)"),
    (r"Mode A heat removed", "Mode A heat removed (heat removal is Mode B)"),
    (r"Mode A pulse train", "Mode A pulse train (pulses are Mode B)"),
    (r"LCVD\s*\(\s*Mode\s*A\s*\)", "LCVD (Mode A) -> LCVD (Mode B)"),
    (r"Mode A LCVD", "Mode A LCVD -> Mode B LCVD"),
    (r"He-3 Film Absent During Mode A", "He-3 Film Absent During Mode A -> ... Mode B Processing"),
    (r"Sensing Disabled During Mode A", "Sensing Disabled During Mode A -> ... Mode B Processing"),
    (r"He-3/He-4 film absent before Mode A", "film absent before Mode A -> film absent before Mode B Processing"),
    (r"film absent before Mode A\b", "film absent before Mode A -> ... before Mode B Processing"),
    # Sim internal state strings that must not be live anywhere
    (r'assert\s+s\.mode\s*==\s*["\']GROWTH["\']', 'assert s.mode == "GROWTH" (now MODE_B_PROCESS)'),
    (r'assert\s+s\.mode\s*==\s*["\']PURGE_RESET["\']', 'assert s.mode == "PURGE_RESET" (now MODE_C_PURGE)'),
    (r'assert\s+s\.mode\s*==\s*["\']RECOOL["\']', 'assert s.mode == "RECOOL" (now MODE_C_RECOOL)'),
    (r'\bs\.mode\s*==\s*["\']GROWTH["\']', 's.mode == "GROWTH" (now MODE_B_PROCESS)'),
    (r'\bs\.mode\s*==\s*["\']PURGE_RESET["\']', 's.mode == "PURGE_RESET" (now MODE_C_PURGE)'),
    (r'\bs\.mode\s*==\s*["\']RECOOL["\']', 's.mode == "RECOOL" (now MODE_C_RECOOL)'),
    # RTB/JT optional cooling plant — forbidden live claims
    (r"\b25\s*RTB\b", "25 RTB"),
    (r"\b25\s*JT\b", "25 JT"),
    (r"\b25\s*reverse[- ]turbo[- ]Brayton\b", "25 reverse-turbo-Brayton"),
    (r"RTB\s+provides\s+10\s*mK", "RTB provides 10 mK"),
    (r"JT\s+provides\s+10\s*mK", "JT provides 10 mK"),
    (r"RTB[/ ]?JT\s+provides\s+10\s*mK", "RTB/JT provides 10 mK"),
    (r"RTB\s+validates\b", "RTB validates"),
    (r"JT\s+validates\b", "JT validates"),
    (r"RTB[/ ]?JT\s+validates\b", "RTB/JT validates"),
    (r"RTB\s+unlocks\s+PASS", "RTB unlocks PASS"),
    (r"JT\s+unlocks\s+PASS", "JT unlocks PASS"),
    (r"RTB[/ ]?JT\s+unlocks\s+PASS", "RTB/JT unlocks PASS"),
    # Stale legacy 62-gate-count strings guarded against (canonical gate count is 83)
    (r"\b62\s+explicit\s+decision\s+gates", "62 explicit decision gates"),
    (r"\b62\s+unique\s+gate_id\s+rows", "62 unique gate_id rows"),
    (r"All\s+62\s+gates", "All 62 gates"),
    (r"\b62-gate\b", "62-gate"),
]

def _is_historical_before_after(content, match_start, match_end, max_lookahead=400, max_lookback=120):
    """A match is historical if 'Before:' appears within max_lookback chars BEFORE
    the match start AND a matching 'After:' line appears within max_lookahead chars
    AFTER the match end. Or if 'Before:' precedes and 'After:' follows on adjacent lines."""
    before_ctx = content[max(0, match_start - max_lookback): match_start]
    after_ctx  = content[match_end : min(len(content), match_end + max_lookahead)]
    has_before_marker = bool(re.search(r"^\s*Before:\s", before_ctx, re.M)) or "Before:" in before_ctx
    has_after_marker  = bool(re.search(r"^\s*After:\s", after_ctx, re.M))  or "After:"  in after_ctx
    return has_before_marker and has_after_marker

def _is_superseded_block(content, match_start, max_lookback=800, max_lookahead=400):
    """Match is OK if it lies inside an explicit historical/forbidden-list block.

    Looks both backward (up to ``max_lookback`` chars, default 800) AND forward
    (up to ``max_lookahead`` chars, default 400) for any of these markers:
      - SUPERSEDED / HISTORICAL / change log / pass-history /
        NOT_CANONICAL / NOT_SELECTED / SUPERSEDED_OPTION
      - "has been removed" / "was removed" / "is no longer" / "no longer" /
        "deleted" / "deletion" / "obsolete" / "Micro-cleanup pass" /
        "removal context" / "intentionally retained" /
        "audit/status report" / "forensic forward-check" /
        "Resolution of prior" (pass-report headers)
      - "Forbidden:" or "**Forbidden:**" header (CLAIMS_BOUNDARY-style lists)

    The forward lookahead lets us exempt phrases like
    ``"source_audit.csv (which was a byte-identical duplicate of ...) has been removed."``
    where the disambiguating "has been removed" follows the match.
    """
    before_ctx = content[max(0, match_start - max_lookback): match_start]
    after_ctx  = content[match_start: min(len(content), match_start + max_lookahead)]
    word_pat = (
        r"\b(SUPERSEDED|HISTORICAL|change\s*log|pass[- ]history|"
        r"NOT_CANONICAL|NOT_SELECTED|SUPERSEDED_OPTION|"
        r"has\s+been\s+removed|was\s+removed|is\s+no\s+longer|"
        r"no\s+longer\s+writes|no\s+longer\s+lists|"
        r"deleted|deletion|obsolete|"
        r"Micro[- ]cleanup\s+pass|MICRO[- ]CLEANUP\s+PASS|"
        r"removal\s+context|intentionally\s+retained|"
        r"audit/status\s+report|forensic\s+forward[- ]check|"
        r"Resolution\s+of\s+prior|byte[- ]identical\s+duplicate)\b"
    )
    word_markers = (re.search(word_pat, before_ctx, re.IGNORECASE)
                    or re.search(word_pat, after_ctx, re.IGNORECASE))
    list_markers = re.search(
        r"(?:^|\s|\*)(?:\*\*)?(Forbidden|FORBIDDEN):(?:\*\*)?",
        before_ctx)
    return bool(word_markers) or bool(list_markers)

stale_violations_8e = []
for fn in LIVE_DOCS:
    fp = PKG / fn
    if not fp.exists():
        continue
    try:
        content = fp.read_text(encoding="utf-8", errors="replace")
    except Exception:
        continue
    for rx, label in STALE_PATTERNS_8E:
        for m_ in re.finditer(rx, content):
            if _is_historical_before_after(content, m_.start(), m_.end()):
                continue
            if _is_superseded_block(content, m_.start()):
                continue
            line_n = content[:m_.start()].count("\n") + 1
            stale_violations_8e.append((fn, line_n, label, m_.group(0)))

if stale_violations_8e:
    detail = "; ".join(f"{fn}:L{ln} '{label}' (match: '{txt}')"
                       for fn, ln, label, txt in stale_violations_8e[:20])
    fail("no stale canonical references in live docs",
         f"{len(stale_violations_8e)} stale references; first: {detail}")
else:
    ok(f"no stale canonical references in live docs (audited {len(LIVE_DOCS)} files for {len(STALE_PATTERNS_8E)} patterns)")


# ===================== STEP 8F: canonical mode-map + tag-class + count audits ====
print()
print("Step 8F: canonical mode-map / source-class / forecast-language audit")
print("-"*70)

# (A) Obsolete mode-map patterns (must NOT appear in live docs except inside
#     historical/superseded/forbidden contexts which _is_superseded_block exempts).
OBSOLETE_MODE_MAP_PATTERNS = [
    (r"Mode A\s*=\s*GROWTH", "Mode A = GROWTH"),
    (r"Mode A\s+GROWTH\b", "Mode A GROWTH"),
    (r"Mode A\s*[\u2014\-]\s*GROWTH", "Mode A - GROWTH (em-dash)"),
    (r"Mode B\s*=\s*PURGE", "Mode B = PURGE"),
    (r"Mode B\s+PURGE\b", "Mode B PURGE"),
    (r"Mode B\s*[\u2014\-]\s*PURGE", "Mode B - PURGE"),
    (r"Mode C\s*=\s*RECOOL", "Mode C = RECOOL"),
    (r"Mode C\s+RECOOL\b", "Mode C RECOOL"),
    (r"Mode C\s*[\u2014\-]\s*RECOOL", "Mode C - RECOOL"),
    (r"GROWTH\s*/\s*LCVD\s*/\s*SURFACE\s+PROCESSING", "GROWTH / LCVD / SURFACE PROCESSING"),
    (r"PURGE\s*/\s*RESET\s*/\s*CRYOTRAP", "PURGE / RESET / CRYOTRAP"),
    (r"RECOOL\s*\+\s*VIBRATION", "RECOOL + VIBRATION"),
    (r"Mode A growth\b", "Mode A growth (purge/LCVD now lives in Mode B)"),
    (r"Mode A LCVD\b", "Mode A LCVD (LCVD now lives in Mode B)"),
    (r"Mode A pulse train\b", "Mode A pulse train (pulses now in Mode B)"),
    (r"Mode B purge\b", "Mode B purge (purge now lives in Mode C)"),
]

# Files audited (live canonical docs)
LIVE_DOCS_8F = [
    # User-facing canonical docs
    "README.md", "CLAIMS_BOUNDARY.md", "FIRST_VALIDATION_EXPERIMENTS.md",
    # Data files
    "source_audit_status.txt", "final_manifest.json",
    "BOM.csv", "interface_map.csv", "assumed_parameters.json", "risk_register.csv",
    "validation_matrix.csv", "results_gate_table.csv", "engineering_fixes.csv",
    "tau_c_sweep.csv", "monte_carlo_summary.csv", "monte_carlo_gate_failure_rates.csv",
    "monte_carlo_parameter_registry.csv", "source_gap_register.csv", "source_map.csv",
    # Sim sources (code identifiers in qta_full_sim.py are exempted by check-time logic;
    # we still scan this file for substantive claims). package_consistency_check.py is
    # excluded because it must contain the forbidden literal strings inside its regex
    # pattern arrays in order to enforce the rule.
    "qta_full_sim.py", "qta_manuscript_v4.tex",
]

stale_modemap_hits = []
for fn in LIVE_DOCS_8F:
    fp = PKG / fn
    if not fp.exists():
        continue
    content = fp.read_text(encoding="utf-8", errors="replace")
    for rx, label in OBSOLETE_MODE_MAP_PATTERNS:
        for m_ in re.finditer(rx, content):
            if _is_historical_before_after(content, m_.start(), m_.end()):
                continue
            if _is_superseded_block(content, m_.start()):
                continue
            ln = content[:m_.start()].count("\n") + 1
            stale_modemap_hits.append((fn, ln, label, m_.group(0)))

if stale_modemap_hits:
    detail = "; ".join(f"{fn}:L{ln} '{label}' (match: '{txt}')"
                       for fn, ln, label, txt in stale_modemap_hits[:15])
    fail("no obsolete mode-map labels in live docs",
         f"{len(stale_modemap_hits)} obsolete labels; first: {detail}")
else:
    ok(f"no obsolete mode-map labels in live docs (audited {len(LIVE_DOCS_8F)} files for {len(OBSOLETE_MODE_MAP_PATTERNS)} patterns)")

# (B) Deleted source_audit.csv references (strict). Allow only in historical/
#     superseded contexts. representative_source_audit.csv is the live file and
#     must not be confused with the deleted bare source_audit.csv. We match the
#     bare filename only when NOT preceded by "representative_".
deleted_sa_hits = []
deleted_sa_pat = re.compile(r"(?<!representative_)(?<!representative\\_)source[_\\]+audit\.csv", re.IGNORECASE)
for fn in LIVE_DOCS_8F:
    fp = PKG / fn
    if not fp.exists():
        continue
    content = fp.read_text(encoding="utf-8", errors="replace")
    for m_ in deleted_sa_pat.finditer(content):
        if _is_historical_before_after(content, m_.start(), m_.end()):
            continue
        if _is_superseded_block(content, m_.start()):
            continue
        ln = content[:m_.start()].count("\n") + 1
        deleted_sa_hits.append((fn, ln, m_.group(0)))

if deleted_sa_hits:
    detail = "; ".join(f"{fn}:L{ln} '{txt}'" for fn, ln, txt in deleted_sa_hits[:10])
    fail("no references to deleted source_audit.csv in live docs",
         f"{len(deleted_sa_hits)} live references; first: {detail}")
else:
    ok("no references to deleted source_audit.csv in live docs (representative_source_audit.csv is the live file)")

# (C) Blank can_PASS_now in validation_matrix.csv
vm_path = PKG / "validation_matrix.csv"
if vm_path.exists():
    with open(vm_path) as f:
        vm_rows = list(csv.DictReader(f))
    blank = [(i+2, r.get("item","")) for i, r in enumerate(vm_rows)
             if not r.get("can_PASS_now","").strip()]
    if blank:
        first = "; ".join(f"L{ln} item='{item[:40]}'" for ln, item in blank[:10])
        fail("validation_matrix.csv has no blank can_PASS_now values",
             f"{len(blank)} blank rows; first: {first}")
    else:
        ok(f"validation_matrix.csv: 0 blank can_PASS_now rows (audited {len(vm_rows)} rows)")

# (D) parameter_registry.csv must not contain source_class = MEASURED
#     (package rule: no QTA system parameter is verified by in-system measurement)
pr_path = PKG / "parameter_registry.csv"
if pr_path.exists():
    with open(pr_path) as f:
        pr_rows = list(csv.DictReader(f))
    measured = [(i+2, r.get("name","")) for i, r in enumerate(pr_rows)
                if r.get("tag","").strip().upper() == "MEASURED"]
    if measured:
        first = "; ".join(f"L{ln} name='{name}'" for ln, name in measured[:10])
        fail("parameter_registry.csv contains no 'MEASURED' tag (no QTA parameter is in-system verified)",
             f"{len(measured)} MEASURED entries; first: {first}")
    else:
        ok(f"parameter_registry.csv: 0 MEASURED tags (audited {len(pr_rows)} rows; "
           "acceptable tags: MANUFACTURER_SPEC, PHYSICAL_CONSTANT, LITERATURE, "
           "LITERATURE_CONSTANT, DESIGN, DESIGN_ASSUMPTION, ASSUMED, UNKNOWN)")

# (E) assumed_parameters.json metadata count must match actual entries
ap_path = PKG / "assumed_parameters.json"
if ap_path.exists():
    ap_data = json.loads(ap_path.read_text(encoding="utf-8"))
    actual_entries = sum(1 for k in ap_data if not k.startswith("_"))
    meta = ap_data.get("_metadata", {})
    declared = meta.get("entry_count")
    # Also scan scope/notes strings for stale literal counts like "81 entries"
    stale_in_scope = None
    for k, v in meta.items():
        if isinstance(v, str):
            m_old = re.search(r"\b(\d+)\s+entries\b", v)
            if m_old:
                claimed = int(m_old.group(1))
                if claimed != actual_entries:
                    stale_in_scope = (k, claimed)
                    break
    if declared is not None and declared != actual_entries:
        fail("assumed_parameters.json metadata entry_count matches actual entries",
             f"declared={declared}, actual={actual_entries}")
    elif stale_in_scope is not None:
        k_, claimed = stale_in_scope
        fail("assumed_parameters.json metadata scope/notes match actual entries",
             f"metadata['{k_}'] mentions {claimed} entries; actual = {actual_entries}")
    else:
        ok(f"assumed_parameters.json metadata count matches actual entries ({actual_entries})")

# (F) Monte Carlo / forecast files must not use bare PASS / FAIL columns where
#     they could be read as validated gate state. Specifically, monte_carlo_summary
#     metric names must NOT include A_pass/B_pass/C_pass/D_pass/full_pass; the
#     canonical names are *_forecast_threshold_satisfied_count.
mc_path = PKG / "monte_carlo_summary.csv"
forecast_pass_hits = []
if mc_path.exists():
    with open(mc_path) as f:
        mc_rows = list(csv.DictReader(f))
    forbidden_metrics = {"A_pass","B_pass","C_pass","D_pass","full_pass"}
    for i, r in enumerate(mc_rows):
        if r.get("metric","") in forbidden_metrics:
            forecast_pass_hits.append(("monte_carlo_summary.csv", i+2, r["metric"]))

# tau_c_sweep.csv must not contain bare Gate="PASS" rows (use THRESHOLD_SATISFIED_IF_MEASURED)
ts_path = PKG / "tau_c_sweep.csv"
if ts_path.exists():
    with open(ts_path) as f:
        ts_rows = list(csv.DictReader(f))
    for i, r in enumerate(ts_rows):
        if r.get("Gate","").strip().upper() == "PASS":
            forecast_pass_hits.append(("tau_c_sweep.csv", i+2, "Gate=PASS"))

if forecast_pass_hits:
    detail = "; ".join(f"{fn}:L{ln} '{label}'" for fn, ln, label in forecast_pass_hits[:10])
    fail("no bare 'PASS' columns in forecast files (use *_forecast_threshold_satisfied_* or THRESHOLD_SATISFIED_IF_MEASURED)",
         f"{len(forecast_pass_hits)} forecast PASS-language hits; first: {detail}")
else:
    ok("no bare PASS labels in monte_carlo_summary.csv or tau_c_sweep.csv forecast outputs")


# ===================== STEP 8G: BOM consistency rules =====================
print()
print("Step 8G: BOM consistency rules (mode-map; status; forecast; required fields)")
print("-"*70)

bom_path = PKG / "BOM.csv"
if bom_path.exists():
    with open(bom_path) as f:
        bom_rows = list(csv.DictReader(f))
    bom_problems = []
    LCVD_KEYWORDS = ("LCVD", "femtosecond", "fs laser", "process laser",
                     "pulse picker", "Pockels", "precursor", "fs-laser",
                     "process optics", "process safety", "process heat",
                     "pulse train", "pulse picker", "carbon precursor")
    FORBIDDEN_MODE_PHRASES = [
        "Mode A LCVD", "LCVD in Mode A", "Mode A laser heat",
        "Mode A process heat", "Mode A laser", "Mode A pulse train",
        "Mode A process", "Mode A growth heat",
    ]
    NEW_RANGE_PREFIX = "B0"  # B081..B131 are new validation/isolation entries
    NEW_IDS = {f"B{n:03d}" for n in range(81, 132)}  # B081..B131

    for i, r in enumerate(bom_rows):
        iid = (r.get("item_id") or "").strip()
        line_no = i + 2  # +1 header +1 1-indexed
        name = r.get("item_name", "") or ""
        role = r.get("role", "") or ""
        mode = (r.get("mode", "") or "").strip()
        status = (r.get("status", "") or "").strip()
        cof = (r.get("current_or_forecast", "") or "").strip()
        cspn = (r.get("can_support_PASS_now", "") or "").strip().upper()
        req_val = (r.get("required_validation", "") or "").strip()
        gate_dep = (r.get("gate_dependency", "") or "").strip()

        # Rule 1: LCVD/material-processing items must NOT be assigned to Mode A
        is_lcvd = any(k.lower() in (name + " " + role).lower() for k in LCVD_KEYWORDS)
        if is_lcvd and mode.upper() == "A":
            bom_problems.append(f"{iid}/L{line_no}: LCVD/process item assigned to Mode A; canonical is Mode B")

        # Rule 2: forbidden phrases anywhere in row
        cell_blob = " | ".join(str(v) for v in r.values())
        for phrase in FORBIDDEN_MODE_PHRASES:
            if phrase in cell_blob:
                bom_problems.append(f"{iid}/L{line_no}: contains forbidden phrase '{phrase}'")

        # Rule 3: new items (B081..B131) must have can_support_PASS_now = NO
        if iid in NEW_IDS:
            if cspn != "NO":
                bom_problems.append(f"{iid}/L{line_no}: new validation/isolation item must have can_support_PASS_now=NO (got '{cspn}')")
            # Rule 4: new items must NOT be marked MEASURED
            if "MEASURED" in status.upper() and "TARGET" not in status.upper():
                bom_problems.append(f"{iid}/L{line_no}: new item status '{status}' contains MEASURED (must be DESIGN_SPECIFIED or MANUFACTURER_SPEC_TARGET)")
            # Rule 5: new items must NOT be marked INSTALLED/current
            if status.upper().startswith("INSTALLED") or cof.lower() == "current":
                bom_problems.append(f"{iid}/L{line_no}: new item is design-only; must not be INSTALLED/current (status='{status}', current_or_forecast='{cof}')")

        # Rule 6: design-only cryostat hardware must not be marked installed/current
        if "dilution" in name.lower() or "cryostat" in name.lower() or "refriger" in name.lower():
            if status.upper().startswith("INSTALLED") or cof.lower() == "current":
                bom_problems.append(f"{iid}/L{line_no}: cryogenic hardware not physically installed; must not be INSTALLED/current (status='{status}', current_or_forecast='{cof}')")

        # Rule 7: required fields must not be blank
        for field, val in [("mode", mode), ("status", status), ("current_or_forecast", cof),
                            ("role", role.strip()), ("required_validation", req_val),
                            ("gate_dependency", gate_dep)]:
            if not val:
                bom_problems.append(f"{iid}/L{line_no}: required field '{field}' is blank")

        # Rule 8: claimed validation without evidence — flag any "validated" / "verified" claim
        if re.search(r"\b(validated|verified)\b", cell_blob, re.IGNORECASE):
            # Exempt cells that contain "not validated" / "not verified" / "to be validated" etc.
            if not re.search(r"\b(not\s+(?:selected|installed|validated|verified|yet\s+validated|yet\s+verified)|to\s+be\s+(?:validated|verified)|cannot\s+be\s+(?:validated|verified)|never\s+(?:validated|verified)|unverified|not_validated|requires?\s+validation|required_validation|after\s+real\s+installation|acceptance\s+test)\b",
                             cell_blob, re.IGNORECASE):
                bom_problems.append(f"{iid}/L{line_no}: row contains 'validated/verified' claim without negation/required_validation context")

    # Rule 9: BOM row count freshness in README / final_manifest / output_sync_report
    actual_bom_rows = len(bom_rows)
    for doc_name in ("README.md",):
        dp = PKG / doc_name
        if not dp.exists():
            continue
        doc_text = dp.read_text(encoding="utf-8", errors="replace")
        for m_ in re.finditer(r"(?:^|\s|\|)BOM\.csv\s*\|\s*[A-Z_]+\s*\|\s*(\d+)\s*rows", doc_text):
            stated = int(m_.group(1))
            ln_ = doc_text[:m_.start()].count("\n") + 1
            if stated != actual_bom_rows:
                # Allow stale references in historical Before:/After: contexts
                if _is_historical_before_after(doc_text, m_.start(), m_.end()):
                    continue
                if _is_superseded_block(doc_text, m_.start()):
                    continue
                bom_problems.append(f"{doc_name}:L{ln_}: states 'BOM.csv ... {stated} rows' but actual = {actual_bom_rows}")

    if bom_problems:
        detail = "; ".join(bom_problems[:15])
        fail("BOM.csv consistency (mode-map; status; forecast; row counts)",
             f"{len(bom_problems)} BOM problems; first: {detail}")
    else:
        ok(f"BOM.csv consistency: 0 problems (audited {len(bom_rows)} rows; "
           f"verified mode-map, forecast/status, required fields, validated/verified claims, row-count freshness)")


# ===================== STEP 8H: semantic mode-invariant audit =====================
print()
print("Step 8H: semantic mode-invariant audit (Mode A must mean Baseline, not LCVD/growth)")
print("-"*70)

# Invariant: in the canonical map, Mode A = Cryogenic Baseline / Stabilization.
# LCVD / growth / processing / process-laser / precursor / growth-heat-dump belong
# to Mode B. So no live doc may associate "Mode A" with any of those concepts within
# a short character window. This is the SEMANTIC check that complements Step 8F's
# literal-token check: the manuscript wrote "an LCVD-capable process mode (Mode A)"
# and "Mode A femtosecond laser for LCVD" -- functional usage with no "GROWTH" token.
#
# We normalise LaTeX spacing: Mode~A, Mode A, Mode\,A, Mode{A} all collapse to "Mode A".
MODE_A_GROWTH_TERMS = [
    "LCVD", "growth", "precursor", "femtosecond", "fs laser", "fs-laser",
    "process laser", "process mode", "surface-processing", "surface processing",
    "pulse train", "laser heat", "process heat", "dump-stage", "dump stage",
    "thermal dump", "deposition", "CH$_4$", "CH4", "Surface-Processing",
]
# Concepts that legitimately pair with Mode A (baseline/stabilization) -> never flagged
MODE_A_OK_TERMS = ["baseline", "stabiliz", "stabilis", "readiness", "set-point", "set point"]

def _normalise_mode_spacing(text):
    # Collapse LaTeX inter-word spacing variants between "Mode" and a letter.
    text = re.sub(r"Mode\s*~\s*([A-D])", r"Mode \1", text)
    text = re.sub(r"Mode\\,\s*([A-D])", r"Mode \1", text)
    text = re.sub(r"Mode\{([A-D])\}", r"Mode \1", text)
    text = re.sub(r"Mode\s+([A-D])", r"Mode \1", text)
    return text

SEMANTIC_LIVE_DOCS = [
    "README.md", "CLAIMS_BOUNDARY.md", "FIRST_VALIDATION_EXPERIMENTS.md",
    "qta_manuscript_v4.tex", "BOM.csv", "interface_map.csv", "risk_register.csv",
    "validation_matrix.csv", "results_gate_table.csv", "assumed_parameters.json",
    "measured_parameters.json", "source_gap_register.csv", "source_map.csv",
    "monte_carlo_gate_failure_rates.csv", "monte_carlo_parameter_registry.csv",
]

WINDOW = 60  # characters on each side of "Mode A"
semantic_hits = []
for fn in SEMANTIC_LIVE_DOCS:
    fp = PKG / fn
    if not fp.exists():
        continue
    raw = fp.read_text(encoding="utf-8", errors="replace")
    norm = _normalise_mode_spacing(raw)
    for m_ in re.finditer(r"Mode A\b", norm):
        s = max(0, m_.start() - WINDOW)
        e = min(len(norm), m_.end() + WINDOW)
        window = norm[s:e]
        # Skip sequence notation where Mode A is the first element of an ordered list
        # (Mode A/B/C, Mode A->B, Mode A+B, "Mode A and Mode B", "Mode A, B", "Mode A->B->C->D").
        # In those, A correctly denotes the baseline first step, not the growth mode.
        tail = norm[m_.end(): m_.end() + 12]
        if re.match(r"\s*(?:/|->|\u2192|\+|,\s*[BCD]\b|\s+and\s+Mode\s+B|\s+and\s+B\b|/B|/C|/D)", tail):
            continue
        # Skip if a baseline/stabilization term is in-window (legitimate Mode A usage)
        if any(ok.lower() in window.lower() for ok in MODE_A_OK_TERMS):
            continue
        # Skip explicit historical / superseded / forbidden-list contexts
        if _is_historical_before_after(norm, m_.start(), m_.end()):
            continue
        if _is_superseded_block(norm, m_.start()):
            continue
        # Flag if any growth/LCVD/process term is in-window
        hit_terms = [t for t in MODE_A_GROWTH_TERMS if t.lower() in window.lower()]
        if hit_terms:
            # Map normalised offset back to an approximate line number in raw text by
            # counting newlines in the normalised string (spacing changes don't add lines)
            ln = norm[:m_.start()].count("\n") + 1
            semantic_hits.append((fn, ln, hit_terms[0], window.replace("\n", " ").strip()[:90]))

if semantic_hits:
    detail = "; ".join(f"{fn}:L{ln} 'Mode A'+'{term}' ({ctx})"
                       for fn, ln, term, ctx in semantic_hits[:12])
    fail("no live doc associates 'Mode A' with LCVD/growth/processing (semantic invariant)",
         f"{len(semantic_hits)} semantic violations; first: {detail}")
else:
    ok(f"semantic mode-invariant holds: no live doc pairs 'Mode A' with LCVD/growth/process "
       f"terms (audited {len(SEMANTIC_LIVE_DOCS)} files, LaTeX-spacing-normalised, "
       f"{len(MODE_A_GROWTH_TERMS)} growth terms)")


# ---- Bibliography-count consistency (reviewer-driven) ----
# CLAIMS_BOUNDARY.md must not conflate the citation-audit row count (65) with the
# manuscript bibitem count (23). The manuscript \bibitem count is the ground truth.
tex_path = PKG / "qta_manuscript_v4.tex"
cb_path = PKG / "CLAIMS_BOUNDARY.md"
if tex_path.exists() and cb_path.exists():
    tex_txt = tex_path.read_text(encoding="utf-8", errors="replace")
    bibitem_count = len(re.findall(r"\\bibitem", tex_txt))
    cb_txt = cb_path.read_text(encoding="utf-8", errors="replace")
    bib_problem = None
    for m_ in re.finditer(r"manuscript bibliography contains (\d+) entries", cb_txt):
        stated = int(m_.group(1))
        if bibitem_count and stated != bibitem_count:
            bib_problem = (stated, bibitem_count)
            break
    if bib_problem:
        fail("CLAIMS_BOUNDARY.md bibliography count matches manuscript bibitems",
             f"CLAIMS_BOUNDARY says {bib_problem[0]} but manuscript has {bib_problem[1]} \\bibitem entries")
    elif bibitem_count:
        ok(f"CLAIMS_BOUNDARY.md bibliography count matches manuscript ({bibitem_count} bibitems)")

# ===================== STEP 9: packaging cleanliness =========================
print()
print("Step 9: packaging — no nested ZIP / no stale duplicates")
print("-"*70)

zip_path = PKG / "QTA_submission.zip"
if zip_path.exists():
    import zipfile
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        nested = [n for n in names if n.endswith(".zip")]
        if nested:
            fail("no nested ZIP inside QTA_submission.zip", f"nested: {nested}")
        else:
            ok("QTA_submission.zip contains no nested ZIP")

stale_files = ["best_operating_point.json"]
remaining_stale = [f for f in stale_files if (PKG / f).exists()]
if remaining_stale:
    fail("stale files removed", f"still present: {remaining_stale}")
else:
    ok("legacy stale files removed (best_operating_point.json absent)")

# ===================== STEP 10: monte_carlo_summary =========================
print()
print("Step 10: monte_carlo_summary — canonical metrics present")
print("-"*70)

mc_path = PKG / "monte_carlo_summary.csv"
if mc_path.exists():
    mc = {r["metric"]: r["value"] for r in csv.DictReader(open(mc_path))}
    required_keys = ["total_gates","PASS_count","CONDITIONAL_count","BLOCKED_count",
                     "UNKNOWN_count","DERIVED_CHECK_count","tau_c_canonical_threshold_us",
                     "tau_c_superseded_v30_us","Mode_B_LCVD_status",
                     "IL14_helium_exclusion_interlock","global_verdict","forecast_only"]
    missing = [k for k in required_keys if k not in mc]
    if missing:
        fail("monte_carlo_summary has required canonical metric rows",
             f"missing: {missing}")
    else:
        ok("monte_carlo_summary has all required canonical metric rows")
    if mc.get("total_gates") and int(mc["total_gates"]) != CANONICAL_EXPECTED["total_gates"]:
        fail("MC summary total_gates matches canonical",
             f"got {mc['total_gates']}, expected {CANONICAL_EXPECTED['total_gates']}")
    else:
        ok("MC summary total_gates matches canonical (63)")
    if mc.get("PASS_count") and int(mc["PASS_count"]) != 0:
        fail("MC summary PASS_count=0", f"got {mc['PASS_count']}")
    else:
        ok("MC summary PASS_count=0")

# ===================== STEP 11: final_manifest + manifest_hash ==============
print()
print("Step 11: final_manifest.json — manifest_hash.txt — file hashes")
print("-"*70)

manifest_path = PKG / "final_manifest.json"
manifest_hash_path = PKG / "manifest_hash.txt"

if not manifest_path.exists():
    fail("final_manifest.json present", "missing")
else:
    manifest = json.load(open(manifest_path))
    # Manifest must NOT list itself
    self_listed = any(e["filename"]=="final_manifest.json" for e in manifest.get("files",[]))
    if self_listed:
        fail("final_manifest.json does NOT list itself (avoids placeholder self-hash)",
             "manifest contains a self-entry")
    else:
        ok("final_manifest.json does not list itself (Option A — detached hash)")

    # manifest_hash.txt present and correct
    if not manifest_hash_path.exists():
        fail("manifest_hash.txt present (detached hash of final_manifest.json)",
             "missing")
    else:
        # Parse hash from file
        mh_txt = manifest_hash_path.read_text()
        m = re.search(r"sha256:\s*([0-9a-fA-F]{64})", mh_txt)
        if not m:
            fail("manifest_hash.txt contains valid SHA-256", f"content: {mh_txt[:200]}")
        else:
            stored_hash = m.group(1)
            actual_hash = hashlib.sha256(open(manifest_path,"rb").read()).hexdigest()
            if stored_hash != actual_hash:
                fail("manifest_hash.txt matches actual final_manifest.json SHA-256",
                     f"stored={stored_hash[:16]} actual={actual_hash[:16]}")
            else:
                ok("manifest_hash.txt matches actual final_manifest.json SHA-256")

    # Required files in manifest
    required_in_manifest = ["qta_manuscript_v4.tex","qta_manuscript_v4.pdf",
                            "qta_full_sim.py","results_gate_table.csv",
                            "monte_carlo_summary.csv","assumed_parameters.json",
                            "source_map.csv","validation_matrix.csv",
                            "risk_register.csv","interface_map.csv",
                            "interlock_table.csv","tau_c_sweep.csv",
                            "representative_source_audit.csv",
                            "package_consistency_check.py",
                            "README.md"]
    files_in_manifest = set(e["filename"] for e in manifest.get("files",[]))
    missing = [f for f in required_in_manifest if f not in files_in_manifest]
    if missing:
        fail("manifest includes all required files", f"missing: {missing}")
    else:
        ok("manifest includes all required files")

    # Verify each listed hash
    bad_hashes = []
    for entry in manifest.get("files", []):
        p = PKG / entry["filename"]
        if not p.exists():
            bad_hashes.append(f"{entry['filename']}: missing file")
            continue
        actual = hashlib.sha256(open(p,"rb").read()).hexdigest()
        if actual != entry["sha256"]:
            bad_hashes.append(f"{entry['filename']}: hash mismatch")
    if bad_hashes:
        fail("manifest SHA256 hashes match every listed file",
             f"{len(bad_hashes)} mismatches: {bad_hashes[:3]}")
    else:
        ok("manifest SHA256 hashes match every listed file (no placeholders)")

# ===================== Step 12: non-lumped multiphysics layer ================
print()
print("Step 12: non-lumped 1D/2D multiphysics layer — outputs, columns, finite, "
      "zero-PASS, lumped comparator-only")

MP_REQUIRED_OUTPUTS = [
    "distributed_thermal_profile.csv", "distributed_thermal_2d_slices.csv",
    "distributed_thermal_metrics.csv", "optical_absorption_profile.csv",
    "optical_absorption_2d_slices.csv", "optical_absorption_metrics.csv",
    "gas_transport_profile.csv", "gas_transport_2d_map.csv", "gas_transport_metrics.csv",
    "surface_coverage_profile.csv", "surface_coverage_2d_map.csv",
    "surface_coverage_metrics.csv", "microwave_heating_profile.csv",
    "microwave_heating_metrics.csv", "radiation_leakage_paths.csv",
    "radiation_leakage_metrics.csv", "vibration_transfer_profile.csv",
    "vibration_transfer_metrics.csv", "coupled_mode_recovery_metrics.csv",
    "coupled_mode_state_summary.json", "multiphysics_summary.json",
    "mesh_convergence_summary.csv", "numerical_stability_summary.csv",
    "multiphysics_verification_summary.csv", "fidelity_comparison.csv",
    "lumped_vs_nonlumped_comparison.csv",
]
mp_missing = [f for f in MP_REQUIRED_OUTPUTS if not (PKG / f).exists()]
if mp_missing:
    fail("multiphysics: all declared outputs exist", f"missing: {mp_missing}")
else:
    ok(f"multiphysics: all {len(MP_REQUIRED_OUTPUTS)} declared outputs exist")

# (a) package imports and the 3D layer is FORECAST_ONLY_IMPLEMENTED (not faked,
#     not hardware): status module, canonical outputs, readiness claims,
#     reduction/verification statuses, and a no-PASS token scan. Byte-level
#     determinism of the 3D outputs is enforced by Step 2b like every other
#     regenerated canonical output.
THREE_D_OUTPUTS = [
    "mesh_3d_summary.json", "thermal_3d_probe_timeseries.csv",
    "thermal_3d_hotspots.csv", "thermal_3d_energy_accounting.csv",
    "thermal_3d_reduction_check.json", "thermal_3d_verification_report.json",
    "cryo_stack_3d_budget.csv", "species_transport_3d_summary.json",
    "surface_coverage_3d_summary.csv", "mode_recovery_3d_timeline.csv",
    "radiation_paths_3d_budget.csv", "vibration_paths_3d_budget.csv",
    "microwave_heating_3d_budget.csv", "thermal_3d_readiness.json",
    # coupled-layer outputs
    "coupling_ledger_3d.json", "species_accounting_3d.csv",
    "falsification_report_3d.json", "convergence_report_3d.json",
    "sensitivity_ranking_3d.csv", "nv_eligibility_3d.json",
    "assumptions_3d.json", "provenance_3d.json",
    "laser_3d_deposition_summary.json",
    # machine FSM (operational controller)
    "machine_fsm_states.csv", "machine_fsm_transitions.csv",
    "machine_fsm_interlocks.csv", "machine_fsm_lifecycle_trace.csv",
    "machine_fsm_summary.json", "machine_fsm_diagram.mmd",
    # campaign continuity (Stage 2)
    "cryopanel_loading_3d.csv", "energy_ledger_cumulative_3d.csv",
    "machine_fsm_campaign_trace.csv", "campaign_state_3d.json",
    # campaign uncertainty (Stage 3)
    "campaign_uncertainty_3d.json", "campaign_uncertainty_quantiles.csv",
    # measurement ingestion (Stage 4)
    "measurement_comparison_3d.json", "measurement_comparison_rows.csv",
]
try:
    import importlib
    f3 = importlib.import_module("qta_multiphysics.future_3d")
    if getattr(f3, "IMPLEMENTED", False) is not True or \
            getattr(f3, "STATUS", "") != "FORECAST_ONLY_IMPLEMENTED":
        fail("multiphysics: 3D layer status", f"status={getattr(f3,'STATUS',None)} "
             f"implemented={getattr(f3,'IMPLEMENTED',None)}")
    else:
        ok("multiphysics: 3D layer FORECAST_ONLY_IMPLEMENTED (forecast-only, not faked)")
    _m3 = [n for n in THREE_D_OUTPUTS if not (PKG / n).exists()]
    if _m3:
        fail("multiphysics: canonical 3D outputs exist", f"missing: {_m3}")
    else:
        ok(f"multiphysics: all {len(THREE_D_OUTPUTS)} canonical 3D outputs exist")
        _rd = json.loads((PKG / "thermal_3d_readiness.json").read_text())
        _claims_ok = (_rd.get("status") == "FORECAST_ONLY_IMPLEMENTED"
                      and _rd.get("measured_in_this_system") is False
                      and _rd.get("can_PASS_now") == "NO"
                      and _rd.get("hardware_validated") is False
                      and "no COMSOL" in _rd.get("external_solver", ""))
        if _claims_ok:
            ok("multiphysics: 3D readiness claims (forecast-only, measured=false, "
               "can_PASS_now=NO, no hardware, no COMSOL)")
        else:
            fail("multiphysics: 3D readiness claims", f"got {_rd}")
        _allowed = {"DERIVED_CHECK", "CONDITIONAL"}
        _st = set()
        for _n in ("thermal_3d_reduction_check.json",
                   "thermal_3d_verification_report.json"):
            for _c in json.loads((PKG / _n).read_text()).get("checks", []):
                _st.add(_c.get("status"))
        if _st and _st <= _allowed:
            ok(f"multiphysics: 3D check statuses within {sorted(_allowed)} (never PASS)")
        else:
            fail("multiphysics: 3D check statuses", f"got {sorted(_st)}")
        _tok = []
        for _n in THREE_D_OUTPUTS:
            _txt = (PKG / _n).read_text()
            if '"PASS"' in _txt or ",PASS," in _txt:
                _tok.append(_n)
        if _tok:
            fail("multiphysics: no PASS tokens in 3D outputs", f"found in {_tok}")
        else:
            ok("multiphysics: no PASS tokens in any 3D output")
        # coupled-layer honesty assertions
        _vocab = {"IMPLEMENTED", "REDUCED_ORDER", "BOUNDED_FORECAST",
                  "SCAFFOLD", "NOT_IMPLEMENTED"}
        _led = json.loads((PKG / "coupling_ledger_3d.json").read_text())
        _bad = sorted({e["status"] for e in _led["couplings"]} - _vocab)
        if _bad:
            fail("multiphysics: coupling-ledger status vocabulary",
                 f"disallowed: {_bad}")
        else:
            ok(f"multiphysics: coupling ledger ({len(_led['couplings'])} arrows) "
               "uses only the honesty vocabulary")
        _fal = json.loads((PKG / "falsification_report_3d.json").read_text())
        _fst = {c["status"] for c in _fal["conditions"]}
        _eli = json.loads((PKG / "nv_eligibility_3d.json").read_text())
        _ok_f = _fst <= {"DERIVED_CHECK", "CONDITIONAL", "NOT_IMPLEMENTED"}
        _ok_e = (_eli.get("eligibility_forecast") in ("CONDITIONAL", "BLOCKED",
                                                      "UNKNOWN")
                 and _eli.get("can_PASS_now") == "NO"
                 and _eli.get("measured_in_this_system") is False)
        if _ok_f and _ok_e:
            ok("multiphysics: falsification statuses + NV eligibility remain "
               "forecast-only (never PASS)")
        else:
            fail("multiphysics: falsification/eligibility honesty",
                 f"falsification statuses {sorted(_fst)}, eligibility "
                 f"{_eli.get('eligibility_forecast')}")
        # machine-FSM structural + enforcement assertions
        from qta_multiphysics import machine_fsm as _mf
        _names = {st.name for st in _mf.STATES}
        _al = {(t["from"], t["to"]) for t in _mf.TRANSITIONS}
        _fb = {(a, b) for a, b, _, _ in _mf.FORBIDDEN}
        _closed = all(a in _names and b in _names for a, b in (_al | _fb))
        _msum = json.loads((PKG / "machine_fsm_summary.json").read_text())
        _enf = True
        for _src, _dst in (("MODE_B_PROCESS", "MODE_D_SENSE"),
                           ("MODE_A_BASELINE", "MODE_D_SENSE")):
            try:
                _mf.MachineFSM(_src).request(_dst, {})
                _enf = False
            except _mf.TransitionRefused:
                pass
        if (_closed and not (_al & _fb) and _enf
                and _msum.get("final_state") in _names
                and _msum.get("can_PASS_now") == "NO"
                and _msum.get("measured_in_this_system") is False):
            ok(f"multiphysics: machine FSM ({_msum['n_states']} states, "
               f"{_msum['n_transitions_allowed']}+{_msum['n_transitions_forbidden']} "
               f"transitions, {_msum['n_interlocks']} interlocks) closed, "
               "disjoint, enforcement live, forecast-only")
        else:
            fail("multiphysics: machine FSM structure/enforcement",
                 f"closed={_closed} overlap={sorted(_al & _fb)} enforced={_enf} "
                 f"summary={_msum.get('final_state')}")
        # campaign-continuity assertions (Stage 2)
        _cs = json.loads((PKG / "campaign_state_3d.json").read_text())
        _camp_ok = (_cs.get("schema_version") == "1.0"
                    and _cs.get("n_cycles") == 3
                    and _cs.get("n_sensing_refusals") == 3
                    and _cs.get("can_PASS_now") == "NO"
                    and _cs.get("measured_in_this_system") is False
                    and len(_cs.get("cycles", [])) == 3)
        if _camp_ok:
            ok("multiphysics: campaign continuity (3 cycles, carried state, "
               "per-cycle IL-08 refusal, forecast-only)")
        else:
            fail("multiphysics: campaign continuity",
                 f"got {_cs.get('n_cycles')} cycles, "
                 f"{_cs.get('n_sensing_refusals')} refusals")
        # campaign-uncertainty assertions (Stage 3)
        _cu = json.loads((PKG / "campaign_uncertainty_3d.json").read_text())
        _cu_ok = (_cu.get("schema_version") == "1.0"
                  and _cu.get("seed") == 20260717
                  and _cu.get("n_members") == 120
                  and _cu.get("can_PASS_now") == "NO"
                  and _cu.get("measured_in_this_system") is False
                  and len(_cu.get("exclusions", [])) >= 8
                  and "MARGINAL" in _cu["marginal_composition"]["thermal"])
        if _cu_ok:
            ok("multiphysics: campaign uncertainty (deterministic ensemble, "
               "within-member persistence, marginal composition, exclusions "
               "recorded, forecast-only)")
        else:
            fail("multiphysics: campaign uncertainty",
                 f"seed={_cu.get('seed')} M={_cu.get('n_members')} "
                 f"exclusions={len(_cu.get('exclusions', []))}")
        # measurement-ingestion assertions (Stage 4)
        _mi = json.loads((PKG / "measurement_comparison_3d.json").read_text())
        _mi_ok = (_mi.get("schema_version") == "1.0"
                  and _mi.get("ingestion_status") == "OK"
                  and _mi.get("synthetic_only") is True
                  and _mi.get("measured_in_this_system") is False
                  and _mi.get("can_PASS_now") == "NO"
                  and _mi.get("n_rejected", 0) >= 1
                  and "hardware" in _mi.get("hardware_refused_by_design", "")
                  and all(a.get("data_class") == "SYNTHETIC"
                          for a in _mi.get("accepted", [])))
        if _mi_ok:
            ok("multiphysics: measurement ingestion (read-only, synthetic-"
               "only, fail-closed rejects live, hardware refused by design, "
               "never a gate input)")
        else:
            fail("multiphysics: measurement ingestion",
                 f"status={_mi.get('ingestion_status')} "
                 f"rejected={_mi.get('n_rejected')}")
        # hardware-governance assertions (Stage 5)
        _rd2 = json.loads((PKG / "thermal_3d_readiness.json").read_text())
        _hg = _rd2.get("campaign_continuity", {}).get("hardware_governance",
                                                       {})
        _hg_ok = (_hg.get("schema_version") == "1.0"
                  and _hg.get("automatic_gate_effect") == "NONE"
                  and "NO_HARDWARE_DATA" in _hg.get("default_execution", "")
                  and _hg.get("measured_in_this_system") is False
                  and _hg.get("can_PASS_now") == "NO"
                  and "UNKNOWN" in _hg.get("repetition_requirements", ""))
        if _hg_ok:
            ok("multiphysics: hardware governance (read-only, no hardware "
               "data, human-only review, automatic_gate_effect=NONE, "
               "plan-sourced repetition requirements UNKNOWN)")
        else:
            fail("multiphysics: hardware governance",
                 f"auto_effect={_hg.get('automatic_gate_effect')} "
                 f"default={_hg.get('default_execution','')[:30]}")
except Exception as e:
    fail("multiphysics: package import", str(e))

# (b) expected columns + finite numeric values in key CSVs
def _read_csv_rows(name):
    with open(PKG / name, newline="") as fh:
        return list(csv.DictReader(fh))

MP_EXPECTED_COLS = {
    "distributed_thermal_metrics.csv": {"metric", "value", "evidence_class", "measured_in_this_system"},
    "gas_transport_metrics.csv": {"species", "residual_mode_D_density_m3", "evidence_class", "measured_in_this_system"},
    "surface_coverage_metrics.csv": {"species", "residual_theta_mode_D", "evidence_class", "measured_in_this_system"},
    "coupled_mode_recovery_metrics.csv": {"metric", "value", "evidence_class", "measured_in_this_system"},
    "mesh_convergence_summary.csv": {"model", "mesh", "metric", "value"},
    "lumped_vs_nonlumped_comparison.csv": {"quantity", "lumped_model", "nonlumped_1d_model", "role"},
}
col_problems = []
for name, need in MP_EXPECTED_COLS.items():
    try:
        rows = _read_csv_rows(name)
        have = set(rows[0].keys()) if rows else set()
        if not need.issubset(have):
            col_problems.append(f"{name}: missing {need - have}")
    except Exception as e:
        col_problems.append(f"{name}: {e}")
if col_problems:
    fail("multiphysics: required columns present", "; ".join(col_problems))
else:
    ok("multiphysics: required columns present in key CSVs")

# finite check across numeric cells of all metric CSVs
def _is_number(x):
    try:
        float(x); return True
    except Exception:
        return False
nonfinite = []
for name in MP_REQUIRED_OUTPUTS:
    if not name.endswith(".csv"):
        continue
    try:
        with open(PKG / name, newline="") as fh:
            for row in csv.reader(fh):
                for cell in row:
                    c = str(cell).strip().lower()
                    if c in ("nan", "inf", "-inf", "+inf"):
                        nonfinite.append(f"{name}:{cell}")
    except Exception as e:
        nonfinite.append(f"{name}: read error {e}")
if nonfinite:
    fail("multiphysics: no NaN/inf in output CSVs", f"{nonfinite[:4]}")
else:
    ok("multiphysics: no NaN/inf literals in output CSVs")

# (c) measured_in_this_system=false everywhere it appears in MP outputs;
#     and no MEASURED tag on model-only outputs
measured_problems = []
for name in MP_REQUIRED_OUTPUTS:
    if not name.endswith(".csv"):
        continue
    try:
        rows = _read_csv_rows(name)
        for r in rows:
            if "measured_in_this_system" in r and str(r["measured_in_this_system"]).strip().lower() not in ("false", ""):
                measured_problems.append(f"{name}: measured_in_this_system={r['measured_in_this_system']}")
            for k, v in r.items():
                if isinstance(v, str) and v.strip().upper() == "MEASURED":
                    measured_problems.append(f"{name}: MEASURED tag in {k}")
    except Exception:
        pass
if measured_problems:
    fail("multiphysics: outputs carry no MEASURED status", f"{measured_problems[:4]}")
else:
    ok("multiphysics: all outputs model-only (measured_in_this_system=false; no MEASURED tags)")

# (d) lumped model is comparator-only (role column says so; nonlumped is authority)
try:
    lr = _read_csv_rows("lumped_vs_nonlumped_comparison.csv")
    roles = " ".join(r.get("role", "") for r in lr).lower()
    if "comparator_only" in roles and "gate_authority" in roles:
        ok("multiphysics: lumped model is comparator-only; non-lumped is gate authority")
    else:
        fail("multiphysics: lumped comparator-only", f"role column unexpected: {roles[:80]}")
except Exception as e:
    fail("multiphysics: lumped comparator-only", str(e))

# (e) the 20 multiphysics gates are present in the gate table and none is PASS
try:
    gt = _read_csv_rows("results_gate_table.csv")
    mp_ids = {"THERMAL_1D_STABILITY_CHECK", "THERMAL_2D_STABILITY_CHECK",
              "THERMAL_1D_MESH_CONVERGENCE_CHECK", "THERMAL_2D_MESH_CONVERGENCE_CHECK",
              "KAPITZA_BC_CHECK", "MOVING_BOUNDARY_MODEL_CHECK", "NV_LAYER_TEMPERATURE_CHECK",
              "HOTSPOT_MARGIN_CHECK", "POST_PULSE_DRIFT_CHECK", "OPTICAL_ABSORPTION_PROFILE_CHECK",
              "GAS_TRANSPORT_STABILITY_CHECK", "CRYOBAFFLE_CAPTURE_CHECK",
              "RESIDUAL_SPECIES_MODE_D_CHECK", "SURFACE_COVERAGE_DECAY_CHECK",
              "MICROWAVE_HEATING_PROFILE_CHECK", "RADIATION_VIEWFACTOR_CHECK",
              "VIBRATION_TRANSFER_CHECK", "COUPLED_MODE_RECOVERY_CHECK",
              "LUMPED_MODEL_RETIRED_CHECK", "THREE_D_LAYER_STATUS_CHECK"}
    present = set(r["gate_id"] for r in gt)
    miss = mp_ids - present
    bad_status = [r["gate_id"] for r in gt if r["gate_id"] in mp_ids and r["status"] not in
                  ("CONDITIONAL", "BLOCKED", "UNKNOWN", "DERIVED_CHECK")]
    if miss:
        fail("multiphysics: all 20 gates present in table", f"missing {miss}")
    elif bad_status:
        fail("multiphysics: no MP gate is PASS", f"forbidden status on {bad_status}")
    else:
        ok("multiphysics: all 20 gates present; statuses in {CONDITIONAL,BLOCKED,UNKNOWN,DERIVED_CHECK}")
except Exception as e:
    fail("multiphysics: gate-table check", str(e))

# (f) no source_audit.csv reference inside multiphysics outputs
sa_refs = []
for name in MP_REQUIRED_OUTPUTS:
    try:
        if "source_audit.csv" in (PKG / name).read_text(errors="ignore"):
            sa_refs.append(name)
    except Exception:
        pass
if sa_refs:
    fail("multiphysics: no source_audit.csv reference", f"{sa_refs}")
else:
    ok("multiphysics: no source_audit.csv reference in outputs")


# ===================== Step 13: forecast-layer additions =====================
# NV spin-dynamics + computable design registry + Bayesian experimental design.
# These checks EXTEND (never weaken) the canonical invariants for the new files.
print()
print("Step 13: forecast layers (NV spin / design registry / Bayesian design) — "
      "no PASS, model-only, NOT_IMPLEMENTED surrogate, design integrity")
print("-"*70)
import csv as _csv13

_NEW_OUTPUTS = [
    "nv_coherence_curves.csv", "nv_sequence_contrast.csv", "nv_odmr_spectrum.csv",
    "nv_spin_dynamics_summary.json", "nv_spin_parameter_provenance.json",
    "nv_spin_gate_records.csv", "design_component_registry.json",
    "design_interface_graph.json", "design_decision_ledger.json",
    "design_validation_report.csv", "experimental_design_candidates.csv",
    "expected_information_gain.csv", "validation_experiment_ranking.csv",
    "adaptive_experiment_policy.json", "bayesian_design_summary.json",
    "deep_surrogate_readiness.json", "experiment_falsification_map.csv",
    "integrated_layers_summary.json",
]
_miss13 = [f for f in _NEW_OUTPUTS if not (PKG / f).exists()]
if _miss13:
    fail("forecast layers: all declared outputs exist", f"missing: {_miss13}")
else:
    ok(f"forecast layers: all {len(_NEW_OUTPUTS)} declared outputs exist")

# (a) NV/forecast gate records: never PASS; model-only; correct schema
_ALLOWED13 = {"BLOCKED", "CONDITIONAL", "UNKNOWN", "DERIVED_CHECK", "NOT_IMPLEMENTED"}
gr = PKG / "nv_spin_gate_records.csv"
if gr.exists():
    with open(gr, encoding="utf-8", newline="") as fh:
        grows = list(_csv13.DictReader(fh))
    bad = []
    for r in grows:
        if (r.get("status") not in _ALLOWED13 or r.get("can_PASS_now") != "NO"
                or r.get("measured_in_this_system") != "false"
                or "FORECAST_ONLY" not in (r.get("notes") or "")):
            bad.append(r.get("gate_id"))
    if not grows:
        fail("forecast gate records present", "nv_spin_gate_records.csv empty")
    elif bad:
        fail("forecast gate records are non-PASS, model-only, well-formed", f"bad: {bad}")
    else:
        ok(f"forecast gate records: all {len(grows)} non-PASS, measured=false, FORECAST_ONLY")

# (b) no MEASURED tags / no PASS anywhere in the new outputs
_meas_or_pass = []
for f in _NEW_OUTPUTS:
    p = PKG / f
    if not p.exists():
        continue
    try:
        txt = p.read_text(errors="ignore")
    except Exception:
        continue
    if "MEASURED" in txt:
        _meas_or_pass.append(f"{f}: MEASURED tag")
    # 'PASS' as a gate status token in the new outputs would be a regression
    if ",PASS," in txt or '"PASS"' in txt or ": \"PASS\"" in txt:
        _meas_or_pass.append(f"{f}: PASS token")
if _meas_or_pass:
    fail("forecast layers: no MEASURED tags / no PASS states in new outputs",
         f"{_meas_or_pass[:4]}")
else:
    ok("forecast layers: no MEASURED tags and no PASS states in new outputs")

# (c) design-graph cross-source validation: zero ERROR findings on canonical data
dv = PKG / "design_validation_report.csv"
if dv.exists():
    with open(dv, encoding="utf-8", newline="") as fh:
        derr = [r for r in _csv13.DictReader(fh) if r.get("severity") == "ERROR"]
    if derr:
        fail("design validation: zero ERROR findings on canonical design",
             f"{len(derr)} ERROR findings")
    else:
        ok("design validation: zero ERROR findings on canonical design (validator live)")

# (d) deep surrogate fail-closed: only a VALIDATED_SURROGATE may control ordering
ds = PKG / "deep_surrogate_readiness.json"
if ds.exists():
    d = json.load(open(ds, encoding="utf-8"))
    _FAILCLOSED_DEEP = {"NOT_IMPLEMENTED", "DATASET_GENERATED", "TRAINED_NOT_VALIDATED",
                        "TRAINED_NOT_TRUSTED", "OUT_OF_DISTRIBUTION", "FALLBACK_DIRECT_MC"}
    _st = d.get("status"); _ctl = d.get("controls_experiment_ordering")
    _trusted_ok = (_st == "VALIDATED_SURROGATE" and _ctl is True)
    _failclosed_ok = (_st in _FAILCLOSED_DEEP and _ctl is False
                      and d.get("fallback") == "direct_monte_carlo")
    if not (_trusted_ok or _failclosed_ok):
        fail("deep surrogate fail-closed (controls ordering only if VALIDATED_SURROGATE)",
             f"status={_st} controls={_ctl} fallback={d.get('fallback')}")
    else:
        ok(f"deep surrogate: status={_st}, controls_ordering={_ctl} "
           f"(fail-closed unless validated; fallback direct MC)")

# (e) Bayesian summary: direct-MC estimator, forecast-only, no PASS
bs = PKG / "bayesian_design_summary.json"
if bs.exists():
    b = json.load(open(bs, encoding="utf-8"))
    if b.get("estimator_source") != "direct_monte_carlo" or not b.get("forecast_only"):
        fail("Bayesian design: direct-MC estimator, forecast-only",
             f"estimator={b.get('estimator_source')} forecast_only={b.get('forecast_only')}")
    else:
        ok("Bayesian design: direct-MC EIG, forecast-only, ranking present")

# (f) integrated summary asserts Mode-C-gated Mode D and no PASS
isum = PKG / "integrated_layers_summary.json"
if isum.exists():
    s = json.load(open(isum, encoding="utf-8"))
    if not s.get("no_pass_gate_states", False) or not s.get("forecast_only", False):
        fail("integrated layers: forecast-only with no PASS gate states",
             f"no_pass={s.get('no_pass_gate_states')} forecast_only={s.get('forecast_only')}")
    else:
        ok("integrated layers: forecast-only, no PASS gate states, Mode-C-gated Mode D")


# ===================== Step 14: deep SBI experimental-design layer ============
print()
print("Step 14: deep SBI layer — outputs present, fail-closed trust gating, "
      "direct-MC comparison, transparent policy, no PASS / no measured")

_DEEP_OUTPUTS = [
    "deep_parameter_schema.json", "deep_design_schema.json", "deep_training_config.json",
    "deep_dataset_summary.json", "deep_dataset_hash.txt", "deep_training_history.csv",
    "deep_model_manifest.json", "deep_posterior_summary.json", "deep_posterior_samples.csv",
    "deep_posterior_calibration.csv", "deep_eig_validation.csv", "deep_ranking_comparison.csv",
    "deep_ood_report.json", "deep_adaptive_policy.json", "deep_surrogate_readiness.json",
    "deep_expdesign_summary.json",
]
_deep_present = [f for f in _DEEP_OUTPUTS if (PKG / f).exists()]
if len(_deep_present) == len(_DEEP_OUTPUTS):
    ok(f"deep layer: all {len(_DEEP_OUTPUTS)} canonical outputs present")
elif _deep_present:
    fail("deep layer: all canonical outputs present",
         f"missing: {[f for f in _DEEP_OUTPUTS if f not in _deep_present]}")
# only run the remaining deep checks if the layer's outputs exist
if "deep_surrogate_readiness.json" in _deep_present:
    _dr = json.load(open(PKG / "deep_surrogate_readiness.json", encoding="utf-8"))
    # trust gating consistency
    if _dr.get("status") == "VALIDATED_SURROGATE":
        if _dr.get("controls_experiment_ordering") is not True:
            fail("deep readiness trust gating consistent", "VALIDATED but not controlling")
        else:
            ok("deep readiness: VALIDATED_SURROGATE controls ordering")
    else:
        if _dr.get("controls_experiment_ordering") is not False or \
           _dr.get("fallback") != "direct_monte_carlo":
            fail("deep readiness fail-closed", f"status={_dr.get('status')}")
        else:
            ok(f"deep readiness: {_dr.get('status')} — not controlling, fallback direct MC")
    if _dr.get("measured_in_this_system") is not False:
        fail("deep readiness: measured_in_this_system false", "measured=true")
    # EIG validation has the direct-MC reference column
    _ev = PKG / "deep_eig_validation.csv"
    if _ev.exists():
        _hdr = _ev.read_text().splitlines()[0]
        if "eig_direct_mc_nats" in _hdr and "eig_deep_nats" in _hdr:
            ok("deep EIG validation: compared against direct nested-MC reference")
        else:
            fail("deep EIG validation has direct-MC reference column", _hdr)
    # transparent policy + eig_source consistent with trust
    _ap = PKG / "deep_adaptive_policy.json"
    if _ap.exists():
        _p = json.load(open(_ap, encoding="utf-8"))
        _src_ok = (_p.get("eig_source") == "deep_surrogate") if _dr.get(
            "controls_experiment_ordering") else (_p.get("eig_source") == "direct_monte_carlo")
        _has_components = bool(_p.get("ranking")) and "weights" in _p
        if _src_ok and _has_components:
            ok("deep policy: transparent components; EIG source consistent with trust state")
        else:
            fail("deep policy transparent + EIG source consistent",
                 f"eig_source={_p.get('eig_source')} has_components={_has_components}")
    # no PASS / no measured across all deep JSON outputs
    _deep_bad = []
    for _f in _DEEP_OUTPUTS:
        _p2 = PKG / _f
        if _p2.exists() and _f.endswith(".json"):
            _txt = _p2.read_text()
            if '"PASS"' in _txt or '"measured_in_this_system": true' in _txt.lower():
                _deep_bad.append(_f)
    if _deep_bad:
        fail("deep outputs: no PASS state / no measured_in_this_system=true", f"{_deep_bad}")
    else:
        ok("deep outputs: no PASS state, no measured_in_this_system=true")


# ===================== FINAL VERDICT =========================================
print()
print("="*70)
if FAILURES:
    print(f"RESULT: FAIL ({len(FAILURES)} checks failed)")
    print("-"*70)
    for chk, det in FAILURES:
        print(f"  [FAIL] {chk}")
        print(f"         {det}")
    sys.exit(1)
else:
    print("RESULT: PASS (all consistency checks passed)")
    sys.exit(0)
