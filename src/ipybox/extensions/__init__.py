# !/usr/bin/env python3
"""ipybox pluggable kernel extensions.

Sub-packages (``core``, ``remote``, ...) declare individual ``<name>.py``
modules that expose a ``register(registry)`` entrypoint. Each module adds one
or more callable helpers to the
:class:`ipybox.kernel.extensions.ExtensionRegistry` used by the IPython startup
script (builtins) and the MCP template engine.

The set of loaded extensions is controlled by ``/etc/ipybox/project.yaml``
(``extensions.load`` and ``extensions.extra_extensions``); when unset, the
built-in ``core`` extensions are enabled by default.
"""
