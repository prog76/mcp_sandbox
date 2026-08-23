"""
jobs extension — long-running background jobs inside the kernel.

Jobs execute code strings (or zero-arg callables) as threads in the owning
kernel, so they share the live user namespace: variables submitted are visible
to the job and assignments made by the job persist back into the session.

Agent workflow:
    jid = job_submit("train_model(df)")   # returns immediately
    job_wait(jid, timeout=60)             # blocks <=60s; result OR progress+logs
    job_kill(jid); job_list()

Killing is COOPERATIVE: long loops should call ``cancelled()`` (injected into
the submitted code) to exit early — a running thread cannot be forced dead.
"""

import io
import os
import threading
import time
import traceback
import uuid
from collections import deque

MAX_JOBS = int(os.environ.get("IPYBOX_MAX_JOBS", "8"))
JOB_MAX_RUNTIME = float(os.environ.get("IPYBOX_JOB_MAX_RUNTIME", "3600"))
LOG_TAIL_LINES = 20

_jobs = {}


class _Job:
    def __init__(self, jid, name):
        self.id = jid
        self.name = name
        self.status = "running"          # running | done | error | killed
        self.submitted = time.monotonic()
        self.finished = None
        self.result_repr = None
        self.error = None
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.logs = deque(maxlen=500)

    @property
    def elapsed(self):
        return (self.finished or time.monotonic()) - self.submitted


class _JobKilled(Exception):
    """Raised at cancelled() checkpoints inside killed jobs."""


def _fmt_state(job, tail_lines=None):
    lines = [
        f"job_id: {job.id}",
        f"name: {job.name}",
        f"status: {job.status}",
        f"elapsed: {job.elapsed:.0f}s",
    ]
    if job.status == "done":
        lines.append(f"result: {job.result_repr}")
    elif job.status == "error":
        lines.append(f"error: {job.error}")
    if tail_lines is not None and job.logs:
        lines.append("recent log:")
        for entry in list(job.logs)[-tail_lines:]:
            lines.append(f"  {entry}")
    return "\n".join(lines)


def _run_job(job, target, user_ns):
    real_print = print

    def job_print(*args, **kwargs):
        kwargs = {k: v for k, v in kwargs.items() if k != "file"}
        buf = io.StringIO()
        real_print(*args, file=buf, **kwargs)
        text = buf.getvalue()
        for line in text.rstrip("\n").splitlines():
            job.logs.append(line)
        real_print(text, end="")  # still echo to kernel output

    def job_cancelled():
        # Checkpoint: raises inside killed jobs so loops exit promptly.
        if job.cancel.is_set():
            raise _JobKilled()
        return False

    local_ns = dict(user_ns) if user_ns else {}
    try:
        local_ns["print"] = job_print
        local_ns.setdefault("cancelled", job_cancelled)
        result = target(local_ns)
        job.result_repr = repr(result) if result is not None else None
        job.status = "killed" if job.cancel.is_set() else "done"
    except _JobKilled:
        job.status = "killed"
    except BaseException as e:  # noqa: BLE001 — report, never propagate out of the thread
        job.status = "error"
        job.error = f"{type(e).__name__}: {e}"
        job.logs.append(f"{type(e).__name__}: {e}")
    finally:
        job.finished = time.monotonic()
        # Persist assignments made by the job back into the session namespace
        # BEFORE signalling done, so job_wait callers see the final namespace.
        if user_ns is not None:
            skip = ("print", "cancelled", "__builtins__", "get_ipython", "exit", "quit")
            try:
                for k, v in local_ns.items():
                    if k not in skip:
                        user_ns[k] = v
            except Exception:
                pass
        job.done.set()


def register(registry):
    """Register background-job helpers."""

    def job_submit(fn_or_code, name: str = "") -> str:
        """Run code in a background job; returns a job_id immediately.

        Args:
            fn_or_code: Zero-arg callable, or a code string executed in a copy
                of the session namespace (assignments persist back afterwards).
                Inside submitted code these helpers are available:
                  cancelled() -> bool  raises after job_kill(jid); use in loops
                  print(...)           captured into the job's log
            name: Optional short label shown by job_list/job_wait.

        Returns plain text starting with "job_id: <id>".
        """
        if len(_jobs) >= MAX_JOBS:
            active = sum(1 for j in _jobs.values() if j.status == "running")
            return (f"Error: job limit reached ({len(_jobs)} tracked, "
                    f"{active} running). Use job_kill or collect finished jobs.")

        try:
            ip = get_ipython()  # noqa: F821 — present inside kernels
            user_ns = ip.user_ns if ip is not None else None
        except Exception:
            user_ns = None

        if callable(fn_or_code):
            def target(ns, _fn=fn_or_code):
                return _fn()
            default_name = getattr(fn_or_code, "__name__", "callable")
        else:
            code = str(fn_or_code)

            def target(ns, _code=code):
                # Execute at namespace level (NOT wrapped in a function) so
                # top-level assignments persist into the session namespace.
                exec(compile(_code, "<job>", "exec"), ns)  # noqa: S102
                # If the job ended with a bare expression, use its value as
                # the reported result.
                stripped = _code.strip()
                if stripped:
                    try:
                        return eval(compile(stripped.splitlines()[-1], "<job>", "eval"), dict(ns))  # noqa: S307
                    except Exception:
                        return None

            default_name = "code"

        job = _Job(uuid.uuid4().hex[:12], name or default_name[:40])
        _jobs[job.id] = job
        threading.Thread(target=_run_job, args=(job, target, user_ns),
                         daemon=True, name=f"job-{job.id}").start()
        return f"job_id: {job.id}\nstatus: submitted\nuse job_wait('{job.id}', timeout=60) to poll"

    def job_wait(job_id: str, timeout: int = 60, tail: int = LOG_TAIL_LINES) -> str:
        """Block up to `timeout` seconds waiting for a job to finish.

        Args:
            job_id: Id returned by job_submit.
            timeout: Max seconds to block (clamped 1..300).
            tail: How many recent log lines to include.

        Returns the final state (result/error + logs) on completion, otherwise
        status + recent log lines, so every poll carries new information.
        """
        job = _jobs.get(job_id)
        if job is None:
            known = ", ".join(sorted(_jobs)) or "(none)"
            return f"Error: unknown job_id '{job_id}'. Known jobs: {known}"
        finished = job.done.wait(timeout=max(1, min(int(timeout), 300)))
        note = ""
        if not finished and JOB_MAX_RUNTIME and job.elapsed > JOB_MAX_RUNTIME:
            job.cancel.set()
            note = f"\n(wall-time limit {JOB_MAX_RUNTIME:.0f}s exceeded — kill requested)"
        return _fmt_state(job, tail_lines=tail) + note

    def job_kill(job_id: str) -> str:
        """Request cancellation of a background job (cooperative).

        Sets the flag seen by cancelled(); the submitted code exits at its next
        checkpoint. Code that never calls cancelled() keeps running.
        """
        job = _jobs.get(job_id)
        if job is None:
            return f"Error: unknown job_id '{job_id}'"
        if job.status != "running":
            return f"job_id: {job.id}\nstatus: {job.status} (already finished)"
        job.cancel.set()
        return (f"job_id: {job.id}\n"
                f"kill requested — job exits at its next cancelled() checkpoint")

    def job_list() -> str:
        """List this session's background jobs (newest first)."""
        if not _jobs:
            return "No jobs submitted in this session."
        lines = ["jobs:"]
        for job in sorted(_jobs.values(), key=lambda j: -j.submitted):
            lines.append(f"- {job.id}  {job.status:<8} {job.elapsed:.0f}s  {job.name}")
        return "\n".join(lines)

    registry.add("job_submit", job_submit,
                 description="Run code in a background job; returns job_id immediately",
                 category="core")
    registry.add("job_wait", job_wait,
                 description="Block up to timeout s for a job; result, or progress + log tail",
                 category="core")
    registry.add("job_kill", job_kill,
                 description="Request cooperative cancellation of a background job",
                 category="core")
    registry.add("job_list", job_list,
                 description="List this session's background jobs",
                 category="core")

