"""OAuth login/refresh tests against a fake authorization server + MCP endpoint (stdlib unittest)."""
import base64
import hashlib
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

# Config paths resolve at import time: point HOME at a temp dir BEFORE importing cli.
HOME = tempfile.mkdtemp(prefix="mcp-cli-test-")
os.environ["HOME"] = HOME
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ.pop("SSH_CONNECTION", None)  # else --login skips webbrowser.open
os.environ.setdefault("DISPLAY", ":0")  # same, on headless Linux
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, SRC)

from mcp_cli_skill import cli  # noqa: E402

assert cli.CONFIG_DIR.startswith(HOME), "tests must never touch the real ~/.mcp-cli"


def tearDownModule():
    shutil.rmtree(HOME, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    """Fake AS (/register, /authorize, /token) + MCP endpoints /mcp (OAuth), /static, /plain."""

    def log_message(self, *args):
        pass

    def _send(self, status, doc=None, headers=None):
        body = json.dumps(doc).encode() if doc is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        s = self.server
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/.well-known/oauth-protected-resource/mcp":
            return self._send(200, {"resource": s.resource, "authorization_servers": [s.base],
                                    "scopes_supported": ["mcp"]})
        if u.path == "/.well-known/oauth-authorization-server":
            return self._send(200, {"issuer": s.base, "authorization_endpoint": s.base + "/authorize",
                                    "token_endpoint": s.base + "/token", "registration_endpoint": s.base + "/register",
                                    "code_challenge_methods_supported": ["S256"]})
        if u.path == "/authorize":
            if s.authorize_status:  # error rendered in the browser, never reaches the callback
                return self._send(s.authorize_status, {"error": "invalid_request"})
            uri = q.get("redirect_uri", "")
            ok = (q.get("client_id") in s.clients and uri.startswith("http://127.0.0.1:") and uri.endswith("/callback")
                  and q.get("code_challenge_method") == "S256" and q.get("resource") == s.resource
                  and q.get("response_type") == "code")
            if not ok:
                return self._send(400, {"error": "invalid_request"})
            code = "code-" + secrets.token_hex(8)
            with s.lock:
                s.codes[code] = (q["client_id"], uri, q["code_challenge"])
                s.issued.append(code)
            loc = uri + "?" + urllib.parse.urlencode({"code": code, "state": q["state"], "iss": s.base})
            return self._send(302, headers={"Location": loc})
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        s = self.server
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        path = urllib.parse.urlsplit(self.path).path
        if path == "/register":
            reg = json.loads(body)
            with s.lock:
                s.registers += 1
                cid = f"client-{s.registers}"
                s.clients[cid] = reg["redirect_uris"]
            return self._send(201, {"client_id": cid, **reg})
        if path == "/token":
            f = dict(urllib.parse.parse_qsl(body.decode()))
            with s.lock:
                if f.get("resource") != s.resource:
                    return self._send(400, {"error": "invalid_target"})
                if f.get("grant_type") == "authorization_code":
                    cid, uri, chal = s.codes.pop(f.get("code"), (None, None, None))
                    want = base64.urlsafe_b64encode(hashlib.sha256(f.get("code_verifier", "").encode()).digest())
                    if cid != f.get("client_id") or uri != f.get("redirect_uri") or chal != want.rstrip(b"=").decode():
                        return self._send(400, {"error": "invalid_grant"})
                    return self._send(200, s.issue())
                if f.get("grant_type") == "refresh_token":
                    s.refreshes += 1
                    if s.refresh_status:
                        return self._send(s.refresh_status, {"error": "temporarily_unavailable"})
                    if f.get("refresh_token") not in s.refresh:
                        return self._send(400, {"error": "invalid_grant"})
                    s.refresh.discard(f["refresh_token"])  # single-use, rotating
                    return self._send(200, s.issue())
            return self._send(400, {"error": "unsupported_grant_type"})
        auth = self.headers.get("Authorization")
        if path in ("/mcp", "/static"):
            with s.lock:
                s.mcp_auth.append(auth)
            token = (auth or "").removeprefix("Bearer ")
            if token not in (s.access if path == "/mcp" else {"static-ok"}):
                challenge = (f'Bearer error="invalid_token", resource_metadata='
                             f'"{s.base}/.well-known/oauth-protected-resource/mcp", scope="mcp"')
                return self._send(401, {"error": "invalid_token"}, {"WWW-Authenticate": challenge})
        elif path != "/plain":
            return self._send(404, {"error": "not_found"})
        msg = json.loads(body)
        if "id" not in msg:
            return self._send(202)
        if msg["method"] == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
        elif msg["method"] == "tools/list":
            result = {"tools": [{"name": "echo", "description": "Echo text",
                                 "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}]}
        else:
            result = {"content": [{"type": "text", "text": msg["params"]["arguments"].get("text", "")}]}
        self._send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": result})


class FakeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server_address[1]}"
        self.reset()

    def reset(self):
        """Forget all clients, codes and tokens; clear failure knobs."""
        self.lock = threading.Lock()
        self.clients, self.codes, self.issued = {}, {}, []
        self.access, self.refresh = set(), set()
        self.registers = self.refreshes = 0
        self.authorize_status = None
        self.refresh_status = None
        self.resource = self.base + "/mcp"
        self.mcp_auth = []

    def issue(self):
        """Mint a valid access/refresh token pair."""
        at, rt = "at-" + secrets.token_hex(8), "rt-" + secrets.token_hex(8)
        self.access.add(at)
        self.refresh.add(rt)
        return {"access_token": at, "refresh_token": rt, "token_type": "Bearer", "expires_in": 3600}


class OAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeServer()
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()
        cls.fake.server_close()

    def setUp(self):
        self.fake.reset()
        shutil.rmtree(cli.CONFIG_DIR, ignore_errors=True)
        b = self.fake.base
        self.servers = {
            "fake": {"type": "http", "url": b + "/mcp"},
            "plain": {"type": "http", "url": b + "/plain"},
            "static": {"type": "http", "url": b + "/static", "headers": {"Authorization": "Bearer wrong"}},
        }
        cli._save_config(self.servers)
        cli._tokens_warned = False
        self.browser = self.open_in_thread
        for p in (mock.patch.object(cli, "LOGIN_TIMEOUT", 5),
                  mock.patch("webbrowser.open", side_effect=lambda url: self.browser(url))):
            p.start()
            self.addCleanup(p.stop)

    def open_in_thread(self, url):
        """Fake browser: follow the authorize redirect from a daemon thread (a sync call would deadlock)."""
        def go():
            try:
                urllib.request.urlopen(url, timeout=5).close()
            except (urllib.error.URLError, OSError):
                pass
        threading.Thread(target=go, daemon=True).start()
        return True

    def run_cli(self, *args, err=None):
        """Run mcp-call in-process; return (exit code, stdout, stderr)."""
        out, err, code = io.StringIO(), err or io.StringIO(), 0
        with mock.patch.object(sys, "argv", ["mcp-call", *args]), mock.patch.object(sys, "stdin", io.StringIO("")), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue(), err.getvalue()

    def tokens(self):
        with open(cli.TOKENS_PATH) as f:
            return json.load(f)

    def seed(self, expired=False, access=None, refresh=None):
        """Write a logged-in 'fake' entry; tokens are valid on the fake server unless overridden."""
        b, tok = self.fake.base, self.fake.issue()
        entry = {"url": b + "/mcp", "resource": b + "/mcp", "issuer": b, "scope": "mcp",
                 "authorization_endpoint": b + "/authorize", "token_endpoint": b + "/token", "client_id": "client-0",
                 "access_token": access or tok["access_token"], "refresh_token": refresh or tok["refresh_token"],
                 "expires_at": int(time.time()) + (-100 if expired else 3600)}
        os.makedirs(cli.CONFIG_DIR, exist_ok=True)
        with open(cli.TOKENS_PATH, "w") as f:
            json.dump({"fake": entry}, f)
        return entry

    def test_01_login_end_to_end(self):
        code, out, err = self.run_cli("--login", "fake")
        self.assertEqual(code, 0, err)
        self.assertIn("Logged in to fake.", out)
        self.assertEqual(os.stat(cli.TOKENS_PATH).st_mode & 0o777, 0o600)
        entry = self.tokens()["fake"]
        code, out2, err2 = self.run_cli("fake", "--tools")
        self.assertEqual(code, 0, err2)
        self.assertIn("echo", out2)
        self.assertEqual(self.fake.mcp_auth[-1], "Bearer " + entry["access_token"])
        for secret in [entry["access_token"], entry["refresh_token"], *self.fake.issued]:
            self.assertNotIn(secret, out + err + out2 + err2)

    def test_02_second_login_reuses_client_id(self):
        self.assertEqual(self.run_cli("--login", "fake")[0], 0)
        first = self.tokens()["fake"]["client_id"]
        self.assertEqual(self.run_cli("fake", "--login")[0], 0)  # alias form
        self.assertEqual(self.fake.registers, 1)
        self.assertEqual(self.tokens()["fake"]["client_id"], first)

    def test_03_proactive_refresh_persists_rotated_token(self):
        old = self.seed(expired=True)
        code, out, err = self.run_cli("fake", "--tools")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.refreshes, 1)
        new = self.tokens()["fake"]
        self.assertNotEqual(new["refresh_token"], old["refresh_token"])
        self.assertIn(new["refresh_token"], self.fake.refresh)
        self.assertGreater(new["expires_at"], time.time() + 3000)

    def test_04_401_refresh_retry(self):
        self.seed(access="revoked-at")  # unexpired locally, rejected by the server
        code, out, err = self.run_cli("fake", "echo", "--text=hi")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "hi")
        self.assertEqual(self.fake.refreshes, 1)
        self.assertEqual(self.fake.mcp_auth[:2], ["Bearer revoked-at", "Bearer " + self.tokens()["fake"]["access_token"]])

    def test_05_parallel_refresh_single_token_call(self):
        self.seed(expired=True)
        env = {**os.environ, "HOME": HOME, "PYTHONPATH": SRC}
        procs = [subprocess.Popen([sys.executable, "-m", "mcp_cli_skill.cli", "fake", "--tools"], env=env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(5)]
        for p in procs:
            out, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, err)
            self.assertIn("echo", out)
        self.assertEqual(self.fake.refreshes, 1)

    def test_06_invalid_grant_drops_tokens_5xx_keeps_them(self):
        self.seed(expired=True, refresh="rt-bogus")
        code, _, err = self.run_cli("fake", "--tools")
        self.assertEqual(code, cli.EXIT_NEEDS_LOGIN, err)
        self.assertIn("needs interactive browser login", err)
        entry = self.tokens()["fake"]
        self.assertEqual(entry["client_id"], "client-0")
        self.assertNotIn("access_token", entry)
        self.assertNotIn("refresh_token", entry)

        seeded = self.seed(expired=True)
        self.fake.refresh_status = 503
        code, _, err = self.run_cli("fake", "--tools")
        self.assertEqual(code, 1)
        self.assertIn("token refresh for fake failed (HTTP 503", err)
        self.assertNotIn("--login", err)
        self.assertEqual(self.tokens()["fake"], seeded)

    def test_07_not_logged_in_exits_4(self):
        code, _, err = self.run_cli("fake", "--tools")
        self.assertEqual(code, 4)
        self.assertIn("needs interactive browser login", err)
        self.assertIn("mcp-call --login fake", err)

    def test_08_static_header_401_never_mentions_login(self):
        code, _, err = self.run_cli("static", "--tools")
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401", err)
        self.assertNotIn("--login", err)
        self.assertEqual(self.run_cli("--login", "static")[0], 1)  # static header would always win

    def test_09_corrupt_tokens_file_plain_server_works(self):
        os.makedirs(cli.CONFIG_DIR, exist_ok=True)
        with open(cli.TOKENS_PATH, "w") as f:
            f.write("{not json")
        code, out, err = self.run_cli("plain", "--tools")
        self.assertEqual(code, 0, err)
        self.assertIn("echo", out)
        self.assertEqual(err.count("Warning"), 1)

    def test_10_authorize_error_times_out_without_tokens(self):
        self.fake.authorize_status = 400
        with mock.patch.object(cli, "LOGIN_TIMEOUT", 2):
            start = time.time()
            code, _, err = self.run_cli("--login", "fake")
        self.assertEqual(code, 1)
        self.assertLess(time.time() - start, 5)
        self.assertIn("no callback received in 2s", err)
        self.assertNotIn("access_token", self.tokens()["fake"])  # client_id persisted, no tokens

    def test_11_wrong_state_rejected_login_keeps_waiting(self):
        statuses = []

        def browser(url):
            def go():
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
                try:
                    urllib.request.urlopen(q["redirect_uri"] + "?code=evil&state=wrong", timeout=5)
                except urllib.error.HTTPError as e:
                    statuses.append(e.code)
                urllib.request.urlopen(url, timeout=5).close()
            threading.Thread(target=go, daemon=True).start()
        self.browser = browser
        code, out, err = self.run_cli("--login", "fake")
        self.assertEqual(code, 0, err)
        self.assertEqual(statuses, [400])
        self.assertIn("Logged in to fake.", out)

    def test_12_changed_url_never_gets_token(self):
        self.assertEqual(self.run_cli("--login", "fake")[0], 0)
        self.servers["fake"]["url"] = self.fake.base.replace("127.0.0.1", "localhost") + "/mcp"
        cli._save_config(self.servers)
        self.fake.mcp_auth.clear()
        code, _, err = self.run_cli("fake", "--tools")
        self.assertEqual(code, 4, err)
        self.assertEqual(self.fake.mcp_auth, [None])

    def test_13_resource_mismatch_aborts_login(self):
        self.fake.resource = "https://evil.example/mcp"
        code, _, err = self.run_cli("--login", "fake")
        self.assertEqual(code, 1)
        self.assertIn("metadata is for resource", err)
        self.assertEqual(self.fake.registers, 0)
        self.assertFalse(os.path.exists(cli.TOKENS_PATH))

    def test_14_no_browser_prints_url_only(self):
        opened = []
        self.browser = opened.append  # webbrowser.open must not be called
        test = self

        class User(io.StringIO):
            """Fake user: opens the printed authorize URL in 'another browser profile'."""
            def write(self, text):
                for word in text.split():
                    if "/authorize?" in word:
                        test.open_in_thread(word)
                return super().write(text)

        code, out, err = self.run_cli("fake", "--login", "--no-browser", err=User())
        self.assertEqual(code, 0, err)
        self.assertEqual(opened, [])
        self.assertIn("browser profile", err)
        self.assertIn("Logged in to fake.", out)


if __name__ == "__main__":
    unittest.main()
