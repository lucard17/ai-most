"""Packaging: the wheel carries the package data and the console script (built offline in a temp copy)."""

from __future__ import annotations

import io
import os
import subprocess
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


class CachedLauncher(unittest.TestCase):
    """bin/aimost of an older version in the Claude Code plugin cache runs the newest version installed next to it."""

    def make(self, base, version):
        d = Path(base) / "plugins" / "cache" / "m" / "ai-most" / version
        (d / "bin").mkdir(parents=True)
        shutil.copy(ROOT / "bin" / "aimost", d / "bin" / "aimost")
        (d / "src" / "aimost").mkdir(parents=True)
        (d / "src" / "aimost" / "__init__.py").write_text("")
        (d / "src" / "aimost" / "__main__.py").write_text(f"print('{version}')\n")
        return d / "bin" / "aimost"

    def run_it(self, launcher):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        return subprocess.run(["sh", str(launcher)], capture_output=True, text=True, env=env).stdout.strip()

    def test_old_runs_newest_never_older(self):
        with tempfile.TemporaryDirectory() as t:
            old, new = self.make(t, "0.1.3"), self.make(t, "0.1.10")
            self.make(t, "0.1.9")
            self.assertEqual(self.run_it(old), "0.1.10", "semantic order, not text order")
            self.assertEqual(self.run_it(new), "0.1.10")

    def test_outside_the_cache_runs_itself(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "checkout"
            (d / "bin").mkdir(parents=True)
            shutil.copy(ROOT / "bin" / "aimost", d / "bin" / "aimost")
            (d / "src" / "aimost").mkdir(parents=True)
            (d / "src" / "aimost" / "__init__.py").write_text("")
            (d / "src" / "aimost" / "__main__.py").write_text("print('checkout')\n")
            self.assertEqual(self.run_it(d / "bin" / "aimost"), "checkout")
