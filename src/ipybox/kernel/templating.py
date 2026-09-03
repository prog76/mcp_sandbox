"""Shared template rendering for prompts and skills.

Both MCP prompts (``ipybox/kernel/mcp_server.py``) and kernel skills
(``extensions/core/skill_mgmt.py``) support ``{{ helper(args) }}``
substitution at read/fetch time.  A single engine lives here so the two paths
behave identically: any registered kernel helper (``list_skills()``,
``mcp_list_upstreams()``, ``mcp_call(...)``, ...) can appear inside a prompt
*or* a skill body and is expanded on return.
"""

import asyncio
import contextvars
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from ipybox.kernel.extensions import get_registry

log = logging.getLogger("ipybox.templating")

_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\s*([^)]*)\)\s*\}\}")


def _parse_template_args(args_str: str) -> list:
    """Parse a ``{{ fn(arg1, arg2) }}`` argument list into Python values.

    Prefers a real literal parse (strings, numbers, dicts/lists) and falls
    back to naive comma splitting with quote trimming.
    """
    if not args_str.strip():
        return []
    try:
        import ast
        parsed = ast.literal_eval("[" + args_str + "]")
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
    out = []
    for tok in args_str.split(","):
        tok = tok.strip()
        if len(tok) >= 2 and tok[0] in ("'", '"') and tok[-1] == tok[0]:
            tok = tok[1:-1]
        out.append(tok)
    return out


async def render_template_async(text: str) -> str:
    """Render ``{{ helper(args) }}`` substitutions (awaitable helpers supported).

    Unknown helpers, and helpers that raise, are left/rendered as a
    ``[template error: ...]`` marker — never fatal.
    """
    registry = get_registry()
    out = []
    last = 0
    for m in _TEMPLATE_RE.finditer(text):
        out.append(text[last:m.start()])
        name = m.group(1)
        args_str = m.group(2)
        fn = registry.get(name)
        if fn is None:
            out.append(m.group(0))
        else:
            try:
                args = _parse_template_args(args_str)
                result = fn(*args)
                if asyncio.iscoroutine(result):
                    result = await result
                out.append(str(result))
            except Exception as e:
                log.warning("Template call %s(%s) failed: %s", name, args_str, e)
                out.append(f"[template error: {name}({args_str})]")
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def _run_to_completion(coro):
    """Drive a coroutine to completion from a synchronous caller.

    If an event loop is already running (e.g. inside the async prompt path)
    the coroutine is executed in a worker thread; otherwise :func:`asyncio.run`
    drives it directly.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(ctx.run, lambda: asyncio.run(coro)).result()


def render_template(text: str) -> str:
    """Synchronous rendering for kernel-side callers (e.g. ``get_skill``).

    Awaitable helper results are driven to completion internally, so skills
    can embed ``{{ mcp_call(...) }}`` even though ``get_skill`` runs in the
    synchronous kernel context.
    """
    return _run_to_completion(render_template_async(text))
