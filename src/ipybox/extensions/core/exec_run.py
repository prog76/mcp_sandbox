"""
exec_run extension — calls the exec backend's run tool.
"""

import shlex
from typing import Dict, List, Optional


def register(registry):
    """Register exec_run helper."""

    def exec_run(
        command: List[str] | str,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        timeout: int = 60,
        stdin: Optional[str] = None,
    ) -> str:
        """Run a command via the exec backend.

        Args:
            command: Command as list or string.
            env: Extra environment variables.
            cwd: Working directory.
            timeout: Seconds (default 60).
            stdin: String piped to subprocess stdin.
        """
        if isinstance(command, str):
            command = shlex.split(command)
        if not command:
            return "Error: command is empty"

        # Get mcp_call from registry (it's already registered)
        mcp_call = registry.get("mcp_call")
        if mcp_call is None:
            return "Error: mcp_call not registered"

        result = mcp_call(
            "exec",
            "run",
            {
                "command": command,
                "binary": command[0],
                "env": env or {},
                "cwd": cwd,
                "timeout": timeout,
                "stdin": stdin,
            },
        )
        return result["text"] if isinstance(result, dict) else str(result)

    registry.add(
        "exec_run",
        exec_run,
        description="Run a command via the exec backend",
        category="core",
    )

