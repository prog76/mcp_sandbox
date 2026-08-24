"""mcp_call extension — synchronous MCP client helpers exposed to the kernel.

Thin synchronous wrappers over :mod:`ipybox.mcp_client` (which in turn wraps
mcp2cli). The per-request endpoint-override ContextVar lives in
:mod:`ipybox.mcp_client`, so these helpers automatically honour the
``X-MCP-Endpoint`` header path used by the server's prompt templating.

Calls go through the ``ipybox.mcp_client`` module (not ``from import``) so the
underlying async helpers can be replaced for testing.
"""

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor

from ipybox import mcp_client


def _sync(coro):
    """Run a coroutine to completion synchronously.

    If a loop is already running (e.g. when called from inside an async template
    handler) the coroutine is executed in a worker thread; otherwise it is
    simply driven by :func:`asyncio.run`.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(ctx.run, lambda: asyncio.run(coro)).result()


def register(registry):
    """Register the synchronous MCP call helpers."""

    def mcp_call(upstream=None, action=None, arguments=None, stdin=None, timeout=None, endpoint=None):
        """Synchronously call any MCP action with structured JSON arguments.

        Both addressing conventions are accepted and resolve to the same
        upstream tool:

          * ``mcp_call(upstream, action, arguments, ...)`` — split form
            (e.g. ``mcp_call('k8s', 'pods_get', {...})``), which is the only
            way to unambiguously target a bare/unprefixed action name.
          * ``mcp_call(action, arguments, ...)`` — combined form where
            ``action`` is the full proxied id (e.g. ``mcp_call('k8s_pods_get',
            {...})``). ``upstream`` may be omitted because the full id already
            names the upstream; when omitted it is inferred from the id.

        If ``upstream`` is given and ``action`` is a full id, that id must
        belong to ``upstream`` (a mismatch is an error, never silently fixed).

        Returns a dict with keys ``ok``, ``is_error``, ``upstream``, ``action``,
        ``text``, ``content`` and ``structured_content``. On tool-resolution
        failure a string error is returned instead.
        """
        return _sync(
            mcp_client.mcp_call_async(
                upstream=upstream,
                action=action,
                arguments=arguments or {},
                stdin=stdin,
                timeout=timeout or mcp_client.DEFAULT_TOOL_TIMEOUT_SECONDS,
                endpoint=endpoint,
            )
        )

    def mcp_call_text(upstream=None, action=None, arguments=None, stdin=None, timeout=None, endpoint=None):
        """Call an MCP action and return only the text payload."""
        result = mcp_call(upstream, action, arguments, stdin, timeout, endpoint)
        return result["text"] if isinstance(result, dict) else str(result)

    def mcp_list_upstreams(endpoint=None):
        """List available MCP upstreams."""
        return _sync(mcp_client.mcp_list_upstreams_async(endpoint=endpoint))

    def mcp_list_actions(upstream, endpoint=None):
        """List actions (tools) for a specific upstream."""
        return _sync(mcp_client.mcp_list_actions_async(upstream=upstream, endpoint=endpoint))

    def mcp_describe(action_id=None, upstream=None, endpoint=None):
        """Describe an MCP action's schema.

        Accepts either a full proxied id (``mcp_describe('k8s_pods_get')``) or,
        with ``upstream``, an unprefixed action name to disambiguate
        (``mcp_describe(upstream='k8s', action_id='pods_get')``).
        """
        return _sync(mcp_client.mcp_describe_async(action_id=action_id, upstream=upstream, endpoint=endpoint))

    registry.add("mcp_call", mcp_call,
                 description="Synchronously call any MCP action", category="core")
    registry.add("mcp_call_text", mcp_call_text,
                 description="Call MCP action and return only text", category="core")
    registry.add("mcp_list_upstreams", mcp_list_upstreams,
                 description="List available MCP upstreams", category="core")
    registry.add("mcp_list_actions", mcp_list_actions,
                 description="List actions for an upstream", category="core")
    registry.add("mcp_describe", mcp_describe,
                 description="Describe an MCP action's schema", category="core")
