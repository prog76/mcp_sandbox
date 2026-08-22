#!/usr/bin/env python3
"""
ipybox IPython startup script (placed in profile_default/startup/00_autoimport.py).

Thin wrapper around ``ipybox_helpers`` — the single source of truth for the
kernel helper functions.  This script imports the helpers and injects them
into the kernel's builtins so the agent can call them directly from any
``execute_code`` cell without an explicit import.

Available helpers (see ipybox_helpers for full docs):
  exec_run, ssh_execute, ssh_execute_background, ssh_ensure_file, kubectl_exec,
  mcp_call, mcp_list_upstreams, mcp_list_actions, mcp_describe,
  list_skills, get_skill, create_skill, update_skill,
  list_functions, describe_function
"""

import builtins

from ipybox.helpers import _helpers

# Inject the helpers into builtins so they're available without imports.
for _name, _fn in _helpers.items():
    setattr(builtins, _name, _fn)

# Also re-export the helpers at module level so introspection works
# (list_functions / describe_function / etc. are callable as
# ipybox_startup.<name>).
globals().update(_helpers)

print("[ipybox startup] Auto-imported: " + ", ".join(sorted(_helpers.keys())))
