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

    def run_cli(self, *args, env=None, stdin=None, script=None):
        """Run mcp-call (or a given script) in a subprocess with an isolated HOME; return (exit code, stdout, stderr)."""
        e = {k: v for k, v in os.environ.items() if not k.startswith("MCP_CALL") and k != "_MCP_CALL_COMPLETE"}
        e.update(HOME=self.home, TMPDIR=self.home, PYTHONPATH=os.path.join(REPO, "src"), NO_COLOR="1",
                 no_proxy="127.0.0.1,localhost", NO_PROXY="127.0.0.1,localhost",
                 SSH_CONNECTION="test 1 test 2")  # belt and braces: --login never opens a browser here
        e.update(env or {})
        cmd = [sys.executable, script] if script else [sys.executable, "-m", "mcp_cli_skill.cli"]
        kw = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        p = subprocess.run([*cmd, *args], env=e, cwd=self.home, capture_output=True, text=True, timeout=30, **kw)
        return p.returncode, p.stdout, p.stderr

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


if __name__ == "__main__":
    unittest.main()
