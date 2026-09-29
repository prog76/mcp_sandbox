#!/usr/bin/env python3
"""
Tests for skill_mgmt name handling, path resolution and write behaviour.

Covers the three defects found 2026-09-29:

  1. _safe_name() destroyed nesting: "methodology/x" -> "methodology_x.md" at
     the ROOT (a different file than the caller asked for), and "../x" ->
     "__x" instead of being rejected.
  2. create_skill/update_skill only understood "<name>.md", so a skill stored
     as "<dir>/SKILL.md" could be READ by get_skill() but never UPDATED.
  3. Traversal was silently rewritten rather than refused, while get_skill()
     already refused it outright - reads and writes disagreed.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ipybox.extensions.core import skill_mgmt  # noqa: E402


class _RegistryStub:
    def __init__(self):
        self._helpers = {}

    def add(self, name, fn, description="", category=""):
        self._helpers[name] = fn

    def get(self, name):
        return self._helpers.get(name)


def _load(skills_dir):
    """Register the extension against a temp skills dir; return helpers."""
    reg = _RegistryStub()
    with patch.dict(os.environ, {"IPYBOX_SKILLS_DIR": skills_dir}):
        skill_mgmt.register(reg)
    return reg._helpers


class TestCreateNaming(unittest.TestCase):
    """create_skill returns a relative path and refuses traversal."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.create = _load(self.d)["create_skill"]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_bare_name_unchanged(self):
        res = self.create("dogfood", "body")
        self.assertIn("created successfully", res)
        self.assertTrue(os.path.isfile(os.path.join(self.d, "dogfood.md")))

    def test_nested_name_preserves_directory(self):
        """The regression: 'methodology/x' must NOT become 'methodology_x.md'."""
        res = self.create("methodology/dogfood", "body")
        self.assertIn("created successfully", res)
        self.assertTrue(
            os.path.isfile(os.path.join(self.d, "methodology", "dogfood.md")),
            "nested skill should land in the subdirectory",
        )
        self.assertFalse(
            os.path.exists(os.path.join(self.d, "methodology_dogfood.md")),
            "must NOT mangle the slash into an underscore at the root",
        )

    def test_deep_nesting_supported(self):
        res = self.create("a/b/c/deep", "body")
        self.assertIn("created successfully", res)
        self.assertTrue(os.path.isfile(os.path.join(self.d, "a", "b", "c", "deep.md")))

    def test_traversal_rejected(self):
        for bad in ("../escape", "a/../../b", "/absolute", ".."):
            with self.subTest(name=bad):
                res = self.create(bad, "body")
                self.assertIn("Error", res)
                self.assertFalse(
                    os.path.exists(os.path.join(os.path.dirname(self.d), "escape.md"))
                )

    def test_no_silent_rename_on_traversal(self):
        """Old behaviour produced '__escape.md'; that must not happen now."""
        self.create("../escape", "body")
        self.assertFalse(os.path.exists(os.path.join(self.d, "__escape.md")))


class TestUpdateResolution(unittest.TestCase):
    """create/update must understand both layouts get_skill() already reads."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.helpers = _load(self.d)
        self.update = self.helpers["update_skill"]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_update_flat_md(self):
        with open(os.path.join(self.d, "flat.md"), "w") as f:
            f.write("old")
        res = self.update("flat", "new")
        self.assertIn("updated successfully", res)
        with open(os.path.join(self.d, "flat.md")) as f:
            self.assertEqual(f.read(), "new")

    def test_update_dir_skill_md(self):
        """The bug hit in practice: <dir>/SKILL.md was readable, not updatable."""
        sub = os.path.join(self.d, "area", "thing")
        os.makedirs(sub)
        with open(os.path.join(sub, "SKILL.md"), "w") as f:
            f.write("old")
        res = self.update("area/thing", "new")
        self.assertIn("updated successfully", res)
        with open(os.path.join(sub, "SKILL.md")) as f:
            self.assertEqual(f.read(), "new")

    def test_update_missing_returns_error(self):
        self.assertIn("not found", self.update("nope", "x"))


class TestListingAndCollisions(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.helpers = _load(self.d)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_duplicate_refused(self):
        c = self.helpers["create_skill"]
        c("dup", "one")
        self.assertIn("already exists", c("dup", "two"))

    def test_listing_shows_nested_path(self):
        self.helpers["create_skill"](
            "methodology/listed", "---\nname: listed\ndescription: >-\n  x\n---\n\nb"
        )
        out = self.helpers["list_skills"]()
        self.assertIn("methodology/listed", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
