#!/usr/bin/env python3
"""Tests for live prompt reload (t_439832ea).

Editing a prompt file under IPYBOX_PROMPTS_DIR must be visible on the next
prompts/get without restarting the server - prompts and skills are both
bind-mounted live config and must behave the same. Registration (the
name/description listed by prompts/list) stays a startup operation: a *new*
prompt file still requires a restart to appear.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.modules.setdefault("ijson", MagicMock())
sys.modules.setdefault("jupyter_client", MagicMock())
sys.modules.setdefault("mcp2cli", MagicMock())
sys.modules.setdefault("mcp2cli.client", MagicMock())

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import ipybox.kernel.mcp_server as server  # noqa: E402
from fastmcp import Client, FastMCP  # noqa: E402


def _write_prompt(directory, name, body):
    path = os.path.join(directory, name + ".md")
    with open(path, "w") as fh:
        fh.write(
            "---\n"
            f"name: {name}\n"
            "description: live-reload test prompt\n"
            "---\n"
            f"{body}"
        )
    return path


class TestPromptLiveReload(unittest.TestCase):
    """prompts/get must reflect on-disk edits without a server restart."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def _server_with(self, prompt_dir):
        """A throwaway FastMCP instance with prompts registered from dir."""
        target = FastMCP("test-prompts")
        prev = server._PROMPTS_DIR
        server._PROMPTS_DIR = prompt_dir
        try:
            server._register_prompts(target)
        finally:
            server._PROMPTS_DIR = prev
        return target

    def _get_text(self, target, name):
        async def _run():
            async with Client(target) as client:
                result = await client.get_prompt(name)
            return result.messages[0].content.text
        return asyncio.run(_run())

    def test_edit_is_served_without_restart(self):
        """Rewriting the file changes the body served by prompts/get."""
        path = _write_prompt(self._tmp.name, "live_edit", "BODY-ONE")
        target = self._server_with(self._tmp.name)
        self.assertEqual(self._get_text(target, "live_edit"), "BODY-ONE")
        with open(path, "w") as fh:
            fh.write(
                "---\n"
                "name: live_edit\n"
                "description: live-reload test prompt\n"
                "---\n"
                "BODY-TWO"
            )
        self.assertEqual(self._get_text(target, "live_edit"), "BODY-TWO")

    def test_missing_file_serves_startup_body(self):
        """A deleted prompt file degrades to the startup body, not a 500."""
        path = _write_prompt(self._tmp.name, "live_gone", "BODY-KEEP")
        target = self._server_with(self._tmp.name)
        self.assertEqual(self._get_text(target, "live_gone"), "BODY-KEEP")
        os.unlink(path)
        self.assertEqual(self._get_text(target, "live_gone"), "BODY-KEEP")

    def test_registration_lists_name_and_description(self):
        """prompts/list still carries frontmatter name+description."""
        _write_prompt(self._tmp.name, "live_reg", "BODY")
        target = self._server_with(self._tmp.name)

        async def _run():
            async with Client(target) as client:
                prompts = await client.list_prompts()
            return {p.name: p.description for p in prompts}

        info = asyncio.run(_run())
        self.assertEqual(info.get("live_reg"), "live-reload test prompt")


if __name__ == "__main__":
    unittest.main()
