#!/usr/bin/env python3
"""Call any MCP server tool from CLI with --flag=value args."""
import base64
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import re
import shutil
import time
import urllib.parse
import urllib.request
import urllib.error
import webbrowser

try:
    import fcntl
except ImportError:  # Windows: the token store runs without a lock
    fcntl = None

MCP_CALL_TMPDIR = os.path.join(tempfile.gettempdir(), "mcp-call")
CONFIG_DIR = os.path.expanduser("~/.mcp-cli")
CONFIG_PATH = os.path.join(CONFIG_DIR, "servers.json")
CACHE_DIR = os.path.join(CONFIG_DIR, "cache")
CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
CLAUDE_JSON = os.path.expanduser("~/.claude.json")
TOKENS_PATH = os.path.join(CONFIG_DIR, "tokens.json")  # OAuth tokens, mode 0600
LOGIN_TIMEOUT = int(os.environ.get("MCP_CALL_LOGIN_TIMEOUT") or 300)  # seconds --login waits for the browser
OAUTH_HTTP_TIMEOUT = 30  # every OAuth request gets an explicit timeout
EXIT_NEEDS_LOGIN = 4  # exit code when the user must run --login

# CLI flags that don't take a positional server/tool — used for completion.
META_FLAGS = (
    "--servers", "--sync", "--add", "--add-http", "--remove",
    "--login", "--logout",
    "--completion", "--refresh-completions", "--clear-cache",
    "--version", "--help",
)
# Flags valid after a server name (no tool yet).
SERVER_FLAGS = ("--tools", "--discover", "--help")
# Flags valid after a server + tool.
TOOL_FLAGS = ("--help", "--schema", "--input-json")


def _load_json(path):
    """Load JSON file if it exists."""
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _save_config(servers):
    """Save servers to standalone config."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(servers, f, indent=2)


def _make_http_entry(cfg):
    """Build HTTP server entry preserving headers."""
    entry = {"type": "http", "url": cfg["url"]}
    if cfg.get("headers"):
        entry["headers"] = cfg["headers"]
    return entry


def _collect_claude_servers():
    """Collect MCP servers from both settings.json and .claude.json."""
    servers = {}
    # settings.json — stdio servers
    for name, cfg in _load_json(CLAUDE_SETTINGS).get("mcpServers", {}).items():
        if "command" in cfg:
            servers[name] = cfg
        elif "url" in cfg:
            servers[name] = _make_http_entry(cfg)
    # .claude.json — root mcpServers + per-project servers
    claude_json = _load_json(CLAUDE_JSON)
    for name, cfg in claude_json.get("mcpServers", {}).items():
        if name not in servers:
            if "command" in cfg:
                servers[name] = cfg
            elif "url" in cfg:
                servers[name] = _make_http_entry(cfg)
    # per-project servers from .claude.json projects
    for proj_path, proj_cfg in claude_json.get("projects", claude_json).items():
        if not isinstance(proj_cfg, dict) or "mcpServers" not in proj_cfg:
            continue
        for name, cfg in proj_cfg["mcpServers"].items():
            if name not in servers:
                if "command" in cfg:
                    servers[name] = cfg
                elif "url" in cfg:
                    servers[name] = _make_http_entry(cfg)
    return servers


def read_config():
    """Read MCP servers, seeding from Claude configs on first run."""
    if os.path.exists(CONFIG_PATH):
        return _load_json(CONFIG_PATH)
    servers = _collect_claude_servers()
    if servers:
        _save_config(servers)
        print(f"Seeded {len(servers)} servers from Claude configs", file=sys.stderr)
    return servers


# --- Tools cache (powers shell completion) ---

def _cache_path(server):
    """Disk path for a server's cached tool list."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", server)
    return os.path.join(CACHE_DIR, f"tools-{safe}.json")


def _cache_write(server, tools):
    """Atomically persist a server's tool list. Never raises."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _cache_path(server)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ts": int(time.time()), "tools": tools}, f)
        os.replace(tmp, path)
    except OSError:
        pass  # cache writes are best-effort; never break the main flow


def _cache_read(server):
    """Return cached tools for a server (possibly stale), or empty list."""
    try:
        with open(_cache_path(server)) as f:
            return json.load(f).get("tools", [])
    except (OSError, json.JSONDecodeError):
        return []


def _cache_clear(server=None):
    """Remove cache for one server, or all if server is None."""
    if not os.path.isdir(CACHE_DIR):
        return
    if server:
        try:
            os.remove(_cache_path(server))
        except FileNotFoundError:
            pass
        return
    for name in os.listdir(CACHE_DIR):
        if name.startswith("tools-") and name.endswith(".json"):
            try:
                os.remove(os.path.join(CACHE_DIR, name))
            except OSError:
                pass


def parse_value(val):
    """Parse string value to appropriate type."""
    try:
        return json.loads(val)
    except (json.JSONDecodeError, ValueError):
        return val


def parse_args():
    """Parse CLI arguments into server, tool, and args dict."""
    args = sys.argv[1:]
    if args and args[0] == "--version":
        # __version__ lives in __init__.py, pyproject.toml reads from there
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "__init__.py")) as f:
            for line in f:
                if line.startswith("__version__"):
                    print(line.split("=")[1].strip().strip('"'))
                    break
        sys.exit(0)
    if not args or args[0] in ("-h", "--help"):
        print("Usage: mcp-call <server> <tool> [--key=value ...] [--input-json '{...}'] [-- <tool params verbatim>]", file=sys.stderr)
        print("       mcp-call --servers", file=sys.stderr)
        print("       mcp-call <server> --tools", file=sys.stderr)
        print("       mcp-call <server> --discover", file=sys.stderr)
        print("       mcp-call <server> <tool> --help     (formatted help)", file=sys.stderr)
        print("       mcp-call <server> <tool> --schema   (raw JSON schema)", file=sys.stderr)
        print("       mcp-call --add <name> <command> [args...] [--env KEY=VAL ...]", file=sys.stderr)
        print("       mcp-call --add-http <name> <url>", file=sys.stderr)
        print("       mcp-call --remove <name>", file=sys.stderr)
        print("       mcp-call --login <name>             (OAuth browser login for an HTTP server)", file=sys.stderr)
        print("       mcp-call --logout <name>            (forget stored OAuth tokens)", file=sys.stderr)
        print("       mcp-call --sync", file=sys.stderr)
        print("       mcp-call --completion <bash|zsh|fish>", file=sys.stderr)
        print("       mcp-call --refresh-completions      (cache tool lists for tab completion)", file=sys.stderr)
        print("       mcp-call --clear-cache [server]", file=sys.stderr)
        sys.exit(0 if args else 1)

    if args[0] == "--servers":
        return "__servers__", None, {}
    if args[0] == "--add":
        return "__add__", None, {"_raw": args[1:]}
    if args[0] == "--add-http":
        if len(args) < 3:
            print("Usage: mcp-call --add-http <name> <url> [-H|--header 'Key: Value' ...]", file=sys.stderr)
            sys.exit(1)
        add_args = {"url": args[2], "headers": {}}
        i = 3
        while i < len(args):
            a = args[i]
            if a in ("-H", "--header") and i + 1 < len(args):
                k, v = args[i + 1].split(":", 1)
                add_args["headers"][k.strip()] = v.strip()
                i += 2
            elif a.startswith("--header=") or a.startswith("-H="):
                _, v = a.split("=", 1)
                k, v = v.split(":", 1)
                add_args["headers"][k.strip()] = v.strip()
                i += 1
            else:
                print(f"Error: unknown flag {a!r} for --add-http. Use -H or --header 'Key: Value'.", file=sys.stderr)
                sys.exit(1)
        return "__add_http__", args[1], add_args
    if args[0] == "--remove":
        if len(args) < 2:
            print("Usage: mcp-call --remove <name>", file=sys.stderr)
            sys.exit(1)
        return "__remove__", args[1], {}
    if args[0] in ("--login", "--logout"):
        if len(args) < 2:
            print(f"Usage: mcp-call {args[0]} <name>", file=sys.stderr)
            sys.exit(1)
        return f"__{args[0][2:]}__", args[1], {}
    if args[0] == "--sync":
        return "__sync__", None, {}
    if args[0] == "--completion":
        shell = args[1] if len(args) > 1 else "bash"
        return "__completion__", shell, {}
    if args[0] == "--refresh-completions":
        return "__refresh_completions__", None, {}
    if args[0] == "--clear-cache":
        return "__clear_cache__", args[1] if len(args) > 1 else None, {}

    server = args[0]
    # `mcp-call <server> --login|--logout` alias the top-level flags
    if len(args) > 1 and args[1] in ("--login", "--logout"):
        return f"__{args[1][2:]}__", server, {}
    if len(args) < 2 or args[1] == "--tools":
        return server, "__tools__", {}
    if args[1] == "--discover":
        return server, "__discover__", {}

    tool = args[1]
    tool_args = {}
    i = 2
    verbatim = False  # after a bare `--`, everything is tool params, never meta-flags
    while i < len(args):
        arg = args[i]
        if not verbatim and arg == "--":
            verbatim = True
        elif not verbatim and arg == "--schema":
            return server, "__schema__", {"_tool": tool}
        elif not verbatim and arg in ("--help", "-h"):
            return server, "__help__", {"_tool": tool}
        elif not verbatim and arg == "--input-json" and i + 1 < len(args):
            tool_args.update(json.loads(args[i + 1]))
            i += 2
            continue
        elif not verbatim and arg.startswith("--input-json="):
            tool_args.update(json.loads(arg[13:]))
        elif arg.startswith("--") and "=" in arg:
            key, val = arg[2:].split("=", 1)
            tool_args[key] = parse_value(val)
        elif arg.startswith("--"):
            # --flag value (space-separated) or --flag (boolean)
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                tool_args[arg[2:]] = parse_value(args[i + 1])
                i += 2
                continue
            tool_args[arg[2:]] = True
        i += 1
    # read JSON from stdin if no args provided and stdin is piped
    if not tool_args and not sys.stdin.isatty():
        stdin_data = sys.stdin.read().strip()
        if stdin_data:
            tool_args = json.loads(stdin_data)
    return server, tool, tool_args


def _print_content(items):
    """Print MCP content blocks (text, image, etc.)."""
    for item in items:
        if item.get("type") == "text":
            try:
                print(json.dumps(json.loads(item["text"]), indent=2, default=str))
            except json.JSONDecodeError:
                print(item["text"])
        elif item.get("type") == "image":
            os.makedirs(MCP_CALL_TMPDIR, exist_ok=True)
            ext = item.get("mimeType", "image/png").split("/")[-1]
            fd, path = tempfile.mkstemp(suffix=f".{ext}", prefix="mcp-", dir=MCP_CALL_TMPDIR)
            os.write(fd, base64.b64decode(item["data"]))
            os.close(fd)
            print(path)
        elif item.get("type") == "resource_link":
            line = item.get("uri", "")
            if item.get("name"):
                line += f"  ({item['name']})"
            print(line)


def _print_result(result):
    """Print a tools/call result; exit 1 if the tool flagged an error."""
    content = result.get("content", [])
    _print_content(content)
    # structured-only results (empty content) would otherwise print nothing
    if not content and result.get("structuredContent") is not None:
        print(json.dumps(result["structuredContent"], indent=2, default=str))
    if result.get("isError"):
        sys.exit(1)


def _expand_env(val):
    """Expand ${VAR} patterns in a string using env variables."""
    return re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), m.group(0)), val)


# --- HTTP transport ---

class HttpSession:
    """Manages HTTP MCP session with session ID tracking."""

    def __init__(self, url, extra_headers=None, server_name=None):
        self.url = _expand_env(url)
        self.config_url = self.url  # pre-redirect URL; credentials stay on its origin
        self.session_id = None
        self.extra_headers = {k: _expand_env(v) for k, v in (extra_headers or {}).items()}
        self.server_name = server_name
        # a static Authorization header always wins over stored OAuth tokens
        self.static_auth = any(k.lower() == "authorization" for k in self.extra_headers)
        self._oauth_token = None  # bearer we sent from the token store
        if server_name and not self.static_auth:
            self.extra_headers.update(oauth_header(server_name, self.url))
            self._oauth_token = self.extra_headers.get("Authorization", "").removeprefix("Bearer ") or None

    def _send(self, data, headers, timeout=None, max_redirects=3):
        """POST data and follow 307/308 redirects preserving method+body.

        urllib's default HTTPRedirectHandler does NOT follow 307/308 on POST,
        only on GET/HEAD. We handle them explicitly here.
        """
        # Long-running tool calls (bash, LLM, etc) can exceed 30s; make it tunable.
        if timeout is None:
            timeout = int(os.environ.get("MCP_CALL_HTTP_TIMEOUT", "300"))
        url = self.url
        for _ in range(max_redirects + 1):
            if _origin(url) != _origin(self.config_url):
                # never forward credentials to another scheme/host/port
                headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
            req = urllib.request.Request(url, data=data, headers=headers)
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code in (307, 308) and e.headers.get("Location"):
                    new_url = urllib.parse.urljoin(url, e.headers["Location"])
                    if _origin(url)[0] == "https" and _origin(new_url)[0] != "https":
                        raise  # refuse https -> http downgrade
                    try:
                        e.close()
                    except Exception:
                        pass
                    url = new_url
                    self.url = url  # cache redirected URL for subsequent calls
                    continue
                raise
        raise urllib.error.HTTPError(url, 308, "Too many redirects", None, None)

    def rpc(self, method, params=None, msg_id=1, _retried=False):
        """Send JSON-RPC over HTTP and return response."""
        msg = {"jsonrpc": "2.0", "method": method, "id": msg_id}
        if params:
            msg["params"] = params
        data = json.dumps(msg).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "mcp-cli/1.0",
        }
        headers.update(self.extra_headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            with self._send(data, headers) as resp:
                # capture session ID from response
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                body = resp.read().decode()
                content_type = resp.headers.get("Content-Type", "")
                if "text/event-stream" in content_type:
                    return _parse_sse(body, msg_id)
                return json.loads(body)
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            # OAuth 401: refresh + retry once; decide on "did we send a token", not the 401's error code
            if e.code == 401 and self.server_name and not self.static_auth and _origin(self.url) == _origin(self.config_url):
                name = self.server_name
                if self._oauth_token and not _retried:
                    fresh = oauth_header(name, self.config_url, failed_token=self._oauth_token)
                    self.extra_headers.pop("Authorization", None)
                    self.extra_headers.update(fresh)
                    self._oauth_token = fresh.get("Authorization", "").removeprefix("Bearer ") or None
                    if self._oauth_token:
                        return self.rpc(method, params, msg_id, _retried=True)
                if self._oauth_token:
                    print(f"Error: {name} rejected a freshly refreshed token (HTTP 401). Ask the user to run: mcp-call --logout {name} && mcp-call --login {name}", file=sys.stderr)
                    sys.exit(EXIT_NEEDS_LOGIN)
                if "resource_metadata=" in e.headers.get("WWW-Authenticate", ""):
                    print(f"Error: {name} needs interactive browser login. Ask the user to run: mcp-call --login {name} (do not run it yourself; it opens a browser).", file=sys.stderr)
                    sys.exit(EXIT_NEEDS_LOGIN)
            print(f"Error: HTTP {e.code} from {self.url}", file=sys.stderr)
            if body.strip():
                # strip HTML, show first 200 chars
                clean = body.strip()
                if "<html" in clean.lower():
                    clean = "Server returned HTML error page (auth required?)"
                print(clean[:500], file=sys.stderr)
            sys.exit(1)
        except urllib.error.URLError as e:
            print(f"Error: cannot connect to {self.url}: {e.reason}", file=sys.stderr)
            sys.exit(1)
        except TimeoutError:
            # urlopen raises bare TimeoutError in Python 3.10+, not URLError.
            print(f"Error: request to {self.url} timed out. Set MCP_CALL_HTTP_TIMEOUT=<sec> for longer.", file=sys.stderr)
            sys.exit(1)

    def notify(self, method, params=None):
        """Send JSON-RPC notification (no id, ignore response)."""
        msg = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        data = json.dumps(msg).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "mcp-cli/1.0"}
        headers.update(self.extra_headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            self._send(data, headers, timeout=10)
        except Exception:
            pass


def _parse_sse(body, expected_id):
    """Parse SSE response and extract JSON-RPC message matching expected_id."""
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                msg = json.loads(line[6:])
                if msg.get("id") == expected_id:
                    return msg
            except json.JSONDecodeError:
                continue
    return None


def http_init(session):
    """Initialize HTTP MCP server."""
    session.rpc("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "mcp-cli", "version": "1.0"}
    }, msg_id=1)
    session.notify("notifications/initialized")



def http_call_tool(url, tool_name, tool_args, extra_headers=None, server_name=None):
    """Call a tool on HTTP MCP server."""
    session = HttpSession(url, extra_headers, server_name)
    http_init(session)
    resp = session.rpc("tools/call", {"name": tool_name, "arguments": tool_args}, msg_id=3)
    if not resp:
        print("Error: no response", file=sys.stderr)
        sys.exit(1)
    if "error" in resp:
        print(json.dumps(resp["error"], indent=2), file=sys.stderr)
        sys.exit(1)
    _print_result(resp.get("result", {}))


# --- OAuth ---
# Browser login via discovery + dynamic client registration + PKCE; tokens never printed.

_tokens_warned = False  # warn about a corrupt tokens.json once per run


def _die(msg, code=1):
    """Print msg to stderr and exit."""
    print(msg, file=sys.stderr)
    sys.exit(code)


def _clean(text):
    """Strip control chars from server-supplied text before printing it."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(text))[:300]


def _origin(url):
    """Return (scheme, host, port) of a URL for same-origin checks."""
    p = urllib.parse.urlsplit(url)
    return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)


def _norm_url(url):
    """Normalize an empty URL path to '/' so https://x and https://x/ compare equal."""
    p = urllib.parse.urlsplit(url)
    return p._replace(path=p.path or "/").geturl()


def _require_https(url):
    """Abort unless url is https (plain http only for loopback hosts, e.g. tests)."""
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" and not (p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost", "::1")):
        _die(f"Error: login failed: refusing non-HTTPS OAuth URL {_clean(url)!r}.")


def _flock(path, wait):
    """Open path and flock it exclusively within `wait` seconds; None on timeout."""
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    f = open(path, "a")
    deadline = time.time() + wait
    while fcntl:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() >= deadline:
                f.close()
                return None
            time.sleep(0.05)
    return f


def _tokens_lock():
    """Lock the token store for a read-modify-write; use as `with _tokens_lock():`."""
    f = _flock(TOKENS_PATH + ".lock", 10)
    if not f:
        _die(f"Error: timed out waiting for {TOKENS_PATH}.lock")
    return f


def _tokens_read():
    """Load tokens.json; if unreadable/corrupt, warn once and act as logged out."""
    global _tokens_warned
    if not os.path.exists(TOKENS_PATH):
        return {}
    try:
        with open(TOKENS_PATH) as f:
            store = json.load(f)
        if isinstance(store, dict) and all(isinstance(v, dict) for v in store.values()):
            return store
    except (OSError, ValueError):
        pass
    if not _tokens_warned:
        _tokens_warned = True
        print(f"Warning: ignoring unreadable {TOKENS_PATH}", file=sys.stderr)
    return {}


def _tokens_write(store):
    """Atomically replace tokens.json (0600); call while holding _tokens_lock()."""
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".tokens-")
    os.chmod(tmp, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(store, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, TOKENS_PATH)


def _tokens_put(name, entry):
    """Set (or with entry=None delete) one server's entry under the lock; return the old one."""
    with _tokens_lock():
        store = _tokens_read()
        old = store.pop(name, None)
        if entry is not None:
            store[name] = entry
        if old is not None or entry is not None:
            _tokens_write(store)
        return old


def _oauth_http(url, data=None, content_type="application/x-www-form-urlencoded"):
    """GET (or POST data to) an OAuth endpoint; return (status, JSON object or {})."""
    headers = {"User-Agent": "mcp-cli/1.0", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = content_type
    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=OAUTH_HTTP_TIMEOUT) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read()
    try:
        doc = json.loads(body)
    except ValueError:
        doc = {}
    return status, doc if isinstance(doc, dict) else {}


def _oauth_meta(urls):
    """Return the first metadata document that answers 200, else {}."""
    for u in urls:
        _require_https(u)
        status, doc = _oauth_http(u)
        if status == 200 and doc:
            return doc
    return {}


def oauth_header(name, url, failed_token=None):
    """Return {"Authorization": "Bearer …"} for a logged-in server (refreshing if needed), else {}."""
    def usable(e):
        # tokens are bound to the URL they were issued for (a changed URL never gets them)
        return bool(e and e.get("access_token")) and _norm_url(e.get("url", "")) == _norm_url(url)

    def fresh(e):
        # after a 401 any token other than the rejected one is new; otherwise trust expires_at
        return e["access_token"] != failed_token if failed_token else time.time() < e.get("expires_at", 0) - 60

    entry = _tokens_read().get(name)
    if not usable(entry):
        return {}
    if fresh(entry):
        return {"Authorization": f"Bearer {entry['access_token']}"}
    with _tokens_lock():
        store = _tokens_read()  # re-read: another mcp-call may have refreshed while we waited
        entry = store.get(name)
        if not usable(entry):
            return {}
        if fresh(entry):
            return {"Authorization": f"Bearer {entry['access_token']}"}
        if not entry.get("refresh_token"):
            return {}
        form = {"grant_type": "refresh_token", "refresh_token": entry["refresh_token"],
                "client_id": entry["client_id"], "resource": entry["resource"]}
        try:
            status, tok = _oauth_http(entry["token_endpoint"], urllib.parse.urlencode(form).encode())
        except OSError as e:  # URLError, TimeoutError, connection reset: keep tokens
            _die(f"Error: token refresh for {name} failed ({_clean(getattr(e, 'reason', e))}).")
        if tok.get("error") == "invalid_grant":
            # refresh token expired/revoked: drop tokens, keep client_id for the next --login
            entry.pop("access_token", None)
            entry.pop("refresh_token", None)
            _tokens_write(store)
            return {}
        if status != 200 or not tok.get("access_token"):
            _die(f"Error: token refresh for {name} failed (HTTP {status} {_clean(tok.get('error', ''))}".rstrip() + ").")
        entry["access_token"] = tok["access_token"]
        entry["refresh_token"] = tok.get("refresh_token") or entry["refresh_token"]  # RFC 6749 §6: may be omitted
        entry["expires_at"] = int(time.time()) + int(tok.get("expires_in") or 3600)
        _tokens_write(store)  # persist at once: refresh tokens may be single-use
        return {"Authorization": f"Bearer {entry['access_token']}"}


def oauth_logout(name):
    """Forget a server's stored OAuth entry locally (no server revocation); True if one existed."""
    return name in _tokens_read() and _tokens_put(name, None) is not None


def oauth_login(name, url):
    """Interactive browser login: discover, register client (DCR), PKCE authorize, store tokens."""
    # fail fast instead of racing a second login; held until this function returns
    lock = _flock(os.path.join(CONFIG_DIR, "login-" + re.sub(r"[^A-Za-z0-9._-]", "_", name) + ".lock"), 0)
    if not lock:
        _die(f"Error: login already in progress for '{name}'.")
    # 1. discover: an unauthenticated initialize's 401 points at the protected-resource metadata
    www_auth = ""
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "mcp-cli", "version": "1.0"}}}).encode()
    try:
        HttpSession(url)._send(init, {"Content-Type": "application/json", "User-Agent": "mcp-cli/1.0",
                                      "Accept": "application/json, text/event-stream"}, timeout=OAUTH_HTTP_TIMEOUT).close()
    except urllib.error.HTTPError as e:
        www_auth = e.headers.get("WWW-Authenticate", "") if e.headers else ""
    m = re.search(r'resource_metadata="([^"]+)"', www_auth)
    p = urllib.parse.urlsplit(url)
    well_known = f"{p.scheme}://{p.netloc}/.well-known/oauth-protected-resource"  # RFC 9728, no trailing slash
    prm = _oauth_meta([m.group(1)] if m else list(dict.fromkeys([well_known + p.path.rstrip("/"), well_known])))
    if not prm:
        _die(f"Error: login failed: {name} doesn't advertise OAuth (no protected-resource metadata).")
    resource = prm.get("resource", "")
    if _norm_url(resource) != _norm_url(url):  # RFC 9728 §3.3: metadata for another resource could phish tokens
        _die(f"Error: login failed: metadata is for resource {_clean(resource)!r}, not {url}.")
    issuer = (prm.get("authorization_servers") or [""])[0]
    _require_https(issuer)
    ip = urllib.parse.urlsplit(issuer)
    meta = _oauth_meta([f"{ip.scheme}://{ip.netloc}/.well-known/{doc}{ip.path.rstrip('/')}"
                        for doc in ("oauth-authorization-server", "openid-configuration")])
    if meta.get("issuer") != issuer:  # RFC 8414 §3.3: exact match
        _die(f"Error: login failed: authorization server metadata doesn't match issuer {_clean(issuer)!r}.")
    if "S256" not in meta.get("code_challenge_methods_supported", []) or not meta.get("registration_endpoint"):
        _die("Error: server doesn't support dynamic client registration; use --header with a static token.")
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        _require_https(meta.get(key, ""))
    m = re.search(r'(?:^|[\s,])scope="([^"]*)"', www_auth)
    scope = m.group(1) if m else " ".join(prm.get("scopes_supported", []))

    # 2. loopback callback server; the first valid callback wins
    state, verifier = secrets.token_urlsafe(16), secrets.token_urlsafe(48)
    outcome, done = [], threading.Event()

    class Callback(http.server.BaseHTTPRequestHandler):
        timeout = 10  # idle browser preconnects can't block the server

        def log_message(self, *args):
            pass  # the request line carries the auth code

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            if u.path != "/callback":
                return self._reply(404, "Not found.")
            # a stray local request with the wrong state must not cancel the login
            if not secrets.compare_digest(q.get("state", "").encode(), state.encode()):
                return self._reply(400, "Invalid state.")
            if q.get("error"):
                outcome.append(("error", _clean(f"{q['error']} {q.get('error_description', '')}").strip()))
            elif q.get("iss", issuer) != issuer:  # RFC 9207 mix-up defense
                outcome.append(("error", "callback came from a different issuer"))
            elif q.get("code"):
                outcome.append(("code", q["code"]))
            else:
                return self._reply(400, "Missing code.")
            done.set()
            self._reply(200, "mcp-call login finished. You can close this tab and return to the terminal.")

        def _reply(self, status, text):
            body = f"<!doctype html><meta charset=utf-8><title>mcp-call</title><p>{text}</p>".encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Callback)
    redirect_uri = f"http://127.0.0.1:{httpd.server_address[1]}/callback"  # host fixed, port may vary
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        # 3. client_id: reuse ours for this issuer, else register one (DCR)
        entry = {"url": url, "resource": resource, "issuer": issuer, "scope": scope,
                 "authorization_endpoint": meta["authorization_endpoint"], "token_endpoint": meta["token_endpoint"]}
        old = _tokens_read().get(name) or {}
        entry["client_id"] = old.get("client_id") if old.get("issuer") == issuer else None
        if not entry["client_id"]:
            reg = {"client_name": "mcp-call", "redirect_uris": [redirect_uri],
                   "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                   "token_endpoint_auth_method": "none"}
            if scope:
                reg["scope"] = scope
            status, doc = _oauth_http(meta["registration_endpoint"], json.dumps(reg).encode(), "application/json")
            entry["client_id"] = doc.get("client_id") if 200 <= status < 300 else None
            if not entry["client_id"]:
                _die(f"Error: login failed: client registration returned HTTP {status}.")
            _tokens_put(name, entry)  # persist now so a killed login leaves no orphan registration
        # 4. PKCE authorize; the browser must open the URL itself (server sets a device cookie)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"response_type": "code", "client_id": entry["client_id"], "redirect_uri": redirect_uri,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "resource": resource}
        if scope:
            params["scope"] = scope
        auth = meta["authorization_endpoint"]
        auth_url = auth + ("&" if urllib.parse.urlsplit(auth).query else "?") + urllib.parse.urlencode(params)
        print(f"Opening the browser to log in to {name}. If it doesn't open, visit:\n{auth_url}", file=sys.stderr)
        # over SSH / headless Linux, webbrowser falls back to a blocking text browser
        headless = sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        if not os.environ.get("SSH_CONNECTION") and not headless:
            webbrowser.open(auth_url)
        if not done.wait(LOGIN_TIMEOUT):
            _die(f"Error: no callback received in {LOGIN_TIMEOUT}s. If the browser showed an error, run `mcp-call --logout {name}` and retry.")
    finally:
        httpd.shutdown()
        httpd.server_close()
    kind, value = outcome[0]
    if kind == "error":
        _die(f"Error: login failed: {value}")
    # 5. exchange the code for tokens
    form = {"grant_type": "authorization_code", "code": value, "redirect_uri": redirect_uri,
            "client_id": entry["client_id"], "code_verifier": verifier, "resource": resource}
    status, tok = _oauth_http(entry["token_endpoint"], urllib.parse.urlencode(form).encode())
    if status != 200 or not tok.get("access_token") or str(tok.get("token_type", "")).lower() != "bearer":
        _die(f"Error: login failed: token exchange returned HTTP {status} {_clean(tok.get('error', ''))}".rstrip() + ".")
    entry.update(access_token=tok["access_token"], refresh_token=tok.get("refresh_token"),
                 expires_at=int(time.time()) + int(tok.get("expires_in") or 3600))
    _tokens_put(name, entry)
    print(f"Logged in to {name}.")


# --- Stdio transport ---

def send(proc, method, params=None, msg_id=None):
    """Send JSON-RPC message via stdio."""
    msg = {"jsonrpc": "2.0", "method": method}
    if params:
        msg["params"] = params
    if msg_id is not None:
        msg["id"] = msg_id
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def recv(proc, expected_id=None):
    """Read JSON-RPC response via stdio, optionally matching by id."""
    non_json = []
    for _ in range(50):
        line = proc.stdout.readline()
        if not line:
            break
        try:
            resp = json.loads(line)
        except json.JSONDecodeError:
            non_json.append(line.rstrip())
            continue
        if expected_id is None or resp.get("id") == expected_id:
            return resp
    # no valid JSON-RPC response — show what server actually said
    if non_json:
        print("\n".join(non_json))
    return None


def spawn_server(config):
    """Spawn MCP server subprocess."""
    cmd = [_expand_env(config["command"])] + [_expand_env(a) for a in config.get("args", [])]
    env = {**os.environ, **{k: _expand_env(v) for k, v in config.get("env", {}).items()}}
    return subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=env
    )


def check_alive(proc):
    """Check if server process is still running, print stderr if dead."""
    if proc.poll() is not None:
        stderr = proc.stderr.read() if proc.stderr else ""
        print(f"Error: server exited with code {proc.returncode}", file=sys.stderr)
        if stderr.strip():
            print(stderr.strip(), file=sys.stderr)
        sys.exit(1)


def init_server(proc):
    """Initialize stdio MCP handshake."""
    check_alive(proc)
    try:
        send(proc, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-cli", "version": "1.0"}
        }, msg_id=1)
        resp = recv(proc, expected_id=1)
        if not resp:
            check_alive(proc)
            print("Error: no response from server during init", file=sys.stderr)
            sys.exit(1)
        send(proc, "notifications/initialized")
    except BrokenPipeError:
        check_alive(proc)
        print("Error: server crashed during init", file=sys.stderr)
        sys.exit(1)



def stdio_call_tool(proc, tool_name, tool_args):
    """Call a tool on stdio server."""
    send(proc, "tools/call", {"name": tool_name, "arguments": tool_args}, msg_id=3)
    resp = recv(proc, expected_id=3)
    if not resp:
        print("Error: no response", file=sys.stderr)
        sys.exit(1)
    if "error" in resp:
        print(json.dumps(resp["error"], indent=2), file=sys.stderr)
        sys.exit(1)
    _print_result(resp.get("result", {}))


# --- Tool discovery ---

def fetch_tools(config, server_name=""):
    """Fetch tools list from server (HTTP or stdio), caching for completion."""
    tools = []
    if is_http(config):
        session = HttpSession(config["url"], config.get("headers"), server_name)
        http_init(session)
        cursor, msg_id = None, 2
        while True:
            params = {"cursor": cursor} if cursor else {}
            resp = session.rpc("tools/list", params, msg_id=msg_id)
            if not resp or "result" not in resp:
                break
            tools += resp["result"].get("tools", [])
            cursor = resp["result"].get("nextCursor")
            if not cursor:
                break
            msg_id += 1
    else:
        proc = spawn_server(config)
        try:
            init_server(proc)
            cursor, msg_id = None, 2
            while True:
                params = {"cursor": cursor} if cursor else {}
                send(proc, "tools/list", params, msg_id=msg_id)
                resp = recv(proc, expected_id=msg_id)
                if not resp or "result" not in resp:
                    break
                tools += resp["result"].get("tools", [])
                cursor = resp["result"].get("nextCursor")
                if not cursor:
                    break
                msg_id += 1
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    if server_name and tools:
        _cache_write(server_name, tools)
    return tools


def _colors():
    """Return ANSI color codes if stdout is a TTY, else empty strings."""
    on = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    return {
        "bold": "\033[1m" if on else "",
        "dim": "\033[2m" if on else "",
        "red": "\033[31m" if on else "",
        "green": "\033[32m" if on else "",
        "yellow": "\033[33m" if on else "",
        "blue": "\033[34m" if on else "",
        "magenta": "\033[35m" if on else "",
        "cyan": "\033[36m" if on else "",
        "reset": "\033[0m" if on else "",
    }


def _print_tools(tools):
    """Print tools in human-readable format with colors on a TTY."""
    c = _colors()
    print(f"{c['dim']}{len(tools)} tools  ({c['yellow']}*{c['dim']} = required){c['reset']}\n")
    for tool in tools:
        schema = tool.get("inputSchema", {})
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        # required flags marked with *, sorted required-first
        flags = []
        for k in sorted(props, key=lambda x: x not in required):
            mark = f"{c['yellow']}*{c['reset']}" if k in required else ""
            col = c["yellow"] if k in required else c["dim"]
            flags.append(f"{col}--{k}{c['reset']}{mark}")
        print(f"  {c['cyan']}{c['bold']}{tool['name']}{c['reset']}")
        # only the summary line — skip verbose "Args:" docstring section
        desc = (tool.get("description") or "").strip().split("\n")[0]
        if desc:
            print(f"    {c['dim']}{desc}{c['reset']}")
        if flags:
            print(f"    {' '.join(flags)}")
        print()


def _print_tool_help(server_name, tool):
    """Print formatted help for a single tool — usage, params, example."""
    c = _colors()
    schema = tool.get("inputSchema", {})
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    # header
    print(f"\n{c['bold']}{c['cyan']}{tool['name']}{c['reset']}  {c['dim']}({server_name}){c['reset']}")
    desc = (tool.get("description") or "").strip()
    if desc:
        print()
        for line in desc.split("\n"):
            print(f"  {c['dim']}{line}{c['reset']}")
    # usage
    print(f"\n{c['bold']}Usage:{c['reset']}")
    req_part = " ".join(f"{c['yellow']}--{k}=<{c['reset']}{c['dim']}{props.get(k, {}).get('type', 'value')}{c['reset']}{c['yellow']}>{c['reset']}" for k in sorted(required))
    print(f"  mcp-call {server_name} {tool['name']} {req_part}".rstrip())
    # required args
    if required:
        print(f"\n{c['bold']}Required:{c['reset']}")
        for k in sorted(required):
            _print_arg(k, props.get(k, {}), c, required=True)
    # optional args
    optional = [k for k in props if k not in required]
    if optional:
        print(f"\n{c['bold']}Optional:{c['reset']}")
        for k in sorted(optional):
            _print_arg(k, props.get(k, {}), c, required=False)
    print()


def _print_arg(name, prop, c, required):
    """Print one argument's signature + description."""
    t = prop.get("type", "any")
    enum = prop.get("enum")
    type_str = f"{'|'.join(map(str, enum))}" if enum else t
    flag_col = c["yellow"] if required else c["reset"]
    mark = f"{c['yellow']}*{c['reset']}" if required else ""
    print(f"  {flag_col}--{name}{c['reset']}{mark} {c['dim']}<{type_str}>{c['reset']}")
    desc = prop.get("description", "").strip()
    if desc:
        for line in desc.split("\n"):
            print(f"      {c['dim']}{line}{c['reset']}")


# --- Server management ---

def is_http(config):
    """Check if server uses HTTP transport."""
    return config.get("type") == "http" or "url" in config


def _config_key(cfg):
    """Hashable identity for a server config — used to collapse duplicates."""
    if is_http(cfg):
        return ("http", cfg["url"])
    return ("stdio", cfg.get("command", "?"), tuple(cfg.get("args", [])))


def _truncate(text, width):
    """Truncate text to width with an ellipsis if it doesn't fit."""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def list_servers(servers):
    """Print configured servers grouped by transport, collapsing duplicates."""
    c = _colors()
    term_w = shutil.get_terminal_size((100, 24)).columns
    # split + group by config identity
    http_groups, stdio_groups = {}, {}
    for name, cfg in servers.items():
        bucket = http_groups if is_http(cfg) else stdio_groups
        bucket.setdefault(_config_key(cfg), []).append(name)
    max_name = min(max((len(n) for n in servers), default=20), 28)

    def print_group(label, color, groups, target_fn):
        total = sum(len(v) for v in groups.values())
        unique = len(groups)
        suffix = "" if unique == total else f" {c['dim']}({unique} unique, {total} total){c['reset']}"
        print(f"\n{c['bold']}{color}{label}{c['reset']} {c['dim']}({total}){c['reset']}{suffix}\n")
        # sort by first name in each group, alphabetical
        for key, names in sorted(groups.items(), key=lambda kv: kv[1][0].lower()):
            names_sorted = sorted(names)
            primary = names_sorted[0]
            target = target_fn(key)
            # compute remaining width for the target
            base = f"  ● {primary:<{max_name}}  "
            visible_len = len(base)
            avail = max(20, term_w - visible_len - 4)
            target_disp = _truncate(target, avail)
            line = f"  {c['green']}●{c['reset']} {c['bold']}{primary:<{max_name}}{c['reset']}  {c['dim']}{target_disp}{c['reset']}"
            if len(names_sorted) > 1:
                line += f"  {c['yellow']}×{len(names_sorted)}{c['reset']}"
            print(line)
            # show extra aliases under primary, indented
            if len(names_sorted) > 1:
                aliases = ", ".join(names_sorted[1:6])
                more = f" +{len(names_sorted) - 6} more" if len(names_sorted) > 6 else ""
                print(f"    {c['dim']}aliases: {aliases}{more}{c['reset']}")

    if http_groups:
        print_group("HTTP", c["cyan"], http_groups, lambda k: k[1])
    if stdio_groups:
        print_group("STDIO", c["magenta"], stdio_groups,
                    lambda k: (k[1] + (" " + " ".join(k[2]) if k[2] else "")))
    print()


def add_server(raw_args):
    """Add a new stdio MCP server."""
    if len(raw_args) < 2:
        print("Usage: --add <name> <command> [args...] [--env KEY=VAL ...]", file=sys.stderr)
        sys.exit(1)
    name = raw_args[0]
    command = raw_args[1]
    cmd_args = []
    env = {}
    i = 2
    while i < len(raw_args):
        if raw_args[i] == "--env" and i + 1 < len(raw_args):
            k, v = raw_args[i + 1].split("=", 1)
            env[k] = v
            i += 2
        else:
            cmd_args.append(raw_args[i])
            i += 1
    servers = read_config()
    entry = {"command": command}
    if cmd_args:
        entry["args"] = cmd_args
    if env:
        entry["env"] = env
    servers[name] = entry
    _save_config(servers)
    print(f"Added server '{name}': {command} {' '.join(cmd_args)}")


def add_http_server(name, url, headers=None):
    """Add a new HTTP MCP server."""
    servers = read_config()
    if name in servers:
        oauth_logout(name)  # a re-added server must not inherit old tokens
    entry = {"type": "http", "url": url}
    if headers:
        entry["headers"] = headers
    servers[name] = entry
    _save_config(servers)
    print(f"Added HTTP server '{name}': {url}")


def remove_server(name):
    """Remove an MCP server."""
    servers = read_config()
    if name not in servers:
        print(f"Error: '{name}' not found.", file=sys.stderr)
        sys.exit(1)
    del servers[name]
    _save_config(servers)
    oauth_logout(name)
    print(f"Removed server '{name}'")


def sync_from_claude():
    """Re-sync servers from Claude configs (merges, doesn't overwrite)."""
    claude_servers = _collect_claude_servers()
    current = read_config()
    added = 0
    for name, cfg in claude_servers.items():
        if name not in current:
            current[name] = cfg
            added += 1
    _save_config(current)
    print(f"Synced: {added} new servers added, {len(current)} total")


def refresh_completions():
    """Fetch tools/list from every configured server and cache results."""
    servers = read_config()
    ok, fail = 0, 0
    for name, cfg in servers.items():
        try:
            tools = fetch_tools(cfg, name)
            if tools:
                ok += 1
                print(f"  ✓ {name}: {len(tools)} tools cached")
            else:
                fail += 1
                print(f"  · {name}: no tools returned", file=sys.stderr)
        except SystemExit:
            # fetch_tools may sys.exit on auth errors; catch to keep going
            fail += 1
            print(f"  ✗ {name}: failed", file=sys.stderr)
        except Exception as e:
            fail += 1
            print(f"  ✗ {name}: {e}", file=sys.stderr)
    print(f"\nCached {ok} servers ({fail} failed)")


# --- Shell completion ---

# Bash hook — passes all prior words + the current word as the last arg.
_BASH_COMPLETION = r"""
_mcp_call_complete() {
    local cur="${COMP_WORDS[COMP_CWORD]}"
    local prior=("${COMP_WORDS[@]:1:COMP_CWORD-1}")
    local IFS=$'\n'
    COMPREPLY=( $(_MCP_CALL_COMPLETE=1 mcp-call "${prior[@]}" "$cur" 2>/dev/null) )
}
complete -F _mcp_call_complete mcp-call
complete -F _mcp_call_complete mcp-cli-skill
""".strip()

_ZSH_COMPLETION = r"""
# Ensure zsh completion system is loaded (safe to run more than once).
if ! type compdef >/dev/null 2>&1; then
    autoload -U +X compinit && compinit -u 2>/dev/null
fi
_mcp_call_complete() {
    local -a completions
    local IFS=$'\n'
    completions=( ${(f)"$(_MCP_CALL_COMPLETE=1 mcp-call "${words[@]:1}" 2>/dev/null)"} )
    compadd -a completions
}
compdef _mcp_call_complete mcp-call 2>/dev/null
compdef _mcp_call_complete mcp-cli-skill 2>/dev/null
""".strip()

_FISH_COMPLETION = r"""
function __mcp_call_complete
    set -l cmd (commandline -opc) (commandline -ct)
    _MCP_CALL_COMPLETE=1 mcp-call $cmd[2..-1] 2>/dev/null
end
complete -c mcp-call -f -a "(__mcp_call_complete)"
complete -c mcp-cli-skill -f -a "(__mcp_call_complete)"
""".strip()


def print_completion_script(shell):
    """Print the shell hook the user should source/eval."""
    scripts = {"bash": _BASH_COMPLETION, "zsh": _ZSH_COMPLETION, "fish": _FISH_COMPLETION}
    if shell not in scripts:
        print(f"Error: unsupported shell '{shell}'. Use bash, zsh, or fish.", file=sys.stderr)
        sys.exit(1)
    print(scripts[shell])


def _completion_candidates(prior, partial):
    """Return candidate completions given the prior args and the partial word.

    Position is determined by *non-flag* prior args:
      - 0 positionals  -> server names + top-level meta flags
      - 1 positional   -> tool names for that server + server-level flags
      - >=2 positionals -> flag names for that tool from cached schema

    Special-cased meta flags that expect a specific kind of next arg
    short-circuit the positional logic.
    """
    # Meta flags whose immediate next arg has a fixed shape — suggest
    # only those, not random server/tool names.
    if prior == ["--remove"] or prior == ["--clear-cache"]:
        return list(_load_json(CONFIG_PATH).keys())
    if prior in (["--login"], ["--logout"]):
        return [n for n, cfg in _load_json(CONFIG_PATH).items() if is_http(cfg)]
    if prior == ["--completion"]:
        return ["bash", "zsh", "fish"]
    # --add / --add-http take free-form command/URL/headers — nothing useful to suggest.
    if prior and prior[0] in ("--add", "--add-http"):
        return []

    positional = [a for a in prior if not a.startswith("-")]

    if not positional:
        servers = _load_json(CONFIG_PATH)
        return list(servers.keys()) + list(META_FLAGS)

    server = positional[0]
    if len(positional) == 1:
        tools = _cache_read(server)
        return [t["name"] for t in tools if t.get("name")] + list(SERVER_FLAGS)

    tool_name = positional[1]
    tools = _cache_read(server)
    for t in tools:
        if t.get("name") == tool_name:
            props = (t.get("inputSchema") or {}).get("properties") or {}
            return [f"--{k}" for k in props] + list(TOOL_FLAGS)
    return list(TOOL_FLAGS)


def do_completion():
    """Print newline-separated completion candidates matching the partial word."""
    # The shell hook appends the partial (possibly empty) as the last arg.
    args = sys.argv[1:]
    if not args:
        partial, prior = "", []
    else:
        partial, prior = args[-1], args[:-1]
    try:
        for cand in _completion_candidates(prior, partial):
            if cand.startswith(partial):
                print(cand)
    except Exception:
        # Never let completion errors leak into the user's terminal.
        pass


# --- Main ---

def run_server(config, tool_name, tool_args, server_name=""):
    """Route to HTTP or stdio transport."""
    # tool discovery commands
    if tool_name in ("__tools__", "__discover__", "__schema__", "__help__"):
        tools = fetch_tools(config, server_name)
        if tool_name == "__tools__":
            _print_tools(tools)
        elif tool_name == "__discover__":
            out = [{"name": t["name"], "description": t.get("description", ""),
                     "inputSchema": t.get("inputSchema", {})} for t in tools]
            print(json.dumps(out, indent=2))
        elif tool_name == "__schema__":
            target = tool_args["_tool"]
            for t in tools:
                if t["name"] == target:
                    print(json.dumps(t.get("inputSchema", {}), indent=2))
                    return
            print(f"Error: tool '{target}' not found", file=sys.stderr)
            sys.exit(1)
        elif tool_name == "__help__":
            target = tool_args["_tool"]
            for t in tools:
                if t["name"] == target:
                    _print_tool_help(server_name or "<server>", t)
                    return
            print(f"Error: tool '{target}' not found", file=sys.stderr)
            sys.exit(1)
        return
    # tool calls
    if is_http(config):
        http_call_tool(config["url"], tool_name, tool_args, config.get("headers"), server_name)
    else:
        proc = spawn_server(config)
        try:
            init_server(proc)
            stdio_call_tool(proc, tool_name, tool_args)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def main():
    # Shell completion is the fast path — runs on every TAB. Handle it
    # before read_config() (which can print "Seeded..." to stderr).
    if os.environ.get("_MCP_CALL_COMPLETE"):
        do_completion()
        return

    servers = read_config()
    server_name, tool_name, tool_args = parse_args()

    if server_name == "__servers__":
        list_servers(servers)
        return
    if server_name == "__add__":
        add_server(tool_args["_raw"])
        return
    if server_name == "__add_http__":
        add_http_server(tool_name, tool_args["url"], tool_args.get("headers"))
        return
    if server_name == "__remove__":
        remove_server(tool_name)
        return
    if server_name == "__login__":
        cfg = servers.get(tool_name, {})
        if not is_http(cfg):
            print(f"Error: '{tool_name}' is not a configured HTTP server.", file=sys.stderr)
            sys.exit(1)
        if any(k.lower() == "authorization" for k in cfg.get("headers") or {}):
            # the static header would always win over OAuth tokens
            print(f"Error: '{tool_name}' has a static Authorization header; re-add it without one to use --login.", file=sys.stderr)
            sys.exit(1)
        try:
            oauth_login(tool_name, _expand_env(cfg["url"]))
        except KeyboardInterrupt:
            print("\nLogin cancelled.", file=sys.stderr)
            sys.exit(130)
        except OSError as e:  # network errors during discovery/registration/exchange
            print(f"Error: login failed: {_clean(getattr(e, 'reason', e))}", file=sys.stderr)
            sys.exit(1)
        return
    if server_name == "__logout__":
        print(f"Logged out of {tool_name}." if oauth_logout(tool_name) else f"Not logged in to {tool_name}.")
        return
    if server_name == "__sync__":
        sync_from_claude()
        return
    if server_name == "__completion__":
        print_completion_script(tool_name)
        return
    if server_name == "__refresh_completions__":
        refresh_completions()
        return
    if server_name == "__clear_cache__":
        _cache_clear(tool_name)
        print(f"Cleared cache{' for ' + tool_name if tool_name else ''}.")
        return

    if server_name not in servers:
        print(f"Error: '{server_name}' not found. Available:", file=sys.stderr)
        list_servers(servers)
        sys.exit(1)

    run_server(servers[server_name], tool_name, tool_args, server_name)


if __name__ == "__main__":
    main()
