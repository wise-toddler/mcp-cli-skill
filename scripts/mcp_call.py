#!/usr/bin/env python3
"""Call any MCP server tool from CLI with --flag=value args."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import re
import shutil
import time
import urllib.parse
import urllib.request
import urllib.error

MCP_CALL_TMPDIR = os.path.join(tempfile.gettempdir(), "mcp-call")
CONFIG_DIR = os.path.expanduser("~/.mcp-cli")
CONFIG_PATH = os.path.join(CONFIG_DIR, "servers.json")
CACHE_DIR = os.path.join(CONFIG_DIR, "cache")
CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
CLAUDE_JSON = os.path.expanduser("~/.claude.json")

# CLI flags that don't take a positional server/tool — used for completion.
META_FLAGS = (
    "--servers", "--sync", "--add", "--add-http", "--remove",
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
        print("Usage: mcp-call <server> <tool> [--key=value ...] [--input-json '{...}']", file=sys.stderr)
        print("       mcp-call --servers", file=sys.stderr)
        print("       mcp-call <server> --tools", file=sys.stderr)
        print("       mcp-call <server> --discover", file=sys.stderr)
        print("       mcp-call <server> <tool> --help     (formatted help)", file=sys.stderr)
        print("       mcp-call <server> <tool> --schema   (raw JSON schema)", file=sys.stderr)
        print("       mcp-call --add <name> <command> [args...] [--env KEY=VAL ...]", file=sys.stderr)
        print("       mcp-call --add-http <name> <url>", file=sys.stderr)
        print("       mcp-call --remove <name>", file=sys.stderr)
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
            print("Usage: mcp-call --add-http <name> <url> [-H 'Key: Value' ...]", file=sys.stderr)
            sys.exit(1)
        add_args = {"url": args[2], "headers": {}}
        i = 3
        while i < len(args):
            if args[i] == "-H" and i + 1 < len(args):
                k, v = args[i + 1].split(":", 1)
                add_args["headers"][k.strip()] = v.strip()
                i += 2
            else:
                i += 1
        return "__add_http__", args[1], add_args
    if args[0] == "--remove":
        if len(args) < 2:
            print("Usage: mcp-call --remove <name>", file=sys.stderr)
            sys.exit(1)
        return "__remove__", args[1], {}
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
    if len(args) < 2 or args[1] == "--tools":
        return server, "__tools__", {}
    if args[1] == "--discover":
        return server, "__discover__", {}

    tool = args[1]
    tool_args = {}
    i = 2
    while i < len(args):
        arg = args[i]
        if arg == "--schema":
            return server, "__schema__", {"_tool": tool}
        elif arg in ("--help", "-h"):
            return server, "__help__", {"_tool": tool}
        elif arg == "--input-json" and i + 1 < len(args):
            tool_args.update(json.loads(args[i + 1]))
            i += 2
            continue
        elif arg.startswith("--input-json="):
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


def _expand_env(val):
    """Expand ${VAR} patterns in a string using env variables."""
    return re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), m.group(0)), val)


# --- HTTP transport ---

class HttpSession:
    """Manages HTTP MCP session with session ID tracking."""

    def __init__(self, url, extra_headers=None):
        self.url = _expand_env(url)
        self.session_id = None
        self.extra_headers = {k: _expand_env(v) for k, v in (extra_headers or {}).items()}

    def _send(self, data, headers, timeout=30, max_redirects=3):
        """POST data and follow 307/308 redirects preserving method+body.

        urllib's default HTTPRedirectHandler does NOT follow 307/308 on POST,
        only on GET/HEAD. We handle them explicitly here.
        """
        url = self.url
        for _ in range(max_redirects + 1):
            req = urllib.request.Request(url, data=data, headers=headers)
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code in (307, 308) and e.headers.get("Location"):
                    new_url = urllib.parse.urljoin(url, e.headers["Location"])
                    try:
                        e.close()
                    except Exception:
                        pass
                    url = new_url
                    self.url = url  # cache redirected URL for subsequent calls
                    continue
                raise
        raise urllib.error.HTTPError(url, 308, "Too many redirects", None, None)

    def rpc(self, method, params=None, msg_id=1):
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



def http_call_tool(url, tool_name, tool_args, extra_headers=None):
    """Call a tool on HTTP MCP server."""
    session = HttpSession(url, extra_headers)
    http_init(session)
    resp = session.rpc("tools/call", {"name": tool_name, "arguments": tool_args}, msg_id=3)
    if not resp:
        print("Error: no response", file=sys.stderr)
        sys.exit(1)
    if "error" in resp:
        print(json.dumps(resp["error"], indent=2), file=sys.stderr)
        sys.exit(1)
    _print_content(resp.get("result", {}).get("content", []))


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
    _print_content(resp.get("result", {}).get("content", []))


# --- Tool discovery ---

def fetch_tools(config, server_name=""):
    """Fetch tools list from server (HTTP or stdio), caching for completion."""
    tools = []
    if is_http(config):
        session = HttpSession(config["url"], config.get("headers"))
        http_init(session)
        resp = session.rpc("tools/list", {}, msg_id=2)
        if resp and "result" in resp:
            tools = resp["result"].get("tools", [])
    else:
        proc = spawn_server(config)
        try:
            init_server(proc)
            send(proc, "tools/list", {}, msg_id=2)
            resp = recv(proc, expected_id=2)
            if resp and "result" in resp:
                tools = resp["result"].get("tools", [])
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
    on = sys.stdout.isatty()
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
#compdef mcp-call mcp-cli-skill
_mcp_call_complete() {
    local -a completions
    local IFS=$'\n'
    completions=( $(_MCP_CALL_COMPLETE=1 mcp-call "${words[@]:1}" 2>/dev/null) )
    compadd -a completions
}
compdef _mcp_call_complete mcp-call
compdef _mcp_call_complete mcp-cli-skill
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
        http_call_tool(config["url"], tool_name, tool_args, config.get("headers"))
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
