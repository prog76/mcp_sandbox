"""Tests for the ssh extension helpers (argv construction — no real ssh)."""

import os
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
    def __init__(self):
        self.tools = {}

    def add(self, name, fn, description=None, category=None):
        self.tools[name] = fn

    def get(self, name):
        return self.tools.get(name)


def _register(mcp_call=None):
    reg = _Registry()
    sshmod.register(reg)
    if mcp_call is not None:
        reg.tools["mcp_call"] = mcp_call
    return reg


def _ok_result(stdout=""):
    return {"ok": True, "stdout": stdout, "stderr": "", "exit_code": 0,
            "timed_out": False}


def test_opts_use_user_option_not_dash_l():
    """SSH_USER maps to `-o User=...`, valid for both ssh and scp.

    Regression: the helper used `-l <user>`, which scp parses as
    'bandwidth limit' -> usage error on every ssh_ensure_file upload.
    """
    env = {"SSH_USER": "aedobshikov", "SSH_KEY_PATH": "/k/id_rsa"}
    with mock.patch.dict(os.environ, env):
        opts = sshmod._ssh_opts("172.16.185.46")
    assert "-l" not in opts
    assert "User=aedobshikov" in opts
    assert opts[opts.index("-i") + 1] == "/k/id_rsa"


def test_opts_skip_user_when_machine_has_user():
    with mock.patch.dict(os.environ, {"SSH_USER": "u1", "SSH_KEY_PATH": ""}):
        opts = sshmod._ssh_opts("someuser@somehost")
    assert "-l" not in opts
    assert "User=u1" not in opts


def test_opts_no_user_without_ssh_user():
    with mock.patch.dict(os.environ, {"SSH_USER": "", "SSH_KEY_PATH": ""}):
        opts = sshmod._ssh_opts("10.0.0.1")
    assert "-l" not in opts
    assert not any(o.startswith("User=") for o in opts)


def test_ensure_file_scp_argv_uses_user_option():
    """The scp step of ssh_ensure_file must carry `-o User=<user>` and no -l."""
    seen = {}

    def fake_mcp_call(upstream, action, args):
        seen.setdefault("argvs", []).append(args["command"])
        return {"ok": True, "structured_content": _ok_result()}

    reg = _register(fake_mcp_call)
    env = {"SSH_USER": "u1", "SSH_KEY_PATH": "/k/id_rsa"}
    with mock.patch.dict(os.environ, env):
        res = reg.tools["ssh_ensure_file"]("172.16.5.6", "iperf3")

    assert res["ok"] is True, res
    assert res["uploaded"] == "/tmp/iperf3"
    scp_argv = seen["argvs"][0]
    assert scp_argv[0] == "scp"
    assert "-l" not in scp_argv
    assert "User=u1" in scp_argv
    assert scp_argv[-1] == "172.16.5.6:/tmp/iperf3"
    # chmod step also uses the shared opts
    chmod_argv = seen["argvs"][1]
    assert chmod_argv[0] == "ssh"
    assert "-l" not in chmod_argv
    assert "User=u1" in chmod_argv


def test_ensure_file_reports_scp_step_failure():
    """A failed scp surfaces as ok=False with step='scp' and the error text."""

    def fake_mcp_call(upstream, action, args):
        return {"ok": False, "text": "usage: scp [-346ABCOpqRrsTv] ..."}

    reg = _register(fake_mcp_call)
    env = {"SSH_USER": "u1", "SSH_KEY_PATH": ""}
    with mock.patch.dict(os.environ, env):
        res = reg.tools["ssh_ensure_file"]("172.16.5.6", "iperf3")

    assert res["ok"] is False
    assert res["step"] == "scp"
    assert "usage" in (res["error"] or "")


def test_execute_argv_uses_user_option():
    seen = {}

    def fake_mcp_call(upstream, action, args):
        seen["argv"] = args["command"]
        return {"ok": True, "structured_content": _ok_result(stdout="host")}

    reg = _register(fake_mcp_call)
    env = {"SSH_USER": "u1", "SSH_KEY_PATH": ""}
    with mock.patch.dict(os.environ, env):
        res = reg.tools["ssh_execute"]("10.1.1.1", "hostname")

    assert res["ok"] is True
    argv = seen["argv"]
    assert argv[0] == "ssh"
    assert "-l" not in argv
    assert "User=u1" in argv
