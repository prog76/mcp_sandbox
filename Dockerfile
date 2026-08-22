# ipybox-sandbox image — sandboxed stateful Python environment for the agent.
#
# The image contains ONLY package code: the `ipybox-sandbox` pip package
# (kernel MCP server, IPython startup script, mcp_client helper, extensions).
# All runtime data (skills, workspace) is mounted by docker-compose in
# deploy-new/ — nothing is baked in or COPYed from a data tree.
#
# Entrypoint ships in the package: ipybox-server (ipybox.cli:main).

FROM python:3.12-slim

# System dependencies for IPython kernel + Jupyter ZMQ comms
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install the ipybox-sandbox package (pulls ipykernel, fastmcp, mcp2cli, ...).
COPY . /build/ipybox-sandbox
RUN pip install --no-cache-dir /build/ipybox-sandbox \
    && rm -rf /build

# IPython startup script from the package — auto-imports the kernel helpers
# (exec_run, ssh_execute, mcp_call, list_skills, ...) into builtins.
RUN mkdir -p /root/.ipython/profile_default/startup/ \
    && ln -s "$(python -c 'import ipybox, os; print(os.path.join(os.path.dirname(ipybox.__file__), "kernel", "startup.py"))')" \
       /root/.ipython/profile_default/startup/00_autoimport.py

# Extensions dir (pluggable kernel extensions, discovered via IPYBOX_EXTENSIONS_DIR)
RUN mkdir -p /opt/ipybox/extensions /var/mcp/skills /var/mcp/workspace

EXPOSE 9006

ENTRYPOINT ["ipybox-server"]
CMD ["--host", "0.0.0.0", "--port", "9006"]