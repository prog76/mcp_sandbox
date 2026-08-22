#!/usr/bin/env python3
"""
ipybox_helpers — shared kernel helper functions for the ipybox sandbox.

This module is the SINGLE SOURCE OF TRUTH for the helper functions available
inside the ipybox IPython kernel.  It is imported by:

  - ``ipybox_startup.py``  — injects the helpers into the kernel's builtins
  - ``ipybox_mcp_server.py`` — uses the ``_helpers`` registry as the template
    engine's callable whitelist (so any function ``list_functions()`` shows is
    automatically callable from a prompt template, and vice versa).

Helpers:
  exec_run(command, env=None, cwd=None, timeout=60, stdin=None)
  ssh_execute(machine, binary, args=None, sudo=False, timeout=60)
  ssh_execute_background(machine, binary, args=None, duration=60)
  ssh_ensure_file(machine, binary)
  kubectl_exec(namespace, pod, command, container=None)
  mcp_call(upstream, action, arguments, stdin=None, timeout=120, endpoint=None)  # sync
  mcp_list_upstreams() / mcp_list_actions(upstream) / mcp_describe(action_id)   # sync
  list_skills() / get_skill(name) / create_skill(name, content) / update_skill(name, content)
  list_functions() / describe_function(name)
"""

import asyncio
import builtins
import inspect
import json
import os
import re
import sys
import shlex

import yaml

# The mcp_client module exposes async helpers with an explicit `_async` suffix.
# The kernel does not need async functions (no reason for the agent to manage an
# event loop), so we import the async ones and wrap them into synchronous
# helpers below (mcp_call, mcp_list_upstreams, mcp_list_actions, mcp_describe).
from ipybox.mcp_client import (
    mcp_call_async,
    mcp_list_upstreams_async,
    mcp_list_actions_async,
    mcp_describe_async,
)

# ---------------------------------------------------------------------------
# Remote-exec (SSH) configuration
# ---------------------------------------------------------------------------
# TOOLS_DIR lives in the gateway (/opt/tools is mounted ro into the gateway
# container). The ipybox kernel only builds argv strings and forwards them to
# the exec backend; credentials are injected per-request by the gateway policy
# (deploy/config/policy/real/exec.yaml), never read from the kernel env.
TOOLS_DIR = os.environ.get("MACRO_TOOLS_DIR", "/opt/tools")
_REMOTE_ARG_RE = re.compile(r"^[a-zA-Z0-9@._:/-]+$")
_REMOTE_BIN_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
# Grace period for an `ssh -f` background detach (handshake completes quickly).
SSH_BG_EXEC_TIMEOUT = int(os.environ.get("SSH_BG_EXEC_TIMEOUT", "30"))
SSH_UPLOAD_TIMEOUT = int(os.environ.get("SSH_UPLOAD_TIMEOUT", "120"))


def _validate_remote_arg(value) -> str:
    """Reject any argument containing shell metacharacters."""
    s = str(value)
    if not _REMOTE_ARG_RE.match(s):
        raise ValueError(f"Invalid remote argument '{s}': only alphanumerics, @ . _ : - / are allowed")
    return s


def _validate_remote_binary(binary: str) -> str:
    """Validate a bare binary name (used as a policy whitelist hint)."""
    b = str(binary).strip()
    if not _REMOTE_BIN_RE.match(b):
        raise ValueError(f"Invalid binary name '{b}'")
    return b



# ---------------------------------------------------------------------------
# Frontmatter parsing — shared between skill discovery and prompt rendering
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str):
    """Parse YAML frontmatter from a markdown string.

    The frontmatter is delimited by leading ``---`` lines::

        ---
        name: my_skill
        description: >-
          A description
        ---
        # Body
        ...

    Args:
        text: Full file contents (frontmatter + body).

    Returns:
        A tuple ``(frontmatter_dict, body_text)``.  If no frontmatter is
        present, returns ``({}, text)``.
    """
    if not text.startswith("---"):
        return {}, text
    # Split into at most 3 parts: fence, yaml, body
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    # parts[0] is '' (before the first ---), parts[1] is the YAML block,
    # parts[2] is the body after the closing ---
    yaml_block = parts[1]
    body = parts[2].lstrip("\n")
    try:
        fm = yaml.safe_load(yaml_block) or {}
    except Exception:
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    return fm, body


def _sync(coro):
    """Run an async coroutine to completion in a fresh event loop.

    ipybox kernels may already have a running loop (IPython's own), so
    we use the get_event_loop / run_until_complete pattern rather than
    asyncio.run() which would fail if a loop is already running.

    When falling back to a ThreadPoolExecutor (because a loop is already
    running), the current ``contextvars`` context is copied into the new
    thread so that per-request ContextVars — such as the MCP endpoint
    override set by the ipybox MCP server from the ``X-MCP-Endpoint``
    HTTP header — propagate correctly.
    """
    import contextvars
    import concurrent.futures
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're in a context with a running loop — fall back to creating
            # a new loop in a thread.  Copy the current context so that
            # ContextVars (e.g. per-request MCP endpoint override) are
            # available in the spawned thread.
            ctx = contextvars.copy_context()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(ctx.run, lambda: asyncio.run(coro)).result()
    except RuntimeError:
        pass
    return asyncio.run(coro)


def mcp_call(upstream, action, arguments, stdin=None, timeout=120, endpoint=None):
    """Synchronously call any MCP action (see mcp_call_async) and return a
    structured, machine-readable result dict.

    The kernel exposes only synchronous helpers; the underlying async
    ``mcp_client.mcp_call_async`` is run to completion via ``_sync``.

    Returns a dict with a stable schema (all keys always present):
      ok                 bool   True unless the call reported an error
      is_error           bool   mirror of the downstream isError flag
      upstream           str    upstream prefix used for the call
      action             str    resolved tool id that was invoked
      text               str    combined plain-text payload (cat/exec output, ...)
      content            list   raw MCP content blocks as JSON-able dicts
      structured_content any    downstream structuredContent (null for text-only)

    Example:
      >>> r = mcp_call(upstream='k8s', action='nodes_top', arguments={'context':'infra.test'})
      >>> r['ok']
      True
      >>> print(r['text'])      # the command output
      >>> r['structured_content']  # structured payload if the tool provides one

    To get just the string payload use ``mcp_call_text(...)``.
    """
    return _sync(mcp_call_async(upstream, action, arguments, stdin=stdin, timeout=timeout, endpoint=endpoint))


def mcp_call_text(upstream, action, arguments, stdin=None, timeout=120, endpoint=None):
    """Call an MCP action and return ONLY its plain-text payload (a ``str``).

    Convenience wrapper over ``mcp_call`` for callers that want the raw text
    output without the structured envelope (e.g. scripting inside a helper).
    """
    res = mcp_call(upstream, action, arguments, stdin=stdin, timeout=timeout, endpoint=endpoint)
    return res["text"] if isinstance(res, dict) else str(res)


def mcp_list_upstreams(endpoint=None):
    """Synchronously list available MCP upstreams (see mcp_list_upstreams_async)."""
    return _sync(mcp_list_upstreams_async(endpoint=endpoint))


def mcp_list_actions(upstream, endpoint=None):
    """Synchronously list tools for an upstream (see mcp_list_actions_async)."""
    return _sync(mcp_list_actions_async(upstream, endpoint=endpoint))


def mcp_describe(action_id, endpoint=None):
    """Synchronously describe an action's schema (see mcp_describe_async)."""
    return _sync(mcp_describe_async(action_id, endpoint=endpoint))


# ---------------------------------------------------------------------------
# Helper: exec_run — calls the exec backend's run tool
# ---------------------------------------------------------------------------

def exec_run(command, env=None, cwd=None, timeout=60, stdin=None):
    """Run a command via the exec backend.

    Builds the `binary` field (first element of command) for policy
    matching and forwards to exec_run on the gateway endpoint.

    Args:
        command: list of strings (argv vector).
        env: dict of extra env vars.
        cwd: working directory.
        timeout: seconds (default 60).
        stdin: string piped to subprocess stdin.

    Returns:
        str: combined stdout + stderr from the remote command.
    """
    if isinstance(command, str):
        import shlex
        command = shlex.split(command)
    if not command:
        return "Error: command is empty"
    binary = command[0]
    res = mcp_call("exec", "run", {
        "command": command,
        "binary": binary,
        "env": env or {},
        "cwd": cwd,
        "timeout": timeout,
        "stdin": stdin,
    })
    # exec_run keeps the legacy string contract for downstream ssh_*/kubectl_*
    # helpers and skills; mcp_call itself returns the structured dict.
    return res["text"] if isinstance(res, dict) else str(res)


def ssh_execute(machine, binary, args=None, sudo=False, timeout=60):
    """Run a whitelisted binary on a remote machine via SSH.

    Uses the exec backend's ``run`` tool with ``binary=ssh``. The remote
    command is a single validated binary plus shlex-quoted args — no shell,
    no redirections, no pipes are assembled by the agent. Credentials and,
    when ``sudo`` is set, the sudo password are injected by the gateway policy
    (exec.yaml); the agent never supplies them.

    Args:
        machine: hostname or IP (optionally user@host).
        binary: remote binary to run (must be in the policy whitelist).
        args: list of arguments for the binary.
        sudo: run under ``sudo -S`` (binary must be in the sudo whitelist).
        timeout: ssh/exec timeout in seconds.

    Returns:
        str: combined stdout + stderr from the remote command.
    """
    b = _validate_remote_binary(binary)
    if args is None:
        safe_args = []
    elif isinstance(args, str):
        safe_args = [_validate_remote_arg(args)]
    else:
        safe_args = [_validate_remote_arg(a) for a in args]

    remote = 'PATH="/tmp:$PATH" ' + b + " " + " ".join(shlex.quote(a) for a in safe_args)
    if sudo:
        # `sudo -S` reads the password from stdin; the gateway policy injects it
        # via the `run` tool's stdin from the SUDO_PASSWORD env var (never seen
        # by the kernel). `-p ''` silences the password prompt.
        remote_cmd = f"sudo -S -p '' bash -c {shlex.quote(remote)} 2>&1"
    else:
        remote_cmd = f"bash -c {shlex.quote(remote)} 2>&1"

    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=10",
        "-o", "LogLevel=ERROR",
        str(machine),
        remote_cmd,
    ]
    # REMOTE_BIN tells the gateway policy which remote binary is executing;
    # SSH_SUDO selects the sudo/non-sudo whitelist band and triggers the
    # sudo-password (stdin) injection. These are non-secret hints only.
    env = {"REMOTE_BIN": b, "SSH_SUDO": "1" if sudo else "0"}
    return exec_run(cmd, env=env, timeout=timeout)


def ssh_execute_background(machine, binary, args=None, duration=60):
    """Run a whitelisted binary on a remote machine in the background.

    Wraps the remote command in ``timeout <duration>`` and uses ``ssh -f`` to
    detach, so the call returns immediately after the ``ssh -f`` handshake
    completes (a few seconds). Remote output is logged to ``/tmp/<binary>.log``.
    The ipybox-side timeout (SSH_BG_EXEC_TIMEOUT) stays short because ``ssh -f``
    detaches right after handshake.

    Background execution is **non-sudo only**: a detached ``ssh -f`` channel
    cannot feed a password to the remote ``sudo -S``. Use
    ``ssh_execute(..., sudo=True)`` for privileged one-shot commands.

    Args:
        machine: hostname or IP (optionally user@host).
        binary: remote binary (must be in the non-sudo whitelist).
        args: list of arguments for the binary.
        duration: auto-stop seconds for the remote command.

    Returns:
        str: confirmation including the remote log path (or an error string).
    """
    b = _validate_remote_binary(binary)
    if args is None:
        safe_args = []
    elif isinstance(args, str):
        safe_args = [_validate_remote_arg(args)]
    else:
        safe_args = [_validate_remote_arg(a) for a in args]
    d = max(1, int(duration))

    # `timeout` self-terminates the remote process; redirect to a per-binary log.
    remote = (
        f'timeout {d} PATH="/tmp:$PATH" {b} '
        + " ".join(shlex.quote(a) for a in safe_args)
        + f' >/tmp/{b}.log 2>&1'
    )
    cmd = [
        "ssh", "-f", "-n",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=10",
        "-o", "LogLevel=ERROR",
        str(machine),
        remote,
    ]
    env = {"REMOTE_BIN": b, "SSH_SUDO": "0"}

    # Option A: a short exec_run timeout lets `ssh -f` detach and return; the
    # remote `timeout <duration>` bounds the actual workload.
    try:
        exec_run(cmd, env=env, timeout=SSH_BG_EXEC_TIMEOUT)
        return (f"Background process started on {machine}: {b} "
                f"(auto-stop in ~{d}s). Log: /tmp/{b}.log")
    except Exception as e:
        return f"Error: {e}"



def ssh_ensure_file(machine, binary):
    """Upload /opt/tools/<binary> to /tmp/<binary> on the remote host and chmod +x.

    Only binaries in the policy upload whitelist (iperf3, melisai) are allowed.
    The upload uses the gateway's mounted ``/opt/tools`` (the exec backend runs
    in the gateway container); credentials are injected by the gateway policy.

    Args:
        machine: hostname or IP (optionally user@host).
        binary: tool binary to upload (must be in the upload whitelist).

    Returns:
        str: confirmation with the remote path.
    """
    b = _validate_remote_binary(binary)

    # 1. scp local /opt/tools/<binary>  ->  remote /tmp/<binary>
    scp_cmd = [
        "scp",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes",
        "-o", "LogLevel=ERROR",
        f"{TOOLS_DIR}/{b}",
        f"{machine}:/tmp/{b}",
    ]
    exec_run(scp_cmd, env={"REMOTE_BIN": b, "SSH_SUDO": "0", "SSH_UPLOAD": "1"}, timeout=SSH_UPLOAD_TIMEOUT)

    # 2. chmod +x on the remote (trusted backend plumbing; REMOTE_BIN=chmod).
    chmod_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15",
        "-o", "LogLevel=ERROR",
        str(machine),
        f"chmod +x /tmp/{b}",
    ]
    exec_run(chmod_cmd, env={"REMOTE_BIN": "chmod", "SSH_SUDO": "0", "SSH_UPLOAD": "1"},
             timeout=30)
    return f"Binary '{b}' ready on {machine} at /tmp/{b}"


def kubectl_exec(namespace, pod, command, container=None):
    """Execute a command inside a Kubernetes pod via kubectl.

    KUBECONFIG is injected by the gateway policy.

    Args:
        namespace: Kubernetes namespace.
        pod: Pod name.
        command: list of strings (command + args) to run inside the pod.
        container: optional container name (-c flag).

    Returns:
        str: combined stdout + stderr from the pod.
    """
    if isinstance(command, str):
        command = [command]
    cmd = ["kubectl", "exec", "-n", namespace, pod, "--"]
    if container:
        # Insert -c container before the -- separator
        cmd = ["kubectl", "exec", "-n", namespace, "-c", container, pod, "--"]
    cmd.extend(command)
    return exec_run(cmd)


# ---------------------------------------------------------------------------
# Skill helpers — direct file I/O on the shared /var/mcp/skills volume
# ---------------------------------------------------------------------------

_SKILLS_DIR = "/var/mcp/skills"


def _safe_skill_name(name: str) -> str:
    """Sanitize a skill name for filesystem safety (create/update only)."""
    return name.replace("/", "_").replace("\\", "_").replace("..", "_")


def _read_frontmatter(path: str):
    """Read a markdown file and return its (frontmatter_dict, body_text)."""
    try:
        with open(path) as fh:
            text = fh.read()
    except Exception:
        return {}, ""
    return _parse_frontmatter(text)


def _skill_dirs():
    """Set of directories (abspath) that contain a SKILL.md (Claude-style groups)."""
    sds = set()
    for root, dirs, files in os.walk(_SKILLS_DIR):
        dirs[:] = [d for d in dirs if d not in ("prompts",)]
        if "SKILL.md" in files:
            sds.add(root)
    return sds


def _under_skill_dir(abspath, sds):
    """True if `abspath` sits beneath a directory that owns a SKILL.md.

    Such files are nested supporting docs (references/cases/playbooks) —
    they are referenced, not listed as separate skills.
    """
    p = os.path.dirname(abspath)
    while p and p != _SKILLS_DIR:
        if p in sds:
            return True
        p = os.path.dirname(p)
    return False


def _list_nested(abspath, sds):
    """List .md files nested under the skill dir owning `abspath` (excluding it).

    `abspath` is a SKILL.md entry file. Returns paths relative to its dir.
    """
    skill_dir = os.path.dirname(abspath)
    nested = []
    for root, dirs, files in os.walk(skill_dir):
        for f in sorted(files):
            if f.endswith(".md"):
                ap = os.path.join(root, f)
                if ap == abspath:
                    continue
                nested.append(os.path.relpath(ap, skill_dir))
    return nested


def list_skills() -> str:
    """List available skills as a README-style catalog tree.

    Walks /var/mcp/skills recursively.  Only skills whose entry file has
    YAML frontmatter with a `description` are listed (README, test files
    without descriptions are skipped).  Claude-style dirs (containing a
    SKILL.md) expose the directory itself as the skill; their nested .md
    files are referenced, not recursed as separate skills.

    Each line's path is a valid argument to ``get_skill()``.
    """
    if not os.path.isdir(_SKILLS_DIR):
        return "No skills directory found at /var/mcp/skills"

    sds = _skill_dirs()
    entries = []  # (path_label, first_desc_line, nested_list)

    for root, dirs, files in os.walk(_SKILLS_DIR):
        dirs[:] = sorted([d for d in dirs if d not in ("prompts",) and not d.startswith(".")])
        for f in sorted(files):
            if not f.endswith(".md"):
                continue
            ap = os.path.join(root, f)
            rel = os.path.relpath(ap, _SKILLS_DIR)
            rel = rel.replace(os.sep, "/")

            if f == "SKILL.md":
                fm, _ = _read_frontmatter(ap)
                desc = (fm.get("description") or "").strip()
                if not desc:
                    continue
                skillpath = os.path.relpath(root, _SKILLS_DIR).replace(os.sep, "/")
                entries.append((skillpath, desc, _list_nested(ap, sds)))
            elif _under_skill_dir(ap, sds):
                continue  # nested supporting file — not a separate skill
            else:
                fm, _ = _read_frontmatter(ap)
                desc = (fm.get("description") or "").strip()
                if not desc:
                    continue
                entries.append((rel[:-3], desc, []))

    if not entries:
        return "No skills available."

    entries.sort(key=lambda e: e[0])
    lines = ["Skills catalog:"]
    for path_label, desc, nested in entries:
        first = desc.splitlines()[0] if desc else desc
        lines.append(f"- {path_label}: {first}")
        if nested:
            lines.append(f"    (nested: {', '.join(nested)} — load via get_skill)")
    return "\n".join(lines)

def get_skill(name: str) -> str:
    """Read a skill by name or path.

    Accepts: 'grafana' (root), 'alerts' (alerts/alerts.md),
    'traffic/incomming-traffic' (nested .md),
    'team/skills/infra-problems-analyzer' (a SKILL.md dir),
    or an exact relative path.  Path traversal (..) is rejected.
    """
    if name.startswith("/") or ".." in name.split("/"):
        return f"Error: Invalid skill path '{name}' (path traversal not allowed)."

    candidates = [
        os.path.join(_SKILLS_DIR, name),
        os.path.join(_SKILLS_DIR, name + ".md"),
        os.path.join(_SKILLS_DIR, name, "SKILL.md"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            try:
                with open(c) as f:
                    return f.read()
            except Exception as e:
                return f"Error reading skill '{name}': {e}"

    # Fallback: bare stem search in any subdir (e.g. get_skill('alerts')).
    bare = name.split("/")[-1]
    for root, dirs, files in os.walk(_SKILLS_DIR):
        if "prompts" in dirs:
            dirs.remove("prompts")
        for f in files:
            if f.endswith(".md") and f[:-3] == bare:
                ap = os.path.join(root, f)
                try:
                    with open(ap) as fh:
                        return fh.read()
                except Exception as e:
                    return f"Error reading skill '{name}': {e}"

    return f"Error: Skill '{name}' not found."


def create_skill(name: str, content: str) -> str:
    """Create a new skill markdown file.

    Args:
        name: Skill name (without .md extension).
        content: Skill content as markdown.
    """
    safe_name = _safe_skill_name(name)
    filepath = os.path.join(_SKILLS_DIR, f"{safe_name}.md")
    if os.path.exists(filepath):
        return f"Error: Skill '{name}' already exists."
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w") as f:
            f.write(content)
        return f"Skill '{name}' created successfully."
    except Exception as e:
        return f"Error creating skill '{name}': {e}"


def update_skill(name: str, content: str) -> str:
    """Update an existing skill markdown file.

    Args:
        name: Skill name (without .md extension).
        content: New content as markdown.
    """
    safe_name = _safe_skill_name(name)
    filepath = os.path.join(_SKILLS_DIR, f"{safe_name}.md")
    if not os.path.exists(filepath):
        return f"Error: Skill '{name}' not found."
    try:
        with open(filepath, "w") as f:
            f.write(content)
        return f"Skill '{name}' updated successfully."
    except Exception as e:
        return f"Error updating skill '{name}': {e}"


# ---------------------------------------------------------------------------
# Function introspection helpers
# ---------------------------------------------------------------------------


def _short_doc(fn) -> str:
    """Return the first docstring line of a callable (or a fallback)."""
    doc = inspect.getdoc(fn)
    if not doc:
        return "No description"
    return doc.strip().splitlines()[0]


def list_functions() -> str:
    """List available kernel helper functions (skipping private names).

    Returns a sorted list of function names with a one-line description
    pulled from each function's docstring.  Names starting with "_" are
    excluded (internal helpers like _sync, _safe_skill_name, ...).
    """
    visible = sorted(
        (name, fn)
        for name, fn in _helpers.items()
        if not name.startswith("_")
    )
    if not visible:
        return "No functions available."
    lines = [f"Available kernel functions ({len(visible)}):"]
    for name, fn in visible:
        lines.append(f"- {name}: {_short_doc(fn)}")
    return "\n".join(lines)


def describe_function(name: str) -> str:
    """Describe a kernel helper function: signature, inputs, output, docstring.

    Resolves the name against the injected helpers first, then the
    module/global namespace, then builtins (e.g. os.getcwd).

    Args:
        name: The function name to describe.

    Returns:
        A formatted block with the signature, input parameters, return
        annotation, and full docstring.
    """
    fn = None
    if name in _helpers:
        fn = _helpers[name]
    elif name in globals():
        fn = globals()[name]
    else:
        b = getattr(builtins, name, None)
        if callable(b):
            fn = b

    if fn is None or not callable(fn):
        return f"Error: Function '{name}' not found."

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        sig = None

    parts = [f"{name}{sig if sig is not None else '(...) '}"]
    parts.append("")
    if sig is not None:
        parts.append("Input parameters:")
        params = list(sig.parameters.values())
        if not params:
            parts.append("  (none)")
        for p in params:
            ann = f": {p.annotation.__name__}" if p.annotation is not inspect.Parameter.empty else ""
            parts.append(f"  - {p.name}{ann}")
    parts.append(f"Output type: {getattr(fn, '__annotations__', {}).get('return', 'not annotated')}")
    parts.append("")
    parts.append("Docstring:")
    doc = inspect.getdoc(fn)
    parts.append(doc if doc else "No docstring available.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Registry — the single source of truth for callable kernel helpers.
# Used both to inject into builtins (startup) and as the template engine's
# whitelist (mcp server).  list_functions() reflects this dict.
# ---------------------------------------------------------------------------

_helpers = {
    "exec_run": exec_run,
    "ssh_execute": ssh_execute,
    "ssh_execute_background": ssh_execute_background,
    "ssh_ensure_file": ssh_ensure_file,
    "kubectl_exec": kubectl_exec,
    "mcp_call": mcp_call,
    "mcp_call_text": mcp_call_text,
    "mcp_list_upstreams": mcp_list_upstreams,
    "mcp_list_actions": mcp_list_actions,
    "mcp_describe": mcp_describe,
    "list_skills": list_skills,
    "get_skill": get_skill,
    "create_skill": create_skill,
    "update_skill": update_skill,
    "list_functions": list_functions,
    "describe_function": describe_function,
}