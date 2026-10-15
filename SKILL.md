---
name: mcp-sprawl-audit
description: Audit MCP server sprawl on this machine for Claude Code, Codex CLI and Grok CLI. Counts how many copies of each MCP server the running agent sessions hold, what they cost in memory, which ones are duplicates, unused, unpinned or bundled in plugins, and which can run as one shared HTTP copy instead. Use when the machine is slow with many agent sessions open, when someone asks how much memory MCP servers use, or before adding more MCP servers. Read-only; it writes a plan and changes nothing.
---

# MCP sprawl audit

Each agent session starts its own copy of every local (stdio) MCP server in its config.
Fifty sessions means fifty copies, plus an idle npx or uvx launcher above each one. This
skill measures that and proposes a plan. It never edits a config, starts or stops a
process, or prints a secret value.

## 1. Run the audit

```sh
python3 <this skill's folder>/scripts/audit.py          # Markdown report
python3 <this skill's folder>/scripts/audit.py --json   # the same data, structured
```

Python 3.11 or newer reads every config. On older Python, Codex and Grok servers that
exist only in their `config.toml` are missed, and the script says so. Reading 30 days of
session logs takes a few seconds; `--no-usage` skips it, `--days N` changes the window.

What it reads, all locally:

| Agent | Config | Usage (tool calls per session) |
|---|---|---|
| Claude Code | `~/.claude.json` (user and local scope), each project's `.mcp.json`, enabled plugins, managed-mcp.json | `~/.claude/projects/**/*.jsonl` |
| Codex CLI | `codex mcp list --json` (includes plugin servers), else `~/.codex/config.toml` | `~/.codex/sessions/**/*.jsonl` |
| Grok CLI | `~/.grok/config.toml`, plus Claude's and Cursor's config, which Grok loads by default | `~/.grok/sessions/**/updates.jsonl` |

Processes come from `ps`. A copy is a process an agent started that matches a configured
server, together with everything below it.

## 2. Finish the classification

Each server gets one action:

| Action | Meaning |
|---|---|
| `share` | It can serve Streamable HTTP. Run one copy on 127.0.0.1 and point every agent at the URL. |
| `use-hosted` | The vendor runs a hosted endpoint. Use the URL and drop the local copy. |
| `keep` | Stdio only and used often, or HTTP-capable but holding per-session state (a browser). |
| `per-project` | Stdio only and rarely called. Configure it only in the projects that need it. |
| `remove` / `remove?` | Not called in the usage window. `remove?` means confirm with the user first. |
| `plugin` | Bundled with a plugin or app. Only its author can change how it starts. |
| `check` | Unknown server. Your job, below. |
| `ok` | Already HTTP. Nothing to do. |

For every `check` row, find out whether the server can serve Streamable HTTP. Read its
README or docs (npm: `npm view <package> readme`; PyPI or GitHub for Python servers) and
look for a transport, port or HTTP option. Do not run an unknown server to probe it.
Record what you found with the link. If the docs do not say, the answer is "unknown",
not "yes".

## 3. Write the plan

Show the user one table ordered by saving: server, action, copies, memory, sessions that
used it, estimated saving, and the reason with its source link. Then the flags:

- **holds a credential**: a shared copy listens on a port any local process can reach. It
  must refuse callers that lack a key, and that has to be proven with a real tool call
  without the key, not assumed. Some servers check the key only on tool calls, others
  ignore their stored token over HTTP. The recipe notes say which.
- **version not pinned**: a long-running shared copy should never pull `@latest`.
- **idle launchers**: npx or uvx processes that only wait for their child.
- **duplicates**: one server configured under two names, running twice per session.

Memory figures are resident size from `ps`, which undercounts on a machine that is
already swapping. Say so when you quote them.

Stop at the plan. Changing configs, installing services or removing servers is the
user's decision and a separate step.
