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

    def ssh_execute(machine, binary, args=None, sudo=False, timeout=60):
        """Run a whitelisted binary on a remote machine via SSH."""
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
        exec_run = registry.get("exec_run")
        if exec_run is None:
            return "Error: exec_run not registered"
        return exec_run(cmd, env=env, timeout=timeout)

    def ssh_execute_background(machine, binary, args=None, duration=60):
        """Run a whitelisted binary on a remote machine in the background."""
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
        exec_run = registry.get("exec_run")
        if exec_run is None:
            return "Error: exec_run not registered"
        try:
            exec_run(cmd, env=env, timeout=SSH_BG_EXEC_TIMEOUT)
            return f"Background process started on {machine}: {b} (auto-stop in ~{d}s). Log: /tmp/{b}.log"
        except Exception as e:
            return f"Error: {e}"

    def ssh_ensure_file(machine, binary):
        """Upload /opt/tools/<binary> to /tmp/<binary> on remote host."""
        b = _validate_remote_binary(binary)
        exec_run = registry.get("exec_run")
        if exec_run is None:
            return "Error: exec_run not registered"

        scp_opts = [
            "-o", "BatchMode=yes",
        ]
        scp_cmd = ["scp", *_ssh_opts(machine), *scp_opts,
                   f"{TOOLS_DIR}/{b}", f"{machine}:/tmp/{b}"]
        exec_run(scp_cmd, env={"REMOTE_BIN": b, "SSH_SUDO": "0", "SSH_UPLOAD": "1"},
                 timeout=SSH_UPLOAD_TIMEOUT)

        chmod_cmd = ["ssh", *_ssh_opts(machine),
                     str(machine), f"chmod +x /tmp/{b}"]
        exec_run(chmod_cmd, env={"REMOTE_BIN": "chmod", "SSH_SUDO": "0", "SSH_UPLOAD": "1"},
                 timeout=30)
        return f"Binary '{b}' ready on {machine} at /tmp/{b}"

    registry.add("ssh_execute", ssh_execute, description="Run a command via SSH", category="remote")
    registry.add("ssh_execute_background", ssh_execute_background,
                 description="Run a command via SSH in background", category="remote")
    registry.add("ssh_ensure_file", ssh_ensure_file,
                 description="Upload a binary to remote host", category="remote")
