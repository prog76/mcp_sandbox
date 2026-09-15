#!/usr/bin/env python3
"""Regression for card t_c1eec8f7: the bridge ceiling must be self-explanatory.

Two properties of the two sync-bridge guard sites
(``extensions.core.mcp_call._sync`` and ``kernel.templating._run_to_completion``)
are locked here:

1. The ceiling stays env-configurable - the canonical
   ``IPYBOX_BRIDGE_CALL_TIMEOUT_SECONDS`` wins, the legacy
   ``MCP_BRIDGE_TIMEOUT_SECONDS`` alias is still honoured, and the default is
   unchanged at 30s.
2. The ``TimeoutError`` raised on expiry is actionable: it names the ceiling,
   the env var, and ``job_submit()`` / ``job_wait()`` as the supported path for
   operations longer than the ceiling. Before this change an agent hitting the
   ceiling saw only a bare timeout with no route forward.
"""

import asyncio
import os
import subprocess
import sys
import textwrap
import time
import unittest
from unittest.mock import MagicMock, patch

# Stub container-only deps so the modules import on the host (same pattern as
# test_bridge_timeout.py / test_ipybox_sessions.py).
sys.modules.setdefault("jupyter_client", MagicMock())

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ipybox.extensions.core import mcp_call as mcp_call_mod  # noqa: E402
from ipybox.kernel import templating  # noqa: E402

_ENV_KEYS = ("IPYBOX_BRIDGE_CALL_TIMEOUT_SECONDS", "MCP_BRIDGE_TIMEOUT_SECONDS")

# Resolution must be checked in a fresh interpreter: both modules read the env
# at import time, so reloading in-process would carry state between cases.
_RESOLVE = textwrap.dedent(
    """
    import sys
    from unittest.mock import MagicMock
    sys.path.insert(0, sys.argv[1])
    sys.modules.setdefault("jupyter_client", MagicMock())
    from ipybox.extensions.core import mcp_call
    from ipybox.kernel import templating
    print(mcp_call._BRIDGE_TIMEOUT_SECONDS, templating._BRIDGE_TIMEOUT_SECONDS)
    """
)


def _resolve(env):
    """Resolve the ceiling in both guard modules under ``env``."""
    clean = {k: v for k, v in os.environ.items() if k not in _ENV_KEYS}
    clean.update(env)
    proc = subprocess.run(
        [sys.executable, "-c", _RESOLVE, os.path.abspath(_SRC)],
        capture_output=True, text=True, env=clean, timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return [float(v) for v in proc.stdout.split()]


class TestCeilingResolution(unittest.TestCase):
    """Both guard sites read the same knob; default unchanged."""

    def test_unset_keeps_the_30s_default(self):
        self.assertEqual(_resolve({}), [30.0, 30.0])

    def test_legacy_alias_is_still_honoured(self):
        self.assertEqual(_resolve({"MCP_BRIDGE_TIMEOUT_SECONDS": "45"}), [45.0, 45.0])

    def test_canonical_name_wins_over_the_legacy_alias(self):
        self.assertEqual(
            _resolve({
                "IPYBOX_BRIDGE_CALL_TIMEOUT_SECONDS": "600",
                "MCP_BRIDGE_TIMEOUT_SECONDS": "45",
            }),
            [600.0, 600.0],
        )


class TestActionableTimeoutText(unittest.TestCase):
    """Expiry must tell the caller what to do instead of a bare timeout."""

    def assertActionable(self, message, ceiling):
        self.assertIn("Bridge call did not complete within", message)
        self.assertIn("%.0fs" % ceiling, message)
        self.assertIn("job_submit()", message)
        self.assertIn("job_wait()", message)
        self.assertIn("IPYBOX_BRIDGE_CALL_TIMEOUT_SECONDS", message)

    def test_mcp_call_guard_names_job_submit(self):
        async def main():
            async def hang():
                await asyncio.sleep(60)

            with patch.object(mcp_call_mod, "_BRIDGE_TIMEOUT_SECONDS", 2):
                start = time.monotonic()
                with self.assertRaises(TimeoutError) as caught:
                    mcp_call_mod._sync(hang())
                self.assertLess(time.monotonic() - start, 10)
            self.assertActionable(str(caught.exception), 2)

        asyncio.run(main())

    def test_templating_guard_names_job_submit(self):
        async def main():
            async def hang():
                await asyncio.sleep(60)

            with patch.object(templating, "_BRIDGE_TIMEOUT_SECONDS", 2):
                with self.assertRaises(TimeoutError) as caught:
                    templating._run_to_completion(hang())
            self.assertActionable(str(caught.exception), 2)

        asyncio.run(main())

    def test_interpolated_ceiling_tracks_the_configured_value(self):
        async def main():
            async def hang():
                await asyncio.sleep(60)

            with patch.object(mcp_call_mod, "_BRIDGE_TIMEOUT_SECONDS", 7):
                with self.assertRaises(TimeoutError) as caught:
                    mcp_call_mod._sync(hang())
            self.assertIn("within 7s", str(caught.exception))

        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
