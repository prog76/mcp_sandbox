#!/usr/bin/env python3
"""
ipybox_mcp_server — MCP server that wraps stateful IPython kernels.

Exposes an MCP server (streamable HTTP on :9006) with two tools:
  - execute_code(code, session_id=None, kernel_env=None) — runs Python in a
    stateful IPython kernel, isolated per session.
  - mcp_call(upstream, action, arguments, stdin=None, timeout=120,
    endpoint=None) — calls any downstream MCP action (exec, k8s, ...) through
    the gateway from the server process (no IPython shell needed).  This is
    the MCP-tool equivalent of the kernel's ``mcp_call`` builtin helper, so
    the agent can proxy to other backends without going through execute_code.

Sessions:
  - Every call is associated with a *session id*.
  - The session id is returned at the top of the result ("session_id: <id>")
    and can be passed back as the optional argument on the next call to
    continue the same kernel (variables persist between calls).
  - Idle sessions are automatically shut down after IPYBOX_IDLE_TIMEOUT
    (default 600 seconds = 10 min), scanned every IPYBOX_CLEANUP_INTERVAL
    (default 60 seconds). Both are env-overridable.
  - Session id resolution priority:
      1. explicit `session_id` tool argument,
      2. kernel_env["MCP_SESSION_ID"] if set to a real value (the gateway
         injects this from a captured MCP client session id header),
      3. otherwise a fresh random session id is generated (new kernel).
    No IP-based fallback: clients without a session identifier get isolated
    single-use kernels.

Skill discovery / loading is NOT an MCP tool — it is available as direct
Python functions in the kernel (list_skills, get_skill, create_skill,
update_skill) via the IPython startup script, operating on the shared
/var/mcp/skills volume.

No .ssh or .kube mounts.  All privileged operations go through the exec
backend (reachable via mcp_call from inside the kernel).

The IPython startup script auto-imports mcp_client helpers and defines
sync wrappers: exec_run, ssh_execute, ssh_execute_background, ssh_ensure_file,
kubectl_exec, list_skills, etc.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from fastmcp import FastMCP
from fastmcp.server.context import Context as FastMCPContext

# Shared helper registry — the single source of truth for callable kernel
# helpers.  Used as the template engine's whitelist: any function
# list_functions() shows is automatically callable from a prompt template.
from ipybox.helpers import _helpers, _parse_frontmatter
from ipybox.mcp_client import (
    set_endpoint_override,
    reset_endpoint_override,
    mcp_call_async,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
)

log = logging.getLogger("ipybox-mcp-server")

mcp = FastMCP("ipybox")

# Directory where prompt markdown files live (next to the skills they reference).
_PROMPTS_DIR = os.environ.get("IPYBOX_PROMPTS_DIR", "/var/mcp/skills/prompts")

# Regex matching {{ func_name(args) }} templates in prompt text.
_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\s*([^)]*)\)\s*\}\}")


def _parse_template_args(args_str: str) -> list:
    """Parse a comma-separated argument list into Python literals.

    Handles strings (single/double quotes), numbers, booleans, None, and
    simple lists/dicts.  Falls back to treating each token as a string.
    """
    if not args_str.strip():
        return []
    # Try to parse as a Python literal list of args.
    try:
        import ast
        parsed = ast.literal_eval("[" + args_str + "]")
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
    # Fallback: split on commas, strip quotes.
    out = []
    for tok in args_str.split(","):
        tok = tok.strip()
        if len(tok) >= 2 and tok[0] in "'\"" and tok[-1] == tok[0]:
            tok = tok[1:-1]
        out.append(tok)
    return out


async def _render_prompt(text: str) -> str:
    """Render {{ func_name(args) }} templates in prompt text.

    Only functions in the ``_helpers`` whitelist are callable.  Unknown
    templates (e.g. {{ instructions }} for Cline) pass through untouched.

        ``_helpers`` contains synchronous wrapper functions (``mcp_call``,
    ``mcp_list_upstreams``, ``mcp_describe``, ``mcp_list_actions``).  These
    wrap the underlying ``_async`` coroutines and return resolved values
    directly.  The ``iscoroutine`` check below is a safety fallback: if any
    future async helper is added to ``_helpers``, it will be awaited rather
    than stringified as ``<coroutine object ...>``.
    """
    out = []
    last = 0
    for m in _TEMPLATE_RE.finditer(text):
        out.append(text[last:m.start()])
        name = m.group(1)
        args_str = m.group(2)
        fn = _helpers.get(name)
        if fn is None:
            out.append(m.group(0))  # not whitelisted — leave as-is
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


def _list_prompt_files() -> list:
    """Return a list of (name, description) for prompt files in _PROMPTS_DIR."""
    if not os.path.isdir(_PROMPTS_DIR):
        return []
    prompts = []
    for f in sorted(os.listdir(_PROMPTS_DIR)):
        if not f.endswith(".md"):
            continue
        path = os.path.join(_PROMPTS_DIR, f)
        try:
            with open(path) as fh:
                fm, _ = _parse_frontmatter(fh.read())
        except Exception:
            continue
        name = fm.get("name") or f[:-3]
        desc = fm.get("description") or ""
        prompts.append((name, desc))
    return prompts


def _make_prompt(path: str, body: str):
    """Build an async prompt callable that renders a prompt file's body.

    The file path/body are captured at registration time so each prompt
    is independent.  The callable optionally accepts a FastMCP ``Context``
    parameter (injected via dependency injection) so the prompt renderer
    can read per-request HTTP headers — specifically ``X-MCP-Endpoint`` —
    which the gateway sets per-compound and forwards to this server when
    proxying ``prompts/get`` requests.

    When the header is present, it is stored in the
    ``_mcp_endpoint_ctx`` ContextVar so that helper functions like
    ``mcp_list_upstreams()`` (which read the endpoint from the environment
    or ContextVar) use the correct per-compound endpoint instead of the
    container-wide ``MCP_ENDPOINT`` env var.
    """
    async def _prompt(ctx: Optional[FastMCPContext] = None):
        endpoint = None
        if ctx is not None:
            try:
                rc = ctx.request_context
                if rc is not None and rc.request is not None:
                    endpoint = rc.request.headers.get("X-MCP-Endpoint")
            except Exception:
                pass
        if endpoint:
            token = set_endpoint_override(endpoint)
            try:
                return await _render_prompt(body)
            finally:
                reset_endpoint_override(token)
        else:
            return await _render_prompt(body)
    _prompt.__name__ = os.path.splitext(os.path.basename(path))[0]
    return _prompt


def _register_prompts():
    """Register a FastMCP prompt for each file in _PROMPTS_DIR.

    Each prompt's name/description come from the file's YAML frontmatter
    (falling back to the filename).  The prompt body is rendered through
    the template engine ({{ func_name(args) }}) on every invoke, so it
    always reflects the latest skills/functions.
    """
    from fastmcp.prompts import FunctionPrompt

    if not os.path.isdir(_PROMPTS_DIR):
        log.warning("Prompts dir not found at %s — no bootstrap prompts registered", _PROMPTS_DIR)
        return

    registered = 0
    for f in sorted(os.listdir(_PROMPTS_DIR)):
        if not f.endswith(".md"):
            continue
        path = os.path.join(_PROMPTS_DIR, f)
        try:
            with open(path) as fh:
                fm, body = _parse_frontmatter(fh.read())
        except Exception as e:
            log.warning("Could not read prompt file %s: %s", path, e)
            continue
        name = fm.get("name") or f[:-3]
        desc = fm.get("description") or ""
        mcp.add_prompt(
            FunctionPrompt.from_function(
                _make_prompt(path, body),
                name=name,
                description=desc,
            )
        )
        registered += 1
    log.info("Registered %d ipybox prompts from %s", registered, _PROMPTS_DIR)


# Register file-based prompts at import time so prompts/list + prompts/get
# are served over the MCP protocol (and proxied by the gateway).
_register_prompts()

# ---------------------------------------------------------------------------
# Session configuration (env-overridable)
# ---------------------------------------------------------------------------

# Seconds a session may remain idle before it is automatically shut down.
IPYBOX_IDLE_TIMEOUT = float(os.environ.get("IPYBOX_IDLE_TIMEOUT", "600"))
# How often the cleanup thread scans for idle sessions.
IPYBOX_CLEANUP_INTERVAL = float(os.environ.get("IPYBOX_CLEANUP_INTERVAL", "60"))


# Path to the IPython startup script that defines kernel helpers
# (exec_run, ssh_execute, kubectl_exec, list_skills, get_skill, ...).
# We execute it explicitly in the kernel at session creation because the
# `python3` kernelspec does not reliably load IPython's profile startup dir.
_STARTUP_SCRIPT = os.environ.get(
    "IPYBOX_STARTUP_SCRIPT",
    "/root/.ipython/profile_default/startup/00_autoimport.py",
)


def _start_kernel(kernel_env: Optional[Dict[str, str]] = None) -> Any:
    """Start a persistent IPython kernel and return (km, kc).

    After the kernel is ready, the startup script (which defines the
    kernel helpers: exec_run, ssh_execute, kubectl_exec, list_skills,
    get_skill, create_skill, update_skill, mcp_call, ...) is executed
    explicitly so the helpers are guaranteed to be available regardless
    of IPython profile/startup-directory loading.
    """
    import jupyter_client

    env = os.environ.copy()
    if kernel_env:
        env.update({str(k): str(v) for k, v in kernel_env.items()})

    # NOTE: jupyter_client >= 8.9 silently ignores the `env=` constructor kwarg
    # (there is no `env` traitlet on KernelManager, and start_kernel only receives
    # kwargs via pre_start_kernel -> _launch_args).  To inject MCP_ENDPOINT /
    # MCP_SESSION_ID (and any other kernel_env) into the kernel subprocess env, it
    # must be passed to start_kernel(env=...) so the LocalProvisioner forwards it
    # to Popen.  Passing it to the constructor here would silently drop it.
    km = jupyter_client.KernelManager(kernel_name="python3")
    km.start_kernel(env=env)
    kc = km.client()
    kc.wait_for_ready(timeout=30)

    # Explicitly load the startup script into the kernel so the helpers
    # are defined even if IPython's profile startup dir is not picked up.
    if os.path.isfile(_STARTUP_SCRIPT):
        with open(_STARTUP_SCRIPT) as f:
            startup_code = f.read()
        _execute_sync(kc, startup_code, timeout=60)
    else:
        log.warning("Startup script not found at %s — kernel helpers unavailable", _STARTUP_SCRIPT)

    return km, kc


def _execute_sync(kc: Any, code: str, timeout: int = 120) -> str:
    """Execute a snippet in a kernel synchronously and return output."""
    try:
        reply = kc.execute(code, reply=True, timeout=timeout)
        output_parts = []
        while True:
            try:
                msg = kc.get_iopub_msg(timeout=0.5)
                msg_type = msg["msg_type"]
                if msg_type == "stream":
                    output_parts.append(msg["content"].get("text", ""))
                elif msg_type in ("display_data", "execute_result"):
                    data = msg["content"].get("data", {})
                    if "text/plain" in data:
                        output_parts.append(data["text/plain"])
                elif msg_type == "error":
                    output_parts.append("\n".join(msg["content"].get("traceback", [])))
            except Exception:
                break
        if reply["content"].get("status") == "error":
            errors = reply["content"].get("traceback", [])
            if errors:
                output_parts.append("\n".join(errors))
        return "".join(output_parts).strip()
    except Exception as e:
        return f"Error executing code: {e}"


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------

@dataclass
class KernelSession:
    """A single isolated IPython kernel tied to a session id."""
    km: Any             # KernelManager
    kc: Any             # KernelClient
    env: Dict[str, str] = field(default_factory=dict)
    last_used: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)


_kernels: Dict[str, KernelSession] = {}
_kernels_lock = threading.Lock()


# Sentinel injected by the gateway when a request header was absent:
# resolve_injections leaves ${request_header:NAME} unresolved (literal text).
_UNRESOLVED_PREFIX = "${request_header:"


def _resolve_session_id(session_id: Optional[str], kernel_env: Optional[Dict[str, str]]) -> str:
    """Resolve the session id for this call.

    Priority:
      1. explicit `session_id` argument,
      2. kernel_env["MCP_SESSION_ID"] when it is a real value
         (i.e. not an unresolved "${request_header:...}" template),
      3. fresh random uuid (new isolated session).
    """
    if session_id:
        return str(session_id)
    if kernel_env:
        env_sid = kernel_env.get("MCP_SESSION_ID")
        if env_sid and not str(env_sid).startswith(_UNRESOLVED_PREFIX):
            return str(env_sid)
    return str(uuid.uuid4())


def _get_or_create_session(session_id: str, kernel_env: Optional[Dict[str, str]]) -> KernelSession:
    """Return the session's kernel, starting it if it doesn't exist."""
    with _kernels_lock:
        session = _kernels.get(session_id)
        if session is None:
            env = dict(kernel_env or {})
            km, kc = _start_kernel(env)
            session = KernelSession(km=km, kc=kc, env=env)
            _kernels[session_id] = session
            log.info("Started new ipybox session %s (%d active)", session_id[:8], len(_kernels))
        return session


def _reap_idle_sessions(now: Optional[float] = None) -> list:
    """Shut down and remove sessions idle longer than IPYBOX_IDLE_TIMEOUT.

    Sessions currently executing (their per-session lock is held) are skipped.
    """
    now = now if now is not None else time.monotonic()
    reaped = []
    with _kernels_lock:
        for sid, session in list(_kernels.items()):
            idle_for = now - session.last_used
            if idle_for <= IPYBOX_IDLE_TIMEOUT:
                continue
            if not session.lock.acquire(blocking=False):
                continue  # actively in use — skip
            try:
                _kernels.pop(sid, None)
            finally:
                session.lock.release()
            try:
                session.km.shutdown_kernel(now=True)
            except Exception:
                pass
            reaped.append(sid)
            log.info("Reaped idle ipykernel session %s (idle %.0fs)", sid[:8], idle_for)
    return reaped


def _cleanup_loop():
    """Background daemon: scan for idle sessions periodically."""
    while True:
        time.sleep(IPYBOX_CLEANUP_INTERVAL)
        try:
            _reap_idle_sessions()
        except Exception as e:
            log.warning("Cleanup scan error: %s", e)


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def execute_code(
    code: str,
    session_id: Optional[str] = None,
    kernel_env: Optional[Dict[str, str]] = None,
) -> str:
    """Execute Python code in a stateful, per-session IPython kernel.

    State (variables, imports) persists across calls that use the SAME
    session_id.  A fresh session_id (or none) creates a new isolated kernel.

    Args:
        code: Python code to execute.
        session_id: Optional id of the session to reuse.  If omitted, the
            session is derived from kernel_env["MCP_SESSION_ID"] (injected by
            the gateway from the Mcp-Session-Id request header); if neither is
            present a NEW session id is generated for this call.
        kernel_env: Environment variables to inject into the kernel process
            on FIRST use of a session (e.g. MCP_ENDPOINT for mcp_client
            helpers).  Ignored for existing sessions.

    Returns:
        "session_id: <sid>" followed by the kernel's combined
        stdout + stderr + return value.  Pass the session_id back on your next
        call to continue the same kernel.
    """
    key = None
    try:
        key = _resolve_session_id(session_id, kernel_env)
        session = _get_or_create_session(key, kernel_env)
        session.last_used = time.monotonic()
        loop = asyncio.get_event_loop()
        output = await loop.run_in_executor(None, _execute_sync, session.kc, code)
        session.last_used = time.monotonic()
        return f"session_id: {key}\n{output}"
    except Exception as e:
        log.error("execute_code error (session_id=%s): %s", (key or session_id), e, exc_info=True)
        return f"session_id: {key or 'unknown'}\nError executing code: {e}"


# ---------------------------------------------------------------------------
# MCP passthrough tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def mcp_call(
    ctx: Optional[FastMCPContext] = None,
    upstream: str = "",
    action: str = "",
    arguments: Optional[Dict[str, Any]] = None,
    stdin: Optional[str] = None,
    timeout: int = DEFAULT_TOOL_TIMEOUT_SECONDS,
    endpoint: Optional[str] = None,
) -> str:
    """Call any MCP action (tool) through the gateway with structured JSON arguments.

    This is the server-side MCP-tool equivalent of the kernel's ``mcp_call``
    builtin: it proxies the call directly from the ipybox server process (no
    IPython shell needed).  Use ``mcp_list_upstreams`` / ``mcp_list_actions`` /
    ``mcp_describe`` (available as kernel builtins via ``execute_code``) to
    discover available upstreams and actions first.

    Args:
        upstream: Upstream prefix (e.g. 'exec', 'k8s', 'vscode').
        action: Action name (e.g. 'run', 'pods_list').
        arguments: Action parameters as a JSON object.
        stdin: Optional content piped to the action's stdin.
        timeout: Timeout in seconds (default from MCP_TOOL_TIMEOUT_SECONDS env
            or 120).
        endpoint: MCP endpoint URL.  Defaults to the ``MCP_ENDPOINT`` env var
            or the ``X-MCP-Endpoint`` HTTP header forwarded by the gateway.

    Returns:
        The action's combined text output, prefixed with a status line.
        If the downstream call fails, the error message is returned instead.
    """
    # The gateway forwards the per-compound endpoint via the X-MCP-Endpoint
    # header so this tool calls back to the correct compound.  This mirrors
    # the prompt-rendering path in _make_prompt().
    resolved_endpoint = endpoint
    if resolved_endpoint is None and ctx is not None:
        try:
            rc = ctx.request_context
            if rc is not None and rc.request is not None:
                resolved_endpoint = rc.request.headers.get("X-MCP-Endpoint")
        except Exception:
            pass

    result = await mcp_call_async(
        upstream=upstream,
        action=action,
        arguments=arguments or {},
        stdin=stdin,
        timeout=timeout,
        endpoint=resolved_endpoint,
    )

    # mcp_call_async returns a plain string only on tool-resolution failure
    # (e.g. ambiguous tool name); otherwise it returns a structured dict.
    if isinstance(result, str):
        return result

    ok = result.get("ok", False)
    is_error = result.get("is_error", False)
    status = "OK" if (ok and not is_error) else "ERROR"
    action_used = result.get("action") or action
    upstream_used = result.get("upstream") or upstream
    text = result.get("text", "")
    structured = result.get("structured_content")

    lines = [f"[{status}] {upstream_used}/{action_used}"]
    if text:
        lines.append("")
        lines.append(text)
    if structured is not None:
        lines.append("")
        lines.append("--- structured ---")
        lines.append(json.dumps(structured))
    return "\n".join(lines).strip()


def main():
    parser = argparse.ArgumentParser(description="ipybox MCP Server")
    parser.add_argument("--port", type=int, default=9006)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    log.info("Starting ipybox MCP server on %s:%d (idle_timeout=%.0fs, cleanup_interval=%.0fs)",
             args.host, args.port, IPYBOX_IDLE_TIMEOUT, IPYBOX_CLEANUP_INTERVAL)

    # Start the idle-session cleanup thread.
    cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
    cleanup_thread.start()

    mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()