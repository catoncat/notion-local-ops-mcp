# notion-local-ops-mcp

Use Notion AI with your local files, shell, and fallback local agents.

📖 **[Project Introduction (Notion Page)](https://www.notion.so/notion-local-ops-mcp-344b4da3979d80e8958ae3fdf1d5e4d9?source=copy_link)**


## What It Provides

- `list_skills`
- `list_files`
- `glob_files`
- `grep_files`
- `search_files`
- `read_file`
- `read_files`
- `replace_in_file`
- `write_file`
- `apply_patch`
- `git_status`
- `git_diff`
- `git_commit`
- `git_log`
- `run_command`
- `delegate_doctor`
- `codex_exec`
- `claude_exec`
- `claudecode_exec`
- `codex_review`
- `claude_review`
- `delegate_task`
- `get_task`
- `list_tasks`
- `wait_task`
- `cancel_task`

Recommended executor flow:

- use `delegate_doctor` first when codex/claude execution looks broken
- prefer `codex_exec` / `claude_exec` / `codex_review` / `claude_review`
- keep `delegate_task` only as the backward-compatible fallback

Review ranges default to **per-commit slicing** so large diff reviews do not get stuffed into one payload.

`list_skills` returns lightweight project/global skill summaries and scans `.agents/skills`, `.codex/skills`, and global `.claude/skills`.

## Requirements

- Python 3.11+
- `cloudflared`
- Notion Custom Agent with custom MCP support
- Optional: `codex` CLI
- Optional: `claude` CLI

## Quick Start

For a fresh clone, the shortest path is:

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

`NOTION_LOCAL_OPS_WORKSPACE_ROOT` must already exist as a directory before startup; the server no longer auto-creates it.

Then run:

```bash
./scripts/dev-tunnel.sh
```

On Windows PowerShell, use:

```powershell
.\scripts\dev-tunnel.ps1
```

What you should expect:

- the script creates or reuses `.venv`
- the script installs missing Python dependencies automatically
- the script starts the local MCP server on `http://127.0.0.1:8766/mcp`
- the script prefers `cloudflared.local.yml` for a named tunnel
- otherwise it falls back to a `cloudflared` quick tunnel and prints a public HTTPS URL

Use the printed tunnel URL with `/mcp` appended in Notion, and use `NOTION_LOCAL_OPS_AUTH_TOKEN` as the Bearer token.

Important:

- `http://127.0.0.1:8766/mcp` is your **local origin**
- Notion should use the **public HTTPS Cloudflare URL** ending in `/mcp`
- if the tunnel restarts, reconnect the Notion MCP entry so it picks up the new public URL

## Manual Install

```bash
git clone https://github.com/<your-account>/notion-local-ops-mcp.git
cd notion-local-ops-mcp

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

PowerShell equivalent:

```powershell
git clone https://github.com/<your-account>/notion-local-ops-mcp.git
cd notion-local-ops-mcp

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
```

## Configure

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
NOTION_LOCAL_OPS_COMMAND_TIMEOUT="30"
NOTION_LOCAL_OPS_DELEGATE_TIMEOUT="1800"
```

## Manual Start

```bash
source .venv/bin/activate
notion-local-ops-mcp
```

PowerShell equivalent:

```powershell
.\.venv\Scripts\Activate.ps1
notion-local-ops-mcp
```

Local streamable HTTP endpoint:

```text
http://127.0.0.1:8766/mcp
```

## One-Command Local Dev Tunnel

Recommended local workflow:

```bash
./scripts/dev-tunnel.sh
```

PowerShell equivalent:

```powershell
.\scripts\dev-tunnel.ps1
```

For a detached one-click quick tunnel on Windows PowerShell:

```powershell
.\scripts\quick-start.ps1
```

Useful helpers:

```powershell
.\scripts\quick-status.ps1
.\scripts\quick-stop.ps1
```

If you need two separate MCP endpoints for two Notion agents at the same time:

```powershell
.\scripts\quick-start-dual.ps1
```

Optional matching stop helper:

```powershell
.\scripts\quick-stop-dual.ps1
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
- the MCP transport at `/mcp` is **streamable HTTP**, not legacy SSE
- Notion never connects directly to `127.0.0.1`; it connects to the Cloudflare HTTPS `/mcp` URL that forwards to the local origin
- for a fresh clone, you do not need to run `pip install` manually before using this script

## One-Command Dual-Agent Quick Tunnels

If you want two Notion agents to use this MCP server concurrently, start two isolated instances instead of sharing one endpoint:

```powershell
.\scripts\quick-start-dual.ps1
```

Defaults:

- first instance: port `8766`, token from `NOTION_LOCAL_OPS_AUTH_TOKEN`
- second instance: port `8767`, token from `NOTION_LOCAL_OPS_AUTH_TOKEN_SECOND` if present
- if `NOTION_LOCAL_OPS_AUTH_TOKEN_SECOND` is missing, the script generates a random second token for you

Optional overrides:

```powershell
.\scripts\quick-start-dual.ps1 `
  -FirstPort 8766 `
  -SecondPort 9876 `
  -FirstToken "agent-a-token" `
  -SecondToken "agent-b-token"
```

What it does:

- starts two local MCP server processes with separate ports, bearer tokens, and state directories
- opens one cloudflared quick tunnel per instance
- prints two Notion-ready MCP URLs
- writes process and log metadata to `.state\quick-dual-tunnel-state.json`

To stop both instances later:

```powershell
.\scripts\quick-stop-dual.ps1
```

## Expose With cloudflared

### Quick tunnel

```bash
cloudflared tunnel --url http://127.0.0.1:8766
```

Use the generated HTTPS URL with `/mcp`.

### Named tunnel

Copy [`cloudflared-example.yml`](./cloudflared-example.yml) to `cloudflared.local.yml`, fill in your real values, then run:

```bash
cp cloudflared-example.yml cloudflared.local.yml
./scripts/dev-tunnel.sh
```

PowerShell equivalent:

```powershell
Copy-Item cloudflared-example.yml cloudflared.local.yml
.\scripts\dev-tunnel.ps1
```

Or run cloudflared manually:

```bash
cloudflared tunnel --config ./cloudflared-example.yml run <your-tunnel-name>
```

## Add To Notion

Use:

- URL: `https://<your-domain-or-tunnel>/mcp`
- Auth type: `Bearer`
- Token: your `NOTION_LOCAL_OPS_AUTH_TOKEN`

Recommended agent instruction:

```text
Act like a coding agent, not a Notion page editor.
When the context contains repo paths, filenames, code extensions, README, AGENTS.md, CLAUDE.md, or .cursorrules, treat "document", "file", "notes", and "instructions" as local files unless the user explicitly says Notion page, wiki, or workspace page.
For local file changes, do not use <edit_reference>. Use local file tools and, when useful, verify with git_diff, git_status, or tests.
Use direct tools first: list_skills, glob_files, grep_files, read_file, read_files, replace_in_file, write_file, apply_patch, git_status, git_diff, git_commit, git_log, run_command.
Use list_files only when directory structure itself matters, and paginate with limit/offset instead of assuming full output.
Use search_files only for simple substring search when regex or context is unnecessary.
Use read_files when you need a few files at once after search or glob discovery.
Use apply_patch for multi-change edits, same-file multi-location edits, file moves, deletes, or creates. Use dry_run=true or return_diff=true when you want a preview before writing.
Use replace_in_file only for one small exact edit or clearly intentional replace_all edits.
Do not issue parallel writes to the same file.
Use git_status, git_diff, git_commit, and git_log for repository state and traceability instead of raw git shell commands when possible.
Use run_command for verification, tests, builds, rg, pwd, ls, and other non-git shell work. If a command may take longer, set run_in_background=true and follow with get_task or wait_task.
Use delegate_task only when direct tools are insufficient for complex multi-file reasoning, long-running fallback execution, or repeated failed attempts with direct tools. When delegating non-trivial work, pass goal, acceptance_criteria, verification_commands, and commit_mode.
After each logically meaningful change, create a small focused git commit so progress stays traceable. Keep unrelated changes out of the same commit.
```

Recommended full prompt for Notion Agent:

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
- In coding tasks, search the local repo first. Do not default to searching the Notion workspace.
- list_skills: inspect project/global skill summaries when agent capabilities or skill locations matter.
- glob_files: narrow candidate paths by pattern.
- grep_files: search code or text with regex, glob filtering, and output modes.
- list_files: inspect directory structure only when structure matters; paginate with limit and offset when needed.
- search_files: use only for simple substring search when regex or context is unnecessary.
- read_file: read relevant file sections before editing.
- read_files: batch read a few files after search or glob discovery.
- replace_in_file: make one small exact edit; use replace_all only when clearly intended.
- apply_patch: prefer this for multi-hunk edits, same-file multi-location edits, moves, deletes, or adds in one patch. Use dry_run=true or return_diff=true when you want a preview before writing.
- write_file: create new files or rewrite short files when that is simpler than patching.
- git_status / git_diff / git_commit / git_log: use these as the default repository workflow and traceability tools.
- run_command: proactively use for non-destructive commands such as pwd, ls, rg, tests, builds, or smoke checks; set run_in_background=true for longer jobs.
- delegate_task: use only for complex multi-file reasoning, long-running fallback execution, or repeated failed attempts with direct tools by local codex or claude. For non-trivial work, pass goal, acceptance_criteria, verification_commands, and commit_mode.
- get_task / wait_task: check delegated task or background command status; prefer wait_task when blocking is useful.
- cancel_task: stop a delegated task if needed.

Execution rules:
- When exploring a codebase, prefer glob_files and grep_files over broad list_files calls.
- Follow the loop: probe, edit, verify, summarize.
- Do the minimum necessary read/explore work before editing.
- After each edit, re-read the changed section or run a minimal verification command when useful.
- Prefer one apply_patch over multiple replace_in_file calls when changing the same file in several places.
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
| `NOTION_LOCAL_OPS_CODEX_COMMAND` | no | `codex` |
| `NOTION_LOCAL_OPS_CLAUDE_COMMAND` | no | `claude` |
| `NOTION_LOCAL_OPS_COMMAND_TIMEOUT` | no | `30` |
| `NOTION_LOCAL_OPS_DELEGATE_TIMEOUT` | no | `1800` |

## Tool Notes

- `list_files`: list files and directories, with `limit` and `offset` pagination
- `glob_files`: find files or directories by glob pattern
- `grep_files`: advanced regex search with glob filtering and output modes
- `search_files`: simple substring search for backward compatibility
- `read_file`: read text files with offset and limit
- `read_files`: read a batch of text files with shared offset and limit
- `replace_in_file`: replace one exact text fragment or all exact matches
- `write_file`: write full file content
- `apply_patch`: apply codex-style add/update/move/delete patches, with `dry_run`, `validate_only`, and optional diff output
- `git_status`: structured repository status
- `git_diff`: structured diff output with changed file paths
- `git_commit`: stage selected paths or all changes and create a commit
- `git_log`: recent commit history
- `run_command`: run local shell commands, optionally in background
- `list_skills`: summarize project/global skills and their source roots without loading full skill bodies
- `delegate_doctor`: check local codex/claude readiness, git visibility, and external MCP auth warnings
- `codex_exec` / `claude_exec` / `claudecode_exec`: explicit executor entrypoints with structured task metadata
- `codex_review` / `claude_review`: explicit review entrypoints; commit ranges default to per-commit slicing
- `delegate_task`: backward-compatible fallback to local `codex` or `claude`, with optional `goal`, `instructions`, `acceptance_criteria`, `verification_commands`, and `commit_mode`
- `get_task`: read task status and output tail
- `list_tasks`: list recent delegated/background tasks with optional filters
- `wait_task`: block until a delegated or background shell task completes or times out
- `cancel_task`: stop a delegated or background shell task

## Verify

```bash
source .venv/bin/activate
pytest -q
python -m compileall src tests
```

## Troubleshooting

### Notion says it cannot connect

- Check the URL ends with `/mcp`
- Check the auth type is `Bearer`
- Check the token matches `NOTION_LOCAL_OPS_AUTH_TOKEN`
- Check `cloudflared` is still running
- Check `NOTION_LOCAL_OPS_WORKSPACE_ROOT` already exists and is a directory

### `/mcp` works locally but not over tunnel

- This server uses **streamable HTTP**, not legacy SSE
- Retry with a named tunnel instead of a quick tunnel
- A plain browser `GET /mcp` can return `406` or `405` if the request is not MCP-shaped; that alone does not mean the server is down
- Validate with an MCP-capable client or by reconnecting the Notion MCP integration after the tunnel URL changes

### Two Notion agents keep colliding

- Do not point two agents at the same quick-tunnel MCP URL if one of them is already mid-session
- Prefer two isolated endpoints by running `.\scripts\quick-start-dual.ps1`
- Give each Notion agent its own URL and bearer token pair
- If one agent still shows stale tools, disconnect and reconnect that MCP entry in Notion

### `delegate_task` fails

- Run `delegate_doctor` first
- Check `codex --help`
- Check `codex exec review --help`
- Check `claude --help`
- Set `NOTION_LOCAL_OPS_CODEX_COMMAND` or `NOTION_LOCAL_OPS_CLAUDE_COMMAND` if needed
- Prefer `codex_exec` / `claude_exec` / `codex_review` / `claude_review` over raw `delegate_task`
- Use `goal`, `instructions`, `acceptance_criteria`, `verification_commands`, and `commit_mode`
- For commit ranges, keep the default per-commit slicing unless you explicitly need `split_strategy="single"`
