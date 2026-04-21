# notion-local-ops-mcp

[中文说明](./README.zh-CN.md)

Turn a Notion **MCP Agent** into a local coding agent for local files, shell, git, and delegated tasks.

![MCP Agent working in a local repo](./assets/notion/notion-handoff-chat.png)

## What This Project Does

- exposes local files, shell, git, and patch-style editing through MCP
- lets an MCP Agent work on a real local repo instead of only editing Notion pages
- supports delegated long-running tasks through local `codex` or `claude`

## Quick Start

```bash
git clone https://github.com/<your-account>/notion-local-ops-mcp.git
cd notion-local-ops-mcp
cp .env.example .env
./scripts/dev-tunnel.sh
```

Set at least:

```bash
NOTION_LOCAL_OPS_WORKSPACE_ROOT="/absolute/path/to/workspace"
NOTION_LOCAL_OPS_AUTH_TOKEN="replace-me"
```

## Important MCP Agent Configuration

Use this in your MCP Agent configuration inside Notion:

- URL: `https://<your-domain-or-tunnel>/mcp`
- Auth type: `Bearer`
- Token: `NOTION_LOCAL_OPS_AUTH_TOKEN`

Use the prompt below for the **MCP Agent**. It is not for the Notion AI instruction page.

<details>
<summary><strong>Recommended MCP Agent prompt</strong></summary>

```text
You are a pragmatic local operations agent connected to my computer through MCP.

Goals:
- Complete file, code, shell, and task workflows end-to-end with minimal interruption.
- Act more like a coding agent than a chat assistant.
- Stay concise, direct, and outcome-focused.

Disambiguation rules:
- If the context contains local repo paths, filenames, code extensions, README, AGENTS.md, CLAUDE.md, or .cursorrules, treat "document", "file", "notes", "instructions", and "docs" as local files unless the user explicitly says Notion page, wiki, or workspace page.
- If the user asks to edit AGENTS.md, CLAUDE.md, README, or project instructions inside the repo, edit the local file. Do not switch into self-configuration or setup behavior unless the user explicitly says to change the agent itself.
- For local file edits, do not use <edit_reference>. That is for Notion page editing, not MCP file changes.
- When answering code questions, prefer file paths, line references, function names, command output, or git diff over Notion-style citation footnotes.

Working style:
- First restate the goal in one sentence.
- Default to the current workspace root unless the target path is genuinely ambiguous.
- For non-trivial tasks, give a short plan and keep progress updated.
- Prefer direct tools first. Use delegate_task only when direct tools are not enough.
- Keep moving forward instead of asking for information that can be discovered via tools.
- If the user says fix, change, implement, deploy, update, or similar imperative requests, execute directly instead of stopping after analysis.
- If information is missing, probe with tools first. Use ask-survey only when tool probing still cannot resolve a decision and the next step is destructive or high-risk.

Tool strategy:
- list_skills: use when the user asks what skills are available in this repo or globally.
- server_info: call first when troubleshooting connection/runtime mismatches.
- set_default_cwd / get_default_cwd: set once for repeated repo operations instead of passing cwd every time.
- In coding tasks, search the local repo first. Do not default to searching the Notion workspace.
- apply_patch: use this as the default edit tool for existing files, including small edits, multi-hunk edits, moves, deletes, or adds in one patch. Use dry_run=true, validate_only=true, or return_diff=true when you want validation or a preview before writing.
- write_file: create new files or rewrite short files when that is simpler than patching; use dry_run=true for no-write preview.
- run_command_stream: start long-running shell jobs with immediate task_id return for polling progress. Prefer it for tests, installs, builds, compile steps, and other jobs that may take a while.
- get_task / wait_task: check delegated task or background command status; prefer wait_task when blocking is useful.
- run_command: proactively use for short non-destructive commands such as pwd, ls, rg, or small smoke checks.
- search: canonical query tool. mode='glob' for path discovery, mode='regex' for regex/code search, mode='text' for literal substring search. Hidden entries and .gitignore'd paths are excluded by default; regex/text search can target a single file path directly.
- list_files: inspect directory structure only when structure matters; paginate with limit and offset when needed.
- read_text: canonical single/batch file reader with line-based pagination; set include_line_numbers=true when the result will be cited or reviewed line-by-line.
- git_status / git_diff / git_commit / git_log / git_show / git_blame: use these as the default repository workflow and traceability tools only when the current cwd is actually inside a git repo.
- delegate_task: use only for complex multi-file reasoning, long-running fallback execution, or repeated failed attempts with direct tools by local codex or claude-code. For non-trivial work, pass goal, acceptance_criteria, verification_commands, and commit_mode.
- cancel_task: stop a delegated task if needed.
- purge_tasks: garbage-collect stale task artifacts under STATE_DIR/tasks (dry_run first).

Execution rules:
- When exploring a codebase, prefer search(mode='glob' or 'regex') over broad list_files calls.
- Follow the loop: probe, edit, verify, summarize.
- Do the minimum necessary read/explore work before editing.
- After each edit, re-read the changed section or run a minimal verification command when useful.
- Prefer apply_patch for edits to existing files; reserve write_file for new files or full rewrites.
- Do not issue parallel writes to the same file.
- After a logically meaningful change, inspect git_status and git_diff, then create a small focused commit instead of waiting until the end.
- Use focused commits. Do not mix unrelated changes in one commit.
- Use clear commit messages, preferably conventional commit style such as fix, feat, docs, test, refactor, or chore.
- For destructive actions such as deleting files, resetting changes, or dangerous shell commands, ask first.
- If a command or delegated task fails, summarize the root cause and adjust the approach instead of retrying blindly.

Verification rules:
- After code changes, prefer this minimum verification ladder when applicable:
- 1. Syntax or compile check such as cargo check, tsc --noEmit, python -m py_compile, or equivalent.
- 2. Focused tests for the changed area, or the nearest relevant test target.
- 3. Smoke test for the changed behavior, such as starting a service or running curl against the affected endpoint.
- Do not skip verification unless the user explicitly says not to run it.

Output style:
- Before tool use, briefly say what you are about to do.
- During longer tasks, send short progress updates.
- At the end, summarize result, verification, and any remaining risk or next step.
```

</details>

## Optional Use Case

If you also want the **Notion AI instruction page + project-management** workflow, see:

- [Optional use case: Notion AI instruction page + project management](./docs/notion-use-case.md)
- [可选应用场景：Notion AI 页面级指令 + 项目管理](./docs/notion-use-case.zh-CN.md)

## Requirements

- Python 3.11+
- `cloudflared`
- A Notion workspace where you can configure an **MCP Agent** with custom MCP support
- Optional: `codex` CLI
- Optional: `claude` CLI

## Detailed Setup

If you prefer the full step-by-step setup, follow this path:

```bash
git clone https://github.com/<your-account>/notion-local-ops-mcp.git
cd notion-local-ops-mcp

cp .env.example .env
```

Edit `.env` and set at least:

```bash
NOTION_LOCAL_OPS_WORKSPACE_ROOT="/absolute/path/to/workspace"
NOTION_LOCAL_OPS_AUTH_TOKEN="replace-me"
```

Then run:

```bash
./scripts/dev-tunnel.sh
```

What you should expect:

- the script creates or reuses `.venv`
- the script installs missing Python dependencies automatically
- the script starts the local MCP server on `http://127.0.0.1:8766/mcp`
- the script prefers `cloudflared.local.yml` for a named tunnel
- otherwise it falls back to a `cloudflared` quick tunnel and prints a public HTTPS URL

Use the printed tunnel URL with `/mcp` appended in Notion, and use `NOTION_LOCAL_OPS_AUTH_TOKEN` as the Bearer token.

### Manual Install

```bash
git clone https://github.com/<your-account>/notion-local-ops-mcp.git
cd notion-local-ops-mcp

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Configure

If you are not using the one-command flow, copy `.env.example` to `.env` and set at least:

```bash
cp .env.example .env
NOTION_LOCAL_OPS_WORKSPACE_ROOT="/absolute/path/to/workspace"
NOTION_LOCAL_OPS_AUTH_TOKEN="replace-me"
```

Optional:

```bash
NOTION_LOCAL_OPS_CODEX_COMMAND="codex"
NOTION_LOCAL_OPS_CLAUDE_COMMAND="claude"
NOTION_LOCAL_OPS_COMMAND_TIMEOUT="120"
NOTION_LOCAL_OPS_DELEGATE_TIMEOUT="1800"
```

### Manual Start

```bash
source .venv/bin/activate
notion-local-ops-mcp
```

Local endpoint:

```text
http://127.0.0.1:8766/mcp
```

### One-Command Local Dev Tunnel

Recommended local workflow:

```bash
./scripts/dev-tunnel.sh
```

What it does:

- reuses or creates `.venv`
- installs missing runtime dependencies
- loads `.env` from the repo root if present
- starts `notion-local-ops-mcp`
- prefers `cloudflared.local.yml` or `cloudflared.local.yaml` if present
- otherwise opens a `cloudflared` quick tunnel to your local server

Notes:

- `.env` is gitignored, so your local token and workspace path stay out of git
- `cloudflared.local.yml` is gitignored, so your local named tunnel config stays out of git
- if `NOTION_LOCAL_OPS_WORKSPACE_ROOT` is unset, the script defaults it to the repo root
- if `NOTION_LOCAL_OPS_AUTH_TOKEN` is unset, the script exits with an error instead of guessing
- for a fresh clone, you do not need to run `pip install` manually before using this script

### Windows Optional Multi-Instance Launcher

If you want a Windows-focused operator workflow for multiple local MCP instances and quick tunnels, use:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\launch-mcp-manager.ps1
```

Notes:

- this launcher is optional and separate from the core MCP server
- install `cloudflared` on PATH, or set `NOTION_LOCAL_OPS_CLOUDFLARED_COMMAND` to an explicit executable path
- set `NOTION_LOCAL_OPS_STATUS_PATH` if you want the status snapshot written somewhere other than the Desktop
- per-instance logs and state live under `.state/launcher/`

### Expose With cloudflared

#### Quick tunnel

```bash
cloudflared tunnel --url http://127.0.0.1:8766
```

Use the generated HTTPS URL with `/mcp`.

#### Named tunnel

Copy [`cloudflared-example.yml`](./cloudflared-example.yml) to `cloudflared.local.yml`, fill in your real values, then run:

```bash
cp cloudflared-example.yml cloudflared.local.yml
./scripts/dev-tunnel.sh
```

Or run cloudflared manually:

```bash
cloudflared tunnel --config ./cloudflared-example.yml run <your-tunnel-name>
```

## Environment Variables

| Variable | Required | Default |
| --- | --- | --- |
| `NOTION_LOCAL_OPS_HOST` | no | `127.0.0.1` |
| `NOTION_LOCAL_OPS_PORT` | no | `8766` |
| `NOTION_LOCAL_OPS_WORKSPACE_ROOT` | yes | home directory |
| `NOTION_LOCAL_OPS_STATE_DIR` | no | `~/.notion-local-ops-mcp` |
| `NOTION_LOCAL_OPS_AUTH_TOKEN` | no | empty |
| `NOTION_LOCAL_OPS_CLOUDFLARED_CONFIG` | no | empty |
| `NOTION_LOCAL_OPS_TUNNEL_NAME` | no | empty |
| `NOTION_LOCAL_OPS_CLOUDFLARED_COMMAND` | no | empty |
| `NOTION_LOCAL_OPS_STATUS_PATH` | no | Desktop `Notion-MCP-status.txt` |
| `NOTION_LOCAL_OPS_CODEX_COMMAND` | no | `codex` |
| `NOTION_LOCAL_OPS_CLAUDE_COMMAND` | no | `claude` |
| `NOTION_LOCAL_OPS_COMMAND_TIMEOUT` | no | `120` |
| `NOTION_LOCAL_OPS_DELEGATE_TIMEOUT` | no | `1800` |
| `NOTION_LOCAL_OPS_DEBUG_MCP_LOGGING` | no | `0` |

## MCP Tools

- `list_files`: list files and directories with pagination; excludes hidden/junk dirs and respects `.gitignore` by default
- `list_skills`: discover project and global skills with name and description summaries
- `search`: canonical query tool that unifies glob path search, regex grep, and literal substring search; excludes hidden and `.gitignore`d paths by default and supports regex/text search against a single file path
- `read_text`: canonical single/batch reader with line-based pagination (`start_line`/`line_limit`), optional `include_line_numbers`, and `language` hint
- `write_file`: write full file content, supports `dry_run`
- `apply_patch`: default edit tool for existing files; supports add/update/move/delete patches plus `dry_run`, `validate_only`, and optional diff output
- `server_info`: inspect runtime config and the registered MCP tool list
- `set_default_cwd`: set session default working directory for subsequent calls
- `get_default_cwd`: inspect current session/effective working directory
- `git_status`: structured repository status (use when cwd is inside a git repo)
- `git_diff`: structured diff output grouped by file with per-file truncation
- `git_commit`: stage selected paths or all changes and create a commit (`amend` / `allow_empty` / `author` / `sign_off` / `dry_run`)
- `git_log`: recent commit history
- `git_show`: inspect metadata and per-file diff for a commit/ref
- `git_blame`: line-level blame metadata for a file/range
- `run_command`: run local shell commands, optionally in background
- `run_command_stream`: start a background shell job and poll output by task id; this is the preferred route for long tests/builds/installs
- `delegate_task`: send a task to local `codex` or `claude-code`, with optional `goal`, `acceptance_criteria`, `verification_commands`, and `commit_mode`
- `get_task`: read task status and output tail
- `wait_task`: block until a delegated or background shell task completes or times out
- `cancel_task`: stop a delegated or background shell task
- `purge_tasks`: clean old task artifacts from `STATE_DIR/tasks` with dry-run support

## Debugging Notion / MCP handshake issues

If a client appears connected but hangs during initialize, tools/list, or tool calls, enable verbose MCP request logging:

```bash
NOTION_LOCAL_OPS_DEBUG_MCP_LOGGING=1 ./scripts/dev-tunnel.sh
```

When enabled, the server log includes `MCP_DEBUG` lines with:

- HTTP method and path
- session id hint
- JSON-RPC method
- tool name for `tools/call`
- response status and duration

## Verify

```bash
source .venv/bin/activate
pytest -q
python -m compileall src tests
```

### Local MCP call simulation tests

Use these to simulate real MCP client/server flows locally (initialize + call_tool + wait_task):

```bash
source .venv/bin/activate
pytest -q tests/test_server_transport.py tests/test_concurrent_clients.py tests/test_mcp_local_simulation.py
```

## Troubleshooting

### Notion says it cannot connect

- Check the URL ends with `/mcp`
- Check the auth type is `Bearer`
- Check the token matches `NOTION_LOCAL_OPS_AUTH_TOKEN`
- Check `cloudflared` is still running

### MCP endpoint works locally but not over tunnel

- Retry with a named tunnel instead of a quick tunnel
- Confirm a real MCP client can list tools from `/mcp`, for example:

```bash
source .venv/bin/activate
fastmcp list http://127.0.0.1:8766/mcp
```

### Logs show repeated 404s

- If the 404 is for `GET /`, the configured URL likely missed the `/mcp` suffix
- If the 404/405 happens while using `/mcp`, upgrade to a build that serves streamable HTTP on `/mcp`

### `delegate_task` fails

- Check `codex --help`
- Check `claude --help`
- Set `NOTION_LOCAL_OPS_CODEX_COMMAND` or `NOTION_LOCAL_OPS_CLAUDE_COMMAND` if needed
