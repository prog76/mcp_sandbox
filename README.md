# ipybox-sandbox

Stateful IPython kernel sandbox with pluggable extensions.

## Installation

```bash
pip install -e ".[dev]"
```

## Usage

```bash
ipybox-server --host 0.0.0.0 --port 9006
```

## Kernel output policy (plain text for agents)

Kernel output is consumed by LLM agents, so it must be plain text:

- `src/ipybox/kernel/startup.py` (loaded by every kernel) sets
  `sys.tracebacklimit = 0`, forces IPython `NoColor`, and overrides
  `showtraceback` to print a single short line (`ExcType: message`) — IPython's
  ultraTB ignores `tracebacklimit` and colors independently of PYTHON_COLORS.
- `kernel/mcp_server._execute_sync` deduplicates errors (iopub `error` messages
  are skipped; only the execute-reply traceback is kept) and strips any
  residual ANSI escapes from all returned text.
- The container also sets `PYTHON_COLORS=0` and `PYTHONDONTWRITEBYTECODE=1`.

The container runs with a read-only root filesystem: writable locations are
`/var/mcp/skills` (skills volume), `/var/mcp/ipybox` (IPython profile), and
the `/tmp` tmpfs.

## Extensions

Extensions are discovered from `IPYBOX_EXTENSIONS_DIR` (default `/opt/ipybox/extensions`).

## Docker

Images are built and published by CI on `v*` tags:
`ghcr.io/prog76/mcp-sandbox:<version>` (+ `latest`). See `.github/workflows/`.

Local build:

```bash
docker build -t mcp-sandbox .
docker run -p 9006:9006 mcp-sandbox
```

## Releasing

1. Bump `version` in `pyproject.toml`.
2. Commit, `git tag vX.Y.Z && git push && git push --tags`.
3. CI tests and pushes the image to GHCR.
4. Bump `SANDBOX_VERSION` in the deploy repo's `.env`.
