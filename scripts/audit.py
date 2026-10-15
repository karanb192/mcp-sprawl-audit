#!/usr/bin/env python3
"""Read-only audit of MCP server sprawl for Claude Code, Codex CLI and Grok CLI.

Finds every configured MCP server, counts the processes each one runs per agent session,
classifies it and prints a plan. It changes no file and no process. It reads the names of
env vars and headers, never their values.

    python3 audit.py              # Markdown report
    python3 audit.py --json       # the same data for an agent to read
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11: TOML configs are skipped, CLI listings still work
    tomllib = None

AGENTS = ("claude", "codex", "grok")
LAUNCHERS = {"npx", "npm", "uvx", "uv", "pnpm", "bunx", "pipx", "yarn"}
LAUNCHER_SUBCOMMANDS = {"exec", "dlx", "run", "tool", "x", "--"}
SECRET_RE = re.compile(r"TOKEN|KEY|SECRET|PASS|AUTH|COOKIE|CREDENTIAL", re.I)
VAR_RE = re.compile(r"\\\$\\\{[^}]*\\\}|\\\$[A-Za-z_][A-Za-z0-9_]*")
STEM_SKIP = {"dist", "build", "src", "bin", "lib", "out", "index.js", "index.mjs", "main.py", "server.js"}
LOW_USE = 3
SHELLS = {"sh", "bash", "zsh", "fish", "dash"}
BUNDLED_RE = re.compile(r"/\.codex/plugins/|/\.claude/plugins/|\.app/Contents/")
RECIPES = json.loads((Path(__file__).resolve().parent / "recipes.json").read_text())


def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def load_toml(path):
    if tomllib is None:
        return None
    try:
        return tomllib.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def base(s):
    return os.path.basename(s.split()[0]) if s.strip() else ""


def pkg_name(spec):
    """'@scope/name@1.2' -> '@scope/name', 'name==1.2' -> 'name'."""
    spec = re.split(r"==|>=|<=|~=|\[", spec)[0]
    if spec.startswith("@"):
        scope, _, rest = spec[1:].partition("/")
        return "@" + scope + "/" + rest.split("@")[0]
    return spec.split("@")[0]


def pkg_version(spec):
    m = re.search(r"(?:==|@)([^@=\s]+)$", spec[1:] if spec.startswith("@") else spec)
    return m.group(1) if m else None


def make_entry(agent, name, scope, source, s, plugin_root=None):
    s = s or {}
    url = s.get("url") or ""
    kind = s.get("type") or s.get("transport") or ("http" if url else "stdio")
    command = str(s.get("command") or "")
    args = [str(a) for a in s.get("args") or []]
    if plugin_root:
        command = command.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))
        args = [a.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root)) for a in args]
    header_keys = set()
    for k in ("headers", "http_headers", "env_http_headers"):
        header_keys |= set((s.get(k) or {}).keys())
    if s.get("bearer_token_env_var"):
        header_keys.add("Authorization")
    bundled = bool(plugin_root) or bool(BUNDLED_RE.search(" ".join([command] + args)))
    return {
        "agent": agent, "name": name, "scope": scope, "source": source,
        "transport": "stdio" if kind == "stdio" else "http",
        "command": command, "args": args, "url": url,
        "env_keys": sorted((s.get("env") or {}).keys()), "header_keys": sorted(header_keys),
        "plugin": bundled, "log_name": name,
    }


def plugin_servers(root):
    root = Path(root)
    found = {}
    data = load_json(root / ".mcp.json")
    if isinstance(data, dict):
        found.update(data.get("mcpServers", data))
    manifest = load_json(root / ".claude-plugin" / "plugin.json") or {}
    inline = manifest.get("mcpServers")
    refs = inline if isinstance(inline, list) else [inline]
    for ref in refs:
        if isinstance(ref, dict):
            found.update(ref)
        elif isinstance(ref, str):
            data = load_json(root / ref) or {}
            found.update(data.get("mcpServers", data))
    return {k: v for k, v in found.items() if isinstance(v, dict)}


def claude_entries(home):
    out = []
    cj = load_json(home / ".claude.json") or {}
    for name, s in (cj.get("mcpServers") or {}).items():
        out.append(make_entry("claude", name, "user", "~/.claude.json", s))
    for proj, pv in (cj.get("projects") or {}).items():
        for name, s in ((pv or {}).get("mcpServers") or {}).items():
            out.append(make_entry("claude", name, "local:" + proj, "~/.claude.json", s))
        mcp_json = load_json(Path(proj) / ".mcp.json")
        if isinstance(mcp_json, dict):
            for name, s in (mcp_json.get("mcpServers") or {}).items():
                out.append(make_entry("claude", name, "project:" + proj, proj + "/.mcp.json", s))
    for managed in (Path("/Library/Application Support/ClaudeCode/managed-mcp.json"),
                    Path("/etc/claude-code/managed-mcp.json")):
        data = load_json(managed) or {}
        for name, s in (data.get("mcpServers") or {}).items():
            out.append(make_entry("claude", name, "managed", str(managed), s))
    installed = (load_json(home / ".claude/plugins/installed_plugins.json") or {}).get("plugins") or {}
    enabled = (load_json(home / ".claude/settings.json") or {}).get("enabledPlugins") or {}
    for plugin_id, installs in installed.items():
        if enabled.get(plugin_id) is False or not installs:
            continue
        root = Path(installs[-1].get("installPath", ""))
        plugin = plugin_id.split("@")[0]
        for name, s in plugin_servers(root).items():
            e = make_entry("claude", f"{plugin}:{name}", "plugin:" + plugin_id, str(root), s, plugin_root=root)
            e["log_name"] = f"plugin_{plugin}_{name}"
            out.append(e)
    return out


def codex_entries(home, use_cli):
    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    if use_cli:
        try:
            r = subprocess.run(["codex", "mcp", "list", "--json"], capture_output=True, text=True, timeout=30)
            listed = json.loads(r.stdout) if r.returncode == 0 else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            listed = None
        if isinstance(listed, list):
            out = []
            for d in listed:
                if d.get("enabled") is False:
                    continue
                t = dict(d.get("transport") or {})
                t["type"] = "stdio" if t.get("type") == "stdio" else "http"
                out.append(make_entry("codex", d.get("name", "?"), "user", "codex mcp list", t))
            return out
    cfg = load_toml(codex_home / "config.toml") or {}
    return [make_entry("codex", name, "user", "~/.codex/config.toml", s)
            for name, s in (cfg.get("mcp_servers") or {}).items() if s.get("enabled", True)]


def grok_entries(home, claude):
    cfg = load_toml(home / ".grok/config.toml") or {}
    disabled = set(cfg.get("disabled_mcp_servers") or [])
    out, seen = [], set()
    for name, s in (cfg.get("mcp_servers") or {}).items():
        if name in disabled or s.get("enabled") is False:
            continue
        out.append(make_entry("grok", name, "user", "~/.grok/config.toml", s))
        seen.add(name)
    compat = cfg.get("compat") or {}
    if os.environ.get("GROK_CLAUDE_MCPS_ENABLED", "1") not in ("0", "false") and \
            (compat.get("claude") or {}).get("mcps", True):
        for e in claude:
            if e["plugin"] or e["scope"] == "managed" or e["name"] in seen or e["name"] in disabled:
                continue
            out.append(dict(e, agent="grok", source=e["source"] + " (Grok reads Claude's config)"))
            seen.add(e["name"])
    if (compat.get("cursor") or {}).get("mcps", True):
        cursor = load_json(home / ".cursor/mcp.json") or {}
        for name, s in (cursor.get("mcpServers") or {}).items():
            if name not in seen and name not in disabled:
                out.append(make_entry("grok", name, "user", "~/.cursor/mcp.json (Grok reads Cursor's config)", s))
    return out


def identity(e):
    """What makes two entries the same server: the package for npx/uvx-style launches,
    otherwise the command and its first plain argument."""
    if e["transport"] == "http":
        return ("url", e["url"])
    b = base(e["command"])
    plain = [a for a in e["args"] if not a.startswith("-")]
    if b in LAUNCHERS:
        for a in plain:
            if a not in LAUNCHER_SUBCOMMANDS:
                return ("pkg", pkg_name(a))
    if plain:
        return ("path", b + " " + plain[0])
    return ("cmd", b)


def stem(key):
    kind, value = key
    if kind == "pkg":
        return value.split("/")[-1]
    if kind == "url":
        return None
    parts = [p for p in re.split(r"[/\s]", value) if p]
    for p in reversed(parts):
        if p not in STEM_SKIP and not p.startswith("$"):
            return re.sub(r"\.(m?js|py|ts)$", "", p)
    return parts[-1] if parts else None


def spec_of(e):
    plain = [a for a in e["args"] if not a.startswith("-") and a not in LAUNCHER_SUBCOMMANDS]
    return plain[0] if plain and base(e["command"]) in LAUNCHERS else None


def matcher(key):
    kind, value = key
    if kind == "pkg":
        return re.compile(re.escape(value) + r"(?:[@=\s]|$)")
    if kind == "path":
        b, _, arg = value.partition(" ")
        arg = VAR_RE.sub(".*", re.escape(arg))
        return re.compile(r"(?:^|/)" + re.escape(b) + r"\s.*" + arg)
    if kind == "cmd":
        return re.compile(r"(?:^|/)" + re.escape(value) + r"(?:\s|$)")
    return None


def read_ps(text=None):
    if text is None:
        text = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,command="], capture_output=True, text=True).stdout
    procs = {}
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit():
            procs[int(parts[0])] = {"pid": int(parts[0]), "ppid": int(parts[1]),
                                   "rss": int(parts[2]), "cmd": parts[3]}
    return procs


def agent_kind(cmd):
    b = base(cmd)
    if b in AGENTS:
        return b
    if re.search(r"@anthropic-ai/claude-code/cli\.js", cmd):
        return "claude"
    return None


def is_launcher(cmd):
    toks = cmd.split()
    if not toks:
        return False
    if base(toks[0]) in LAUNCHERS or " npm exec " in f" {cmd} ":
        return True
    return base(toks[0]) in ("node", "bun") and len(toks) > 1 and (
        base(toks[1]) in LAUNCHERS or toks[1].endswith(("npx-cli.js", "npm-cli.js")))


def find_instances(procs, entries):
    """One instance = a process started directly by an agent that matches a configured server,
    plus everything below it (the npx/uvx launcher and the real server)."""
    children = defaultdict(list)
    for p in procs.values():
        children[p["ppid"]].append(p["pid"])

    def ancestors(pid):
        seen = set()
        while pid in procs and pid not in seen:
            seen.add(pid)
            pid = procs[pid]["ppid"]
            if pid in procs:
                yield procs[pid]

    sessions = {}
    for p in procs.values():
        kind = agent_kind(p["cmd"])
        if kind and not any(agent_kind(a["cmd"]) for a in ancestors(p["pid"])):
            sessions[p["pid"]] = kind

    keys = {}
    for e in entries:
        if e["transport"] == "stdio" and e["command"]:
            keys.setdefault(identity(e), []).append(e)
    patterns = [(k, matcher(k)) for k in keys]

    claimed, instances = set(), []
    for p in sorted(procs.values(), key=lambda p: p["pid"]):
        parent = procs.get(p["ppid"])
        if not parent or not agent_kind(parent["cmd"]) or p["pid"] in claimed:
            continue
        session = p["ppid"] if p["ppid"] in sessions else next(
            (a["pid"] for a in ancestors(p["pid"]) if a["pid"] in sessions), None)
        if session is None:
            continue
        subtree, stack = [], [p["pid"]]
        while stack:
            pid = stack.pop()
            subtree.append(pid)
            stack.extend(children[pid])
        text = " \n".join(procs[q]["cmd"] for q in subtree)
        key = next((k for k, rx in patterns if rx and rx.search(text)), None)
        if key is None:
            if base(p["cmd"]) in SHELLS or not re.search(r"mcp", p["cmd"], re.I):
                continue
            key = ("unconfigured", base(p["cmd"]) + " " + " ".join(p["cmd"].split()[1:2]))
        claimed.update(subtree)
        launch = [q for q in subtree if is_launcher(procs[q]["cmd"])]
        instances.append({
            "key": key, "agent": sessions[session], "session": session,
            "procs": len(subtree), "rss_kb": sum(procs[q]["rss"] for q in subtree),
            "launchers": len(launch), "launcher_rss_kb": sum(procs[q]["rss"] for q in launch),
        })
    return sessions, instances


def scan_usage(home, days):
    """Which servers each agent actually called, counted as sessions in the last `days` days."""
    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    sources = {
        "claude": (home / ".claude/projects", "*.jsonl", re.compile(rb'"name":"mcp__(.+?)__')),
        "codex": (codex_home / "sessions", "*.jsonl", re.compile(rb'"server":"([^"]+)"')),
        "grok": (home / ".grok/sessions", "updates.jsonl", re.compile(rb'"server_name":"([^"]+)"')),
    }
    cutoff = time.time() - days * 86400
    usage = {}
    for agent, (root, pattern, rx) in sources.items():
        used, total = defaultdict(set), set()
        if root.is_dir():
            for f in root.rglob(pattern):
                try:
                    if f.stat().st_mtime < cutoff:
                        continue
                    data = f.read_bytes()
                except OSError:
                    continue
                rel = f.relative_to(root).parts
                session = rel[1] if agent == "claude" and len(rel) > 2 else str(f)
                session = session.removesuffix(".jsonl")
                total.add(session)
                for name in set(rx.findall(data)):
                    used[name.decode(errors="replace")].add(session)
        usage[agent] = {"sessions": len(total), "used": {k: len(v) for k, v in used.items()}}
    return usage


def recipe_for(key):
    name = key[1] if key[0] == "pkg" else None
    if name in RECIPES:
        return name, RECIPES[name]
    s = stem(key)
    for rname, r in RECIPES.items():
        if s and (rname == s or rname.split("/")[-1] == s or s in r.get("aliases", [])):
            return rname, r
    return None, None


def classify(key, entries, insts, usage, days):
    names = sorted({e["name"] for e in entries})
    agents = sorted({e["agent"] for e in entries} | {i["agent"] for i in insts})
    rss = sum(i["rss_kb"] for i in insts)
    launch_rss = sum(i["launcher_rss_kb"] for i in insts)
    one_copy = (rss - launch_rss) / len(insts) if insts else 0
    used = {a: sum(usage.get(a, {}).get("used", {}).get(e["log_name"], 0)
                   for e in entries if e["agent"] == a) for a in AGENTS} if usage else None
    used_total = sum(used.values()) if used else None
    secrets = sorted({k for e in entries for k in e["env_keys"] + e["header_keys"] if SECRET_RE.search(k)})
    rname, recipe = recipe_for(key)
    plugin = any(e["plugin"] for e in entries)
    flags, saving = [], 0

    if key[0] == "url":
        host = re.sub(r"^\w+://", "", key[1]).split("/")[0].split(":")[0]
        local = host in ("127.0.0.1", "localhost", "::1", "[::1]")
        action, why = "ok", ("one shared local copy already" if local else "hosted remote, no local process")
    elif key[0] == "unconfigured":
        action, why = "check", "an agent started it but no config entry matched; likely a plugin or extension"
    elif plugin:
        action, why = "plugin", "bundled with a plugin or app, which decides how it starts; only its author can share it"
        saving = max(rss - one_copy, 0)
    elif used_total == 0 and insts:
        action, why, saving = "remove?", f"not called in {days} days; confirm before removing", rss
    elif recipe and recipe.get("hosted"):
        action, why = "use-hosted", f"a hosted endpoint exists: {recipe['hosted']}"
        saving = rss
    elif recipe and recipe.get("http") and recipe.get("stateful"):
        action, why = "keep", "can serve HTTP, but one shared copy would share state across sessions"
    elif recipe and recipe.get("http"):
        action, why = "share", "serves Streamable HTTP; run one copy on 127.0.0.1 and point every agent at it"
        saving = max(rss - one_copy, 0)
    elif recipe and recipe.get("http") is False:
        if used_total is not None and used_total <= LOW_USE:
            action, why = "per-project", f"stdio only and called in {used_total} sessions in {days} days"
            saving = rss
        else:
            action, why = "keep", "stdio only and used often"
    else:
        action, why = "check", "unknown server; look up whether it serves Streamable HTTP"

    if action in ("share", "check") and secrets:
        flags.append("holds a credential: a shared copy must refuse callers without a key, proven by a real tool call")
    if any(spec_of(e) and pkg_version(spec_of(e)) in (None, "latest") for e in entries):
        flags.append("version not pinned")
    launchers = sum(i["launchers"] for i in insts)
    if launchers:
        flags.append(f"{launchers} idle npx/uvx launcher processes, {launch_rss / 1024:.0f} MB")
    per_session = defaultdict(int)
    for i in insts:
        per_session[i["session"]] += 1
    if per_session and max(per_session.values()) > 1:
        flags.append(f"up to {max(per_session.values())} copies in one session: configured twice, or started again by subagents")

    return {
        "server": ", ".join(names) or key[1], "identity": f"{key[0]}:{key[1]}", "agents": agents,
        "transport": "http" if key[0] == "url" else "stdio",
        "copies": len(insts), "processes": sum(i["procs"] for i in insts), "rss_mb": round(rss / 1024),
        "used_sessions": used, "action": action, "why": why, "recipe": rname,
        "how": (recipe or {}).get("how"), "auth": (recipe or {}).get("auth"), "source": (recipe or {}).get("source"),
        "credential_names": secrets, "flags": flags, "saving_mb": round(saving / 1024),
        "scopes": sorted({e["agent"] + " " + e["scope"].split(":")[0] for e in entries}),
    }


def duplicates(entries):
    by_agent = defaultdict(lambda: defaultdict(set))
    for e in entries:
        s = stem(identity(e))
        if s:
            by_agent[e["agent"]][s].add(e["name"])
    found = defaultdict(list)
    for agent, stems in sorted(by_agent.items()):
        for s, names in stems.items():
            if len(names) > 1:
                found[(s, tuple(sorted(names)))].append(agent)
    return [{"agents": agents, "stem": s, "names": list(names)} for (s, names), agents in found.items()]


def audit(home, ps_text=None, use_cli=True, days=30, usage=True):
    claude = claude_entries(home)
    entries = claude + codex_entries(home, use_cli) + grok_entries(home, claude)
    procs = read_ps(ps_text)
    sessions, instances = find_instances(procs, entries)
    use = scan_usage(home, days) if usage else None
    dupes = duplicates(entries)
    groups = defaultdict(list)
    for e in entries:
        groups[identity(e)].append(e)
    by_key = defaultdict(list)
    for i in instances:
        by_key[i["key"]].append(i)
    rows = [classify(k, groups.get(k, []), by_key.get(k, []), use, days) for k in set(groups) | set(by_key)]
    for d in dupes:
        for r in rows:
            mine = set(r["server"].split(", ")) & set(d["names"])
            if mine:
                others = ", ".join(n for n in d["names"] if n not in mine)
                r["flags"].append(f"possible duplicate of {others}: keep one")
    rows.sort(key=lambda r: (-r["saving_mb"], -r["rss_mb"], r["server"]))
    counts = defaultdict(int)
    for kind in sessions.values():
        counts[kind] += 1
    return {
        "sessions": dict(counts),
        "totals": {
            "copies": len(instances), "processes": sum(i["procs"] for i in instances),
            "rss_mb": round(sum(i["rss_kb"] for i in instances) / 1024),
            "launchers": sum(i["launchers"] for i in instances),
            "launcher_rss_mb": round(sum(i["launcher_rss_kb"] for i in instances) / 1024),
            "saving_mb": sum(r["saving_mb"] for r in rows if r["action"] != "plugin"),
            "plugin_locked_mb": sum(r["saving_mb"] for r in rows if r["action"] == "plugin"),
        },
        "usage_window_days": days if usage else None,
        "usage_sessions": {a: v["sessions"] for a, v in use.items()} if use else None,
        "duplicates": dupes, "servers": rows,
    }


def markdown(report, home):
    t = report["totals"]
    sess = ", ".join(f"{n} {a}" for a, n in sorted(report["sessions"].items())) or "none"
    out = [
        "# MCP sprawl audit", "",
        f"Agent sessions running: {sess}.",
        f"Local MCP server copies: {t['copies']}, using {t['processes']} processes and {t['rss_mb']} MB.",
        f"Idle npx/uvx launchers among them: {t['launchers']} processes, {t['launcher_rss_mb']} MB.",
        f"Saving you can act on: about {t['saving_mb']} MB. "
        f"Locked inside plugins and apps: about {t['plugin_locked_mb']} MB.", "",
        "Memory is resident size from ps. It undercounts on a machine that is swapping, because "
        "compressed and swapped pages are not resident.", "",
        "## Plan", "",
        "| Server | Action | Copies | Procs | MB | Used (sessions) | Saving MB | Why |",
        "|---|---|---|---|---|---|---|---|",
    ]
    shared = [r["server"] for r in report["servers"] if r["action"] == "ok"]
    for r in report["servers"]:
        if r["action"] == "ok":
            continue
        used = r["used_sessions"]
        used_s = "n/a" if used is None else " ".join(f"{a}:{n}" for a, n in used.items() if n) or "0"
        out.append(f"| {r['server']} | {r['action']} | {r['copies']} | {r['processes']} | {r['rss_mb']} "
                   f"| {used_s} | {r['saving_mb']} | {r['why']} |")
    if shared:
        names = ", ".join(shared)
        out += ["", f"Already over HTTP, no process per session ({names.count(',') + 1}): {names}."]
    notes = [r for r in report["servers"] if r["action"] != "ok" and (r["flags"] or r["how"] or r["auth"])]
    if notes:
        out += ["", "## Details", ""]
        for r in notes:
            out.append(f"- **{r['server']}** ({r['identity']})")
            for f in r["flags"]:
                out.append(f"  - {f}")
            if r["how"]:
                out.append(f"  - how: {r['how']}")
            if r["auth"]:
                out.append(f"  - auth: {r['auth']}")
            if r["source"]:
                out.append(f"  - source: {r['source']}")
    if report["duplicates"]:
        out += ["", "## Possible duplicates", ""]
        for d in report["duplicates"]:
            out.append(f"- {', '.join(d['names'])} look like the same server ({d['stem']}), "
                       f"configured for {', '.join(d['agents'])}")
    if report["usage_sessions"]:
        window = ", ".join(f"{n} {a}" for a, n in report["usage_sessions"].items())
        out += ["", f"Usage counts come from session logs of the last {report['usage_window_days']} days "
                    f"({window} sessions)."]
    text = "\n".join(out) + "\n"
    return text.replace(str(home), "~")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only audit of MCP server sprawl.")
    ap.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    ap.add_argument("--days", type=int, default=30, help="usage window in days (default 30)")
    ap.add_argument("--no-usage", action="store_true", help="skip reading session logs")
    ap.add_argument("--no-cli", action="store_true", help="read config files only, never run agent CLIs")
    ap.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    ap.add_argument("--ps-file", type=Path, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if tomllib is None:
        print("warning: Python 3.11+ is needed to read Codex and Grok config.toml; "
              "servers configured only there are missing from this report", file=sys.stderr)
    ps_text = a.ps_file.read_text() if a.ps_file else None
    report = audit(a.home, ps_text, use_cli=not a.no_cli, days=a.days, usage=not a.no_usage)
    if a.json:
        print(json.dumps(report, indent=2).replace(str(a.home), "~"))
    else:
        sys.stdout.write(markdown(report, a.home))


if __name__ == "__main__":
    main()
