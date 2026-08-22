#!/usr/bin/env python3
"""
Tests for ipybox kernel function introspection helpers:
  - list_functions(): lists non-underscore helpers with docstring descriptions
  - describe_function(name): signature, input params, output type, docstring
"""

import sys
import os
import unittest
from unittest.mock import MagicMock

# Stub out container-only deps so the startup module imports on the host.
sys.modules.setdefault("ijson", MagicMock())

# Ensure src/ is on sys.path and import the startup module fresh.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
if "ipybox.kernel.startup" in sys.modules:
    del sys.modules["ipybox.kernel.startup"]


class TestListFunctions(unittest.TestCase):
    """Tests for list_functions()."""

    @classmethod
    def setUpClass(cls):
        from ipybox.kernel import startup
        cls.s = startup

    def test_lists_non_underscore_helpers(self):
        result = self.s.list_functions()
        self.assertIn("Available kernel functions", result)
        self.assertIn("- exec_run:", result)
        self.assertIn("- list_functions:", result)
        self.assertIn("- describe_function:", result)
        self.assertIn("- list_skills:", result)

    def test_excludes_private_functions(self):
        result = self.s.list_functions()
        self.assertNotIn("_sync", result)
        self.assertNotIn("_safe_skill_name", result)
        self.assertNotIn("_SKILLS_DIR", result)

    def test_includes_one_line_description(self):
        result = self.s.list_functions()
        self.assertIn("exec_run: Run a command via the exec backend", result)


class TestDescribeFunction(unittest.TestCase):
    """Tests for describe_function()."""

    @classmethod
    def setUpClass(cls):
        from ipybox.kernel import startup
        cls.s = startup

    def test_describe_known_helper(self):
        result = self.s.describe_function("exec_run")
        self.assertIn("exec_run(", result)
        self.assertIn("Input parameters:", result)
        self.assertIn("command", result)
        self.assertIn("timeout", result)
        self.assertIn("Run a command via the exec backend", result)

    def test_describe_builtin(self):
        result = self.s.describe_function("os.getcwd")
        # os.getcwd is importable in the host test env
        import os as os_mod
        self.assertIn("getcwd", result)

    def test_describe_unknown(self):
        result = self.s.describe_function("nonexistent_fn_123")
        self.assertEqual(result, "Error: Function 'nonexistent_fn_123' not found.")

    def test_describe_globals(self):
        result = self.s.describe_function("_short_doc")
        # _short_doc is in globals() (not _helpers since underscore), still describable
        self.assertIn("_short_doc(", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)