"""Regression-audit gaps: clean errors instead of tracebacks, no silently dropped args (stdlib unittest)."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TB = "Traceback (most recent call last)"

# stdio fake; argv[1] picks a mode: normal, scalar (JSON lines that aren't objects), listerr (tools/list errors)
FAKE_SERVER = textwrap.dedent("""
    import json, os, sys
    mode = sys.argv[1]
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("id") is None:
            continue
        if mode == "scalar":
            print("42"); print("null"); print("[]")
        if msg["method"] == "initialize":
            out = {"result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"}}}
        elif msg["method"] == "tools/list":
            tools = [{"name": "echo", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}]
            out = {"error": {"code": -32601, "message": "tools/list not supported"}} if mode == "listerr" else {"result": {"tools": tools}}
        else:  # tools/call: log the params that reached the server, echo the arguments back
            with open(os.environ["FAKE_LOG"], "a") as f:
                f.write(json.dumps(msg["params"]) + "\\n")
            out = {"result": {"content": [{"type": "text", "text": json.dumps(msg["params"].get("arguments"))}]}}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], **out}) + "\\n")
        sys.stdout.flush()
""")


class Http(BaseHTTPRequestHandler):
    """HTTP MCP fake whose path picks a broken answer to every request after initialize."""

    def log_message(self, *args):
        pass

    def _reply(self, status, body=b"", ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        if "id" not in msg:
            return self._reply(202)
        if self.path == "/html200":  # e.g. a captive portal / CDN challenge page answering 200
            return self._reply(200, b"<!DOCTYPE html>\n<html><body>Just a moment...</body></html>", "text/html")
        if msg["method"] == "initialize":
            return self._reply(200, json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"capabilities": {}}}).encode())
        if self.path == "/empty202":
            return self._reply(202)
        if self.path == "/hangup":
            self.close_connection = True
            return  # no status line: the client sees RemoteDisconnected
        if self.path == "/ssenospace":  # spec-valid SSE: the space after "data:" is optional
            body = {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [{"name": "echo"}]}}
            return self._reply(200, ("event: message\ndata:" + json.dumps(body) + "\n\n").encode(), "text/event-stream")
        self._reply(404)


class GapsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Http)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.log = os.path.join(self.home, "calls.log")
        script = os.path.join(self.home, "fake_server.py")
        with open(script, "w") as f:
            f.write(FAKE_SERVER)
        stdio = lambda mode, **kw: {"command": sys.executable, "args": [script, mode], "env": {"FAKE_LOG": self.log}, **kw}  # noqa: E731
        self.servers = {
            "fake": stdio("normal"), "scalar": stdio("scalar"), "listerr": stdio("listerr"),
            "missing": {"command": "/nonexistent/mcp-cli-test-server", "args": ["--stdio"]},
            **{p: {"type": "http", "url": self.base + "/" + p} for p in ("html200", "empty202", "hangup", "ssenospace")},
        }
        self.write_servers()

    def write_servers(self):
        os.makedirs(os.path.join(self.home, ".mcp-cli"), exist_ok=True)
        with open(os.path.join(self.home, ".mcp-cli", "servers.json"), "w") as f:
            json.dump(self.servers, f)

    def run_cli(self, *args, env=None, stdin=None, cmd=None):
        """Run mcp-call (or cmd) in a subprocess with an isolated HOME; return (exit code, stdout, stderr)."""
        e = {k: v for k, v in os.environ.items() if not k.startswith("MCP_CALL") and k != "_MCP_CALL_COMPLETE"}
        e.update(HOME=self.home, TMPDIR=self.home, PYTHONPATH=os.path.join(REPO, "src"), NO_COLOR="1",
                 no_proxy="127.0.0.1,localhost", NO_PROXY="127.0.0.1,localhost",
                 SSH_CONNECTION="test 1 test 2")  # belt and braces: --login never opens a browser here
        e.update(env or {})
        cmd = cmd or [sys.executable, "-m", "mcp_cli_skill.cli"]
        kw = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        p = subprocess.run([*cmd, *args], env=e, cwd=self.home, capture_output=True, text=True, timeout=30, **kw)
        return p.returncode, p.stdout, p.stderr

    def parse(self, *argv):
        """parse_args() for argv, in a subprocess with the test HOME: (exit code, result repr or stderr)."""
        code = ("import sys; from mcp_cli_skill import cli; sys.argv = ['mcp-call', *sys.argv[1:]]; "
                "print(repr(cli.parse_args(cli.read_config())))")
        rc, out, err = self.run_cli(*argv, cmd=[sys.executable, "-c", code])
        return rc, out.strip() if rc == 0 else err

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    # gap 1: broken HTTP answers
    def test_http_bad_bodies_clean_error(self):
        want = {"html200": "non-JSON response from %s/html200 (HTTP 200): <!DOCTYPE html> <html><body>Just a moment...",
                "empty202": "non-JSON response from %s/empty202 (HTTP 202): empty body",
                "hangup": "bad response from %s/hangup: Remote end closed connection without response"}
        for name, msg in want.items():
            code, out, err = self.run_cli(name, "--tools")
            self.assertEqual((code, out), (1, ""), name)
            self.assertNotIn(TB, err, name)
            self.assertIn("Error: " + msg % self.base, err, name)
            self.assertEqual(len(err.strip().splitlines()), 1, err)

    # gap 9b
    def test_sse_data_without_space(self):
        code, out, err = self.run_cli("ssenospace", "--tools", "-o", "name")
        self.assertEqual((code, out), (0, "ssenospace echo\n"), err)

    # gap 4
    def test_stdio_json_scalar_lines_are_not_jsonrpc(self):
        code, out, err = self.run_cli("scalar", "--tools", "-o", "name")
        self.assertEqual((code, out), (0, "scalar echo\n"), err)

    # gap 5
    def test_missing_stdio_command(self):
        code, out, err = self.run_cli("missing", "--tools")
        self.assertEqual(code, 1)
        self.assertNotIn(TB, err)
        self.assertEqual(err, "Error: cannot start server 'missing': /nonexistent/mcp-cli-test-server: No such file or directory\n")

    # gap 9a
    def test_tools_list_error_is_reported(self):
        code, out, err = self.run_cli("listerr", "--tools")
        self.assertEqual((code, out), (1, ""))
        self.assertIn('Error: tools/list failed: {"code": -32601, "message": "tools/list not supported"}', err)

    def test_tools_list_error_refresh_keeps_going(self):
        self.servers = {k: self.servers[k] for k in ("fake", "listerr", "missing")}
        self.write_servers()
        code, out, err = self.run_cli("--refresh-completions")
        self.assertEqual(code, 0, err)
        self.assertIn("✓ fake: 1 tools cached", out)
        self.assertIn("tools/list not supported", err)
        self.assertIn("✗ listerr: failed", err)
        self.assertIn("✗ missing: failed", err)
        self.assertIn("Cached 1 servers (2 failed)", out)

    def test_tools_list_error_still_lets_a_call_through(self):
        # unchanged since v0.8.2: a server that can't list tools still gets the call
        code, out, err = self.run_cli("listerr", "echo", "--text=x")
        self.assertEqual((code, json.loads(out)), (0, {"text": "x"}), err)
        self.assertEqual(self.calls(), [{"name": "echo", "arguments": {"text": "x"}}])


    # gap 2
    def test_top_level_list_is_servers(self):
        for fmt in ((), ("-o", "name"), ("-o", "json")):
            code, out, err = self.run_cli("--list", *fmt)
            self.assertEqual((code, out), (0, self.run_cli("--servers", *fmt)[1]), err)
        self.assertIn("listerr", out)

    def test_top_level_server_flags_need_a_server(self):
        for argv in (["--tools"], ["--discover"], ["-o", "json"], ["--output=name"], ["--tools", "-o", "json"]):
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, out), (2, ""), argv)
            self.assertEqual(err, f"Error: {argv[0]} needs a server: mcp-call <server> {' '.join(argv)}"
                                  " — or search every server: mcp-call --search <words>\n")

    def test_top_level_unknown_option(self):
        for argv, hint in ((["--serves"], " Did you mean --servers?"), (["-v"], ""), (["--qqqq", "x"], "")):
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, out), (2, ""), argv)
            self.assertEqual(err, f"Error: unknown option '{argv[0]}'.{hint} See: mcp-call --help\n")

    def test_dash_server_name_still_works(self):
        self.servers["-odd"] = self.servers["fake"]
        self.write_servers()
        code, out, err = self.run_cli("-odd", "--tools", "-o", "name")
        self.assertEqual((code, out), (0, "-odd echo\n"), err)

    def test_unknown_server_hint_on_stderr(self):
        code, out, err = self.run_cli("fak", "--tools")
        self.assertEqual((code, out), (2, ""))  # stdout stays clean for pipes
        self.assertTrue(err.startswith("Error: 'fak' not found. Did you mean: fake? Available:\n"), err)
        self.assertIn("listerr", err)  # the server listing moved to stderr

    # gap 3
    def test_login_no_browser_any_position(self):
        for argv in (["--login", "html200", "--no-browser"], ["--login", "--no-browser", "html200"],
                     ["html200", "--login", "--no-browser"], ["html200", "--no-browser", "--login"],
                     ["--no-browser", "--login", "html200"]):
            self.assertEqual(self.parse(*argv), (0, "('__login__', 'html200', {'no_browser': True})"), argv)
        for argv in (["--login", "html200"], ["html200", "--login"]):
            self.assertEqual(self.parse(*argv), (0, "('__login__', 'html200', {'no_browser': False})"), argv)
        self.assertEqual(self.parse("--logout", "--no-browser", "html200"), (0, "('__logout__', 'html200', {'no_browser': True})"))

    def test_login_extra_args_exit_2(self):
        code, err = self.parse("--login", "html200", "--no-brower")
        self.assertEqual(code, 2)
        self.assertIn("unexpected argument '--no-brower' for --login. Did you mean --no-browser?", err)
        for argv in (["--login", "html200", "extra"], ["--login", "--bogus", "html200"], ["html200", "--login", "x"]):
            self.assertEqual(self.parse(*argv)[0], 2, argv)
        self.assertEqual(self.parse("--login"), (1, "Usage: mcp-call --login <name>\n"))
        # end to end: the typo never reaches the login flow (no "Opening the browser", no request)
        code, out, err = self.run_cli("--login", "html200", "--no-brower")
        self.assertEqual(code, 2)
        self.assertNotIn("Opening the browser", err)

    # gap 7
    def test_bad_user_input_clean_errors(self):
        cases = [(["fake", "echo", "--input-json", "{bad"], None, "Error: --input-json needs a JSON object: Expecting property name"),
                 (["fake", "echo", "--input-json=42"], None, "Error: --input-json needs a JSON object:"),
                 (["fake", "echo"], "not json", "Error: stdin is not valid JSON: Expecting value"),
                 (["--add-http", "x", "https://e.example", "-H", "NoColon"], None, "Error: header 'NoColon' must look like 'Key: Value'."),
                 (["--add-http", "x", "https://e.example", "--header=NoColon"], None, "Error: header 'NoColon' must look like 'Key: Value'."),
                 (["--add", "x", "cmd", "--env", "NOEQ"], None, "Error: --env needs KEY=VALUE, got 'NOEQ'.")]
        for argv, stdin, msg in cases:
            code, out, err = self.run_cli(*argv, stdin=stdin)
            self.assertEqual(code, 2, argv)
            self.assertNotIn(TB, err, argv)
            self.assertTrue(err.startswith(msg), (argv, err))
        with open(os.path.join(self.home, ".mcp-cli", "servers.json")) as f:
            self.assertNotIn("x", json.load(f))  # nothing saved
        self.assertEqual(self.calls(), [])

    def test_bad_timeout_env_warns_once_and_uses_default(self):
        for name, server in (("MCP_CALL_LOGIN_TIMEOUT", "fake"), ("MCP_CALL_HTTP_TIMEOUT", "ssenospace")):
            code, out, err = self.run_cli(server, "--tools", "-o", "name", env={name: "abc"})
            self.assertEqual((code, out), (0, f"{server} echo\n"), err)
            self.assertEqual(err, f"Warning: ignoring {name}='abc' (not an integer); using 300.\n")

    # gap 8
    def test_leftover_args_exit_2(self):
        cases = [(["fake", "echo", "--text=x", "--input-json"], "Error: --input-json needs a JSON object: no value given."),
                 (["fake", "echo", "hello"], "Error: unexpected argument 'hello' for 'fake echo'. Tool params look like"),
                 (["fake", "echo", "--text=x", "-o", "json"], "Error: unexpected argument '-o' for 'fake echo'."),
                 (["--servers", "--bogus"], "Error: unexpected argument '--bogus' after --servers."),
                 (["--servers", "--ouput", "json"], "Error: unexpected argument '--ouput' after --servers. Did you mean --output?"),
                 (["--search", "echo", "--ouput", "json"], "Error: unexpected argument '--ouput' after --search. Did you mean --output?"),
                 (["fake", "--tools", "--ouput", "json"], "Error: unexpected argument '--ouput' for 'fake --tools'. Did you mean --output?"),
                 (["fake", "--tools", "junk"], "Error: unexpected argument 'junk' for 'fake --tools'. For one tool: mcp-call fake junk --help"),
                 (["--sync", "--bogus"], "Error: unexpected argument '--bogus' after --sync.")]
        for argv, msg in cases:
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, out), (2, ""), argv)
            self.assertTrue(err.startswith(msg), (argv, err))
        self.assertEqual(self.calls(), [])

    def test_normal_tool_args_unchanged(self):
        code, out, err = self.run_cli("fake", "echo", "--text", "hi", "--count", "3", "--flag", "--mode=b", "--neg", "-5",
                                      "--", "--help", "--schema=1", "-x", "word")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"text": "hi", "count": 3, "flag": True, "mode": "b", "neg": -5, "help": True, "schema": 1})
        code, out, err = self.run_cli("fake", "echo", "--input-json", '{"text": "ij"}', "--n=1")
        self.assertEqual(json.loads(out), {"text": "ij", "n": 1}, err)
        code, out, err = self.run_cli("fake", "echo", stdin='{"text": "in"}')
        self.assertEqual(json.loads(out), {"text": "in"}, err)
        code, out, err = self.run_cli("fake", "echo", "hello", "--help")  # a later --help still wins over a stray word
        self.assertEqual(code, 0, err)
        self.assertIn("Usage:", out)
        for argv in (["fake", "-o", "json", "--tools"], ["fake", "--tools", "--help"]):  # listing flags in any order
            self.assertEqual(self.run_cli(*argv)[0], 0, argv)


    # gap 6
    def test_servers_keeps_env_distinct_stdio_apart(self):
        stdio = lambda token: {"command": "uvx", "args": ["slack-mcp"], "env": {"TOKEN": token}}  # noqa: E731
        self.servers = {"slack-a": stdio("secret-a"), "slack-b": stdio("secret-b"), "slack-c": stdio("secret-a")}
        self.write_servers()
        code, out, err = self.run_cli("--servers")
        self.assertEqual(code, 0, err)
        self.assertIn("STDIO (3) (2 unique, 3 total)", out)
        self.assertIn("aliases: slack-c", out)  # identical config (env too) still collapses
        self.assertNotIn("aliases: slack-b", out)
        self.assertIn("● slack-b", out)
        self.assertNotIn("secret", out)  # env is never printed

    # gap 11
    def test_piped_servers_full_alias_list(self):
        self.servers = {f"dup{i}": {"command": "same", "args": ["x"]} for i in range(8)}
        self.write_servers()
        out = self.run_cli("--servers")[1]
        self.assertIn("aliases: dup1, dup2, dup3, dup4, dup5, dup6, dup7\n", out)
        self.assertNotIn("more", out)

    def test_clear_cache_unknown(self):
        self.assertEqual(self.run_cli("--clear-cache", "nosuch"), (1, "", "No cache for 'nosuch'.\n"))
        # a configured server with no cache yet, and a leftover cache of a removed server, still clear fine
        self.assertEqual(self.run_cli("--clear-cache", "fake"), (0, "Cleared cache for fake.\n", ""))
        os.makedirs(os.path.join(self.home, ".mcp-cli", "cache"))
        gone = os.path.join(self.home, ".mcp-cli", "cache", "tools-gone.json")
        with open(gone, "w") as f:
            json.dump({"ts": 1, "tools": []}, f)
        self.assertEqual(self.run_cli("--clear-cache", "gone"), (0, "Cleared cache for gone.\n", ""))
        self.assertFalse(os.path.exists(gone))

    # gap 10
    def test_completion(self):
        def complete(*words):
            return self.run_cli(*words, env={"_MCP_CALL_COMPLETE": "1"})[1].split()
        http = sorted(n for n, c in self.servers.items() if "url" in c)
        self.assertEqual(complete("--servers", ""), ["-o", "--output"])
        self.assertEqual(complete("--list", ""), ["-o", "--output"])
        self.assertEqual(complete("fake", "--tools", ""), ["-o", "--output"])
        self.assertEqual(complete("--servers", "-o", ""), ["table", "json", "name"])
        self.assertEqual(complete("--login", "html200", ""), ["--no-browser"])
        self.assertEqual(complete("html200", "--login", ""), ["--no-browser"])
        self.assertEqual(complete("--login", "html200", "--no-browser", ""), [])
        self.assertEqual(sorted(complete("--login", "--no-browser", "")), http)
        self.assertEqual(sorted(complete("--login", "")), http)  # unchanged
        self.assertEqual(complete("fake", "--l"), ["--login", "--logout"])
        self.assertEqual(complete("fake", "echo", "--"), ["--help", "--schema", "--input-json"])  # unchanged (no cache)


if __name__ == "__main__":
    unittest.main()
