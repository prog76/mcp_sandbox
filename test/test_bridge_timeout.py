#!/usr/bin/env python3
"""Regression: a hung upstream must never freeze the ipybox server.

Two independent lines of defence now bound every synchronous bridge call:

1. ``mcp2cli._fetch_tool_list_live`` wraps the unbounded tools/list session in
   ``asyncio.wait_for(..., timeout=DEFAULT_TOOL_TIMEOUT_SECONDS)`` (see the
   mcp2cli timeout test).
2. The sync bridge helpers (``mcp_call._sync`` and
   ``templating._run_to_completion``) run a hung coroutine in a worker thread
   and ``.result(timeout=_BRIDGE_TIMEOUT_SECONDS)`` instead of blocking the
   event-loop thread forever.

Before #2, a single hung ``tools/list`` blocked the FastMCP event-loop thread
in ``_sync().result()`` — wedging ``execute_code``, ``list-servers``,
everything — and even ``ThreadPoolExecutor.shutdown(wait=True)`` would then hang
on the stuck worker. These tests prove the wait is bounded, the loop stays
responsive, and a hung helper inside a prompt degrades to a ``[template error]``
marker instead of hanging.
"""

import asyncio
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

# Stub container-only deps so the modules import on the host (same pattern as
# test_ipybox_sessions.py / test_mcp_server_mcp_call.py).
sys.modules.setdefault("jupyter_client", MagicMock())

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ipybox.extensions.core import mcp_call as mcp_call_mod  # noqa: E402
from ipybox.kernel import templating  # noqa: E402


class _RegistryStub:
    """Minimal stand-in for ExtensionRegistry (only what templating uses)."""

    def __init__(self):
        self._helpers = {}

    def add(self, name, fn, description="", category=""):
        self._helpers[name] = fn

    def get(self, name):
        return self._helpers.get(name)


class TestSyncBridgeBounded(unittest.TestCase):
    """``_sync`` / ``_run_to_completion`` must bound their worker wait."""

    def test_sync_raises_timeout_and_loop_recovers(self):
        """A hung coroutine must raise within the bound; the loop must stay alive."""
        hung_seconds = 60  # would hang for minutes without the bound

        async def main():
            async def hang():
                await asyncio.sleep(hung_seconds)

            with patch.object(mcp_call_mod, "_BRIDGE_TIMEOUT_SECONDS", 1):
                start = time.monotonic()
                with self.assertRaises(TimeoutError):
                    # Called from within a running loop -> worker branch.
                    mcp_call_mod._sync(hang())
                elapsed = time.monotonic() - start
                self.assertLess(elapsed, 10, "worker wait was not bounded")

            # The event loop must still be usable after the bounded failure.
            await asyncio.sleep(0.01)
            self.assertEqual(await asyncio.sleep(0, result="ok"), "ok")

        asyncio.run(main())

    def test_run_to_completion_raises_timeout_and_loop_recovers(self):
        hung_seconds = 60

        async def main():
            async def hang():
                await asyncio.sleep(hung_seconds)

            with patch.object(templating, "_BRIDGE_TIMEOUT_SECONDS", 1):
                start = time.monotonic()
                with self.assertRaises(TimeoutError):
                    templating._run_to_completion(hang())
                self.assertLess(time.monotonic() - start, 10)

            await asyncio.sleep(0.01)

        asyncio.run(main())

    def test_sync_fast_path_still_works(self):
        """A completing coroutine must return its value (no false timeouts)."""

        async def main():
            async def quick():
                return 42

            self.assertEqual(await asyncio.sleep(0, result="warmup"), "warmup")
            # From within a running loop this exercises the worker branch.
            self.assertEqual(mcp_call_mod._sync(quick()), 42)

        asyncio.run(main())


class TestPromptDegradesOnHungHelper(unittest.TestCase):
    """A hung helper inside a prompt must degrade, not hang the render.

    Faithfully reproduces the real chain: ``render_template_async`` calls the
    *sync* helper registered in the kernel registry (e.g. ``mcp_list_upstreams``),
    which internally calls ``_sync(async_coro)``. The bridge timeout inside
    ``_sync`` is what bounds the wait; the renderer's ``except`` then turns the
    ``TimeoutError`` into a ``[template error]`` marker.
    """

    def test_hung_helper_renders_template_error_marker(self):
        hung_seconds = 60  # would hang for minutes without the bound

        async def hung_async(*args, **kwargs):
            await asyncio.sleep(hung_seconds)
            return "unreachable"

        # Mirror the real mcp_list_upstreams: a SYNC helper that drives an async
        # coroutine through _sync (the bounded bridge).
        def sync_hung_helper(*args, **kwargs):
            return mcp_call_mod._sync(hung_async(*args, **kwargs))

        reg = _RegistryStub()
        reg.add("mcp_list_upstreams", sync_hung_helper)

        async def main():
            with patch.object(templating, "get_registry", return_value=reg), \
                    patch.object(mcp_call_mod, "_BRIDGE_TIMEOUT_SECONDS", 1):
                start = time.monotonic()
                rendered = await templating.render_template_async(
                    "Upstreams: {{ mcp_list_upstreams() }}"
                )
                elapsed = time.monotonic() - start

            self.assertLess(elapsed, 10, "prompt render hung on a stuck helper")
            self.assertIn("[template error: mcp_list_upstreams()]", rendered)

        asyncio.run(main())


class TestMcpListGracefulDegradation(unittest.TestCase):
    """``mcp_list_upstreams_async`` must degrade to a friendly string, not hang.

    This is the layer that actually wraps ``fetch_tool_list_async``'s
    ``TimeoutError`` into a human-readable error string (via
    ``_format_tool_call_error``). Proves the end-to-end behaviour an agent sees.
    """

    def test_hung_endpoint_returns_timeout_string(self):
        from unittest.mock import AsyncMock

        import ipybox.mcp_client as mcp_client_mod

        async def main():
            # Force a live (uncached) fetch against a hung endpoint.
            with patch.object(
                mcp_client_mod, "fetch_tool_list_async",
                side_effect=TimeoutError("timed out fetching tool list"),
            ), patch.object(mcp_client_mod, "_cache_dir", return_value="/tmp/ipybox_test_cache"):
                start = time.monotonic()
                out = await mcp_client_mod.mcp_list_upstreams_async(
                    endpoint="http://127.0.0.1:9/mcp/none",
                )
                elapsed = time.monotonic() - start

            self.assertLess(elapsed, 5)
            self.assertIn("list_upstreams", out)
            self.assertIn("timed out", out)

        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()