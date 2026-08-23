#!/usr/bin/env python3
"""
Tests for the jobs extension (background long-running kernel jobs):
  - job_submit: immediate return, code-string and callable targets
  - job_wait: blocking until done, progress+log tail on timeout, unknown id
  - job_kill: cooperative cancel via cancelled() checkpoint
  - job_list / namespace persistence back into user_ns
"""

import os
import sys
import time
import unittest

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ipybox.extensions.core import jobs as jobs_ext  # noqa: E402


class _RegistryStub:
    def __init__(self):
        self.helpers = {}

    def add(self, name, fn, description="", category=""):
        self.helpers[name] = fn


class JobsTestBase(unittest.TestCase):
    def setUp(self):
        jobs_ext._jobs.clear()
        self.reg = _RegistryStub()
        jobs_ext.register(self.reg)
        for name in ("job_submit", "job_wait", "job_kill", "job_list"):
            setattr(self, name, self.reg.helpers[name])
        self.user_ns = {"x": 41, "time": time}

    def tearDown(self):
        jobs_ext._jobs.clear()

    def submit_with_ns(self, code):
        """Submit a code string against a fake get_ipython().user_ns."""
        import builtins

        class FakeIP:
            user_ns = self.user_ns

        real = getattr(builtins, "get_ipython", None)
        builtins.get_ipython = lambda: FakeIP()
        try:
            return self.job_submit(code)
        finally:
            if real is not None:
                builtins.get_ipython = real
            else:
                del builtins.get_ipython


class TestJobSubmit(JobsTestBase):
    def test_returns_job_id_immediately(self):
        out = self.submit_with_ns("time.sleep(5)")
        self.assertTrue(out.startswith("job_id: "))
        jid = out.splitlines()[0].split(": ")[1]
        self.assertIn(jid, jobs_ext._jobs)
        self.assertEqual(jobs_ext._jobs[jid].status, "running")

    def test_callable_target(self):
        out = self.job_submit(lambda: 7 * 6, name="mul")
        jid = out.splitlines()[0].split(": ")[1]
        state = self.job_wait(jid, timeout=5)
        self.assertIn("status: done", state)
        self.assertIn("result: 42", state)

    def test_job_limit(self):
        old_max = jobs_ext.MAX_JOBS
        jobs_ext.MAX_JOBS = 0
        try:
            out = self.job_submit("pass")
            self.assertTrue(out.startswith("Error: job limit reached"))
        finally:
            jobs_ext.MAX_JOBS = old_max


class TestJobWait(JobsTestBase):
    def test_waits_for_completion_and_captures_logs(self):
        code = "\n".join(
            f"print('step {i}'); time.sleep(0.05)" for i in range(3)
        )
        out = self.submit_with_ns(code)
        jid = out.splitlines()[0].split(": ")[1]
        state = self.job_wait(jid, timeout=10)
        self.assertIn("status: done", state)
        self.assertIn("recent log:", state)
        self.assertIn("step 2", state)

    def test_timeout_reports_progress_not_failure(self):
        code = (
            "for i in range(10):\n"
            "    print(f'working {i}')\n"
            "    time.sleep(0.4)\n"
        )
        out = self.submit_with_ns(code)
        jid = out.splitlines()[0].split(": ")[1]
        state = self.job_wait(jid, timeout=1)
        self.assertIn("status: running", state)
        self.assertIn("elapsed:", state)
        self.assertIn("recent log:", state)
        final = self.job_wait(jid, timeout=10)
        self.assertIn("status: done", final)

    def test_unknown_job_id(self):
        self.assertTrue(self.job_wait("nope").startswith("Error: unknown job_id"))

    def test_error_reported_not_raised(self):
        out = self.submit_with_ns("raise ValueError('boom')")
        jid = out.splitlines()[0].split(": ")[1]
        state = self.job_wait(jid, timeout=5)
        self.assertIn("status: error", state)
        self.assertIn("ValueError: boom", state)


class TestJobKillAndList(JobsTestBase):
    def test_cooperative_kill(self):
        code = (
            "total = 0\n"
            "for i in range(100):\n"
            "    cancelled()\n"
            "    total += i\n"
            "    time.sleep(0.05)\n"
            "result = total\n"
        )
        out = self.submit_with_ns(code)
        jid = out.splitlines()[0].split(": ")[1]
        kill_out = self.job_kill(jid)
        self.assertIn("kill requested", kill_out)
        state = self.job_wait(jid, timeout=10)
        self.assertIn("status: killed", state)

    def test_kill_after_finish_is_reported(self):
        out = self.submit_with_ns("cancelled()\n")
        jid = out.splitlines()[0].split(": ")[1]
        self.job_wait(jid, timeout=5)
        again = self.job_kill(jid)
        self.assertIn("already finished", again)

    def test_job_list(self):
        self.assertEqual(self.job_list(), "No jobs submitted in this session.")
        out = self.submit_with_ns("pass")
        jid = out.splitlines()[0].split(": ")[1]
        listing = self.job_list()
        self.assertIn("jobs:", listing)
        self.assertIn(jid, listing)


class TestNamespacePersistence(JobsTestBase):
    def test_reads_session_variables(self):
        self.user_ns["base"] = 100
        out = self.submit_with_ns("answer = base + 1")
        jid = out.splitlines()[0].split(": ")[1]
        state = self.job_wait(jid, timeout=5)
        self.assertIn("status: done", state)
        self.assertEqual(self.user_ns.get("answer"), 101)

    def test_result_stored_in_session(self):
        out = self.submit_with_ns("computed = [1, 2, 3]")
        jid = out.splitlines()[0].split(": ")[1]
        self.job_wait(jid, timeout=5)
        self.assertEqual(self.user_ns.get("computed"), [1, 2, 3])

    def test_helpers_not_leaked_into_session(self):
        out = self.submit_with_ns("val = 5")
        jid = out.splitlines()[0].split(": ")[1]
        self.job_wait(jid, timeout=5)
        self.assertNotIn("__ipybox_job__", self.user_ns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
