#!/usr/bin/env python3
"""Tests for the structured mcp_call result schema."""
import os, sys, types, unittest
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path: sys.path.insert(0, _SRC)
for _m in ("ipybox",): sys.modules.pop(_m, None)
import ipybox.mcp_client as mcp_client
import ipybox.helpers as ipybox_helpers

def _text(v): return types.SimpleNamespace(type="text", text=v)
def _result(content, is_error=False, sc=None):
    return types.SimpleNamespace(content=content, isError=is_error, structuredContent=sc)

class TestResultSchema(unittest.TestCase):
    def test_build_success(self):
        r = mcp_client._build_call_result(_result([_text("NAME\nrow")]), "k8s", "k8s_nodes_top")
        self.assertTrue(r["ok"]); self.assertFalse(r["is_error"])
        self.assertEqual(r["text"], "NAME\nrow")
        self.assertEqual(r["content"], [{"type": "text", "text": "NAME\nrow"}])
        self.assertIsNone(r["structured_content"])
    def test_join(self):
        self.assertEqual(mcp_client._build_call_result(_result([_text("a"), _text("b")]), "e", "x")["text"], "a\nb")
    def test_error(self):
        r = mcp_client._build_call_result(_result([_text("oops")], True), "e", "x")
        self.assertFalse(r["ok"]); self.assertTrue(r["is_error"]); self.assertEqual(r["text"], "oops")
    def test_structured(self):
        sc={"nodes":[{"name":"n1"}]}
        r = mcp_client._build_call_result(_result([_text("t")], sc=sc), "k8s", "k")
        self.assertEqual(r["structured_content"], sc)
    def test_error_result(self):
        r = mcp_client._error_result("e","a","boom")
        self.assertFalse(r["ok"]); self.assertEqual(r["text"], "boom"); self.assertEqual(r["content"], [])

class TestWrappers(unittest.TestCase):
    async def _fake(self, upstream, action, arguments=None, stdin=None, timeout=120, endpoint=None):
        return mcp_client._build_call_result(_result([_text("OUT-42")]), upstream, "exec_run")
    def test_mcp_call_dict_and_text(self):
        orig = ipybox_helpers.mcp_call_async; ipybox_helpers.mcp_call_async = self._fake
        try:
            self.assertIsInstance(ipybox_helpers.mcp_call("exec","run",{"command":["a"]}), dict)
            self.assertEqual(ipybox_helpers.mcp_call_text("exec","run",{"command":["a"]}), "OUT-42")
        finally: ipybox_helpers.mcp_call_async = orig
    def test_exec_run_text(self):
        async def fake(upstream, action, arguments=None, stdin=None, timeout=120, endpoint=None):
            return {"ok": True, "is_error": False, "upstream": "exec", "action": "run",
                    "text": "hello", "content": [], "structured_content": None}
        orig = ipybox_helpers.mcp_call_async; ipybox_helpers.mcp_call_async = fake
        try: self.assertEqual(ipybox_helpers.exec_run(["echo","hi"]), "hello")
        finally: ipybox_helpers.mcp_call_async = orig

if __name__ == "__main__": unittest.main(verbosity=2)
