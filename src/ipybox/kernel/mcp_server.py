#!/usr/bin/env python3
"""
ipybox_mcp_server — MCP server that wraps stateful IPython kernels.
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

from ipybox.kernel.extensions import get_registry
from ipybox.mcp_client import (
    set_endpoint_override,
    reset_endpoint_override,
    mcp_call_async,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
)

log = logging.getLogger("ipybox-mcp-server")

mcp = FastMCP("ipybox")

_PROMPTS_DIR = os.environ.get("IPYBOX_PROMPTS_DIR", "/var/mcp/skills/prompts")
_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\s*([^)]*)\)\s*\}\}")


def _parse_frontmatter(text: str):
    import yaml
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except Exception:
        fm = {}
    return fm if isinstance(fm, dict) else {}, parts[2].lstrip("\n")


def _parse_template_args(args_str: str) -> list:
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


async def _render_prompt(text: str) -> str:
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


def _make_prompt(path: str, body: str):
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
    from fastmcp.prompts import FunctionPrompt
    if not os.path.isdir(_PROMPTS_DIR):
        log.warning("Prompts dir not found at %s", _PROMPTS_DIR)
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
            log.warning("Could not read prompt %s: %s", path, e)
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


_register_prompts()

IPYBOX_IDLE_TIMEOUT = float(os.environ.get("IPYBOX_IDLE_TIMEOUT", "600"))
IPYBOX_CLEANUP_INTERVAL = float(os.environ.get("IPYBOX_CLEANUP_INTERVAL", "60"))


_STARTUP_SCRIPT = os.environ.get(
    "IPYBOX_STARTUP_SCRIPT",
    "/root/.ipython/profile_default/startup/00_autoimport.py",
)


def _start_kernel(kernel_env: Optional[Dict[str, str]] = None) -> Any:
    import jupyter_client
    env = os.environ.copy()
    if kernel_env:
        env.update({str(k): str(v) for k, v in kernel_env.items()})
    km = jupyter_client.KernelManager(kernel_name="python3")
    km.start_kernel(env=env)
    kc = km.client()
    kc.wait_for_ready(timeout=30)
    if os.path.isfile(_STARTUP_SCRIPT):
        with open(_STARTUP_SCRIPT) as f:
            startup_code = f.read()
        _execute_sync(kc, startup_code, timeout=60)
    else:
        log.warning("Startup script not found at %s", _STARTUP_SCRIPT)
    return km, kc


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences — agents consume plain text."""
    return _ANSI_RE.sub("", text)


# Hard ceiling for a single execute_code request (kernel-side). Long-running
# jobs should use job_submit/job_wait; this timeout must accommodate the
# blocking job_wait polls (up to 300s each). Keep-alive progress notifications
# keep MCP clients alive for the whole duration.
_EXECUTE_TIMEOUT_SECONDS = int(os.environ.get("IPYBOX_EXECUTE_TIMEOUT", "600"))
# Interval for keep-alive progress notifications sent while code executes.
# Must stay below MCP clients' idle timeout (SuperAssistant extension: 30s).
_KEEPALIVE_INTERVAL_SECONDS = float(os.environ.get("IPYBOX_KEEPALIVE_INTERVAL", "10"))


async def _keepalive(ctx: Any, started: float, total: float, label: str) -> None:
    """Emit periodic progress notifications while a long execute is running.

    Each notification resets the MCP client's idle timeout, so long-running
    code (e.g. blocking job_wait polls) is not killed client-side. Best-effort:
    failures (no progressToken from client, older fastmcp without `message`,
    closed stream) are swallowed.
    """
    beat = 0
    while True:
        await asyncio.sleep(_KEEPALIVE_INTERVAL_SECONDS)
        beat += 1
        elapsed = time.monotonic() - started
        if ctx is None:
            continue
        try:
            await ctx.report_progress(
                progress=elapsed,
                total=total,
                message=f"{label} running ({elapsed:.0f}s)",
            )
        except TypeError:
            # Older fastmcp Context.report_progress has no `message` kwarg.
            try:
                await ctx.report_progress(progress=elapsed, total=total)
            except Exception:
                pass
        except Exception:
            pass
        log.debug("execute_code keepalive beat %d (%.0fs elapsed) %s", beat, elapsed, label)


def _execute_sync(kc: Any, code: str, timeout: int = 120) -> str:
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
                # NOTE: 'error' iopub messages are intentionally skipped —
                # the execute reply carries the same traceback; taking both
                # duplicated every error.
            except Exception:
                break
        if reply["content"].get("status") == "error":
            errors = reply["content"].get("traceback", [])
            if errors:
                output_parts.append("\n".join(errors))
        return _strip_ansi("".join(output_parts)).strip()
    except Exception as e:
        return f"Error executing code: {e}"


@dataclass
class KernelSession:
    km: Any
    kc: Any
    env: Dict[str, str] = field(default_factory=dict)
    last_used: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)


_kernels: Dict[str, KernelSession] = {}
_kernels_lock = threading.Lock()

_UNRESOLVED_PREFIX = "${request_header:"


def _resolve_session_id(session_id: Optional[str], kernel_env: Optional[Dict[str, str]]) -> str:
    if session_id:
        return str(session_id)
    if kernel_env:
        env_sid = kernel_env.get("MCP_SESSION_ID")
        if env_sid and not str(env_sid).startswith(_UNRESOLVED_PREFIX):
            return str(env_sid)
    return str(uuid.uuid4())


def _get_or_create_session(session_id: str, kernel_env: Optional[Dict[str, str]]) -> KernelSession:
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
    now = now if now is not None else time.monotonic()
    reaped = []
    with _kernels_lock:
        for sid, session in list(_kernels.items()):
            idle_for = now - session.last_used
            if idle_for <= IPYBOX_IDLE_TIMEOUT:
                continue
            if not session.lock.acquire(blocking=False):
                continue
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
    while True:
        time.sleep(IPYBOX_CLEANUP_INTERVAL)
        try:
            _reap_idle_sessions()
        except Exception as e:
            log.warning("Cleanup scan error: %s", e)


@mcp.tool()
async def execute_code(
    code: str,
    session_id: Optional[str] = None,
    kernel_env: Optional[Dict[str, str]] = None,
    ctx: Optional[FastMCPContext] = None,
) -> str:
    key = None
    ka_task: Optional[asyncio.Task] = None
    try:
        key = _resolve_session_id(session_id, kernel_env)
        session = _get_or_create_session(key, kernel_env)
        session.last_used = time.monotonic()
        loop = asyncio.get_event_loop()
        started = time.monotonic()

        # Keep-alive: emit progress notifications while the code runs so MCP
        # clients (e.g. browser extensions with progress-reset timeouts) don't
        # kill long-running executions such as blocking job_wait polls.
        ka_task = asyncio.create_task(_keepalive(ctx, started, float(_EXECUTE_TIMEOUT_SECONDS), "code"))
        try:
            output = await loop.run_in_executor(
                None, lambda: _execute_sync(session.kc, code, timeout=_EXECUTE_TIMEOUT_SECONDS)
            )
        finally:
            if ka_task is not None:
                ka_task.cancel()
                try:
                    await ka_task
                except (asyncio.CancelledError, Exception):
                    pass

        session.last_used = time.monotonic()
        return f"session_id: {key}\n{output}"
    except Exception as e:
        log.error("execute_code error (session_id=%s): %s", (key or session_id), e, exc_info=True)
        return f"session_id: {key or 'unknown'}\nError executing code: {e}"


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
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9006)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    log.info("Starting ipybox MCP server on %s:%d", args.host, args.port)

    cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
    cleanup_thread.start()

    mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
