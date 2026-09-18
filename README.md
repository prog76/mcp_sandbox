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
the `/tmp` tmpfs. Each new session also gets an isolated, fresh working
directory created under `/tmp/ipybox/<session>` (the kernel's CWD is set to
that dir on start, and it is removed when the session is reaped on idle), so
sessions never share a mutable CWD. Override the root with
`IPYBOX_WORKDIR_BASE` if needed.

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

## Prompts (live reload)

MCP prompts come from `IPYBOX_PROMPTS_DIR` (default `/var/mcp/skills/prompts`,
bind-mounted from the deploy repo's `config/skills/prompts/`).

- **Editing an existing prompt file is live**: the body is re-read on every
  `prompts/get`, exactly like skills read through `get_skill()`. No container
  restart is needed.
- **Adding or removing a prompt file needs a restart**: the name and
  description listed by `prompts/list` are read once at server startup
  (`_register_prompts()`).
- A prompt file that becomes unreadable (deleted mid-flight, bad perms) falls
  back to the body captured at startup and logs a warning instead of failing
  the request.

If a prompt edit does not seem to take effect, check the file the container
actually sees (`docker compose exec ipybox cat /var/mcp/skills/prompts/<f>.md`)
before restarting anything.
