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
    from setuptools import build_meta
except ImportError:  # pragma: no cover
    build_meta = None


@unittest.skipUnless(build_meta, "setuptools not available")
class Wheel(unittest.TestCase):
    def test_wheel_has_data_and_script(self):
        import os
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            for f in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copy(ROOT / f, t / f)
            shutil.copytree(ROOT / "src" / "aimost", t / "src" / "aimost",
                            ignore=shutil.ignore_patterns("__pycache__"))
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
            ep = next(n for n in names if n.endswith("entry_points.txt"))
            self.assertIn("aimost = aimost.cli:main", z.read(ep).decode())


if __name__ == "__main__":
    unittest.main()
