"""
kubectl extension — execute commands in Kubernetes pods.
"""


def register(registry):
    """Register kubectl helper."""

    def kubectl_exec(namespace, pod, command, container=None):
        """Execute a command inside a Kubernetes pod via kubectl."""
        if isinstance(command, str):
            command = [command]

        cmd = ["kubectl", "exec", "-n", namespace, pod, "--"]
        if container:
            cmd = ["kubectl", "exec", "-n", namespace, "-c", container, pod, "--"]
        cmd.extend(command)

        exec_run = registry.get("exec_run")
        if exec_run is None:
            return "Error: exec_run not registered"
        return exec_run(cmd)

    registry.add("kubectl_exec", kubectl_exec,
                 description="Execute a command in a Kubernetes pod", category="remote")
