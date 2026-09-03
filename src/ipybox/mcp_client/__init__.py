#!/usr/bin/env python3
"""
mcp_client — Thin async wrapper over mcp2cli.client for use inside ipybox.

The public helpers are **async** and carry an explicit ``_async`` suffix so it
is obvious they return coroutines. The ipybox kernel does not want async
functions (there is no reason for the agent to manage a loop); ``ipybox_helpers``
imports these ``*_async`` functions and wraps them into synchronous helpers
(``mcp_call``, ``mcp_list_upstreams``, ...) that the agent calls directly.

  - mcp_list_upstreams_async — list available upstream backends
  - mcp_list_actions_async    — list tools for a given upstream
  - mcp_describe_async        — describe a tool's schema
  - mcp_call_async            — call any MCP action with structured arguments

Endpoint resolution: reads MCP_ENDPOINT from environment if not passed
explicitly.  Inside ipybox, this is injected by the gateway policy
via the `execute_code` tool's kernel_env.
"""

import asyncio
import contextvars
import os
from typing import Any, Callable, Dict, List, Optional, Awaitable

from mcp2cli.client import (
    DEFAULT_ENDPOINT,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    _default_cache_dir,
    _call_tool_live,
    _format_tool_call_error,
    fetch_tool_list_async,
    format_tool_schema,
)

__version__ = "0.1.0"

__all__ = [
    "mcp_list_upstreams_async",
    "mcp_list_actions_async",
    "mcp_describe_async",
    "mcp_call_async",
    "DEFAULT_ENDPOINT",
    "set_endpoint_override",
    "reset_endpoint_override",
]


# ---------------------------------------------------------------------------
# Per-request endpoint override (ContextVar)
# ---------------------------------------------------------------------------
# The ipybox MCP server's prompt rendering (`_render_prompt`) calls helper
# functions like ``mcp_list_upstreams()`` that need to know which gateway
# compound endpoint to call back to.  The gateway sends this endpoint in the
# ``X-MCP-Endpoint`` HTTP header (configured per-compound in compounds.yaml).
# The ipybox MCP server reads this header and stores it in the ContextVar
# below *before* rendering the prompt.  The helpers then pick it up instead
# of falling back to the ``MCP_ENDPOINT`` environment variable.
#
# ContextVars are inherited by child tasks within the same thread/event-loop,
# so the override set in the prompt-rendering call chain propagates to
# ``_sync()`` → ``mcp_list_upstreams_async()`` → ``_endpoint()``.
# ---------------------------------------------------------------------------
_mcp_endpoint_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "mcp_endpoint_override", default=None
)


def set_endpoint_override(endpoint: str) -> contextvars.Token:
    """Temporarily override the MCP endpoint for the current request/task."""
    return _mcp_endpoint_ctx.set(endpoint)


def reset_endpoint_override(token: contextvars.Token) -> None:
    """Reset the endpoint override to its previous value."""
    _mcp_endpoint_ctx.reset(token)


def _endpoint(endpoint: Optional[str] = None) -> str:
    """Resolve the MCP endpoint URL.

    Resolution order:
      1. Explicit ``endpoint`` argument (passed by the caller).
      2. Per-request ContextVar override (set by the ipybox MCP server from
         the ``X-MCP-Endpoint`` HTTP header).
      3. ``MCP_ENDPOINT`` environment variable (set in docker-compose; acts
         as a fallback for direct access without the gateway).
      4. ``DEFAULT_ENDPOINT`` (compile-time default).
    """
    if endpoint is None:
        ctx_endpoint = _mcp_endpoint_ctx.get()
        if ctx_endpoint is not None:
            return ctx_endpoint
        endpoint = os.environ.get("MCP_ENDPOINT", DEFAULT_ENDPOINT)
    return endpoint


def _cache_dir() -> Any:
    """Resolve the cache directory (Path)."""
    return _default_cache_dir()


async def mcp_list_upstreams_async(endpoint: Optional[str] = None) -> str:
    """List available MCP upstreams (backend prefixes).

    Returns a formatted string listing upstream names.
    """
    ep = _endpoint(endpoint)
    tools = await fetch_tool_list_async(endpoint=ep, cache_dir=_cache_dir(), cache_ttl_s=3600)

    upstreams = set()
    for t in tools:
        name = t.get("name") or ""
        if name and "_" in name:
            upstreams.add(name.split("_", 1)[0])
        elif name:
            upstreams.add(name)

    if not upstreams:
        return "No upstreams available."
    return "Available MCP upstreams:\n" + "\n".join(f"- {s}" for s in sorted(upstreams))


def _resolve_action_id(upstream, action, tool_names):
    """Resolve a tool id from the split and/or combined addressing forms.

    Both conventions are accepted and normalise to the same upstream tool id,
    so ``mcp_call`` and ``mcp_describe`` agree on how a downstream action is
    addressed:

    - Split form: ``upstream`` + ``action`` (e.g. ``'k8s'``, ``'pods_get'``).
    - Combined form: ``action`` is a full proxied id (e.g. ``'k8s_pods_get'``).

    Resolution rules (deterministic, never silently guesses):

      * ``upstream`` given, ``action`` is an existing full id
          - belonging to ``upstream``  -> that id
          - belonging to *another*    -> ``ValueError`` (mismatch, no silent fix)
      * ``upstream`` given, ``f"{upstream}_{action}"`` exists -> that id
      * ``upstream`` omitted, ``action`` is an existing full id -> that id
      * anything else                -> ``ValueError``
          (notably an unprefixed ``action`` with no ``upstream`` when the
          endpoint prefixes tools, or an action that does not belong to the
          given upstream — we never fall back to mcp2cli's fuzzy suffix match)

    Notes:
    - Unprefixed/bare names have no special handling in the multiplexed
      (compound/gateway) case: a bare name is not a full id, so without an
      ``upstream`` it cannot be resolved. Skills that mention a tool without
      its prefix must pass ``upstream`` (or the code must use a direct
      non-prefixed endpoint).
    - ``tool_names`` should carry every proxied id the endpoint exposes.
    """
    if action is None:
        return None

    if upstream:
        if action in tool_names:
            # Full proxied id supplied. Trust it only if it belongs to the
            # requested upstream; never let a foreign id ride along silently.
            expected = f"{upstream}_"
            if action.startswith(expected):
                return action
            raise ValueError(
                f"Action '{action}' does not belong to upstream '{upstream}'."
            )
        candidate = f"{upstream}_{action}"
        if candidate in tool_names:
            return candidate
        raise ValueError(
            f"Action '{action}' not found under upstream '{upstream}'. "
            "Use mcp_list_actions(upstream) to see available unprefixed names, "
            "or pass the full proxied id (e.g. '<upstream>_<action>')."
        )

    if action in tool_names:
        return action
    raise ValueError(
        f"Action '{action}' not found. "
        "Pass the full proxied id (e.g. '<upstream>_<action>') or supply "
        "upstream=<prefix> for an unprefixed action name (see "
        "mcp_list_upstreams() / mcp_list_actions(upstream))."
    )


async def mcp_list_actions_async(upstream: str, endpoint: Optional[str] = None) -> str:
    """List actions (tools) for a specific MCP upstream.

    Arg:
        upstream: Upstream prefix (e.g. 'exec', 'ipybox', 'k8s').
        endpoint: MCP endpoint URL (defaults to MCP_ENDPOINT env or DEFAULT_ENDPOINT).
    """
    ep = _endpoint(endpoint)
    tools = await fetch_tool_list_async(endpoint=ep, cache_dir=_cache_dir(), cache_ttl_s=3600)

    matched = []
    for t in tools:
        name = t.get("name") or ""
        if name and name.split("_", 1)[0] == upstream:
            matched.append((name, t.get("description") or ""))

    if not matched:
        return f"No tools found for upstream '{upstream}'. Use mcp_list_upstreams() to see available upstreams."

    matched.sort(key=lambda x: x[0])
    lines = [f"{upstream}/"]
    # Expose the unprefixed action name (the tool id minus the upstream
    # prefix). This matches what skills/reusable code reference a tool by and
    # what mcp_describe(upstream=..., action=...) accepts.
    for tool_name, desc in matched:
        desc_short = desc.strip().splitlines()[0] if desc else ""
        action_name = tool_name.split("_", 1)[1] if "_" in tool_name else tool_name
        lines.append(f"- {action_name}  {desc_short}")
    return "\n".join(lines)


async def mcp_describe_async(action: str, upstream: Optional[str] = None, endpoint: Optional[str] = None) -> str:
    """Describe an MCP action's parameters and schema.

    Args:
        action: Action name/suffix (e.g. 'terminal_exec', 'pods_get') or a
            full proxied id (e.g. 'vscode_terminal_exec') when ``upstream`` is
            omitted. If ``upstream`` is given, ``action`` is treated as the
            unprefixed action name (or a full id that must belong to upstream).
        upstream: Optional upstream/backend prefix used to disambiguate an
            unprefixed ``action``.
        endpoint: MCP endpoint URL.
    """
    ep = _endpoint(endpoint)
    tools = await fetch_tool_list_async(endpoint=ep, cache_dir=_cache_dir(), cache_ttl_s=3600)

    tool_names = [t.get("name") for t in tools if t.get("name")]
    try:
        resolved = _resolve_action_id(upstream, action, tool_names)
    except ValueError as e:
        return f"Error: {e}"

    by_name = {t.get("name"): t for t in tools if t.get("name")}
    tool = by_name.get(resolved)
    if not tool:
        return f"Error: Action '{action}' not found."

    return format_tool_schema(tool)


def _content_block_to_dict(block):
    """Normalize an MCP ContentBlock into a JSON-able dict."""
    text = getattr(block, "text", None)
    if isinstance(text, str):
        return {"type": "text", "text": text}
    return {"type": getattr(block, "type", "unknown"), "raw": str(block)}


def _build_call_result(out_obj, upstream, action):
    """Convert an MCP SDK CallToolResult into a stable machine-readable dict.

    Schema (all keys always present):
      ok                 bool    True unless the call reported isError.
      is_error           bool    mirror of the downstream isError flag.
      upstream           str     upstream prefix used for the call.
      action             str     resolved tool id that was invoked.
      text               str     combined plain-text payload (primary).
      content            list    raw content blocks as JSON-able dicts
                                 ({type,text} for text; else {type,raw}).
      structured_content any     downstream structuredContent, if any
                                 (null for text-only tools).
    """
    is_error = bool(getattr(out_obj, "isError", False))
    blocks = [_content_block_to_dict(c) for c in (getattr(out_obj, "content", None) or [])]
    text_parts = [b["text"] for b in blocks if b.get("type") == "text"]
    return {
        "ok": not is_error,
        "is_error": is_error,
        "upstream": upstream,
        "action": action,
        "text": "\n".join(text_parts),
        "content": blocks,
        "structured_content": getattr(out_obj, "structuredContent", None),
    }


def _error_result(upstream, action, message):
    """A structured result representing a failed tool invocation."""
    return {
        "ok": False,
        "is_error": True,
        "upstream": upstream,
        "action": action,
        "text": message,
        "content": [],
        "structured_content": None,
    }


async def mcp_call_async(
    upstream: Optional[str] = None,
    action: Optional[str] = None,
    arguments: Dict[str, Any] = None,
    stdin: Optional[str] = None,
    timeout: int = DEFAULT_TOOL_TIMEOUT_SECONDS,
    endpoint: Optional[str] = None,
    progress_callback: Optional[Callable[[float, Optional[float], Optional[str]], Awaitable[None]]] = None,
) -> Dict[str, Any]:
    """Call any MCP action with structured JSON arguments.

    Args:
        upstream: Optional upstream prefix used to disambiguate an unprefixed
            ``action`` (e.g. 'exec', 'k8s'). When omitted, ``action`` must be a
            full proxied id (e.g. 'vscode_terminal_exec').
        action: Action name — either an unprefixed name (belonging to
            ``upstream``) or a full proxied id. If ``upstream`` is given and
            ``action`` is a full id, it must belong to ``upstream``.
        arguments: Action parameters as a dict.
        stdin: Optional content to pass as stdin.
        timeout: Timeout in seconds.
        endpoint: MCP endpoint URL.
        ctx: (unused, compatibility).
        progress_callback: Optional async callable invoked with
            ``(progress, total, message)`` when the upstream server sends
            progress notifications. When provided, the MCP client includes a
            ``progressToken`` in the request so progress-capable backends
            (e.g. ipybox's _keepalive during job_wait) emit notifications.

    Returns:
        A structured dict (see _build_call_result): ``ok``, ``is_error``,
        ``upstream``, ``action``, ``text`` (combined plain-text payload),
        ``content`` (raw blocks), and ``structured_content`` (when present).
        On resolution failure ``ok`` is False and ``text`` carries the error
        message (a string error is returned only if ``upstream`` could not be
        inferred from ``action``).
    """
    ep = _endpoint(endpoint)
    tools = await fetch_tool_list_async(endpoint=ep, cache_dir=_cache_dir(), cache_ttl_s=3600)
    tool_names = [t.get("name") for t in tools if t.get("name")]

    try:
        resolved_tool_id = _resolve_action_id(upstream, action, tool_names)
    except ValueError as e:
        return f"Error: {e}"
    if resolved_tool_id is None:
        return "Error: Action not provided (pass action=..., optionally with upstream=...)."
    if upstream is None:
        upstream = resolved_tool_id.split("_", 1)[0]

    call_args = dict(arguments) if arguments else {}
    if stdin is not None:
        call_args["stdin"] = stdin

    try:
        out_obj = await asyncio.wait_for(
            _call_tool_live(endpoint=ep, tool_id=resolved_tool_id, arguments=call_args,
                            progress_callback=progress_callback),
            timeout=timeout or DEFAULT_TOOL_TIMEOUT_SECONDS,
        )
        return _build_call_result(out_obj, upstream, resolved_tool_id)
    except Exception as e:
        return _error_result(
            upstream,
            resolved_tool_id,
            _format_tool_call_error(resolved_tool_id, ep, timeout or DEFAULT_TOOL_TIMEOUT_SECONDS, e),
        )