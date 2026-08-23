"""Core extensions — exec_run, mcp_call, skill_mgmt, introspection."""

from . import exec_run, mcp_call, skill_mgmt, introspection


def register(registry):
    """Register all core extensions."""
    exec_run.register(registry)
    mcp_call.register(registry)
    skill_mgmt.register(registry)
    introspection.register(registry)
