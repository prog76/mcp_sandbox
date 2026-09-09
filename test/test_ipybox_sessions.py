#!/usr/bin/env python3
"""
Tests for ipybox per-session kernel isolation and idle cleanup.

Covers the pure session-resolution and reaping logic in
the ipybox kernel MCP server (no real kernel is started).
"""

import sys
import os
import signal
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

    def test_reap_removes_workdir(self):
        """Reaping a session also removes its per-session temp workdir."""
        import tempfile
        workdir = tempfile.mkdtemp(prefix="ipybox-test-workdir-")
        self.assertTrue(os.path.isdir(workdir))
        sid = "workdir-session"
        session = self._make_session(
            sid, time.monotonic() - server.IPYBOX_IDLE_TIMEOUT - 1
        )
        session.workdir = workdir
        server._reap_idle_sessions(now=time.monotonic())
        self.assertFalse(os.path.isdir(workdir))
        self.assertNotIn(sid, server._kernels)

    def test_reap_without_workdir_is_noop_for_cleanup(self):
        """A session with no workdir is reaped normally (no rmtree path)."""
        sid = "no-workdir-session"
        self._make_session(sid, time.monotonic() - server.IPYBOX_IDLE_TIMEOUT - 1)
        reaped = server._reap_idle_sessions(now=time.monotonic())
        self.assertIn(sid, reaped)


class TestShutdownKernelBounded(unittest.TestCase):
    """Regression tests for the zmq-context GC freeze (2026-09-09).

    jupyter_client's shutdown_kernel never stops the channels of the
    ``km.client()`` handle, so its private zmq Context outlived reaped
    sessions with open sockets. When that Context was later GC'd on the
    asyncio event loop thread, ``Context.__del__ → destroy() → term()``
    blocked forever and froze the whole server. The teardown must call
    ``kc.stop_channels()`` (which closes all channel sockets and destroys
    the per-client context) BEFORE ``km.shutdown_kernel``.
    """

    def test_stops_client_channels_before_kernel_shutdown(self):
        """kc.stop_channels runs first — sockets closed before teardown."""
        km, kc = MagicMock(), MagicMock()
        order = []
        km.shutdown_kernel.side_effect = lambda **kw: order.append("shutdown")
        kc.stop_channels.side_effect = lambda: order.append("stop_channels")

        server._shutdown_kernel_bounded(km, kc)

        self.assertEqual(order, ["stop_channels", "shutdown"])
        km.shutdown_kernel.assert_called_once_with(now=True)

    def test_stop_channels_failure_still_shuts_kernel_down(self):
        """A broken client must not prevent the kernel teardown."""
        km, kc = MagicMock(), MagicMock()
        kc.stop_channels.side_effect = RuntimeError("boom")

        server._shutdown_kernel_bounded(km, kc)

        km.shutdown_kernel.assert_called_once_with(now=True)

    def test_no_client_still_shuts_kernel_down(self):
        """kc=None (legacy call sites) still tears the kernel down."""
        km = MagicMock()
        server._shutdown_kernel_bounded(km, None)
        km.shutdown_kernel.assert_called_once_with(now=True)

    def test_reap_stops_client_channels(self):
        """Reaping a session must also stop its client channels."""
        now = time.monotonic()
        sid = "landmine-session"
        session = server.KernelSession(
            km=MagicMock(), kc=MagicMock(), last_used=now - server.IPYBOX_IDLE_TIMEOUT - 1
        )
        server._kernels[sid] = session
        reaped = server._reap_idle_sessions(now=now)
        self.assertIn(sid, reaped)
        session.kc.stop_channels.assert_called_once()
        session.km.shutdown_kernel.assert_called_once_with(now=True)


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


class TestSessionWorkdirPath(unittest.TestCase):
    """Tests for _session_workdir_path (pure, side-effect-free path computation)."""

    def setUp(self):
        self._orig_base = server._IPYBOX_WORKDIR_BASE
        server._IPYBOX_WORKDIR_BASE = "/tmp/ipybox-test"

    def tearDown(self):
        server._IPYBOX_WORKDIR_BASE = self._orig_base

    def test_under_ipybox_tmp_base(self):
        path = server._session_workdir_path("abc")
        self.assertTrue(path.startswith("/tmp/ipybox-test/"))

    def test_same_session_id_is_stable(self):
        self.assertEqual(
            server._session_workdir_path("foo"),
            server._session_workdir_path("foo"),
        )

    def test_different_session_ids_differ(self):
        self.assertNotEqual(
            server._session_workdir_path("s1"),
            server._session_workdir_path("s2"),
        )

    def test_two_uuids_differ(self):
        import uuid
        u1 = str(uuid.uuid4())
        u2 = str(uuid.uuid4())
        self.assertNotEqual(
            server._session_workdir_path(u1),
            server._session_workdir_path(u2),
        )

    def test_sanitizes_unsafe_chars(self):
        path = server._session_workdir_path("hello world/foo")
        rel = path[len("/tmp/ipybox-test/"):]
        self.assertNotIn(" ", rel)
        self.assertNotIn("/", rel)

    def test_path_traversal_neutralized(self):
        path = server._session_workdir_path("../../etc")
        self.assertNotIn("..", path)
        self.assertTrue(path.startswith("/tmp/ipybox-test/"))

    def test_env_override_base(self):
        server._IPYBOX_WORKDIR_BASE = "/tmp/ipybox-custom"
        path = server._session_workdir_path("sid")
        self.assertTrue(path.startswith("/tmp/ipybox-custom/"))


class TestStartKernelWorkdir(unittest.TestCase):
    """Tests for _start_kernel workdir / cwd handling."""

    def setUp(self):
        self._orig_is_file = os.path.isfile
        self._orig_startup = server._STARTUP_SCRIPT
        server._STARTUP_SCRIPT = "/nonexistent/startup.py"
        os.path.isfile = MagicMock(return_value=False)

    def tearDown(self):
        os.path.isfile = self._orig_is_file
        server._STARTUP_SCRIPT = self._orig_startup

    @patch("jupyter_client.KernelManager")
    def test_workdir_creates_dir_and_passes_cwd(self, mock_km_cls):
        """A workdir is created and forwarded as cwd= to start_kernel."""
        km = MagicMock()
        km.client.return_value = MagicMock()
        mock_km_cls.return_value = km
        workdir = "/tmp/ipybox-test-xyz"
        with patch("os.makedirs") as mock_makedirs:
            server._start_kernel({"K": "v"}, workdir=workdir)
        mock_makedirs.assert_called_once_with(workdir, exist_ok=True)
        _, sk_kwargs = km.start_kernel.call_args
        self.assertEqual(sk_kwargs["cwd"], workdir)
        self.assertEqual(sk_kwargs["env"]["K"], "v")

    @patch("jupyter_client.KernelManager")
    def test_no_workdir_no_makedirs_no_cwd(self, mock_km_cls):
        """Without a workdir, no dir is created and cwd is not passed."""
        km = MagicMock()
        km.client.return_value = MagicMock()
        mock_km_cls.return_value = km
        with patch("os.makedirs") as mock_makedirs:
            server._start_kernel({"K": "v"})
        mock_makedirs.assert_not_called()
        _, sk_kwargs = km.start_kernel.call_args
        self.assertNotIn("cwd", sk_kwargs)
        self.assertEqual(sk_kwargs["env"]["K"], "v")


class TestSessionWorkdir(unittest.TestCase):
    """Tests that _get_or_create_session wires up per-session workdirs."""

    def setUp(self):
        self._orig_kernels = server._kernels
        self._orig_base = server._IPYBOX_WORKDIR_BASE
        server._kernels = {}
        server._IPYBOX_WORKDIR_BASE = "/tmp/ipybox-test"

    def tearDown(self):
        server._kernels = self._orig_kernels
        server._IPYBOX_WORKDIR_BASE = self._orig_base

    @patch("ipybox.kernel.mcp_server._start_kernel")
    def test_new_session_gets_workdir(self, mock_start):
        mock_start.return_value = (MagicMock(), MagicMock())
        session = server._get_or_create_session("sess-1", None)
        self.assertIsNotNone(session.workdir)
        self.assertTrue(session.workdir.startswith("/tmp/ipybox-test/"))
        _, kwargs = mock_start.call_args
        self.assertEqual(kwargs["workdir"], session.workdir)

    @patch("ipybox.kernel.mcp_server._start_kernel")
    def test_reused_session_keeps_same_workdir(self, mock_start):
        mock_start.return_value = (MagicMock(), MagicMock())
        s1 = server._get_or_create_session("reused", None)
        s2 = server._get_or_create_session("reused", None)
        self.assertIs(s1, s2)
        mock_start.assert_called_once()
        self.assertEqual(s1.workdir, s2.workdir)

    @patch("ipybox.kernel.mcp_server._start_kernel")
    def test_distinct_sessions_get_distinct_workdirs(self, mock_start):
        mock_start.return_value = (MagicMock(), MagicMock())
        a = server._get_or_create_session("a", None)
        b = server._get_or_create_session("b", None)
        self.assertNotEqual(a.workdir, b.workdir)
        self.assertEqual(mock_start.call_count, 2)


class TestReaperTeardownSafety(unittest.TestCase):
    """Regression tests for the 2026-09-08 total-outage freeze.

    The reaper used to hold ``_kernels_lock`` across the blocking
    ``km.shutdown_kernel(now=True)`` zmq teardown. When a kernel process was
    wedged, that teardown hung forever; every subsequent ``execute_code`` then
    blocked the asyncio event loop on the same lock and the whole MCP server
    went silent (no responses, no logs).
    """

    def setUp(self):
        self._orig_kernels = server._kernels
        self._orig_timeout = server.IPYBOX_IDLE_TIMEOUT
        self._orig_shutdown_timeout = server._KERNEL_SHUTDOWN_TIMEOUT
        server._kernels = {}
        server.IPYBOX_IDLE_TIMEOUT = 600

    def tearDown(self):
        server._kernels = self._orig_kernels
        server.IPYBOX_IDLE_TIMEOUT = self._orig_timeout
        server._KERNEL_SHUTDOWN_TIMEOUT = self._orig_shutdown_timeout

    def test_wedged_teardown_does_not_block_new_sessions(self):
        """While a reap is stuck in shutdown_kernel, _get_or_create_session
        (which needs _kernels_lock) must still complete quickly."""
        now = time.monotonic()
        session = server.KernelSession(
            km=MagicMock(), kc=MagicMock(), last_used=now - server.IPYBOX_IDLE_TIMEOUT - 1
        )
        stuck_done = threading.Event()

        def _wedged_shutdown(*args, **kwargs):
            stuck_done.wait(10.0)

        session.km.shutdown_kernel.side_effect = _wedged_shutdown
        server._kernels["stuck"] = session
        server._KERNEL_SHUTDOWN_TIMEOUT = 0.2

        reaper = threading.Thread(
            target=server._reap_idle_sessions, kwargs={"now": now}, daemon=True
        )
        reaper.start()

        # The victim must be unregistered quickly (lock released before any
        # kernel teardown work happens) ...
        deadline = time.monotonic() + 5.0
        while "stuck" in server._kernels and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertNotIn("stuck", server._kernels)

        # ... and creating a NEW session must not block on the reaper's
        # wedged teardown.
        with patch("ipybox.kernel.mcp_server._start_kernel") as mock_start:
            mock_start.return_value = (MagicMock(), MagicMock())
            t0 = time.monotonic()
            server._get_or_create_session("fresh", None)
            elapsed = time.monotonic() - t0
        self.assertIn("fresh", server._kernels)
        self.assertLess(elapsed, 5.0)

        stuck_done.set()
        reaper.join(10.0)
        self.assertFalse(reaper.is_alive())

    def test_shutdown_kernel_bounded_returns_despite_hang(self):
        """_shutdown_kernel_bounded never waits longer than the timeout."""
        stuck_done = threading.Event()

        def _hang(*args, **kwargs):
            stuck_done.wait(10.0)

        km = MagicMock()
        km.provisioner = None  # no pid available → no kill attempted
        km.shutdown_kernel.side_effect = _hang
        t0 = time.monotonic()
        server._shutdown_kernel_bounded(km, timeout=0.2)
        elapsed = time.monotonic() - t0
        stuck_done.set()  # let the daemon helper thread finish
        self.assertLess(elapsed, 5.0)

    def test_shutdown_kernel_bounded_kills_stuck_kernel(self):
        """When the graceful shutdown hangs, the kernel process is SIGKILLed."""
        stuck_done = threading.Event()

        def _hang(*args, **kwargs):
            stuck_done.wait(10.0)

        km = MagicMock()
        km.provisioner.process_pid = 424242
        km.shutdown_kernel.side_effect = _hang
        kills = []
        t0 = time.monotonic()
        with patch.object(
            server.os, "kill", side_effect=lambda pid, sig: kills.append((pid, sig))
        ):
            server._shutdown_kernel_bounded(km, timeout=0.2)
        elapsed = time.monotonic() - t0
        stuck_done.set()
        self.assertIn((424242, signal.SIGKILL), kills)
        self.assertLess(elapsed, 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestResolveSessionIdFromHeader(unittest.TestCase):
    """Tests for the ctx.request_context.request.headers fallback path."""

    def test_falls_back_to_header_when_env_unresolved(self):
        """When kernel_env has the literal placeholder, fall back to header."""
        fake_ctx = MagicMock()
        fake_ctx.request_context = MagicMock()
        fake_ctx.request_context.request = MagicMock()
        fake_ctx.request_context.request.headers = {"Mcp-Session-Id": "header-session-123"}

        result = server._resolve_session_id(
            None,
            {"MCP_SESSION_ID": "${request_header:Mcp-Session-Id}"},
            ctx=fake_ctx,
        )
        self.assertEqual(result, "header-session-123")

    def test_falls_back_to_header_when_env_missing(self):
        """When kernel_env is None, fall back to header."""
        fake_ctx = MagicMock()
        fake_ctx.request_context = MagicMock()
        fake_ctx.request_context.request = MagicMock()
        fake_ctx.request_context.request.headers = {"Mcp-Session-Id": "header-only"}

        result = server._resolve_session_id(None, None, ctx=fake_ctx)
        self.assertEqual(result, "header-only")

    def test_env_resolved_takes_priority_over_header(self):
        """A resolved env value wins over the header."""
        fake_ctx = MagicMock()
        fake_ctx.request_context = MagicMock()
        fake_ctx.request_context.request = MagicMock()
        fake_ctx.request_context.request.headers = {"Mcp-Session-Id": "header-val"}

        result = server._resolve_session_id(
            None,
            {"MCP_SESSION_ID": "env-val"},
            ctx=fake_ctx,
        )
        self.assertEqual(result, "env-val")

    def test_header_missing_generates_fresh_uuid(self):
        """No header, no env → fresh uuid."""
        fake_ctx = MagicMock()
        fake_ctx.request_context = MagicMock()
        fake_ctx.request_context.request = MagicMock()
        fake_ctx.request_context.request.headers = {}

        result = server._resolve_session_id(None, None, ctx=fake_ctx)
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_ctx_access_failure_falls_through(self):
        """If ctx.request_context access raises, fall through to uuid."""
        fake_ctx = MagicMock()
        type(fake_ctx).request_context = MagicMock(side_effect=AttributeError("no ctx"))

        result = server._resolve_session_id(None, None, ctx=fake_ctx)
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_explicit_session_id_wins_over_header(self):
        """Explicit session_id arg wins over both env and header."""
        fake_ctx = MagicMock()
        fake_ctx.request_context = MagicMock()
        fake_ctx.request_context.request = MagicMock()
        fake_ctx.request_context.request.headers = {"Mcp-Session-Id": "header-val"}

        result = server._resolve_session_id("explicit-id", None, ctx=fake_ctx)
        self.assertEqual(result, "explicit-id")
