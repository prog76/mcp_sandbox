#!/usr/bin/env python3
"""
ipybox-server console-script entrypoint (see pyproject.toml).

Thin wrapper around ipybox.kernel.mcp_server:main so the installed package
exposes the `ipybox-server --host 0.0.0.0 --port 9006` command.
"""

from ipybox.kernel.mcp_server import main

__all__ = ["main"]

if __name__ == "__main__":
    main()