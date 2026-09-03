#!/usr/bin/env python3
"""Tests for the structured mcp_call result schema and the sync kernel wrappers."""
import os, sys, types, unittest
import asyncio
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path: sys.path.insert(0, _SRC)
import ipybox.mcp_client as mcp_client
from ipybox.kernel.extensions import get_registry

_registry = get_registry()
mcp_call = _registry.get("mcp_call")
mcp_call_text = _registry.get("mcp_call_text")
exec_run = _registry.get("exec_run")

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
        orig = mcp_client.mcp_call_async; mcp_client.mcp_call_async = self._fake
        try:
            self.assertIsInstance(mcp_call("exec","run",{"command":["a"]}), dict)
            self.assertEqual(mcp_call_text("exec","run",{"command":["a"]}), "OUT-42")
        finally: mcp_client.mcp_call_async = orig
    def test_exec_run_text(self):
        async def fake(upstream, action, arguments=None, stdin=None, timeout=120, endpoint=None):
            return {"ok": True, "is_error": False, "upstream": "exec", "action": "run",
                    "text": "hello", "content": [], "structured_content": None}
        orig = mcp_client.mcp_call_async; mcp_client.mcp_call_async = fake
        try: self.assertEqual(exec_run(["echo","hi"]), "hello")
        finally: mcp_client.mcp_call_async = orig

class TestAddressingModes(unittest.TestCase):
    """mcp_call / mcp_describe / mcp_list_actions accept both the split
    (upstream, action) and combined (full proxied id) addressing conventions
    and resolve them identically via a single shared resolver.
    """

    _TOOL_NAMES = ["vscode_terminal_exec", "k8s_pods_get", "exec_run"]

    # -- _resolve_action_id (pure resolver) ------------------------------

    def test_split_form_resolves(self):
        r = mcp_client._resolve_action_id("vscode", "terminal_exec", self._TOOL_NAMES)
        self.assertEqual(r, "vscode_terminal_exec")

    def test_split_fullid_belongs_to_upstream_resolves(self):
        r = mcp_client._resolve_action_id("vscode", "vscode_terminal_exec", self._TOOL_NAMES)
        self.assertEqual(r, "vscode_terminal_exec")

    def test_combined_form_resolves(self):
        r = mcp_client._resolve_action_id(None, "vscode_terminal_exec", self._TOOL_NAMES)
        self.assertEqual(r, "vscode_terminal_exec")

    def test_unprefixed_without_upstream_fails(self):
        with self.assertRaises(ValueError):
            mcp_client._resolve_action_id(None, "terminal_exec", self._TOOL_NAMES)

    def test_unprefixed_under_wrong_upstream_fails(self):
        with self.assertRaises(ValueError):
            mcp_client._resolve_action_id("k8s", "terminal_exec", self._TOOL_NAMES)

    def test_foreign_full_id_is_a_mismatch(self):
        with self.assertRaises(ValueError):
            mcp_client._resolve_action_id("k8s", "vscode_terminal_exec", self._TOOL_NAMES)

    # -- mcp_call / mcp_call_async (both forms) --------------------------

    def _call(self, upstream=None, action=None):
        captured = {}
        async def fake_fetch(endpoint=None, cache_dir=None, cache_ttl_s=None):
            return [{"name": n} for n in self._TOOL_NAMES]
        async def fake_call(endpoint=None, tool_id=None, arguments=None, progress_callback=None):
            captured["tool_id"] = tool_id
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text="OUT")],
                isError=False, structuredContent=None,
            )
        orig_f = mcp_client.fetch_tool_list_async
        orig_c = mcp_client._call_tool_live
        mcp_client.fetch_tool_list_async = fake_fetch
        mcp_client._call_tool_live = fake_call
        try:
            result = asyncio.run(mcp_client.mcp_call_async(upstream=upstream, action=action, arguments={}))
        finally:
            mcp_client.fetch_tool_list_async = orig_f
            mcp_client._call_tool_live = orig_c
        return result, captured

    def test_mcp_call_split_form(self):
        r, cap = self._call(upstream="k8s", action="pods_get")
        self.assertTrue(r["ok"]); self.assertEqual(cap["tool_id"], "k8s_pods_get")
        self.assertEqual(r["action"], "k8s_pods_get")

    def test_mcp_call_combined_form_infers_upstream(self):
        r, cap = self._call(action="vscode_terminal_exec")
        self.assertTrue(r["ok"]); self.assertEqual(cap["tool_id"], "vscode_terminal_exec")
        self.assertEqual(r["upstream"], "vscode")

    def test_mcp_call_combined_bare_name_errors(self):
        r, _ = self._call(action="terminal_exec")
        self.assertIsInstance(r, str); self.assertIn("not found", r)

    def test_mcp_call_foreign_full_id_errors(self):
        r, _ = self._call(upstream="k8s", action="vscode_terminal_exec")
        self.assertIsInstance(r, str); self.assertIn("does not belong", r)

    # -- mcp_describe (both forms) ---------------------------------------

    def _describe(self, action=None, upstream=None):
        async def fake_fetch(endpoint=None, cache_dir=None, cache_ttl_s=None):
            return [{"name": n} for n in self._TOOL_NAMES]
        def fake_format(tool):  # format_tool_schema is imported into the module (called synchronously)
            return "schema:" + tool["name"]
        orig_fetch = mcp_client.fetch_tool_list_async
        orig_format = mcp_client.format_tool_schema
        mcp_client.fetch_tool_list_async = fake_fetch
        mcp_client.format_tool_schema = fake_format
        try:
            return asyncio.run(mcp_client.mcp_describe_async(action=action, upstream=upstream, endpoint="x"))
        finally:
            mcp_client.fetch_tool_list_async = orig_fetch
            mcp_client.format_tool_schema = orig_format

    def test_describe_combined_form(self):
        self.assertIn("vscode_terminal_exec", self._describe(action="vscode_terminal_exec"))

    def test_describe_split_uses_upstream_to_resolve(self):
        self.assertIn("vscode_terminal_exec", self._describe(action="terminal_exec", upstream="vscode"))

    def test_describe_wrong_upstream_errors(self):
        d = self._describe(action="terminal_exec", upstream="k8s")
        self.assertTrue(d.startswith("Error"))

    def test_describe_unprefixed_without_upstream_errors(self):
        d = self._describe(action="terminal_exec")
        self.assertTrue(d.startswith("Error"))

    # -- mcp_list_actions (unprefixed output) ----------------------------

    def test_list_actions_prints_unprefixed_names(self):
        async def fake_fetch(endpoint=None, cache_dir=None, cache_ttl_s=None):
            return [{"name": n, "description": "d"} for n in self._TOOL_NAMES]
        orig = mcp_client.fetch_tool_list_async
        mcp_client.fetch_tool_list_async = fake_fetch
        try:
            listing = asyncio.run(mcp_client.mcp_list_actions_async("vscode", endpoint="x"))
        finally:
            mcp_client.fetch_tool_list_async = orig
        self.assertIn("terminal_exec", listing)
        self.assertNotIn("vscode_terminal_exec", listing)


if __name__ == "__main__": unittest.main(verbosity=2)
