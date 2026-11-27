"""Packaging: the wheel carries the package data and the console script (built offline in a temp copy)."""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import helpers  # noqa: F401  (sys.path)

ROOT = Path(__file__).resolve().parent.parent

try:
    import setuptools
    from setuptools import build_meta
    MODERN = int(setuptools.__version__.split(".")[0]) >= 61   # pyproject.toml asks for it; a real build installs it
except ImportError:  # pragma: no cover
    build_meta, MODERN = None, False


@unittest.skipUnless(build_meta and MODERN, "setuptools >= 61 not available (a real build installs it)")
class Wheel(unittest.TestCase):
    def test_wheel_has_data_and_script(self):
        import os
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            for f in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copy(ROOT / f, t / f)
            shutil.copytree(ROOT / "src", t / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
            cwd = os.getcwd()
            os.chdir(t)
            try:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    name = build_meta.build_wheel(str(t / "dist"))
            finally:
                os.chdir(cwd)
            z = zipfile.ZipFile(t / "dist" / name)
            names = set(z.namelist())
            data = {str(p.relative_to(ROOT / "src")) for p in (ROOT / "src" / "aimost" / "data").rglob("*") if p.is_file()}
            self.assertTrue(data and data <= names, data - names)
            for pkg in ("aimost_gitlab", "aimost_health"):          # the built-in plugins ship in the same wheel
                self.assertIn(f"{pkg}/__init__.py", names)
            ep = next(n for n in names if n.endswith("entry_points.txt"))
            self.assertIn("aimost = aimost.cli:main", z.read(ep).decode())


if __name__ == "__main__":
    unittest.main()
