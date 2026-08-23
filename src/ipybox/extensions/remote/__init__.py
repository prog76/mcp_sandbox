"""Remote extensions — SSH, kubectl, etc."""

from . import ssh, kubectl


def register(registry):
    """Register all remote extensions."""
    ssh.register(registry)
    kubectl.register(registry)
