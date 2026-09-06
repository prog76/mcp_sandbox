"""Regression tests for ssh_execute_background env-prefix + truthful started.

Reproduces the iperf-dc-01 breakage (session reference) where the remote
launch line `timeout <d> PATH=... <bin>` made timeout try to exec the env
assignment as a program name (exit 127) while `ssh -f` detach made the
helper report started=True unconditionally.

These tests are designed for THREE environments:

1. CI/dev (default): mcp_call is stubbed — the helper's argv construction is
   asserted and the liveness/capture logic is exercised against a fake exec
   backend that simulates the remote host. No sshd, no hosts, no gateway.
2. Hard integration: when SSH_HOST (a reachable host with sshd running and
   the whitelisted resource available) is set, real ssh_execute_background
   and ssh_ensure_file calls run end-to-end.
3. Red/green archaeology: run against an OLD build (git checkout afae558)
   to watch the reproductions fail (exit-127 started=True), and against the
   patched build to watch them pass.

The red/green run is driven by scripts/red_green_ssh_bg.sh (each build in
its own venv + PYTHONPATH=src, CI-safe, no network/ssh required for the
unit layer; the optional E2E needs SSH_HOST).
"""

import os
import shlex
import sys
from unittest import mock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))

try:
    from ipybox.extensions.remote import ssh as sshmod
except ImportError:  # dev box without the package installed: load by path
    import importlib.util

    _p = os.path.join(_HERE, "..", "src", "ipybox", "extensions", "remote", "ssh.py")
    _spec = importlib.util.spec_from_file_location("ssh_under_test", _p)
    sshmod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(sshmod)


class _Registry:
    """Minimal stand-in for the ipybox registry."""

    def __init__(self):
        self.tools = {}

    def add(self, name, fn, description=None, category=None):
        self.tools[name] = fn

    def get(self, name):
        return self.tools.get(name)


LIVE_HOST = os.environ.get("SSH_HOST", "").strip()


def _register(mcp_call=None):
    reg = _Registry()
    sshmod.register(reg)
    if mcp_call is not None:
        reg.tools["mcp_call"] = mcp_call
    return reg


def _ok_result(stdout="", exit_code=0):
    return {"ok": True, "stdout": stdout, "stderr": "", "exit_code": exit_code,
            "timed_out": False}


def _err_result(text):
    return {"ok": False, "text": text}


# --------------------------------------------------------------------------
# Case 1 — happy path: a long-running server starts and is reported truthful
# --------------------------------------------------------------------------


class _AliveBackend:
    """Fake exec backend simulating the patched remote host.

    The launch ssh call succeeds; ``cat`` returns a readable pid; ``ps``
    output contains the pid (matches the NEW ps -p verification logic).
    ``tail`` returns a recognizable log line.
    """

    def __init__(self, pid="4242"):
        self.calls = []
        self.pid = pid
        self.log_tail = "Server listening on 5201"

    def __call__(self, upstream, action, args):
        self.calls.append(args)
        cmd = args["command"]
        assert cmd[0] == "ssh", cmd
        remote = cmd[-1]
        if remote.startswith("cat "):
            return {"ok": True, "structured_content": _ok_result(stdout=self.pid)}
        if remote.startswith("ps "):
            return {"ok": True, "structured_content":
                    _ok_result(stdout=f"  {self.pid} ?        S      0:00 timeout")}
        if remote.startswith("tail "):
            return {"ok": True, "structured_content": _ok_result(stdout=self.log_tail)}
        return {"ok": True, "structured_content": _ok_result()}  # the launch ssh -f


def test_case1_happy_path_started_true_with_pid():
    """Case 1: a long-running server reports started=True and a real pid; the
    launch line keeps the env assignment BEFORE timeout."""
    backend = _AliveBackend(pid="4242")
    reg = _register(backend)
    res = reg.tools["ssh_execute_background"]("172.16.171.11", "iperf3",
                                              args=["-s", "-p", "5201"],
                                              duration=10)

    assert res is not None
    assert res["started"] is True, res
    assert res["ok"] is True
    assert res["pid"] == 4242
    assert res["log"] == "/tmp/iperf3.log"
    assert res["error"] is None

    launch = backend.calls[0]["command"]
    remote = launch[-1]
    # Regression: old line `timeout 10 PATH="/tmp:$PATH" iperf3 ...` exec'd
    # the assignment as a program name (exit 127). The env prefix must be a
    # real `env` invocation BEFORE timeout.
    assert remote.startswith('env PATH="/tmp:$PATH" timeout 10 iperf3 '), remote


def test_case1_happy_path_no_dash_l_in_ssh_argv():
    """The launcher and the verification steps share _ssh_opts — no scp-style
    -l may leak into the ssh argv (scp -l is bandwidth-limit)."""
    backend = _AliveBackend()
    reg = _register(backend)
    with mock.patch.dict(
            os.environ, {"SSH_USER": "u1", "SSH_KEY_PATH": "/k/id"}):
        reg.tools["ssh_execute_background"]("172.16.171.11", "iperf3",
                                            args=["-s"], duration=2)
    for c in backend.calls:
        assert "-l" not in c["command"], c["command"]


# --------------------------------------------------------------------------
# Case 2 — failure path: a binary that dies instantly must NOT report started
# --------------------------------------------------------------------------


class _DeadBackend(_AliveBackend):
    """Like the happy backend, but `ps` output does NOT contain the pid — the
    process is already gone (bad path / instantly-failing binary)."""

    def __call__(self, upstream, action, args):
        self.calls.append(args)
        cmd = args["command"]
        remote = cmd[-1]
        if remote.startswith("cat "):
            return {"ok": True, "structured_content": _ok_result(stdout="9999")}
        if remote.startswith("ps "):
            return {"ok": True, "structured_content":
                    _ok_result(stdout="no such process")}
        if remote.startswith("tail "):
            return {"ok": True, "structured_content": _ok_result(stdout="nope")}
        return {"ok": True, "structured_content": _ok_result()}


def test_case2_dead_binary_never_reports_started():
    """Case 2: binary dies before the liveness check -> started=False, no
    misleading started=True, log_tail captured."""
    backend = _DeadBackend()
    reg = _register(backend)
    res = reg.tools["ssh_execute_background"]("172.16.171.11", "melisai",
                                              args=["--bogus-flag"], duration=2)

    assert res["started"] is False, res
    assert res["ok"] is False
    assert res["log_tail"] == "nope"
    assert "not alive" in (res["error"] or ""), res["error"]


def test_case2_launch_failure_reports_error():
    """A denied/failed launch (mock exec denies) returns started=False with
    the error surfaced — never started=True."""
    def deny(upstream, action, args):
        return _err_result("exec/exec_run: ACCESS DENIED: ssh")

    reg = _register(deny)
    res = reg.tools["ssh_execute_background"]("172.16.171.11", "iperf3",
                                              args=["-s"], duration=2)
    assert res["started"] is False
    assert res["ok"] is False


def test_case2_missing_pidfile_reports_error():
    """Cat fails to read the pidfile -> started=False with a clear error."""
    def no_pidfile(upstream, action, args):
        cmd = args["command"]
        if cmd[-1].startswith("cat "):
            return {"ok": False, "text": "cat: /tmp/x.pid: No such file or directory"}
        if cmd[-1].startswith("tail "):
            return {"ok": True, "structured_content": _ok_result(stdout="")}
        return {"ok": True, "structured_content": _ok_result()}

    reg = _register(no_pidfile)
    res = reg.tools["ssh_execute_background"]("172.16.171.11", "melisai",
                                              duration=2)
    assert res["started"] is False
    assert "pid" in (res["error"] or "").lower(), res


# --------------------------------------------------------------------------
# Case 3 — env respected: a command that reads $PATH sees /tmp prepended
# --------------------------------------------------------------------------

# `which` is not on the remote whitelist; use `cat` (whitelisted) on a file
# we know lives in /tmp: the background launcher itself writes
# /tmp/<binary>.pid, and the pidfile is created by the shell redirection.
# The PATH=/tmp:$PATH prefix must be applied with `env` so a slashless
# command resolves against the /tmp-first PATH (old `timeout <d> PATH=...`
# form never even ran the command).


def test_case3_env_prefix_uses_env_command():
    """Case 3: the env prefix is applied via `env PATH=...` — not exec'd as
    a program name by timeout."""
    backend = _AliveBackend()
    reg = _register(backend)
    res = reg.tools["ssh_execute_background"]("172.16.171.11", "iperf3",
                                              args=["-s", "-p", "5201"],
                                              duration=60)
    assert res["started"] is True
    remote = backend.calls[0]["command"][-1]
    assert remote.startswith('env PATH="/tmp:$PATH" timeout 60'), remote


# --------------------------------------------------------------------------
# Optional hard integration (only when SSH_HOST is set)
# --------------------------------------------------------------------------

_need_live = pytest.mark.skipif(not LIVE_HOST, reason="SSH_HOST not set")


@_need_live
def test_live_iperf3_server_stays_alive():
    """E2E (case 1): a reachable host starts iperf3 -s -p 5201; assert
    started=True, pid present, the process alive a few seconds later, and
    port 5201 actually accepting connections — the originally broken
    iperf-dc-01 scenario (old build falsely reported started=True)."""
    import time

    reg = _register(None)
    bg = reg.tools["ssh_execute_background"]
    live = reg.tools.get("ssh_execute")

    # The server self-expires via `timeout 10`; no pkill needed (kill/pgrep
    # are not on the remote whitelist).
    res = bg(LIVE_HOST, "iperf3", args=["-s", "-p", "5201"], duration=10)
    assert res["started"] is True, res
    pid = res["pid"]
    assert pid and isinstance(pid, int)

    time.sleep(3)
    ps = live(LIVE_HOST, "ps", args=["-p", str(pid)])
    assert ps["ok"] and str(pid) in (ps["stdout"] or ""), (res, ps)

    # port 5201 accepts connections (per the task's case-1 definition)
    ss = live(LIVE_HOST, "ss", args=["-ltn"])
    assert ss["ok"] and ":5201" in (ss["stdout"] or ""), ss


@_need_live
def test_live_ensure_file_scp_smoke():
    """Smoke: ssh_ensure_file uploads iperf3 to the host without an scp -l
    regression (uploaded path returned, no usage error)."""
    reg = _register(None)
    res = reg.tools["ssh_ensure_file"](LIVE_HOST, "iperf3")
    assert res["ok"] is True, res
    assert res["uploaded"] == "/tmp/iperf3", res
