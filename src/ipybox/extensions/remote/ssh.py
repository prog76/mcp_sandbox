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
    gateway policy at deploy time) and translates them into -o User / -i
    flags so that OpenSSH uses the correct remote user and identity file
    even when the machine string is a bare IP without a user prefix.

    `-o User=...` is used instead of `-l ...` because the same prefix is
    shared with scp: in scp, `-l` means *bandwidth limit* (Kbit/s), not
    login user, so `-l` broke every ssh_ensure_file upload with a usage
    error. `-o User=` is valid for both ssh and scp.
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
        opts.extend(["-o", f"User={ssh_user}"])
    return opts


def _exec_error(res):
    """Build a human-readable error string from an exec-backend result dict.

    The exec backend surfaces failures in three places, and they are not
    interchangeable:

    - ``error`` — crisp machine error (policy denial, missing whitelist
      entry, transport failure). Prefer this verbatim.
    - ``exit_code``/``stderr`` — a completed-but-nonzero subprocess: the
      command RAN and failed, so the remote reason lives in stderr, not in
      ``error`` (which is None). Without this clause scp/ssh failures
      returned ``error: None`` and the cause was lost (e.g. scp ETXTBSY:
      'scp: dest open "/tmp/iperf3": Failure' when the uploaded binary is
      currently executing on the node — retry after it exits).
    - ``timed_out`` — folded into the message so the caller can tell a
      timeout (remote may still be running) from a hard failure.
    """
    parts = []
    if res.get("error"):
        parts.append(str(res["error"]).strip())
    if res.get("timed_out"):
        parts.append("timed out")
    ec = res.get("exit_code")
    if ec not in (None, 0):
        parts.append("exit_code=%s" % ec)
    err = res.get("stderr") or ""
    if err:
        tail = err.strip().splitlines()[-8:]
        parts.append("stderr: " + " | ".join(tail))
    if not parts:
        return None
    return "; ".join(parts)


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
        """Start a whitelisted binary on a remote machine in the background.

        The remote command is `env PATH=/tmp:$PATH timeout <duration> <binary>
        <args>`, detached via `ssh -f -n`, with the remote PID captured to
        /tmp/<binary>.pid. Liveness is verified with a follow-up
        `ps -p <pid>` before started=True is returned, so a binary that dies
        instantly (bad path, bad args) no longer reports started=True.

        The env assignment must stay BEFORE timeout: `timeout <d> PATH=...
        <bin>` execs the assignment string as a program name and fails with
        exit 127 — timeout's argument list is a program plus its arguments,
        not a shell line. With `env ... timeout`, $! is the PID of the whole
        chain (env execs timeout, which execs the binary), so it tracks the
        intended lifetime for the full duration.

        started=True means the process was still alive on the remote host at
        verification time (PID reuse aside). ok mirrors started.

        Returns a machine-readable dict:
        {tool, machine, binary, started, pid, duration,
        log, log_tail, ok, error}.
        """
        b = _validate_remote_binary(binary)
        if args is None:
            safe_args = []
        elif isinstance(args, str):
            safe_args = [_validate_remote_arg(args)]
        else:
            safe_args = [_validate_remote_arg(a) for a in args]

        d = max(1, int(duration))
        pidfile = f"/tmp/{b}.pid"
        logfile = f"/tmp/{b}.log"

        def _fetch_log_tail():
            tail_cmd = ["ssh", *_ssh_opts(machine), str(machine),
                        f"tail -n 20 {logfile}"]
            tail_res = _exec_run(registry, tail_cmd,
                                 {"REMOTE_BIN": "tail", "SSH_SUDO": "0"},
                                 timeout=30)
            if tail_res.get("ok"):
                return (tail_res.get("stdout") or "").strip() or None
            return None

        # Launch. The env prefix sits BEFORE timeout so the assignment is an
        # environment override, never a program name; the command is
        # backgrounded and its PID captured for the liveness check below.
        remote = (
            f'env PATH="/tmp:$PATH" timeout {d} {b} '
            + " ".join(shlex.quote(a) for a in safe_args)
            + f" >{logfile} 2>&1 & echo $! > {pidfile}"
        )
        cmd = ["ssh", "-f", "-n", *_ssh_opts(machine), str(machine), remote]
        res = _exec_run(registry, cmd,
                        {"REMOTE_BIN": b, "SSH_SUDO": "0"},
                        timeout=SSH_BG_EXEC_TIMEOUT)

        result = {
            "tool": "ssh_execute_background",
            "machine": str(machine),
            "binary": b,
            "started": False,
            "pid": None,
            "duration": d,
            "log": logfile,
            "log_tail": None,
            "ok": False,
            "error": res.get("error"),
        }
        if not res.get("ok", False):
            return result

        # ssh -f returns 0 as soon as it detaches, so the launch exit code
        # says nothing about the remote process — verify out-of-band.
        cat_cmd = ["ssh", *_ssh_opts(machine), str(machine), f"cat {pidfile}"]
        cat_res = _exec_run(registry, cat_cmd,
                            {"REMOTE_BIN": "cat", "SSH_SUDO": "0"}, timeout=30)
        raw = (cat_res.get("stdout") or "").strip()
        if not (cat_res.get("ok") and raw.isdigit()):
            result["error"] = f"could not read remote PID file {pidfile}"
            result["log_tail"] = _fetch_log_tail()
            return result
        pid = int(raw)
        result["pid"] = pid

        ps_cmd = ["ssh", *_ssh_opts(machine), str(machine), f"ps -p {pid}"]
        ps_res = _exec_run(registry, ps_cmd,
                           {"REMOTE_BIN": "ps", "SSH_SUDO": "0"}, timeout=30)
        alive = (bool(ps_res.get("ok"))
                 and str(pid) in (ps_res.get("stdout") or "").split())
        if alive:
            result["started"] = True
            result["ok"] = True
            result["error"] = None
            return result

        result["error"] = (f"remote process {pid} not alive right after start "
                           f"(ps -p {pid} matched nothing); see log_tail")
        result["log_tail"] = _fetch_log_tail()
        return result

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
                    "uploaded": None, "ok": False,
                    "error": _exec_error(r1) or r1.get("error"),
                    "step": "scp"}

        chmod_cmd = ["ssh", *ssh_opts, str(machine), f"chmod +x /tmp/{b}"]
        r2 = _exec_run(registry, chmod_cmd,
                       {"REMOTE_BIN": "chmod", "SSH_SUDO": "0", "SSH_UPLOAD": "1"},
                       timeout=30)
        if not r2.get("ok"):
            return {"tool": "ssh_ensure_file", "machine": str(machine), "binary": b,
                    "uploaded": None, "ok": False,
                    "error": _exec_error(r2) or r2.get("error"),
                    "step": "chmod"}
        return {"tool": "ssh_ensure_file", "machine": str(machine), "binary": b,
                "uploaded": f"/tmp/{b}", "ok": True, "error": None, "step": None}

    registry.add("ssh_execute", ssh_execute, description="Run a command via SSH", category="remote")
    registry.add("ssh_execute_background", ssh_execute_background,
                 description="Run a command via SSH in background", category="remote")
    registry.add("ssh_ensure_file", ssh_ensure_file,
                 description="Upload a binary to remote host", category="remote")
