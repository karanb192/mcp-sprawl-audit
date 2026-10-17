import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit  # noqa: E402

SECRET = "xoxp-not-a-real-token-123"


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class Machine:
    """A made-up home directory and process table: two Claude sessions, one Codex, one Grok."""

    def __init__(self, root):
        h = self.home = Path(root)
        plugin = h / ".claude/plugins/cache/market/finder/1.0.0"
        write(plugin / ".mcp.json", json.dumps({"mcpServers": {"mods": {
            "command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/server.mjs"]}}}))
        write(h / ".claude/plugins/installed_plugins.json", json.dumps({"plugins": {
            "finder@market": [{"installPath": str(plugin)}]}}))
        write(h / ".claude/settings.json", json.dumps({"enabledPlugins": {"finder@market": True}}))
        write(h / ".claude.json", json.dumps({"mcpServers": {
            "slack": {"command": "npx", "args": ["-y", "slack-mcp-server@1.3.0"],
                      "env": {"SLACK_MCP_XOXP_TOKEN": SECRET}},
            "atlassian": {"command": "uvx", "args": ["mcp-atlassian@0.23.1"],
                          "env": {"JIRA_API_TOKEN": SECRET}},
            "reddit": {"command": "npx", "args": ["-y", "reddit-mcp-buddy@1.1.14"]},
            "reddit-dev": {"command": "node", "args": [str(h / "src/reddit-mcp-buddy/dist/index.js")]},
            "context7": {"command": "npx", "args": ["-y", "@upstash/context7-mcp@latest"]},
            "mystery": {"command": "npx", "args": ["-y", "mystery-mcp@2.0.0"]},
            "idle": {"command": "npx", "args": ["-y", "idle-mcp@1.0.0"]},
            "remote": {"type": "http", "url": "https://mcp.example.com/mcp"},
        }}))
        write(h / ".codex/config.toml", '[mcp_servers.slack]\ncommand = "npx"\n'
              'args = ["-y", "slack-mcp-server@1.3.0"]\nenv = { SLACK_MCP_XOXP_TOKEN = "' + SECRET + '" }\n')
        write(h / ".grok/config.toml", "")
        write(h / ".claude/projects/-work/s1.jsonl", '{"name":"mcp__slack__channels_list"}\n{"name":"mcp__mystery__go"}\n'
              '{"name":"mcp__context7__docs"}\n{"name":"mcp__reddit__search"}\n')
        write(h / ".claude/projects/-work/s2.jsonl", '{"name":"mcp__plugin_finder_mods__search"}\n')
        write(h / ".codex/sessions/2026/10/15/r1.jsonl", '{"server":"slack"}\n')
        write(h / ".grok/sessions/x/one/updates.jsonl", '{"server_name":"atlassian"}\n')

        rows = [(1, 0, 10, "/sbin/launchd")]
        pid = 100

        def proc(ppid, rss, cmd):
            nonlocal pid
            pid += 1
            rows.append((pid, ppid, rss, cmd))
            return pid

        def stdio(session, launcher, server):
            lp = proc(session, 30000, launcher)
            proc(lp, 50000, server)

        for _ in range(2):
            c = proc(1, 400000, "claude")
            stdio(c, "npm exec slack-mcp-server@1.3.0", "node /cache/_npx/a/node_modules/.bin/slack-mcp-server")
            stdio(c, "uv tool uvx mcp-atlassian@0.23.1", "/cache/uv/env/bin/python /cache/uv/env/bin/mcp-atlassian")
            stdio(c, "npm exec reddit-mcp-buddy@1.1.14", "node /cache/_npx/b/node_modules/.bin/reddit-mcp-buddy")
            proc(c, 40000, f"node {h}/src/reddit-mcp-buddy/dist/index.js")
            stdio(c, "npm exec @upstash/context7-mcp@latest", "node /cache/_npx/c/node_modules/.bin/context7-mcp")
            stdio(c, "npm exec mystery-mcp@2.0.0", "node /cache/_npx/d/node_modules/.bin/mystery-mcp")
            stdio(c, "npm exec idle-mcp@1.0.0", "node /cache/_npx/e/node_modules/.bin/idle-mcp")
            proc(c, 60000, f"node {plugin}/server.mjs")
            proc(c, 3000, "/bin/zsh -c grep mcp ~/notes.txt")
        x = proc(1, 300000, "/opt/homebrew/bin/codex")
        stdio(x, "npm exec slack-mcp-server@1.3.0", "node /cache/_npx/a/node_modules/.bin/slack-mcp-server")
        g = proc(1, 200000, "/home/me/.grok/bin/grok")
        stdio(g, "npm exec slack-mcp-server@1.3.0", "node /cache/_npx/a/node_modules/.bin/slack-mcp-server")
        self.ps = "\n".join(f"{a} {b} {c} {d}" for a, b, c, d in rows)

    def report(self, **kw):
        kw.setdefault("use_cli", False)
        return audit.audit(self.home, self.ps, **kw)


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = Machine(self.tmp.name)
        self.r = self.m.report()
        self.rows = {r["server"]: r for r in self.r["servers"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts_sessions_per_agent(self):
        self.assertEqual(self.r["sessions"], {"claude": 2, "codex": 1, "grok": 1})

    def test_shared_server_counts_every_agents_copies_and_launchers(self):
        slack = self.rows["slack"]
        self.assertEqual(slack["action"], "share")
        self.assertEqual(slack["copies"], 4)
        self.assertEqual(slack["processes"], 8)
        self.assertEqual(slack["agents"], ["claude", "codex", "grok"])
        self.assertTrue(any("4 idle npx/uvx launcher" in f for f in slack["flags"]))
        self.assertGreater(slack["saving_mb"], 0)

    def test_credential_servers_get_the_refusal_warning(self):
        for name in ("slack", "atlassian"):
            self.assertTrue(any("holds a credential" in f for f in self.rows[name]["flags"]), name)
        self.assertNotIn("credential", " ".join(self.rows["reddit"]["flags"]))

    def test_hosted_alternative_and_unpinned_version(self):
        c7 = self.rows["context7"]
        self.assertEqual(c7["action"], "use-hosted")
        self.assertIn("version not pinned to an exact release", c7["flags"])
        self.assertEqual(c7["saving_mb"], c7["rss_mb"])

    def test_unknown_server_is_checked_when_used_and_flagged_when_idle(self):
        self.assertEqual(self.rows["mystery"]["action"], "check")
        self.assertEqual(self.rows["mystery"]["copies"], 2)
        self.assertEqual(self.rows["idle"]["action"], "remove?")

    def test_plugin_server_is_reported_but_not_counted_as_actionable(self):
        plug = self.rows["finder:mods"]
        self.assertEqual(plug["action"], "plugin")
        self.assertEqual(plug["copies"], 2)
        self.assertEqual(plug["used_sessions"]["claude"], 1)
        total = sum(r["saving_mb"] for r in self.r["servers"] if r["action"] != "plugin")
        self.assertEqual(self.r["totals"]["saving_mb"], total)

    def test_same_server_under_two_names_is_a_duplicate(self):
        self.assertEqual(self.r["duplicates"], [
            {"agents": ["claude", "grok"], "stem": "reddit-mcp-buddy", "names": ["reddit", "reddit-dev"]}])
        self.assertTrue(any("possible duplicate of reddit-dev" in f for f in self.rows["reddit"]["flags"]))
        self.assertEqual(self.rows["reddit-dev"]["action"], "remove?")

    def test_shell_commands_that_mention_mcp_are_ignored(self):
        self.assertFalse([r for r in self.r["servers"] if r["identity"].startswith("unconfigured")])

    def test_usage_comes_from_each_agents_logs(self):
        self.assertEqual(self.rows["slack"]["used_sessions"], {"claude": 1, "codex": 1, "grok": 0})
        self.assertEqual(self.rows["atlassian"]["used_sessions"]["grok"], 1)
        self.assertIsNone(self.m.report(usage=False)["servers"][0]["used_sessions"])

    def test_remote_server_needs_nothing(self):
        self.assertEqual(self.rows["remote"]["action"], "ok")
        self.assertEqual(self.rows["remote"]["copies"], 0)

    def test_output_never_contains_secret_values_or_the_home_path(self):
        md = audit.markdown(self.r, self.m.home)
        js = json.dumps(self.r)
        self.assertNotIn(SECRET, md + js)
        self.assertNotIn(str(self.m.home), md)
        self.assertIn("SLACK_MCP_XOXP_TOKEN", self.rows["slack"]["credential_names"])


class SmallMachine(unittest.TestCase):
    """A small made-up machine; each test writes only the config it needs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        write(self.home / ".grok/config.toml", "")
        for d in (".claude/projects", ".codex/sessions", ".grok/sessions"):
            (self.home / d).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def run_audit(self, claude=None, ps="", cwds=None, **kw):
        write(self.home / ".claude.json", json.dumps({"mcpServers": claude or {}}))
        return audit.audit(self.home, ps, use_cli=False, cwds=cwds or [], **kw)

    def rows(self, report):
        return {r["server"]: r for r in report["servers"]}


class ReviewRegressionTest(SmallMachine):
    """One case per defect found in the first review round."""

    def test_argument_values_never_reach_output(self):
        ps = "\n".join(["10 1 100 claude", "11 10 50 custom-mcp --header=Authorization:SENTINEL",
                         "12 10 50 node /srv/server.js"])
        r = self.run_audit({"wrapped": {"command": "env", "args": ["TOKEN=SENTINEL", "node", "/srv/server.js"]}}, ps)
        out = audit.markdown(r, self.home) + json.dumps(r)
        self.assertNotIn("SENTINEL", out)
        self.assertEqual(self.rows(r)["wrapped"]["copies"], 1)

    def test_two_accounts_of_one_server_keep_two_copies(self):
        cfg = {n: {"command": "npx", "args": ["-y", "slack-mcp-server@1.3.0"], "env": {"SLACK_MCP_XOXP_TOKEN": "x"}}
               for n in ("slack-a", "slack-b")}
        ps = "\n".join(["10 1 100 claude", "11 10 1024 npm exec slack-mcp-server@1.3.0", "13 11 10240 node slack",
                         "12 10 1024 npm exec slack-mcp-server@1.3.0", "14 12 10240 node slack"])
        row = self.rows(self.run_audit(cfg, ps, usage=False))["slack-a, slack-b"]
        self.assertEqual((row["rss_mb"], row["saving_mb"]), (22, 2))  # both servers stay, only launchers go
        self.assertTrue(any("2 separate configurations hold credentials" in f for f in row["flags"]))

    def test_nested_agent_is_a_boundary(self):
        cfg = {"alpha": {"command": "npx", "args": ["-y", "alpha-mcp@1.0.0"]},
               "beta": {"command": "npx", "args": ["-y", "beta-mcp@1.0.0"]}}
        ps = "\n".join(["10 1 100 claude", "11 10 5000 npm exec alpha-mcp@1.0.0", "12 10 300000 codex",
                         "13 12 2000 npm exec alpha-mcp@1.0.0", "14 12 2000 npm exec beta-mcp@1.0.0"])
        r = self.run_audit(cfg, ps)
        rows = self.rows(r)
        self.assertEqual(r["sessions"], {"claude": 1})
        self.assertEqual((rows["alpha"]["copies"], rows["beta"]["copies"]), (2, 1))
        self.assertTrue({"claude", "codex"} <= set(rows["alpha"]["agents"]))
        self.assertLess(r["totals"]["rss_mb"], 20)

    def test_shell_searches_and_lookalike_packages_do_not_count(self):
        cfg = {"alpha": {"command": "npx", "args": ["-y", "alpha-mcp@1.0.0"]}}
        ps = "\n".join(["10 1 100 claude", "11 10 50 /bin/zsh -c rg alpha-mcp .",
                         "12 10 50 npm exec evil-alpha-mcp@1.0.0"])
        self.assertEqual(self.rows(self.run_audit(cfg, ps))["alpha"]["copies"], 0)

    def test_recipes_need_the_exact_package(self):
        cfg = {"c7": {"command": "npx", "args": ["-y", "@unrelated/context7-mcp@1.0.0"]}}
        row = self.rows(self.run_audit(cfg, "10 1 100 claude\n11 10 50 npm exec @unrelated/context7-mcp@1.0.0", usage=False))["c7"]
        self.assertEqual(row["action"], "check")
        self.assertIsNone(row["recipe"])

    def test_missing_logs_mean_unknown_not_unused(self):
        (self.home / ".claude/projects").rmdir()
        cfg = {"alpha": {"command": "npx", "args": ["-y", "alpha-mcp@1.0.0"]}}
        row = self.rows(self.run_audit(cfg, "10 1 100 claude\n11 10 50 npm exec alpha-mcp@1.0.0"))["alpha"]
        self.assertNotEqual(row["action"], "remove?")
        self.assertIsNone(row["used_sessions"]["claude"])

    def test_codex_function_call_names_count_as_usage(self):
        write(self.home / ".codex/sessions/r.jsonl", '{"type": "function_call", "name": "mcp__alpha__read"}\n')
        write(self.home / ".codex/config.toml", '[mcp_servers.alpha]\ncommand = "npx"\nargs = ["-y", "alpha-mcp@1.0.0"]\n')
        r = self.run_audit({}, "10 1 100 codex\n11 10 50 npm exec alpha-mcp@1.0.0")
        self.assertEqual(self.rows(r)["alpha"]["used_sessions"]["codex"], 1)

    def test_one_session_counts_once_across_scopes(self):
        write(self.home / ".claude/projects/-p/s1.jsonl", '{"name":"mcp__alpha__x"}\n{"name":"mcp__alpha__y"}\n')
        proj = self.home / "proj"
        write(proj / ".mcp.json", json.dumps({"mcpServers": {"alpha": {"command": "npx", "args": ["-y", "alpha-mcp@1.0.0"]}}}))
        r = self.run_audit({"alpha": {"command": "npx", "args": ["-y", "alpha-mcp@1.0.0"]}}, "10 1 100 claude",
                           cwds=[str(proj)])
        self.assertEqual(self.rows(r)["alpha"]["used_sessions"]["claude"], 1)

    def test_grok_respects_disabled_names_and_cursor_switch(self):
        write(self.home / ".grok/config.toml", '[mcp_servers.alpha]\ncommand = "npx"\nargs = ["-y", "alpha-mcp@1.0.0"]\n'
              'enabled = false\n')
        write(self.home / ".cursor/mcp.json", json.dumps({"mcpServers": {"cur": {"command": "cur-mcp"}}}))
        os.environ["GROK_CURSOR_MCPS_ENABLED"] = "0"
        try:
            r = self.run_audit({"alpha": {"command": "npx", "args": ["-y", "alpha-mcp@1.0.0"]}})
        finally:
            del os.environ["GROK_CURSOR_MCPS_ENABLED"]
        self.assertEqual(self.rows(r)["alpha"]["agents"], ["claude"])
        self.assertNotIn("cur", self.rows(r))

    def test_project_configs_of_running_sessions_are_read(self):
        proj = self.home / "proj"
        write(proj / ".codex/config.toml", '[mcp_servers.worker]\ncommand = "python"\nargs = ["/proj/worker.py"]\n')
        r = self.run_audit({}, "10 1 100 codex\n11 10 50 python /proj/worker.py", cwds=[str(proj)])
        self.assertEqual(self.rows(r)["worker"]["copies"], 1)

    def test_codex_env_vars_count_as_credentials(self):
        write(self.home / ".codex/config.toml", '[mcp_servers.slack]\ncommand = "npx"\n'
              'args = ["-y", "slack-mcp-server@1.3.0"]\nenv_vars = ["SLACK_MCP_XOXP_TOKEN"]\n')
        row = self.rows(self.run_audit({}))["slack"]
        self.assertIn("SLACK_MCP_XOXP_TOKEN", row["credential_names"])

    def test_launcher_options_are_not_packages(self):
        e = audit.make_entry("claude", "a", "user", "-", {"command": "uvx", "args": ["--python", "3.12", "mcp-atlassian@0.23.1"]})
        self.assertEqual(audit.identity(e)[1], "mcp-atlassian")
        e = audit.make_entry("claude", "a", "user", "-", {"command": "npx", "args": ["-p", "pkg-a@1.0.0", "bin-a"]})
        self.assertEqual(audit.identity(e)[1], "pkg-a")

    def test_usage_window_uses_event_time(self):
        old = '{"timestamp":"2020-01-01T00:00:00Z","name":"mcp__alpha__x"}\n'
        write(self.home / ".claude/projects/-p/s1.jsonl", old)
        cfg = {"alpha": {"command": "npx", "args": ["-y", "alpha-mcp@1.0.0"]}}
        row = self.rows(self.run_audit(cfg, "10 1 100 claude\n11 10 50 npm exec alpha-mcp@1.0.0"))["alpha"]
        self.assertEqual(row["used_sessions"]["claude"], 0)
        self.assertEqual(row["action"], "remove?")

    def test_floating_versions_are_unpinned(self):
        for spec in ("x@^1.0.0", "x@next", "x@1", "x"):
            self.assertFalse(audit.pinned(spec), spec)
        for spec in ("x@1.3.0", "@s/x@0.0.83", "x==0.23.1"):
            self.assertTrue(audit.pinned(spec), spec)


class ReviewRoundTwoTest(SmallMachine):
    """One case per defect found in the second review round."""

    def test_bare_argument_values_never_print(self):
        r = self.run_audit({"c": {"command": "custom-mcp", "args": ["--token", "SENTINEL_SECRET"]}},
                           "10 1 100 claude\n11 10 50 custom-mcp --token SENTINEL_SECRET")
        self.assertNotIn("SENTINEL", audit.markdown(r, self.home) + json.dumps(r))

    def test_same_name_in_two_agents_keeps_a_copy_each(self):
        write(self.home / ".codex/config.toml", '[mcp_servers.slack]\ncommand = "npx"\n'
              'args = ["-y", "slack-mcp-server@1.3.0"]\nenv = { SLACK_MCP_XOXP_TOKEN = "y" }\n')
        cfg = {"slack": {"command": "npx", "args": ["-y", "slack-mcp-server@1.3.0"], "env": {"SLACK_MCP_XOXP_TOKEN": "x"}}}
        ps = "\n".join(["10 1 100 claude", "11 10 1024 npm exec slack-mcp-server@1.3.0", "13 11 10240 node s",
                         "20 1 100 codex", "21 20 1024 npm exec slack-mcp-server@1.3.0", "23 21 10240 node s"])
        row = self.rows(self.run_audit(cfg, ps, usage=False))["slack"]
        self.assertEqual(row["saving_mb"], 2)

    def test_scoped_package_is_not_the_unscoped_one(self):
        cfg = {"alpha": {"command": "npx", "args": ["-y", "alpha-mcp@1.0.0"]}}
        ps = "10 1 100 claude\n11 10 50 npm exec @evil/alpha-mcp@1.0.0"
        self.assertEqual(self.rows(self.run_audit(cfg, ps))["alpha"]["copies"], 0)

    def test_uv_directory_option_is_not_the_package(self):
        e = audit.make_entry("claude", "a", "user", "-", {"command": "uvx", "args": ["--directory", "/srv/p", "mcp-atlassian@0.23.1"]})
        self.assertEqual(audit.identity(e)[1], "mcp-atlassian")

    def test_configured_arguments_still_match(self):
        cfg = {n: {"command": "npx", "args": ["-y", "alpha-mcp@1.0.0", "--workspace", n]} for n in ("a", "b")}
        ps = "10 1 100 claude\n11 10 50 npm exec alpha-mcp@1.0.0 --workspace a\n12 10 50 npm exec alpha-mcp@1.0.0 --workspace b"
        rows = self.rows(self.run_audit(cfg, ps))
        self.assertEqual((rows["a"]["copies"], rows["b"]["copies"]), (1, 1))

    def test_one_grok_project_does_not_hide_another(self):
        write(self.home / ".grok/config.toml", '[mcp_servers.worker]\ncommand = "python"\nargs = ["/u/worker.py"]\n')
        a = self.home / "a"
        write(a / ".grok/config.toml", '[mcp_servers.worker]\ncommand = "python"\nargs = ["/a/worker.py"]\n')
        (self.home / "b").mkdir()
        ps = "10 1 100 grok\n11 10 50 python /a/worker.py\n20 1 100 grok\n21 20 50 python /u/worker.py"
        r = self.run_audit({}, ps, cwds=[str(a), str(self.home / "b")])
        self.assertEqual(sum(x["copies"] for x in r["servers"]), 2)

    def test_configured_shell_launch_is_counted(self):
        cfg = {"custom": {"command": "bash", "args": ["-lc", "node /srv/custom-mcp.js; wait"]}}
        ps = "10 1 100 claude\n11 10 50 bash -lc node /srv/custom-mcp.js; wait\n12 11 11264 node /srv/custom-mcp.js"
        row = self.rows(self.run_audit(cfg, ps))["custom"]
        self.assertEqual((row["copies"], row["processes"]), (1, 2))


class HelperTest(unittest.TestCase):
    def test_package_names_and_versions(self):
        self.assertEqual(audit.pkg_name("@scope/name@1.2.3"), "@scope/name")
        self.assertEqual(audit.pkg_name("mcp-atlassian==0.23.1"), "mcp-atlassian")
        self.assertEqual(audit.pkg_version("@scope/name@1.2.3"), "1.2.3")
        self.assertIsNone(audit.pkg_version("@scope/name"))
        self.assertEqual(audit.pkg_version("tool@latest"), "latest")

    def test_launcher_detection(self):
        self.assertTrue(audit.is_launcher("npm exec foo@1"))
        self.assertTrue(audit.is_launcher("node /opt/homebrew/bin/npx -y foo"))
        self.assertTrue(audit.is_launcher("uv tool uvx foo"))
        self.assertFalse(audit.is_launcher("node /x/server.js"))


if __name__ == "__main__":
    unittest.main()
