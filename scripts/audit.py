#!/usr/bin/env python3
"""Read-only audit of MCP server sprawl for Claude Code, Codex CLI and Grok CLI.

Finds every configured MCP server, counts the processes each one runs per agent session,
classifies it and prints a plan. It changes no file and no process. It reads the names of
env vars and headers, never their values, and never prints command-line arguments.

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
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11: TOML configs are skipped, CLI listings still work
    tomllib = None

AGENTS = ("claude", "codex", "grok")
LAUNCHERS = {"npx", "npm", "uvx", "uv", "pnpm", "bunx", "pipx", "yarn"}
NODE_LAUNCHERS = {"npx", "npm", "pnpm", "bunx", "yarn"}
LAUNCHER_SUBCOMMANDS = {"exec", "dlx", "run", "tool", "x", "--"}
OPTS_WITH_VALUE = {"-p", "--package", "--python", "--from", "--with", "--with-requirements", "--with-editable",
                   "--index", "--index-url", "--default-index", "--extra-index-url", "--registry", "--spec",
                   "--cache-dir", "--cache", "--directory", "--project", "--env-file", "--constraints",
                   "--overrides", "--userconfig", "--workspace", "-w", "--prefix", "--python-preference"}
SECRET_RE = re.compile(r"TOKEN|KEY|SECRET|PASS|AUTH|COOKIE|CREDENTIAL", re.I)
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
VAR_RE = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*")
PINNED_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
SAFE_NAME_RE = re.compile(r"^[\w.@+-]+$")
STEM_SKIP = {"dist", "build", "src", "bin", "lib", "out", "index", "main", "server", "cli"}
LOW_USE = 3
SHELLS = {"sh", "bash", "zsh", "fish", "dash"}
BUNDLED_RE = re.compile(r"/\.codex/plugins/|/\.claude/plugins/|\.app/Contents/")
TS_RE = re.compile(rb'"timestamp"\s*:\s*(?:"([^"]+)"|(\d+(?:\.\d+)?))')
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
    spec = re.split(r"==|>=|<=|~=|!=|\[", spec)[0]
    if spec.startswith("@"):
        scope, _, rest = spec[1:].partition("/")
        return "@" + scope + "/" + rest.split("@")[0]
    return spec.split("@")[0]


def pkg_version(spec):
    m = re.search(r"(?:==|@)([^@=\s]+)$", spec[1:] if spec.startswith("@") else spec)
    return m.group(1) if m else None


def pinned(spec):
    v = pkg_version(spec)
    return bool(v and PINNED_RE.fullmatch(v))


def safe_url(url):
    """Scheme, host and path only: no user info, query or fragment, which can carry keys."""
    m = re.match(r"^(\w+://)(?:[^@/]*@)?([^/?#]*)([^?#]*)", url or "")
    return "".join(m.groups()) if m else ""


def unwrap_env(command, args):
    """`env A=1 node x.js` runs `node x.js`; the assignments never become part of an identity."""
    if base(command) != "env":
        return command, args
    rest = [a for a in args if not ASSIGN_RE.match(a) and not a.startswith("-")]
    return (rest[0], rest[1:]) if rest else (command, [])


def launch_spec(command, args):
    """The package an npx/uvx-style launcher runs, honouring options that take a value."""
    b = base(command)
    if b not in LAUNCHERS:
        return None
    package_opts = {"--package", "--from", "--spec"} | ({"-p"} if b in NODE_LAUNCHERS else set())
    explicit, i = None, 0
    while i < len(args):
        a = args[i]
        if a in LAUNCHER_SUBCOMMANDS:
            i += 1
            continue
        if a.startswith("-"):
            opt, eq, val = a.partition("=")
            if opt in package_opts:
                explicit = val if eq else (args[i + 1] if i + 1 < len(args) else None)
                i += 1 if eq else 2
            elif not eq and opt in OPTS_WITH_VALUE:
                i += 2
            else:
                i += 1
            continue
        return explicit or a
    return explicit


def make_entry(agent, name, scope, source, s, plugin_root=None):
    s = s or {}
    url = s.get("url") or ""
    kind = s.get("type") or s.get("transport") or ("http" if url else "stdio")
    command = str(s.get("command") or "")
    args = [str(a) for a in s.get("args") or []]
    if plugin_root:
        command = command.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))
        args = [a.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root)) for a in args]
    command, args = unwrap_env(command, args)
    header_keys = set()
    for k in ("headers", "http_headers", "env_http_headers"):
        header_keys |= set((s.get(k) or {}).keys())
    if s.get("bearer_token_env_var"):
        header_keys.add("Authorization")
    env_keys = set((s.get("env") or {}).keys())
    for v in s.get("env_vars") or []:
        env_keys.add(v.get("name", "") if isinstance(v, dict) else str(v))
    bundled = bool(plugin_root) or bool(BUNDLED_RE.search(" ".join([command] + args)))
    return {
        "agent": agent, "name": name, "scope": scope, "source": source,
        "transport": "stdio" if kind == "stdio" else "http",
        "command": command, "args": args, "url": url,
        "env_keys": sorted(k for k in env_keys if k), "header_keys": sorted(header_keys),
        "plugin": bundled, "log_names": {name},
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


def project_dirs(cwd, home):
    """The working directory and its parents, up to (not past) the home directory."""
    p = Path(cwd)
    out = []
    while True:
        out.append(p)
        if p == home or p.parent == p:
            return out
        p = p.parent


def claude_entries(home, cwds):
    cj = load_json(home / ".claude.json") or {}
    managed = [m for m in (Path("/Library/Application Support/ClaudeCode/managed-mcp.json"),
                           Path("/etc/claude-code/managed-mcp.json")) if m.exists()]
    out = []
    if managed:  # managed-mcp.json takes exclusive control of Claude Code's MCP servers
        for m in managed:
            for name, s in ((load_json(m) or {}).get("mcpServers") or {}).items():
                out.append(make_entry("claude", name, "managed", str(m), s))
    else:
        for name, s in (cj.get("mcpServers") or {}).items():
            out.append(make_entry("claude", name, "user", "~/.claude.json", s))
        projects = set(cj.get("projects") or {})
        for cwd in cwds:
            projects.update(str(d) for d in project_dirs(cwd, home))
        for proj in sorted(projects):
            pv = (cj.get("projects") or {}).get(proj) or {}
            for name, s in (pv.get("mcpServers") or {}).items():
                out.append(make_entry("claude", name, "local:" + proj, "~/.claude.json", s))
            mcp_json = load_json(Path(proj) / ".mcp.json")
            if isinstance(mcp_json, dict):
                for name, s in (mcp_json.get("mcpServers") or {}).items():
                    out.append(make_entry("claude", name, "project:" + proj, proj + "/.mcp.json", s))
    installed = (load_json(home / ".claude/plugins/installed_plugins.json") or {}).get("plugins") or {}
    enabled = (load_json(home / ".claude/settings.json") or {}).get("enabledPlugins") or {}
    for plugin_id, installs in installed.items():
        if enabled.get(plugin_id) is False or not installs:
            continue
        root = Path(installs[-1].get("installPath", ""))
        plugin = plugin_id.split("@")[0]
        for name, s in plugin_servers(root).items():
            e = make_entry("claude", f"{plugin}:{name}", "plugin:" + plugin_id, str(root), s, plugin_root=root)
            e["log_names"] = {f"plugin_{plugin}_{name}"}
            out.append(e)
    return out


def toml_servers(path):
    cfg = load_toml(path) or {}
    return cfg, cfg.get("mcp_servers") or {}


def codex_entries(home, use_cli, cwds):
    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    out = None
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
    if out is None:
        _, servers = toml_servers(codex_home / "config.toml")
        out = [make_entry("codex", n, "user", "~/.codex/config.toml", s)
               for n, s in servers.items() if s.get("enabled", True)]
    seen = {(e["name"], e["source"]) for e in out}
    for d in {d for cwd in cwds for d in project_dirs(cwd, home)} - {home}:
        path = d / ".codex/config.toml"
        for name, s in toml_servers(path)[1].items():
            if s.get("enabled", True) and (name, str(path)) not in seen:
                out.append(make_entry("codex", name, "project:" + str(d), str(path), s))
                seen.add((name, str(path)))
    return out


def env_off(name):
    return os.environ.get(name, "1").strip().lower() in ("0", "false", "no", "off")


def grok_entries(home, claude, cwds):
    """Grok's effective servers for each running directory: project config, then user config,
    then the Claude and Cursor config it imports. Contexts never hide each other's servers."""
    cfg, servers = toml_servers(home / ".grok/config.toml")
    compat = cfg.get("compat") or {}
    use_claude = not env_off("GROK_CLAUDE_MCPS_ENABLED") and (compat.get("claude") or {}).get("mcps", True)
    use_cursor = not env_off("GROK_CURSOR_MCPS_ENABLED") and (compat.get("cursor") or {}).get("mcps", True)
    blocked = set(cfg.get("disabled_mcp_servers") or [])
    out, emitted = [], set()

    def emit(e):
        k = (e["name"], e["scope"], e["source"])
        if k not in emitted:
            emitted.add(k)
            out.append(e)

    for cwd in list(cwds) or [None]:
        dirs = [d for d in project_dirs(cwd, home) if d != home] if cwd else []
        roots = {str(d) for d in dirs}
        seen = set()
        layers = [("project:" + str(d), str(d / ".grok/config.toml"), toml_servers(d / ".grok/config.toml")[1])
                  for d in dirs] + [("user", "~/.grok/config.toml", servers)]
        for scope, source, layer in layers:
            for name, s in layer.items():
                if name in seen or name in blocked:
                    continue
                seen.add(name)  # a disabled entry still hides the same name in lower layers
                if s.get("enabled", True):
                    emit(make_entry("grok", name, scope, source, s))
        if use_claude:
            for e in claude:
                if e["plugin"] or e["scope"] == "managed" or e["name"] in seen or e["name"] in blocked:
                    continue
                if e["scope"] != "user" and e["scope"].split(":", 1)[1] not in roots:
                    continue
                seen.add(e["name"])
                emit(dict(e, agent="grok", source=e["source"] + " (Grok reads Claude's config)"))
        if use_cursor:
            for d in dirs + [home]:
                for name, s in ((load_json(d / ".cursor/mcp.json") or {}).get("mcpServers") or {}).items():
                    if name not in seen and name not in blocked:
                        seen.add(name)
                        emit(make_entry("grok", name, "user" if d == home else "project:" + str(d),
                                        str(d / ".cursor/mcp.json") + " (Grok reads Cursor's config)", s))
    return out


def identity(e):
    """What makes two entries the same running server. Internal only: it holds arguments,
    which can carry secrets, so it is never printed. `display` is the printable form."""
    if e["transport"] == "http":
        return ("url", safe_url(e["url"]))
    spec = launch_spec(e["command"], e["args"])
    if spec:
        after = e["args"][e["args"].index(spec) + 1:] if spec in e["args"] else e["args"]
        return ("pkg", pkg_name(spec), spec, tuple(after))
    plain = [a for a in e["args"] if not a.startswith("-")]
    if plain:
        return ("path", base(e["command"]), plain[0], tuple(e["args"]))
    return ("cmd", base(e["command"]), "", tuple(e["args"]))


def path_like(arg):
    """Only a file path is ever printed from a command line; any other argument may be a key."""
    return bool(("/" in arg or re.search(r"\.(m?js|cjs|py|ts)$", arg)) and re.fullmatch(r"[\w./~@+-]{1,200}", arg))


def display(key):
    kind = key[0]
    if kind == "url":
        return key[1]
    if kind == "pkg":
        return key[1]
    if kind == "path":
        script = key[2] if path_like(key[2]) else "<arg>"
        return f"{key[1]} {script}"
    return key[1]


def stem(key):
    if key[0] == "pkg":
        return key[1].split("/")[-1]
    if key[0] == "path" and path_like(key[2]):
        parts = [re.sub(r"\.(m?js|cjs|py|ts)$", "", p) for p in key[2].split("/") if p]
        for p in reversed(parts):
            if p not in STEM_SKIP and not p.startswith("$") and SAFE_NAME_RE.match(p):
                return p
    return None


def matcher(key):
    """A regex over the command line of the process an agent started. Every configured token
    must appear in order, and the package or program name must stand alone."""
    def tok(t):
        return r"\S*".join(re.escape(part) for part in VAR_RE.split(t))
    kind = key[0]
    if kind == "pkg":
        rest = "".join(r"\s+" + tok(a) for a in key[3])
        return re.compile(r"(?:^|\s)" + tok(key[2]) + r"(?=\s|$)" + (r".*?" + rest if rest else ""))
    if kind in ("path", "cmd"):
        args = "".join(r"\s+" + tok(a) for a in key[3])
        return re.compile(r"^(?:.*/)?" + re.escape(key[1]) + args + r"\s*$")
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


def ancestors(procs, pid):
    seen = set()
    while pid in procs and pid not in seen:
        seen.add(pid)
        pid = procs[pid]["ppid"]
        if pid in procs:
            yield procs[pid]


def find_sessions(procs):
    """Top-level agent processes. An agent started by another agent is not a session."""
    return {p["pid"]: agent_kind(p["cmd"]) for p in procs.values()
            if agent_kind(p["cmd"]) and not any(agent_kind(a["cmd"]) for a in ancestors(procs, p["pid"]))}


def session_cwds(pids):
    out = {}
    if not pids:
        return out
    if Path("/proc").is_dir():
        for pid in pids:
            try:
                out[pid] = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                pass
        return out
    try:
        r = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fn", "-p", ",".join(map(str, pids))],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return out
    pid = None
    for line in r.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid:
            out[pid] = line[1:]
    return out


def find_instances(procs, sessions, entries):
    """One instance = a process an agent started that matches a configured server, plus its
    descendants (the npx/uvx launcher and the real server), stopping at any nested agent."""
    children = defaultdict(list)
    for p in procs.values():
        children[p["ppid"]].append(p["pid"])
    keys = {identity(e) for e in entries if e["transport"] == "stdio" and e["command"]}
    patterns = [(k, matcher(k)) for k in keys]
    by_pkg = defaultdict(list)
    for k in keys:
        if k[0] == "pkg":
            by_pkg[k[1]].append(k)

    instances = []
    for p in procs.values():
        parent = procs.get(p["ppid"])
        if not parent or not agent_kind(parent["cmd"]):
            continue
        shell = base(p["cmd"]) in SHELLS
        session = p["ppid"] if p["ppid"] in sessions else next(
            (a["pid"] for a in ancestors(procs, p["pid"]) if a["pid"] in sessions), None)
        if session is None:
            continue
        cmd = p["cmd"]
        key = next((k for k, rx in patterns if rx and rx.search(cmd)), None)
        if key is None and not shell:  # same package, other args than configured: only if unambiguous
            hits = [ks for name, ks in by_pkg.items()
                    if re.search(r"(?:^|\s)" + re.escape(name) + r"(?=[@=\s]|$)", cmd)]
            key = hits[0][0] if len(hits) == 1 and len(hits[0]) == 1 else None
        if key is None:
            if shell or agent_kind(cmd):
                continue
            words = [os.path.basename(w) for w in cmd.split()[:2]]
            label = next((w for w in words if "mcp" in w.lower() and SAFE_NAME_RE.match(w)), None)
            if not label:
                continue
            key = ("unconfigured", label)
        subtree, stack = [], [p["pid"]]
        while stack:
            pid = stack.pop()
            subtree.append(pid)
            stack.extend(c for c in children[pid] if not agent_kind(procs[c]["cmd"]))
        launch = [q for q in subtree if is_launcher(procs[q]["cmd"])]
        instances.append({
            "key": key, "agent": agent_kind(parent["cmd"]), "session": session,
            "procs": len(subtree), "rss_kb": sum(procs[q]["rss"] for q in subtree),
            "launchers": len(launch), "launcher_rss_kb": sum(procs[q]["rss"] for q in launch),
        })
    return instances


def event_time(data, pos, fallback):
    start = data.rfind(b"\n", 0, pos) + 1
    end = data.find(b"\n", pos)
    m = TS_RE.search(data, start, end if end != -1 else len(data))
    if not m:
        return fallback
    if m.group(2):
        t = float(m.group(2))
        return t / 1000 if t > 1e12 else t
    try:
        return datetime.fromisoformat(m.group(1).decode().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return fallback


def scan_usage(home, days):
    """Which servers each agent called in the last `days` days, as sets of session ids.
    An agent whose log folder is missing has unknown usage, never zero."""
    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    mcp_name = rb'"name"\s*:\s*"mcp__(.+?)__'
    sources = {
        "claude": (home / ".claude/projects", "*.jsonl", [mcp_name]),
        "codex": (codex_home / "sessions", "*.jsonl", [rb'"server"\s*:\s*"([^"]+)"', mcp_name]),
        "grok": (home / ".grok/sessions", "updates.jsonl", [rb'"server_name"\s*:\s*"([^"]+)"']),
    }
    cutoff = time.time() - days * 86400
    usage = {}
    for agent, (root, pattern, rxs) in sources.items():
        if not root.is_dir():
            usage[agent] = None
            continue
        used, total = defaultdict(set), set()
        for f in root.rglob(pattern):
            try:
                mtime = f.stat().st_mtime
                if mtime < cutoff:
                    continue
                data = f.read_bytes()
            except OSError:
                continue
            rel = f.relative_to(root).parts
            session = (rel[1] if agent == "claude" and len(rel) > 1 else str(f)).removesuffix(".jsonl")
            total.add(session)
            for rx in rxs:
                for m in re.finditer(rx, data):
                    if event_time(data, m.start(), mtime) >= cutoff:
                        used[m.group(1).decode(errors="replace")].add(session)
        usage[agent] = {"sessions": len(total), "used": used}
    return usage


def recipe_for(key):
    """Recipes match an exact package name only. A name that merely looks similar is a hint."""
    if key[0] == "pkg":
        for rname, r in RECIPES.items():
            if key[1] == rname or key[1] in r.get("packages", []):
                return rname, r, False
    s = stem(key)
    for rname in RECIPES:
        if s and rname.split("/")[-1] == s:
            return rname, None, True
    return None, None, False


def classify(key, entries, insts, usage, days):
    names = sorted({e["name"] for e in entries})
    agents = sorted({e["agent"] for e in entries} | {i["agent"] for i in insts})
    rss = sum(i["rss_kb"] for i in insts)
    launch_rss = sum(i["launcher_rss_kb"] for i in insts)
    one_copy = (rss - launch_rss) / len(insts) if insts else 0
    used, unknown = None, usage is None
    if usage is not None:
        used = {}
        for a in agents:
            if usage.get(a) is None:
                used[a], unknown = None, True
                continue
            logs = set().union(*[e["log_names"] for e in entries if e["agent"] == a] or [set()])
            used[a] = len(set().union(*[usage[a]["used"].get(n, set()) for n in logs] or [set()]))
    used_total = None if unknown or used is None else sum(v for v in used.values() if v)
    secrets = sorted({k for e in entries for k in e["env_keys"] + e["header_keys"] if SECRET_RE.search(k)})
    configs = len({(e["name"], e["source"]) for e in entries if "(Grok reads" not in e["source"]}) or 1
    rname, recipe, lookalike = recipe_for(key)
    plugin = any(e["plugin"] for e in entries)
    flags, saving = [], 0

    if key[0] == "url":
        host = re.sub(r"^\w+://", "", key[1]).split("/")[0].rsplit(":", 1)[0]
        local = host in ("127.0.0.1", "localhost", "[::1]")
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
        saving = max(rss - one_copy * configs, 0)
    elif recipe and recipe.get("http") is False:
        if used_total is not None and used_total <= LOW_USE:
            action, why = "per-project", f"stdio only and called in {used_total} sessions in {days} days"
            saving = rss
        else:
            action, why = "keep", "stdio only" + ("" if used_total is None else " and used often")
    else:
        action, why = "check", "unknown server; look up whether it serves Streamable HTTP"
        if lookalike:
            why += f"; its name resembles {rname}, which is not proof it is the same server"

    if action in ("share", "check") and secrets:
        flags.append("holds a credential: a shared copy must refuse callers without a key, proven by a real tool call")
    if action == "share" and secrets and configs > 1:
        flags.append(f"{configs} separate configurations hold credentials: the saving keeps one copy each; "
                     "if they are the same account, one copy can serve all of them")
    if key[0] == "pkg" and not pinned(key[2]):
        flags.append("version not pinned to an exact release")
    launchers = sum(i["launchers"] for i in insts)
    if launchers:
        flags.append(f"{launchers} idle npx/uvx launcher processes, {launch_rss / 1024:.0f} MB")
    per_session = defaultdict(int)
    for i in insts:
        per_session[i["session"]] += 1
    if per_session and max(per_session.values()) > 1:
        flags.append(f"up to {max(per_session.values())} copies in one session: "
                     "configured twice, or started again by subagents")

    return {
        "server": ", ".join(names) or display(key), "identity": f"{key[0]}:{display(key)}", "agents": agents,
        "transport": "http" if key[0] == "url" else "stdio",
        "copies": len(insts), "processes": sum(i["procs"] for i in insts), "rss_mb": round(rss / 1024),
        "used_sessions": used, "action": action, "why": why, "recipe": None if lookalike else rname,
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


def audit(home, ps_text=None, use_cli=True, days=30, usage=True, cwds=None):
    procs = read_ps(ps_text)
    sessions = find_sessions(procs)
    if cwds is None:
        agent_pids = [p["pid"] for p in procs.values() if agent_kind(p["cmd"])]
        cwds = [] if ps_text is not None else sorted(set(session_cwds(agent_pids).values()))
    claude = claude_entries(home, cwds)
    entries = claude + codex_entries(home, use_cli, cwds) + grok_entries(home, claude, cwds)
    instances = find_instances(procs, sessions, entries)
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
                r["flags"].append(f"possible duplicate of {others}: if the account and settings match, keep one")
    rows.sort(key=lambda r: (-r["saving_mb"], -r["rss_mb"], r["server"]))
    counts = defaultdict(int)
    for kind in sessions.values():
        counts[kind] += 1
    return {
        "sessions": dict(counts),
        "project_dirs_seen": len(cwds),
        "totals": {
            "copies": len(instances), "processes": sum(i["procs"] for i in instances),
            "rss_mb": round(sum(i["rss_kb"] for i in instances) / 1024),
            "launchers": sum(i["launchers"] for i in instances),
            "launcher_rss_mb": round(sum(i["launcher_rss_kb"] for i in instances) / 1024),
            "saving_mb": sum(r["saving_mb"] for r in rows if r["action"] != "plugin"),
            "plugin_locked_mb": sum(r["saving_mb"] for r in rows if r["action"] == "plugin"),
        },
        "usage_window_days": days if usage else None,
        "usage_sessions": {a: (v["sessions"] if v else None) for a, v in use.items()} if use else None,
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
        if used is None:
            used_s = "n/a"
        else:
            used_s = " ".join(f"{a}:{'?' if n is None else n}" for a, n in used.items() if n != 0) or "0"
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
        window = ", ".join(f"{'no logs' if n is None else n} {a}" for a, n in report["usage_sessions"].items())
        out += ["", f"Usage counts come from session logs of the last {report['usage_window_days']} days "
                    f"({window} sessions). `?` means that agent's logs were not found, so usage is unknown."]
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
