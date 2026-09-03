#!/usr/bin/env python3
"""
Tests for the shared template engine used by both prompts and skills:

  - ipybox.kernel.templating.render_template / render_template_async
    (the single engine behind prompt templating + get_skill)
  - get_skill renders skill content through that engine
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ipybox.kernel import templating  # noqa: E402
from ipybox.kernel.templating import (  # noqa: E402
    _parse_template_args,
    render_template,
    render_template_async,
)


class _RegistryStub:
    """Minimal stand-in for ExtensionRegistry (only what templating uses)."""

    def __init__(self):
        self._helpers = {}

    def add(self, name, fn, description="", category=""):
        self._helpers[name] = fn

    def get(self, name):
        return self._helpers.get(name)


class TestParseTemplateArgs(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_parse_template_args(""), [])
        self.assertEqual(_parse_template_args("   "), [])

    def test_literal_parse(self):
        self.assertEqual(_parse_template_args("'a', 2, [3,4]"), ["a", 2, [3, 4]])

    def test_fallback_quote_trim(self):
        self.assertEqual(_parse_template_args("'a', \"b\""), ["a", "b"])
        self.assertEqual(_parse_template_args("a, b"), ["a", "b"])


class TestRenderTemplate(unittest.TestCase):
    def _render(self, text, helpers):
        reg = _RegistryStub()
        for name, fn in helpers.items():
            reg.add(name, fn)
        with patch.object(templating, "get_registry", return_value=reg):
            return text  # replaced below per-call

    def test_sync_helper_expanded(self):
        reg = _RegistryStub()
        reg.add("greet", lambda name="x": f"hi {name}")
        with patch.object(templating, "get_registry", return_value=reg):
            out = render_template("Hello {{ greet('bob') }}!")
        self.assertEqual(out, "Hello hi bob!")

    def test_async_helper_expanded(self):
        async def shout(s):
            return s.upper()
        reg = _RegistryStub()
        reg.add("shout", shout)
        with patch.object(templating, "get_registry", return_value=reg):
            out = render_template("{{ shout('ab') }}")
        self.assertEqual(out, "AB")

    def test_render_template_async_awaits_coroutine(self):
        async def shout(s):
            return s.upper()
        reg = _RegistryStub()
        reg.add("shout", shout)
        with patch.object(templating, "get_registry", return_value=reg):
            out = asyncio.run(render_template_async("{{ shout('xy') }}"))
        self.assertEqual(out, "XY")

    def test_unknown_helper_left_verbatim(self):
        reg = _RegistryStub()
        with patch.object(templating, "get_registry", return_value=reg):
            out = render_template("a {{ nope('x') }} b")
        self.assertEqual(out, "a {{ nope('x') }} b")

    def test_helper_error_rendered_as_marker(self):
        def boom():
            raise ValueError("bad")
        reg = _RegistryStub()
        reg.add("boom", boom)
        with patch.object(templating, "get_registry", return_value=reg):
            out = render_template("before {{ boom() }} after")
        self.assertIn("before [template error: boom()] after", out)

    def test_multiple_and_intervening_text(self):
        reg = _RegistryStub()
        reg.add("a", lambda: "A")
        reg.add("b", lambda: "B")
        with patch.object(templating, "get_registry", return_value=reg):
            out = render_template("{{ a() }} x {{ b() }}{{ a() }}")
        self.assertEqual(out, "A x BA")


class TestGetSkillRendering(unittest.TestCase):
    """get_skill passes skill content through the shared render engine."""

    def test_get_skill_renders_content(self):
        from ipybox.extensions.core import skill_mgmt

        reg = _RegistryStub()
        with tempfile.TemporaryDirectory() as d:
            body = "Run {{ list_functions() }}"
            with open(os.path.join(d, "myskill.md"), "w") as f:
                f.write(body)
            with patch.dict(os.environ, {"IPYBOX_SKILLS_DIR": d}):
                skill_mgmt.register(reg)
                get_skill = reg._helpers["get_skill"]
                with patch.object(skill_mgmt, "render_template", side_effect=lambda t: f"<{t}>"):
                    out = get_skill("myskill")
        self.assertEqual(out, f"<{body}>")


if __name__ == "__main__":
    unittest.main(verbosity=2)
