"""mcp_call result contract — spill-to-file + structured result object.

Every mcp_call result carries a stable machine-readable contract:

    file          str   absolute path to the FULL raw output (always written)
    truncated     bool  can the inline ``text``/``structured_content`` be
                        elided for context? True when the full payload exceeds
                        the inline budget.
    bytes_total   int   length of the full raw output
    json          object | None  the FULL structured_content object, or the
                        result of ``json.loads(text)`` when upstream gave no
                        structured content; None only when neither exists.

 ``json`` is an INTERNAL attribute, not a dict item — it is never
 serialized (print/repr/MCP wire render it as ``@object[in_file]``).
 Structured_content remains the only serialized JSON copy.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

# Bind-mounted persistent dir on the HOST side of the container (set in
# docker-compose). Survives container restart — the tmpfs (/tmp/ipybox) does
# NOT. Path is READABLE by the ipybox kernel's agent on its next call.
_SPILL_BASE = os.environ.get("IPYBOX_SPILL_DIR", "/mnt/ipybox-spill")

# How long spilled session dirs are kept before a TTL sweep deletes them.
_SPILL_TTL_SECONDS = float(os.environ.get("IPYBOX_SPILL_TTL_SECONDS", str(7 * 24 * 3600)))

# Inline text preview budget. Matches the host-side budget conventions.
_SPILL_THRESHOLD_CHARS = int(os.environ.get("MCP_RESULT_SIZE_CHARS", "50000"))

# Preview split: 40% head / 60% tail of the budget.
_SPILL_PREVIEW_HEAD = int(_SPILL_THRESHOLD_CHARS * 0.4)
_SPILL_PREVIEW_TAIL = int(_SPILL_THRESHOLD_CHARS * 0.6)

# The on-the-wire placeholder shown instead of the real (internal) json object.
_JSON_PLACEHOLDER = "@object[in_file]"
# Placeholder for large string leaves inside bounded structured_content.
_STRING_PLACEHOLDER = "@string[in_file]"


class McpCallResult(dict):
    """dict subclass with an internal ``json`` attribute.

    The real structured_content object lives in ``self.json`` (attribute),
    never in the dict — so serialization (``dict()``, ``repr``, MCP wire,
    ``json.dumps``) cannot double-carry it. Access stays dict-like:

        r["json"], r.get("json")  -> the internal object (or None)
        r["structured_content"]   -> the bounded serialized copy

    ``__repr__`` renders ``json: '@object[in_file]'`` so a human/agent sees
    that the full object is in the file, without ever printing it.
    """

    def __init__(self, *args: Any, json_value: Any = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.json = json_value

    def __missing__(self, key: str) -> Any:
        if key == "json":
            return self.json
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "json":
            return self.json if self.json is not None else default
        return super().get(key, default)

    def __repr__(self) -> str:
        base = dict(self)
        base["json"] = _JSON_PLACEHOLDER if self.json is not None else None
        return repr(base)

    def __reduce_ex__(self, protocol: int = 0) -> Any:
        # Pickling drops the internal attribute (it is not a dict item) —
        # restore it from the bounded serialized copy.
        data = dict(self)
        obj = super().__reduce_ex__(protocol)
        return obj, (None,)


def _spill_root() -> str:
    return _SPILL_BASE


def _session_spill_dir(session_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "-", str(session_id)).strip(".-") or "session"
    return os.path.join(_SPILL_BASE, safe[:80])


def write_spill(session_id: str, tool_use_id: str, text: str, seq: int) -> str:
    """Write the FULL raw output to a per-session spill file.

    Always writes the complete ``text`` (never a preview). Filename is
    ``{seq:06d}.txt`` in the session's spill dir — monotonic per session,
    sortable, debuggable. Returns the absolute path.
    """
    try:
        d = _session_spill_dir(session_id)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{seq:06d}.txt")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        return path
    except Exception as e:
        log.warning("spill write failed: %s", e)
        return ""


def _preview(text: str, total: int) -> str:
    """40/60 head/tail preview with an explicit truncation header."""
    head = text[:_SPILL_PREVIEW_HEAD]
    tail = text[-_SPILL_PREVIEW_TAIL:] if total > _SPILL_THRESHOLD_CHARS else ""
    omitted = total - (len(head) + len(tail))
    if omitted > 0:
        return (
            f"{head}\n\n[... OUTPUT TRUNCATED - {omitted:,} chars omitted "
            f"out of {total:,} total ...]\n\n{tail}"
        )
    return text


def _bounded_structured(
    structured: Any, budget: int = _SPILL_THRESHOLD_CHARS
) -> Any:
    """Return a JSON-able view of ``structured`` whose total repr stays within
    ``budget``; large string leaves are elided to ``@string[in_file]``."""
    def _walk(v: Any) -> Any:
        if isinstance(v, str):
            return _STRING_PLACEHOLDER if len(v) > budget else v
        if isinstance(v, dict):
            return {k: _walk(val) for k, val in v.items()}
        if isinstance(v, (list, tuple)):
            return [_walk(x) for x in v]
        return v
    try:
        s = json.dumps(structured)
        if len(s) > budget:
            return _walk(structured)
        return structured
    except Exception:
        return structured


def build_result(
    *,
    ok: bool,
    is_error: bool,
    upstream: str,
    action: str,
    text: str,
    content: list,
    structured: Any,
    session_id: str,
    seq: int,
) -> McpCallResult:
    """Build the v1 mcp_call contract result (spill + bounded views)."""
    total = len(text)
    path = write_spill(session_id, f"{seq:06d}", text, seq) if text else ""
    truncated = total > _SPILL_THRESHOLD_CHARS
    preview = _preview(text, total) if truncated else text
    json_value = structured if structured is not None else None
    if json_value is None and text:
        try:
            json_value = json.loads(text) if text.strip().startswith(("{", "[")) else None
        except Exception:
            json_value = None
    bounded_sc = _bounded_structured(structured) if structured is not None else None
    return McpCallResult(
        {
            "ok": ok,
            "is_error": is_error,
            "upstream": upstream,
            "action": action,
            "text": preview,
            "content": content,
            "structured_content": bounded_sc,
            "file": path,
            "truncated": truncated,
            "bytes_total": total,
        },
        json_value=json_value,
    )


def cleanup_ttl_sessions(now: Optional[float] = None) -> int:
    """Delete spill session dirs older than the TTL (best-effort)."""
    now = now if now is not None else time.time()
    removed = 0
    try:
        if not os.path.isdir(_SPILL_BASE):
            return 0
        for name in os.listdir(_SPILL_BASE):
            p = os.path.join(_SPILL_BASE, name)
            try:
                if os.path.isdir(p) and (now - os.path.getmtime(p)) > _SPILL_TTL_SECONDS:
                    shutil.rmtree(p, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    except OSError as e:
        log.warning("TTL sweep error: %s", e)
    return removed
