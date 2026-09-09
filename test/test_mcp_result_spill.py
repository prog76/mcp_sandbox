#!/usr/bin/env python3
"""Tests for the mcp_call spill-to-file + result contract (t_3cb401de)."""
import os, sys, types, unittest, tempfile, json, shutil
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path: sys.path.insert(0, _SRC)
import ipybox.mcp_client as mcp_client
from ipybox import mcp_result
from ipybox.mcp_result import McpCallResult, build_result, cleanup_ttl_sessions

class TestSpillContract(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._old_base = mcp_result._SPILL_BASE
        mcp_result._SPILL_BASE = self._tmp
    def tearDown(self):
        mcp_result._SPILL_BASE = self._old_base
        shutil.rmtree(self._tmp, ignore_errors=True)
    def test_small_no_spill(self):
        r = build_result(ok=True, is_error=False, upstream="u", action="a", text="hi", content=[], structured=None, session_id="s1", seq=1)
        self.assertTrue(r["ok"])
        self.assertEqual(r["text"], "hi")
        self.assertFalse(r["truncated"])
        self.assertEqual(r["bytes_total"], 2)
        self.assertTrue(r["file"].startswith(self._tmp))
        self.assertIsNone(r["json"])
    def test_large_spills_and_truncates(self):
        big = "x" * 60000
        r = build_result(ok=True, is_error=False, upstream="u", action="a", text=big, content=[], structured=None, session_id="s1", seq=2)
        self.assertTrue(r["truncated"])
        self.assertTrue(len(r["text"]) < 60000)
        self.assertIn("OUTPUT TRUNCATED", r["text"])
        self.assertEqual(r["bytes_total"], 60000)
        with open(r["file"]) as f:
            self.assertEqual(len(f.read()), 60000)
        self.assertIsNone(r["json"])
    def test_json_from_text_fallback(self):
        payload = json.dumps({"a": 1, "b": [1,2,3]})
        r = build_result(ok=True, is_error=False, upstream="u", action="a", text=payload, content=[], structured=None, session_id="s1", seq=3)
        self.assertEqual(r["json"], {"a": 1, "b": [1,2,3]})
        self.assertIsNotNone(r.get("json"))
    def test_json_attr_internal_not_serialized(self):
        sc = {"data": {"x": 1}}
        r = build_result(ok=True, is_error=False, upstream="u", action="a", text="t", content=[], structured=sc, session_id="s1", seq=4)
        self.assertEqual(r["json"], sc)
        self.assertEqual(r.get("json"), sc)
        self.assertNotIn('"data"', repr(r))
        self.assertIn("@object[in_file]", repr(r))
        self.assertIs(r["json"], r["structured_content"])
    def test_preview_split(self):
        r = build_result(ok=True, is_error=False, upstream="u", action="a", text="x"*50000 + "Z"*11, content=[], structured=None, session_id="s1", seq=5)
        self.assertTrue(r["truncated"])
        self.assertTrue(r["text"].startswith("x"*20000))
        self.assertTrue(r["text"].endswith("Z"*11))
    def test_ttl_sweep(self):
        old = os.path.join(self._tmp, "old")
        new = os.path.join(self._tmp, "new")
        os.makedirs(old); os.makedirs(new)
        os.utime(old, (1, 1))
        n = cleanup_ttl_sessions()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        self.assertEqual(n, 1)

class TestBuildCallResultContract(unittest.TestCase):
    def test_build_call_result_contract(self):
        out = types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="hi")], isError=False, structuredContent=None)
        r = mcp_client._build_call_result(out, "u", "a", session_id="s2", seq=7)
        self.assertIsInstance(r, McpCallResult)
        self.assertIn("file", r)
        self.assertIn("truncated", r)
        self.assertIn("bytes_total", r)
        self.assertIsNone(r["json"])

if __name__ == "__main__":
    unittest.main()
