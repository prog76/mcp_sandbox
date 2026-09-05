"""
SSH extensions — run commands on remote machines.
"""

import os
import re
import shlex

SSH_BG_EXEC_TIMEOUT = int(os.environ.get("SSH_BG_EXEC_TIMEOUT", "30"))
SSH_UPLOAD_TIMEOUT = int(os.environ.get("SSH_UPLOAD_TIMEOUT", "120"))
TOOLS_DIR = os.environ.get("MACRO_TOOLS_DIR", "/opt/tools")

_REMOTE_ARG_RE = re.compile(r"^[a-zA-Z0-9@._:/-]+$")
_REMOTE_BIN_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def _validate_remote_arg(value):
    s = str(value)
    if not _REMOTE_ARG_RE.match(s):
        raise ValueError(f"Invalid remote argument '{s}'")
    return s


def _validate_remote_binary(binary):
    b = str(binary).strip()
    if not _REMOTE_BIN_RE.match(b):
        raise ValueError(f"Invalid binary name '{b}'")
    return b


def _ssh_opts(machine):
    """Build the common SSH/scp option prefix.

    Reads SSH_USER and SSH_KEY_PATH from the environment (injected by the
    gateway policy at deploy time) and translates them into -l / -i flags
    so that OpenSSH uses the correct remote user and identity file even
    when the machine string is a bare IP without a user prefix.
    """
    opts = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=10",
        "-o", "LogLevel=ERROR",
    ]
    ssh_key_path = os.environ.get("SSH_KEY_PATH", "").strip()
    if ssh_key_path:
        opts.extend(["-i", ssh_key_path])
    ssh_user = os.environ.get("SSH_USER", "").strip()
    if ssh_user and "@" not in str(machine):
        opts.extend(["-l", ssh_user])
    return opts


def register(registry):
    """Register SSH helpers."""

    def _exec_run(registry, cmd, env, timeout=60, stdin=None):
        """Call the exec backend `run` via mcp_call and return the machine payload.

        Returns the downstream structured_content dict when available, else a
        normalized error dict so callers always get JSON-friendly fields.
        """
        mcp_call = registry.get("mcp_call")
        if mcp_call is None:
            return {"ok": False, "error": "mcp_call not registered"}
        result = mcp_call(
            "exec", "run",
            {"command": cmd, "binary": cmd[0], "env": env or {}, "cwd": None,
             "timeout": timeout, "stdin": stdin},
        )
        if not isinstance(result, dict):
            return {"ok": False, "error": str(result), "exit_code": None,
                    "stdout": "", "stderr": "", "timed_out": False}
        sc = result.get("structured_content")
        if isinstance(sc, dict) and "ok" in sc:
            return sc
        # No structured payload (e.g. policy denial surfaced as text): normalize.
        text = result.get("text", "")
        ok = bool(result.get("ok", False))
        return {"ok": ok, "error": None if ok else text, "exit_code": None,
                "stdout": text if ok else "", "stderr": "", "timed_out": False}

    def ssh_execute(machine, binary, args=None, sudo=False, timeout=60):
        """Run a whitelisted binary on a remote machine via SSH.

        Returns a machine-readable dict:
        {tool, machine, binary, sudo, ok, exit_code, stdout, stderr, timed_out, error}.
        """
        b = _validate_remote_binary(binary)
        if args is None:
            safe_args = []
        elif isinstance(args, str):
            safe_args = [_validate_remote_arg(args)]
        else:
            safe_args = [_validate_remote_arg(a) for a in args]

        remote = f'PATH="/tmp:$PATH" {b} ' + " ".join(shlex.quote(a) for a in safe_args)
        if sudo:
            remote_cmd = f"sudo -S -p '' bash -c {shlex.quote(remote)} 2>&1"
        else:
            remote_cmd = f"bash -c {shlex.quote(remote)} 2>&1"

        cmd = ["ssh", *_ssh_opts(machine), str(machine), remote_cmd]
        env = {"REMOTE_BIN": b, "SSH_SUDO": "1" if sudo else "0"}
        res = _exec_run(registry, cmd, env, timeout=timeout)
        return {
            "tool": "ssh_execute",
            "machine": str(machine),
            "binary": b,
            "sudo": bool(sudo),
            "ok": bool(res.get("ok", False)),
            "exit_code": res.get("exit_code"),
            "stdout": res.get("stdout", ""),
            "stderr": res.get("stderr", ""),
            "timed_out": bool(res.get("timed_out", False)),
            "error": res.get("error"),
        }

    def ssh_execute_background(machine, binary, args=None, duration=60):
        """Run a whitelisted binary on a remote machine in the background.

        Returns a machine-readable dict:
        {tool, machine, binary, started, duration, log, ok, error}.
        """
        b = _validate_remote_binary(binary)
        if args is None:
            safe_args = []
        elif isinstance(args, str):
            safe_args = [_validate_remote_arg(args)]
        else:
            safe_args = [_validate_remote_arg(a) for a in args]

        d = max(1, int(duration))
        remote = (
            f'timeout {d} PATH="/tmp:$PATH" {b} '
            + " ".join(shlex.quote(a) for a in safe_args)
            + f' >/tmp/{b}.log 2>&1'
        )
        cmd = ["ssh", "-f", "-n", *_ssh_opts(machine), str(machine), remote]
        env = {"REMOTE_BIN": b, "SSH_SUDO": "0"}
        res = _exec_run(registry, cmd, env, timeout=SSH_BG_EXEC_TIMEOUT)
        return {
            "tool": "ssh_execute_background",
            "machine": str(machine),
            "binary": b,
            "started": bool(res.get("ok", False)),
            "duration": d,
            "log": f"/tmp/{b}.log",
            "ok": bool(res.get("ok", False)),
            "error": res.get("error"),
        }

    def ssh_ensure_file(machine, binary):
        """Upload /opt/tools/<binary> to /tmp/<binary> on remote host.

        Returns a machine-readable dict:
        {tool, machine, binary, uploaded, ok, error}.
        """
        b = _validate_remote_binary(binary)
        ssh_opts = _ssh_opts(machine)

        scp_cmd = ["scp", "-o", "BatchMode=yes", *ssh_opts,
                   f"{TOOLS_DIR}/{b}", f"{machine}:/tmp/{b}"]
        r1 = _exec_run(registry, scp_cmd,
                       {"REMOTE_BIN": b, "SSH_SUDO": "0", "SSH_UPLOAD": "1"},
                       timeout=SSH_UPLOAD_TIMEOUT)
        if not r1.get("ok"):
            return {"tool": "ssh_ensure_file", "machine": str(machine), "binary": b,
                    "uploaded": None, "ok": False, "error": r1.get("error"),
                    "step": "scp"}

        chmod_cmd = ["ssh", *ssh_opts, str(machine), f"chmod +x /tmp/{b}"]
        r2 = _exec_run(registry, chmod_cmd,
                       {"REMOTE_BIN": "chmod", "SSH_SUDO": "0", "SSH_UPLOAD": "1"},
                       timeout=30)
        if not r2.get("ok"):
            return {"tool": "ssh_ensure_file", "machine": str(machine), "binary": b,
                    "uploaded": None, "ok": False, "error": r2.get("error"),
                    "step": "chmod"}
        return {"tool": "ssh_ensure_file", "machine": str(machine), "binary": b,
                "uploaded": f"/tmp/{b}", "ok": True, "error": None, "step": None}

    registry.add("ssh_execute", ssh_execute, description="Run a command via SSH", category="remote")
    registry.add("ssh_execute_background", ssh_execute_background,
                 description="Run a command via SSH in background", category="remote")
    registry.add("ssh_ensure_file", ssh_ensure_file,
                 description="Upload a binary to remote host", category="remote")
