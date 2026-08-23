# AGENTS.md

Guidance for coding agents working in this repository (`sandbox` / ipybox).

## What this is
Stateful IPython kernel sandbox exposed as an MCP server (HTTP :9006).
Each MCP session gets its own IPython kernel; extensions inject helper
functions into the kernel namespace at startup.

## Layout
- `src/ipybox/kernel/mcp_server.py` — FastMCP server: `execute_code`, `mcp_call`,
  prompts from `/var/mcp/skills/prompts`; per-session kernel management;
  `_execute_sync` collects iopub output and **strips ANSI + dedupes errors**
- `src/ipybox/kernel/startup.py` — runs in every kernel; enforces plain-text
  output (`tracebacklimit=0`, IPython `NoColor`, short `showtraceback`) and
  auto-imports extension helpers into builtins
- `src/ipybox/extensions/` — pluggable kernel helpers (core: exec_run,
  mcp_call, skill_mgmt...; remote: ssh, kubectl)
- `test/` — pytest suite
- `Dockerfile` — sets `PYTHON_COLORS=0`; CI publishes `ghcr.io/prog76/mcp-sandbox`

## Commands
```bash
pip install -e ".[dev]"       # dev extra provides pytest
python3 -m pytest test/ -v    # from repo root
```

## Rules
- Kernel output goes to LLM agents: keep it plain text — no ANSI codes, no long
  stack traces. If you touch output handling, see README "Kernel output policy".
- The container rootfs is read-only; writable: `/var/mcp/skills`,
  `/var/mcp/ipybox`, `/tmp` (tmpfs). Don't add code that writes elsewhere.
- Don't put secrets in this package — privileged ops go through the gateway's
  exec backend via `mcp_call`.

## Releasing
1. Bump `version` in `pyproject.toml`.
2. Commit, `git tag vX.Y.Z`, `git push && git push --tags`.
3. CI tests and pushes the GHCR image.
4. Bump `SANDBOX_VERSION` in the deploy repo's `.env`, then
   `docker compose pull && make up`.