#!/usr/bin/env python3
"""Tests for the ipybox MCP server's mcp_call passthrough tool.

Verifies tool registration, result formatting, and the X-MCP-Endpoint
header -> endpoint resolution path.
"""

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Stub container-only deps so the module imports on the host.
sys.modules.setdefault("ijson", MagicMock())
sys.modules.setdefault("jupyter_client", MagicMock())
sys.modules.setdefault("mcp2cli", MagicMock())
sys.modules.setdefault("mcp2cli.client", MagicMock())

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import ipybox.kernel.mcp_server as server  # noqa: E402


class TestToolRegistration(unittest.TestCase):
    """The mcp_call tool must be registered alongside execute_code."""

    def test_both_tools_registered(self):
        tools = asyncio.run(server.mcp.list_tools())
        names = sorted(t.name for t in tools)
        self.assertIn("execute_code", names)
        self.assertIn("mcp_call", names)

    def test_mcp_call_excludes_ctx_from_schema(self):
        """The FastMCP Context parameter must NOT appear in the tool schema."""
        tools = asyncio.run(server.mcp.list_tools())
        mc = [t for t in tools if t.name == "mcp_call"][0]
        schema_props = mc.parameters.get("properties", {})
        self.assertNotIn("ctx", schema_props)
        self.assertIn("upstream", schema_props)
        self.assertIn("action", schema_props)
        self.assertIn("arguments", schema_props)


class TestResultFormatting(unittest.TestCase):
    """Tests for mcp_call's output string formatting from mcp_call_async dicts."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_success_with_text(self):
        """A successful call with text output produces a formatted [OK] line."""
        async def fake_call(*a, **kw):
            return {
                "ok": True, "is_error": False,
                "upstream": "exec", "action": "run",
                "text": "hello world", "content": [], "structured_content": None,
            }
        with patch.object(server, "mcp_call_async", side_effect=fake_call):
            result = self._run(server.mcp_call(upstream="exec", action="run", arguments={}))
        self.assertIn("[OK] exec/run", result)
        self.assertIn("hello world", result)

    def test_error_with_text(self):
        """A failed call produces an [ERROR] line with the text payload."""
        async def fake_call(*a, **kw):
            return {
                "ok": False, "is_error": True,
                "upstream": "k8s", "action": "pods_list",
                "text": "connection refused", "content": [], "structured_content": None,
            }
        with patch.object(server, "mcp_call_async", side_effect=fake_call):
            result = self._run(server.mcp_call(upstream="k8s", action="pods_list", arguments={}))
        self.assertIn("[ERROR] k8s/pods_list", result)
        self.assertIn("connection refused", result)

    def test_structured_content_included(self):
        """Structured content is appended as JSON after the text."""
        sc = {"nodes": [{"name": "n1", "ready": True}]}
        async def fake_call(*a, **kw):
            return {
                "ok": True, "is_error": False,
                "upstream": "k8s", "action": "nodes_top",
                "text": "NAME  CPU", "content": [],
                "structured_content": sc,
            }
        with patch.object(server, "mcp_call_async", side_effect=fake_call):
            result = self._run(server.mcp_call(upstream="k8s", action="nodes_top", arguments={}))
        self.assertIn("[OK] k8s/nodes_top", result)
        self.assertIn("NAME  CPU", result)
        self.assertIn("--- structured ---", result)
        self.assertIn(json.dumps(sc), result)

    def test_string_return_passthrough(self):
        """When mcp_call_async returns a plain string (resolution failure),
        it is returned verbatim."""
        async def fake_call(*a, **kw):
            return "Error: Ambiguous tool name 'run'"
        with patch.object(server, "mcp_call_async", side_effect=fake_call):
            result = self._run(server.mcp_call(upstream="exec", action="run", arguments={}))
        self.assertEqual(result, "Error: Ambiguous tool name 'run'")

    def test_empty_text(self):
        """When the text payload is empty, only the status line is returned."""
        async def fake_call(*a, **kw):
            return {
                "ok": True, "is_error": False,
                "upstream": "x", "action": "y",
                "text": "", "content": [], "structured_content": None,
            }
        with patch.object(server, "mcp_call_async", side_effect=fake_call):
            result = self._run(server.mcp_call(upstream="x", action="y", arguments={}))
        self.assertEqual(result, "[OK] x/y")


class TestEndpointResolution(unittest.TestCase):
    """Tests for the X-MCP-Endpoint header -> endpoint resolution."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_header_endpoint_passed_through(self):
        """The X-MCP-Endpoint header value is forwarded as the endpoint arg."""
        captured = {}
        async def fake_call(upstream, action, arguments, stdin, timeout, endpoint):
            captured["endpoint"] = endpoint
            return {
                "ok": True, "is_error": False,
                "upstream": upstream, "action": action,
                "text": "ok", "content": [], "structured_content": None,
            }

        fake_ctx = MagicMock()
        fake_ctx.request_context = MagicMock()
        fake_ctx.request_context.request = MagicMock()
        fake_ctx.request_context.request.headers = {"X-MCP-Endpoint": "http://gateway/mcp/full"}

        with patch.object(server, "mcp_call_async", side_effect=fake_call):
            self._run(server.mcp_call(
                ctx=fake_ctx,
                upstream="exec",
                action="run",
                arguments={},
            ))
        self.assertEqual(captured["endpoint"], "http://gateway/mcp/full")

    def test_explicit_endpoint_overrides_header(self):
        """An explicit endpoint arg wins over the header."""
        captured = {}
        async def fake_call(upstream, action, arguments, stdin, timeout, endpoint):
            captured["endpoint"] = endpoint
            return {
                "ok": True, "is_error": False,
                "upstream": upstream, "action": action,
                "text": "ok", "content": [], "structured_content": None,
            }

        fake_ctx = MagicMock()
        fake_ctx.request_context = MagicMock()
        fake_ctx.request_context.request = MagicMock()
        fake_ctx.request_context.request.headers = {"X-MCP-Endpoint": "http://from-header"}

        with patch.object(server, "mcp_call_async", side_effect=fake_call):
            self._run(server.mcp_call(
                ctx=fake_ctx,
                upstream="exec",
                action="run",
                arguments={},
                endpoint="http://explicit-endpoint",
            ))
        self.assertEqual(captured["endpoint"], "http://explicit-endpoint")

    def test_no_ctx_no_header_falls_through(self):
        """With ctx=None, endpoint is passed as None (mcp_call_async resolves via env)."""
        captured = {}
        async def fake_call(upstream, action, arguments, stdin, timeout, endpoint):
            captured["endpoint"] = endpoint
            return {
                "ok": True, "is_error": False,
                "upstream": upstream, "action": action,
                "text": "ok", "content": [], "structured_content": None,
            }
        with patch.object(server, "mcp_call_async", side_effect=fake_call):
            self._run(server.mcp_call(upstream="exec", action="run", arguments={}))
        self.assertIsNone(captured["endpoint"])

    def test_header_access_failure_does_not_raise(self):
        """If header access throws, the tool should not crash."""
        fake_ctx = MagicMock()
        type(fake_ctx).request_context = MagicMock(side_effect=AttributeError("no ctx"))

        async def fake_call(*a, **kw):
            return {
                "ok": True, "is_error": False,
                "upstream": "exec", "action": "run",
                "text": "ok", "content": [], "structured_content": None,
            }
        with patch.object(server, "mcp_call_async", side_effect=fake_call):
            result = self._run(server.mcp_call(
                ctx=fake_ctx, upstream="exec", action="run", arguments={},
            ))
        self.assertIn("[OK] exec/run", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
