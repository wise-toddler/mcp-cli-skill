"""Mistyped options / tool names fail fast with a hint and never reach the server."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_SERVER = textwrap.dedent("""
    import json, os, sys
    tools = os.environ.get("FAKE_TOOLS", "echo,slack_channel").split(",")
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("id") is None:
            continue
        method = msg["method"]
        if method == "initialize":
            res = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"}}
        elif method == "tools/list":
            res = {"tools": [{"name": n, "inputSchema": {"type": "object", "properties": {}}} for n in tools]}
        else:  # tools/call: log every name that actually reached the server
            with open(os.environ["FAKE_LOG"], "a") as f:
                f.write(msg["params"]["name"] + "\\n")
            res = {"content": [{"type": "text", "text": "ok " + msg["params"]["name"]}]}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}) + "\\n")
        sys.stdout.flush()
""")


class ArgsTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.log = os.path.join(self.home, "calls.log")
        script = os.path.join(self.home, "fake_server.py")
        with open(script, "w") as f:
            f.write(FAKE_SERVER)
        os.makedirs(os.path.join(self.home, ".mcp-cli"))
        with open(os.path.join(self.home, ".mcp-cli", "servers.json"), "w") as f:
            json.dump({"fake": {"command": sys.executable, "args": [script], "env": {"FAKE_LOG": self.log}}}, f)

    def run_cli(self, *args, tools=None):
        """Run mcp-call in a subprocess with an isolated HOME; return (exit code, stdout, stderr)."""
        env = {**os.environ, "HOME": self.home, "PYTHONPATH": os.path.join(REPO, "src"), "NO_COLOR": "1"}
        if tools:
            env["FAKE_TOOLS"] = tools
        p = subprocess.run([sys.executable, "-m", "mcp_cli_skill.cli", *args], env=env, cwd=REPO,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout, p.stderr

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return f.read().split()

    def test_list_help_aliases_show_tools(self):
        for flag in ("--tools", "--list", "--help", "-h"):
            code, out, err = self.run_cli("fake", flag)
            self.assertEqual(code, 0, f"{flag}: {err}")
            self.assertIn("echo", out, flag)
        self.assertEqual(self.calls(), [])

    def test_unknown_option_fails_fast(self):
        code, _, err = self.run_cli("fake", "--list-tools")
        self.assertEqual(code, 2)
        self.assertIn("unknown option '--list-tools'", err)
        self.assertIn("Did you mean --tools?", err)
        self.assertIn("mcp-call fake --tools", err)
        self.assertEqual(self.calls(), [])

    def test_unknown_tool_suggests_and_never_calls(self):
        code, _, err = self.run_cli("fake", "ehco")
        self.assertEqual(code, 2)
        self.assertIn("unknown tool 'ehco'", err)
        self.assertIn("Did you mean: echo", err)
        self.assertEqual(self.calls(), [])

    def test_known_tool_still_called(self):
        code, out, err = self.run_cli("fake", "echo")
        self.assertEqual(code, 0, err)
        self.assertIn("ok echo", out)
        self.assertEqual(self.calls(), ["echo"])

    def test_stale_cache_new_tool_still_callable(self):
        self.assertEqual(self.run_cli("fake", "echo")[0], 0)  # caches echo,slack_channel
        code, out, err = self.run_cli("fake", "brand_new", tools="echo,slack_channel,brand_new")
        self.assertEqual(code, 0, err)
        self.assertIn("ok brand_new", out)
        self.assertEqual(self.calls(), ["echo", "brand_new"])

    def test_schema_unknown_tool_suggests(self):
        code, _, err = self.run_cli("fake", "ech", "--schema")
        self.assertEqual(code, 2)
        self.assertIn("Did you mean: echo", err)


if __name__ == "__main__":
    unittest.main()
