#!/usr/bin/env python3
"""
ipybox IPython startup script.
Injects helpers from the extension registry into builtins.
"""

import builtins
import sys

# Suppress tracebacks in kernel output — agents only need the exception
# type/message, and full stack traces waste tokens / leak internals.
sys.tracebacklimit = 0

from ipybox.kernel.extensions import get_registry

registry = get_registry()
registry.inject_into_builtins()

globals().update({name: fn for name, fn in registry._helpers.items()})

print(f"[ipybox startup] Auto-imported: {', '.join(sorted(registry.list()))}")
