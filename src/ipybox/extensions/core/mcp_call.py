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
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from ipybox import mcp_client

# Max time a synchronous bridge call may block the event-loop thread when a loop
# is already running (the async prompt path). A hung upstream must never be
# allowed to freeze the whole FastMCP loop — this is the second line of defence
# behind the per-call wait_for in mcp2cli's _fetch_tool_list_live. If a bridge
# call exceeds this, we raise instead of wedging the server.
_BRIDGE_TIMEOUT_SECONDS = float(os.environ.get("MCP_BRIDGE_TIMEOUT_SECONDS", "30"))


def _sync(coro):
    """Run a coroutine to completion synchronously.

    If a loop is already running (e.g. when called from inside an async template
    handler) the coroutine is executed in a worker thread; otherwise it is
    simply driven by :func:`asyncio.run`.

    The worker wait is bounded by ``_BRIDGE_TIMEOUT_SECONDS`` and the pool is
    shut down without waiting, so a coroutine that never returns can never freeze
    the event loop (the root cause of the ipybox-wide hang) nor block executor
    teardown.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    ctx = contextvars.copy_context()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(ctx.run, lambda: asyncio.run(coro))
        return future.result(timeout=_BRIDGE_TIMEOUT_SECONDS)
    except FutureTimeoutError as e:
        raise TimeoutError(
            f"Bridge call did not complete within {_BRIDGE_TIMEOUT_SECONDS:.0f}s "
            f"on the event-loop thread; aborting to keep the server responsive"
        ) from e
    finally:
        # Never block the loop thread waiting for a possibly-stuck worker.
        pool.shutdown(wait=False)


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
        failure ``ok`` is False, ``is_error`` is True, and ``text`` carries the error message.
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

    def mcp_describe(action=None, upstream=None, endpoint=None):
        """Describe an MCP action's schema.

        Accepts either a full proxied id (``mcp_describe('k8s_pods_get')``) or,
        with ``upstream``, an unprefixed action name to disambiguate
        (``mcp_describe(upstream='k8s', action='pods_get')``).
        """
        return _sync(mcp_client.mcp_describe_async(action=action, upstream=upstream, endpoint=endpoint))

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
