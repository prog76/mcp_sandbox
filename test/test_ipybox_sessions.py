#!/usr/bin/env python3
"""
Tests for ipybox per-session kernel isolation and idle cleanup.

Covers the pure session-resolution and reaping logic in
the ipybox kernel MCP server (no real kernel is started).
"""

import sys
import os
import time
import threading
import unittest
from unittest.mock import MagicMock, patch

# Stub out container-only deps so the test imports on the host without
# the full dependency stack (same pattern as test_ipybox_startup.py).
sys.modules.setdefault("jupyter_client", MagicMock())

# Add src dir to path so we can import ipybox.kernel.mcp_server
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ipybox.kernel import mcp_server as server


class TestResolveSessionId(unittest.TestCase):
    """Tests for _resolve_session_id priority logic."""

    def test_explicit_session_id_wins(self):
        """Explicit session_id argument takes priority over kernel_env."""
        result = server._resolve_session_id(
            "my-session",
            {"MCP_SESSION_ID": "env-session"},
        )
        self.assertEqual(result, "my-session")

    def test_kernel_env_mcp_session_id_used(self):
        """kernel_env MCP_SESSION_ID is used when no explicit arg."""
        result = server._resolve_session_id(
            None,
            {"MCP_SESSION_ID": "abc-123"},
        )
        self.assertEqual(result, "abc-123")

    def test_unresolved_template_ignored(self):
        """An unresolved ${request_header:...} template is NOT used as a session id."""
        result = server._resolve_session_id(
            None,
            {"MCP_SESSION_ID": "${request_header:Mcp-Session-Id}"},
        )
        # Should fall through to a fresh uuid, not the literal template.
        self.assertNotEqual(result, "${request_header:Mcp-Session-Id}")
        self.assertNotIn("${", result)

    def test_no_session_id_generates_fresh_uuid(self):
        """No session_id and no usable env → fresh random uuid."""
        result = server._resolve_session_id(None, None)
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_no_session_id_with_empty_env(self):
        """Empty kernel_env dict → fresh uuid."""
        result = server._resolve_session_id(None, {})
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_two_calls_without_id_give_different_sessions(self):
        """Two calls with no session id must NOT share a kernel (no IP fallback)."""
        s1 = server._resolve_session_id(None, None)
        s2 = server._resolve_session_id(None, None)
        self.assertNotEqual(s1, s2)

    def test_same_env_session_id_reused(self):
        """Same MCP_SESSION_ID in kernel_env → same session."""
        s1 = server._resolve_session_id(None, {"MCP_SESSION_ID": "shared"})
        s2 = server._resolve_session_id(None, {"MCP_SESSION_ID": "shared"})
        self.assertEqual(s1, s2)


class TestReapIdleSessions(unittest.TestCase):
    """Tests for _reap_idle_sessions idle cleanup."""

    def setUp(self):
        # Save and clear the global kernel dict so tests are isolated.
        self._orig_kernels = server._kernels
        self._orig_timeout = server.IPYBOX_IDLE_TIMEOUT
        server._kernels = {}
        server.IPYBOX_IDLE_TIMEOUT = 600  # 10 min default

    def tearDown(self):
        server._kernels = self._orig_kernels
        server.IPYBOX_IDLE_TIMEOUT = self._orig_timeout

    def _make_session(self, sid, last_used):
        """Create a fake KernelSession with a mock km/kc."""
        km = MagicMock()
        kc = MagicMock()
        session = server.KernelSession(km=km, kc=kc, last_used=last_used)
        server._kernels[sid] = session
        return session

    def test_reaps_idle_session(self):
        """A session idle longer than the timeout is reaped."""
        now = time.monotonic()
        session = self._make_session("idle-session", now - server.IPYBOX_IDLE_TIMEOUT - 1)
        reaped = server._reap_idle_sessions(now=now)
        self.assertIn("idle-session", reaped)
        self.assertNotIn("idle-session", server._kernels)
        # km.shutdown_kernel should have been called on the reaped session
        session.km.shutdown_kernel.assert_called_once_with(now=True)

    def test_keeps_active_session(self):
        """A session used recently is NOT reaped."""
        now = time.monotonic()
        self._make_session("active-session", now - 1)  # 1s ago
        reaped = server._reap_idle_sessions(now=now)
        self.assertNotIn("active-session", reaped)
        self.assertIn("active-session", server._kernels)

    def test_reaps_only_idle(self):
        """Mixed: only the idle session is reaped, active stays."""
        now = time.monotonic()
        self._make_session("idle", now - server.IPYBOX_IDLE_TIMEOUT - 5)
        self._make_session("active", now - 1)
        reaped = server._reap_idle_sessions(now=now)
        self.assertEqual(reaped, ["idle"])
        self.assertIn("active", server._kernels)
        self.assertNotIn("idle", server._kernels)

    def test_skips_locked_session(self):
        """A session whose lock is held (actively executing) is skipped."""
        now = time.monotonic()
        session = self._make_session("busy", now - server.IPYBOX_IDLE_TIMEOUT - 5)
        # Acquire the lock to simulate an in-flight execution.
        session.lock.acquire()
        try:
            reaped = server._reap_idle_sessions(now=now)
        finally:
            session.lock.release()
        self.assertNotIn("busy", reaped)
        self.assertIn("busy", server._kernels)

    def test_shutdown_called_on_reap(self):
        """km.shutdown_kernel is called when a session is reaped."""
        now = time.monotonic()
        session = self._make_session("to-reap", now - server.IPYBOX_IDLE_TIMEOUT - 1)
        server._reap_idle_sessions(now=now)
        session.km.shutdown_kernel.assert_called_once_with(now=True)

    def test_empty_noop(self):
        """Empty kernel dict → nothing reaped."""
        reaped = server._reap_idle_sessions(now=time.monotonic())
        self.assertEqual(reaped, [])


class TestSessionManager(unittest.TestCase):
    """Tests for _get_or_create_session."""

    def setUp(self):
        self._orig_kernels = server._kernels
        server._kernels = {}

    def tearDown(self):
        server._kernels = self._orig_kernels

    @patch("ipybox.kernel.mcp_server._start_kernel")
    def test_creates_new_session(self, mock_start):
        """A new session id creates a new kernel."""
        mock_start.return_value = (MagicMock(), MagicMock())
        session = server._get_or_create_session("new-session", {"MCP_ENDPOINT": "http://x"})
        self.assertIsNotNone(session)
        self.assertIn("new-session", server._kernels)
        mock_start.assert_called_once()

    @patch("ipybox.kernel.mcp_server._start_kernel")
    def test_reuses_existing_session(self, mock_start):
        """An existing session id reuses the same kernel (no new start)."""
        mock_start.return_value = (MagicMock(), MagicMock())
        s1 = server._get_or_create_session("existing", None)
        s2 = server._get_or_create_session("existing", None)
        self.assertIs(s1, s2)
        mock_start.assert_called_once()  # only started once


class TestStartKernelEnv(unittest.TestCase):
    """Tests for _start_kernel's kernel_env -> subprocess env propagation."""

    def setUp(self):
        # Avoid any real kernel launch / startup-script execution.
        self._orig_is_file = os.path.isfile
        self._orig_startup = server._STARTUP_SCRIPT
        server._STARTUP_SCRIPT = "/nonexistent/startup.py"
        os.path.isfile = MagicMock(return_value=False)

    def tearDown(self):
        os.path.isfile = self._orig_is_file
        server._STARTUP_SCRIPT = self._orig_startup

    @patch("jupyter_client.KernelManager")
    def test_injected_env_passed_to_start_kernel(self, mock_km_cls):
        """kernel_env vars must reach km.start_kernel(env=...) — NOT the ctor."""
        km = MagicMock()
        km.client.return_value = MagicMock()
        mock_km_cls.return_value = km

        server._start_kernel({"MCP_ENDPOINT": "http://mcp:8000/mcp/full", "KEEP": "1"})

        # The KernelManager must NOT get env in its constructor (silently dropped
        # by jupyter_client >= 8.9) and must receive it on start_kernel(env=...).
        _, ctor_kwargs = mock_km_cls.call_args
        self.assertNotIn("env", ctor_kwargs)
        _, sk_kwargs = km.start_kernel.call_args
        self.assertIn("env", sk_kwargs)
        self.assertEqual(sk_kwargs["env"]["MCP_ENDPOINT"], "http://mcp:8000/mcp/full")
        self.assertEqual(sk_kwargs["env"]["KEEP"], "1")

    @patch("jupyter_client.KernelManager")
    def test_start_kernel_without_env_still_works(self, mock_km_cls):
        """Calling without kernel_env must still start a kernel (env fallback)."""
        km = MagicMock()
        km.client.return_value = MagicMock()
        mock_km_cls.return_value = km

        server._start_kernel(None)

        _, sk_kwargs = km.start_kernel.call_args
        self.assertIn("env", sk_kwargs)  # parent os.environ copy
        self.assertNotIn("MCP_ENDPOINT", sk_kwargs["env"])


if __name__ == "__main__":
    unittest.main(verbosity=2)