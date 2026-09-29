"""The regeneration wrapper records what the generator read and ran on.

``tools/regenerate_instrumented.py`` runs a generator in its own process and
records (a) the generator's INPUT CLOSURE, measured -- every file under the
generator's directory it imported or opened for reading -- and (b) the
backend of that same process, probed after the generator finished. The
witness profile binds (a); the verdict reads (b). Each test here runs the
real wrapper on a tiny generator in a temporary directory.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "tools" / "regenerate_instrumented.py"

GEN = '''import os, sys
import helper
with open("inputs/params.txt") as fh:
    k = float(fh.read())
os.makedirs("outputs", exist_ok=True)
with open("outputs/out.txt", "w") as fh:
    fh.write(str(helper.scale(k)))
with open("outputs/out.txt") as fh:          # an output read back
    fh.read()
with open("scratch.tmp", "w") as fh:          # written, never read
    fh.write("x")
if "--fail" in sys.argv:
    sys.exit(3)
'''


def _generator(tmp_path):
    g = tmp_path / "gen"
    (g / "inputs").mkdir(parents=True)
    (g / "gen.py").write_text(GEN)
    (g / "helper.py").write_text("def scale(k):\n    return 2 * k\n")
    (g / "inputs" / "params.txt").write_text("1.5")
    (g / "unused.txt").write_text("never read")
    return g


def _run(g, *extra, record=None):
    record = record or (g.parent / "record.json")
    r = subprocess.run([sys.executable, str(WRAPPER), "--entry",
                        str(g / "gen.py"), "--record", str(record), "--",
                        *extra], cwd=g, capture_output=True, text=True,
                       timeout=300)
    rec = json.loads(record.read_text()) if record.exists() else None
    return r, rec


def test_the_closure_is_what_the_generator_read(tmp_path):
    g = _generator(tmp_path)
    r, rec = _run(g)
    assert r.returncode == 0, r.stderr
    assert set(rec["closure"]) == {"gen.py", "helper.py",
                                   "inputs/params.txt"}, rec["closure"]
    assert rec["outputs"] == {"out.txt": rec["outputs"]["out.txt"]}
    assert len(rec["outputs"]["out.txt"]) == 64


def test_outputs_read_back_and_files_only_written_are_not_inputs(tmp_path):
    g = _generator(tmp_path)
    _, rec = _run(g)
    assert "outputs/out.txt" not in rec["closure"]
    assert "scratch.tmp" not in rec["closure"]
    assert "unused.txt" not in rec["closure"]


def test_a_module_imported_from_its_bytecode_cache_is_still_an_input(
        tmp_path):
    """With a valid cache the import reads __pycache__/helper.*.pyc and never
    opens helper.py: the modules the generator LOADED are recorded, not only
    the files it happened to open. The cache is compiled here rather than
    left to a first run, which writes none under PYTHONDONTWRITEBYTECODE,
    and located as the interpreter locates it."""
    import importlib.util
    import py_compile
    g = _generator(tmp_path)
    src = str(g / "helper.py")
    # wherever this interpreter keeps caches (PYTHONPYCACHEPREFIX moves
    # them out of __pycache__); the wrapper inherits the same setting
    cfile = importlib.util.cache_from_source(src)
    py_compile.compile(src, cfile=cfile, doraise=True)
    assert Path(cfile).is_file()
    _, rec = _run(g)
    assert "helper.py" in rec["closure"]
    assert not any(k.endswith(".pyc") for k in rec["closure"])


def test_an_input_that_changes_changes_the_closure(tmp_path):
    g = _generator(tmp_path)
    _, a = _run(g)
    (g / "inputs" / "params.txt").write_text("2.5")
    _, b = _run(g, record=tmp_path / "b.json")
    assert a["closure"]["inputs/params.txt"] != \
        b["closure"]["inputs/params.txt"]
    assert a["closure"]["helper.py"] == b["closure"]["helper.py"]


def test_the_backend_is_probed_in_the_same_process(tmp_path):
    g = _generator(tmp_path)
    _, rec = _run(g)
    env = rec["environment"]
    assert env["runtime"] is not None
    assert "backend_status" in env
    assert isinstance(rec["native_distributions"], list)


def test_a_failed_generator_leaves_no_record(tmp_path):
    g = _generator(tmp_path)
    r, rec = _run(g, "--fail")
    assert r.returncode == 3 and rec is None


def test_the_record_may_not_live_inside_the_output_set(tmp_path):
    g = _generator(tmp_path)
    (g / "outputs").mkdir()
    r, _ = _run(g, record=g / "outputs" / "record.json")
    assert r.returncode == 2 and "may not be written" in r.stderr
    assert not (g / "outputs" / "record.json").exists()
