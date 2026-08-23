#!/usr/bin/env python3
"""
ipybox IPython startup script.
Injects helpers from the extension registry into builtins.
"""

import builtins
import sys

# Suppress tracebacks in kernel output — agents only need the exception
# type/message, and full stack traces waste tokens / leak internals.
# NOTE: sys.tracebacklimit only affects CPython's default traceback printer.
# IPython formats tracebacks itself (ultraTB), ignoring tracebacklimit, so we
# also override showtraceback below once the shell exists.
sys.tracebacklimit = 0

# IPython renders colored output with its own colorizer (not PYTHON_COLORS).
# Force plain text and short, single-line error reporting.
try:
    _ip = get_ipython()  # noqa: F821 — injected by IPython in startup files
    if _ip is not None:
        _ip.colors = "NoColor"

        def _short_showtraceback(*args, **kwargs):
            etype, evalue, _tb = sys.exc_info()
            if etype is not None:
                print(f"{etype.__name__}: {evalue}", file=sys.stderr)

        _ip.showtraceback = _short_showtraceback
except Exception:
    pass

from ipybox.kernel.extensions import get_registry

registry = get_registry()
registry.inject_into_builtins()

globals().update({name: fn for name, fn in registry._helpers.items()})

print(f"[ipybox startup] Auto-imported: {', '.join(sorted(registry.list()))}")
