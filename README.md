# mcp-sprawl-audit

Every Claude Code, Codex or Grok session starts its own copy of each local MCP server in
its config. With many sessions open, that adds up to hundreds of processes and gigabytes
of memory, most of it waiting for a tool call that never comes.

This is an agent skill and a single Python script that measures that on your machine and
writes a plan. It is read-only: it changes no config and touches no process. It reads the
names of env vars and headers, never their values. From a command line it prints only a
package name or a script's file name, and from a URL only the scheme and host, because the
rest can carry keys.

[Why this exists](https://karanbansal.in/blog/mcp-sprawl/) tells the story of 57 agent
sessions running 646 MCP server processes on one laptop, and how sharing three servers over
localhost took them from 19.5 GB to 301 MB.

## Use it

Hand it to your agent:

```text
Clone https://github.com/karanb192/mcp-sprawl-audit and follow its SKILL.md to audit my MCP servers.
```

Or install it as a skill, so any session can run it:

```sh
git clone https://github.com/karanb192/mcp-sprawl-audit ~/.claude/skills/mcp-sprawl-audit   # Claude Code, and Grok reads this folder too
git clone https://github.com/karanb192/mcp-sprawl-audit ~/.codex/skills/mcp-sprawl-audit    # Codex CLI
```

Or run the script yourself. It needs only Python (3.11+ to read Codex and Grok TOML).

```sh
python3 scripts/audit.py           # Markdown report
python3 scripts/audit.py --json    # structured, for agents
python3 scripts/audit.py --no-usage --days 14
```

## What it reports

- Running agent sessions per agent, and every local MCP server copy they hold: processes,
  resident memory, idle npx or uvx launchers.
- How often each server was actually called, from each agent's own session logs.
- One action per server: `share` (serve one copy over HTTP on 127.0.0.1), `use-hosted`,
  `keep`, `per-project`, `remove?`, `plugin` (bundled, only its author can change it),
  `check` (unknown: the agent looks it up) or `ok`.
- Flags: servers holding a credential, versions not pinned to an exact release, and one
  server configured under two names (which may be two accounts, so it never assumes one copy
  can serve both).

A sample from a made-up machine with two Claude sessions, one Codex and one Grok:

```text
Agent sessions running: 2 claude, 1 codex, 1 grok.
Local MCP server copies: 18, using 32 processes and 1289 MB.
Idle npx/uvx launchers among them: 14 processes, 410 MB.

| Server     | Action     | Copies | Procs | MB  | Used (sessions)  | Saving MB |
|------------|------------|--------|-------|-----|------------------|-----------|
| slack      | share      | 4      | 8     | 312 | claude:1 codex:1 | 264       |
| context7   | use-hosted | 2      | 4     | 156 | claude:1         | 156       |
| idle       | remove?    | 2      | 4     | 156 | 0                | 156       |
| mystery    | check      | 2      | 4     | 156 | claude:1         | 0         |
```

## Where it looks

| Agent | Config | Usage |
|---|---|---|
| Claude Code | `~/.claude.json`, project `.mcp.json`, enabled plugins, managed-mcp.json | `~/.claude/projects` |
| Codex CLI | `codex mcp list --json`, else `~/.codex/config.toml`, plus each running project's `.codex/config.toml` | `~/.codex/sessions` |
| Grok CLI | `~/.grok/config.toml` and project ones, then Claude's, Cursor's and the project `.mcp.json`, in Grok's order | `~/.grok/sessions` |

`CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `GROK_HOME` are honoured when set. Project config is read
from each running session's folder up to its git root.

## Known servers

`scripts/recipes.json` holds servers whose HTTP support has been checked, each with the
flags to run it, its auth behaviour and a source link. Anything else comes out as `check`,
including a package whose name only resembles a known one, and the skill tells the agent to
read that server's docs rather than guess. To add one,
open a PR with the entry and the link that proves it.

## Limits

- macOS and Linux. Memory is resident size from `ps`, which undercounts on a machine that
  is already swapping.
- Usage counts depend on each agent's log format, which can change between versions. When
  an agent's log folder is missing, usage shows `?` and nothing is marked for removal.
- Project-level config is read for the folders that running sessions were started in.
  Claude Code scope precedence is approximated: every scope's entry is listed rather than
  resolved. Grok's marker for a dismissed Claude import is not read, so a project
  `.mcp.json` is always assumed loaded.
- It stops at the plan. Setting up shared HTTP services is a separate step, and a server
  that holds a credential must be proven to refuse callers without a key before it is shared.

## Tests

```sh
python3 -m unittest discover -s tests
```
