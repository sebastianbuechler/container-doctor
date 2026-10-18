"""The documentation keeps up with the code.

The environment variable reference is generated from app/config.py and
.env.example; these fail when it was not regenerated, when the code reads a
doctor.* label the labels page does not list, and when a page is missing from
the sidebar. See docs/src/content/docs/contributing/documentation.md.
"""

from __future__ import annotations

import importlib.util
import os
import re
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "docs" / "src" / "content" / "docs"


def gen_docs():
    spec = importlib.util.spec_from_file_location("gen_docs", ROOT / "scripts" / "gen_docs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TheGeneratedReference(unittest.TestCase):
    def test_it_matches_the_code(self):
        page = PAGES / "reference" / "environment-variables.md"
        self.assertEqual(page.read_text(), gen_docs().render(),
                         "out of date: run python scripts/gen_docs.py")

    def test_every_setting_has_a_description(self):
        self.assertNotIn("No description yet.", gen_docs().render(),
                         "describe it in .env.example or above its setting() in app/config.py")


class TheLabelsPage(unittest.TestCase):
    def test_every_label_the_code_reads_is_documented(self):
        source = "\n".join(p.read_text() for p in (ROOT / "app").rglob("*.py"))
        read = {f"doctor.{name}" for name in re.findall(r'\blabel\("([a-z_]+)"\)', source)}
        read |= set(re.findall(r'"(doctor\.[a-z_]+)"', source)) - {"doctor.db"}
        documented = set(re.findall(r"`(doctor\.[a-z_]+)`",
                                    (PAGES / "reference" / "labels.md").read_text()))
        self.assertTrue(read, "found no labels in the code - has the pattern changed?")
        self.assertEqual(sorted(read - documented), [])


class TheSidebar(unittest.TestCase):
    def test_every_page_is_in_it(self):
        config = (ROOT / "docs" / "astro.config.mjs").read_text()
        listed = set(re.findall(r"'([a-z0-9/-]+)'", config))
        pages = {p.relative_to(PAGES).with_suffix("").as_posix()
                 for p in PAGES.rglob("*.md*")} - {"index"}
        self.assertEqual(sorted(pages - listed), [])


if __name__ == "__main__":
    unittest.main()
