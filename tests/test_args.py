"""Mistyped options / tool names fail fast with a hint and never reach the server."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_SERVER = textwrap.dedent("""
    import base64, json, os, sys
    tools = os.environ.get("FAKE_TOOLS", "echo,slack_channel,shot").split(",")
    IMG = b"\\x89PNG fake image bytes"
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("id") is None:
            continue
        method = msg["method"]
        if method == "initialize":
            res = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"}}
        elif method == "tools/list":
            descs = {"shot": "Return an image, like echo but binary", "echo": "Echo text back; handy for slack tests"}
            schemas = {"echo": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}
            res = {"tools": [{"name": n, "description": descs.get(n, ""),
                              "inputSchema": schemas.get(n, {"type": "object", "properties": {}})} for n in tools]}
        else:  # tools/call: log every name that actually reached the server
            with open(os.environ["FAKE_LOG"], "a") as f:
                f.write(msg["params"]["name"] + "\\n")
            res = {"content": [{"type": "text", "text": "ok " + msg["params"]["name"]}]}
            if msg["params"]["name"] == "shot":  # like browser_screenshot: image block (+ saved file if path given)
                img = {"type": "image", "data": base64.b64encode(IMG).decode(), "mimeType": "image/png"}
                path = msg["params"].get("arguments", {}).get("path")
                res = {"content": [img]}
                if path:
                    with open(path, "wb") as f:
                        f.write(IMG)
                    res["content"].append({"type": "text", "text": f"Saved {path} (1 KB)"})
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
            json.dump({"fake": {"command": sys.executable, "args": [script], "env": {"FAKE_LOG": self.log}},
                       "uncached": {"command": sys.executable, "args": [script]}}, f)

    def run_cli(self, *args, tools=None):
        """Run mcp-call in a subprocess with an isolated HOME; return (exit code, stdout, stderr)."""
        env = {**os.environ, "HOME": self.home, "TMPDIR": self.home, "PYTHONPATH": os.path.join(REPO, "src"), "NO_COLOR": "1"}
        if tools:
            env["FAKE_TOOLS"] = tools
        p = subprocess.run([sys.executable, "-m", "mcp_cli_skill.cli", *args], env=env, cwd=REPO,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout, p.stderr

    @staticmethod
    def rows(out):
        """Tool names from --search table output (skipping the indented --param lines)."""
        return [ln.split()[1] for ln in out.splitlines() if ln.split() and not ln.split()[0].startswith("--")]

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

    def copies(self):
        d = os.path.join(self.home, "mcp-call")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def test_image_already_saved_by_server_is_not_copied(self):
        path = os.path.join(self.home, "shot.png")
        code, out, err = self.run_cli("fake", "shot", f"--path={path}")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], path)  # image block points at the server's file
        self.assertEqual(self.copies(), [])

    def test_image_without_saved_file_is_copied_and_old_copies_pruned(self):
        os.makedirs(os.path.join(self.home, "mcp-call"))
        stale, fresh = (os.path.join(self.home, "mcp-call", n) for n in ("mcp-stale.png", "mcp-fresh.png"))
        for p in (stale, fresh):
            open(p, "w").close()
        os.utime(stale, (time.time() - 2 * 86400,) * 2)
        code, out, err = self.run_cli("fake", "shot")
        self.assertEqual(code, 0, err)
        copy = out.strip()
        with open(copy, "rb") as f:
            self.assertEqual(f.read(), b"\x89PNG fake image bytes")
        self.assertEqual(self.copies(), sorted(["mcp-fresh.png", os.path.basename(copy)]))

    def test_search_ranks_name_matches_first_and_needs_every_word(self):
        self.assertEqual(self.run_cli("fake", "--tools")[0], 0)  # fills the cache for "fake" only
        code, out, err = self.run_cli("--search", "SLACK")  # case-insensitive
        self.assertEqual(code, 0, err)
        rows = self.rows(out)
        # name match first even though "echo" (description-only match) sorts earlier alphabetically
        self.assertEqual(rows, ["slack_channel", "echo"])
        self.assertIn("Not searched (no cached tool list): uncached", err)
        for args in (("echo", "binary"), ("echo binary",)):  # separate words or one quoted phrase
            code, out, _ = self.run_cli("--search", *args)
            self.assertEqual(self.rows(out), ["shot"], args)
        self.assertEqual(self.calls(), [])  # search never contacts a server

    def test_search_no_match_exits_1(self):
        self.assertEqual(self.run_cli("fake", "--tools")[0], 0)
        code, out, err = self.run_cli("--search", "nonexistentword")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("No cached tools match 'nonexistentword'.", err)

    def test_output_formats_tools(self):
        code, out, err = self.run_cli("fake", "--tools", "-o", "name")
        self.assertEqual((code, out.splitlines()), (0, ["fake echo", "fake slack_channel", "fake shot"]), err)
        discover = self.run_cli("fake", "--discover")[1]
        for args in (("fake", "--tools", "-o", "json"), ("fake", "-o=json"), ("fake", "--output", "json")):
            self.assertEqual(self.run_cli(*args)[1], discover, args)  # -o json is exactly --discover
        self.assertEqual(json.loads(discover)[0]["inputSchema"]["required"], ["text"])
        self.assertTrue(discover.startswith('[\n  {\n    "name": "echo"'), discover[:40])  # --discover's indented format since v0.3

    def test_output_formats_servers(self):
        code, out, err = self.run_cli("--servers", "-o", "name")
        self.assertEqual((code, out.splitlines()), (0, ["fake", "uncached"]), err)
        out = self.run_cli("--servers", "-o", "json")[1]
        self.assertEqual([(s["name"], s["transport"]) for s in json.loads(out)], [("fake", "stdio"), ("uncached", "stdio")])
        self.assertNotIn("FAKE_LOG", out)  # env/headers can hold secrets: never printed

    def test_output_formats_search(self):
        self.assertEqual(self.run_cli("fake", "--tools")[0], 0)
        out = self.run_cli("--search", "echo")[1]
        self.assertIn("--text*", out)  # params shown so the agent can call without --help
        self.assertEqual(self.run_cli("--search", "echo", "-o", "name")[1].splitlines(), ["fake echo", "fake shot"])
        hits = json.loads(self.run_cli("--search", "-o", "json", "echo")[1])  # -o may come before the words
        self.assertEqual([(h["tool"], h["required"], h["params"]) for h in hits],
                         [("echo", ["text"], ["text"]), ("shot", [], [])])
        code, out, _ = self.run_cli("--search", "nothinghere", "-o", "json")
        self.assertEqual((code, json.loads(out)), (1, []))

    def test_bad_output_format(self):
        code, _, err = self.run_cli("--servers", "-o", "yaml")
        self.assertEqual(code, 2)
        self.assertIn("unknown output format 'yaml'. Use one of: table, json, name.", err)
        code, _, err = self.run_cli("--search", "echo", "-o")
        self.assertEqual(code, 2)
        self.assertIn("-o needs a format", err)

    def test_piped_servers_not_truncated(self):
        url = "https://example.invalid/" + "x" * 200
        path = os.path.join(self.home, ".mcp-cli", "servers.json")
        with open(path) as f:
            cfg = json.load(f)
        cfg["long"] = {"type": "http", "url": url}
        with open(path, "w") as f:
            json.dump(cfg, f)
        self.assertIn(url, self.run_cli("--servers")[1])  # not a TTY here, so no "…"

    def test_schema_unknown_tool_suggests(self):
        code, _, err = self.run_cli("fake", "ech", "--schema")
        self.assertEqual(code, 2)
        self.assertIn("Did you mean: echo", err)


if __name__ == "__main__":
    unittest.main()
