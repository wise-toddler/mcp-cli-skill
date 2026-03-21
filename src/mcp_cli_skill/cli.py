#!/usr/bin/env python3
"""Call any MCP server tool from CLI with --flag=value args."""
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

CONFIG_DIR = os.path.expanduser("~/.mcp-cli")
CONFIG_PATH = os.path.join(CONFIG_DIR, "servers.json")
CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
CLAUDE_JSON = os.path.expanduser("~/.claude.json")


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


def parse_value(val):
    """Parse string value to appropriate type."""
    try:
        return json.loads(val)
    except (json.JSONDecodeError, ValueError):
        return val


def parse_args():
    """Parse CLI arguments into server, tool, and args dict."""
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: mcp-call <server> <tool> [--key=value ...] [--json '{...}']", file=sys.stderr)
        print("       mcp-call --servers", file=sys.stderr)
        print("       mcp-call <server> --tools", file=sys.stderr)
        print("       mcp-call <server> --discover", file=sys.stderr)
        print("       mcp-call <server> <tool> --schema", file=sys.stderr)
        print("       mcp-call --add <name> <command> [args...] [--env KEY=VAL ...]", file=sys.stderr)
        print("       mcp-call --add-http <name> <url>", file=sys.stderr)
        print("       mcp-call --remove <name>", file=sys.stderr)
        print("       mcp-call --sync", file=sys.stderr)
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
        elif arg == "--json" and i + 1 < len(args):
            tool_args.update(json.loads(args[i + 1]))
            i += 2
            continue
        elif arg.startswith("--json="):
            tool_args.update(json.loads(arg[7:]))
        elif arg.startswith("--") and "=" in arg:
            key, val = arg[2:].split("=", 1)
            tool_args[key] = parse_value(val)
        elif arg.startswith("--"):
            tool_args[arg[2:]] = True
        i += 1
    # read JSON from stdin if no args provided and stdin is piped
    if not tool_args and not sys.stdin.isatty():
        stdin_data = sys.stdin.read().strip()
        if stdin_data:
            tool_args = json.loads(stdin_data)
    return server, tool, tool_args


# --- HTTP transport ---

class HttpSession:
    """Manages HTTP MCP session with session ID tracking."""

    def __init__(self, url, extra_headers=None):
        self.url = url
        self.session_id = None
        self.extra_headers = extra_headers or {}

    def rpc(self, method, params=None, msg_id=1):
        """Send JSON-RPC over HTTP and return response."""
        msg = {"jsonrpc": "2.0", "method": method, "id": msg_id}
        if params:
            msg["params"] = params
        data = json.dumps(msg).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(self.extra_headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
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
        headers = {"Content-Type": "application/json"}
        headers.update(self.extra_headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=data, headers=headers)
        try:
            urllib.request.urlopen(req, timeout=10)
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
    for item in resp.get("result", {}).get("content", []):
        if item.get("type") == "text":
            try:
                print(json.dumps(json.loads(item["text"]), indent=2, default=str))
            except json.JSONDecodeError:
                print(item["text"])


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
    for _ in range(10):  # skip spurious responses (e.g. notification acks)
        line = proc.stdout.readline()
        if not line:
            return None
        resp = json.loads(line)
        if expected_id is None or resp.get("id") == expected_id:
            return resp
    return None


def spawn_server(config):
    """Spawn MCP server subprocess."""
    cmd = [config["command"]] + config.get("args", [])
    env = {**os.environ, **config.get("env", {})}
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
    for item in resp.get("result", {}).get("content", []):
        if item.get("type") == "text":
            try:
                print(json.dumps(json.loads(item["text"]), indent=2, default=str))
            except json.JSONDecodeError:
                print(item["text"])


# --- Tool discovery ---

def fetch_tools(config):
    """Fetch tools list from server (HTTP or stdio)."""
    if is_http(config):
        session = HttpSession(config["url"], config.get("headers"))
        http_init(session)
        resp = session.rpc("tools/list", {}, msg_id=2)
        if not resp or "result" not in resp:
            return []
        return resp["result"].get("tools", [])
    proc = spawn_server(config)
    try:
        init_server(proc)
        send(proc, "tools/list", {}, msg_id=2)
        resp = recv(proc, expected_id=2)
        if not resp or "result" not in resp:
            return []
        return resp["result"].get("tools", [])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _print_tools(tools):
    """Print tools in human-readable format."""
    for tool in tools:
        schema = tool.get("inputSchema", {})
        props = schema.get("properties", {})
        flags = " ".join(f"--{k}" for k in props)
        print(f"  {tool['name']:30s} {flags}")
        if tool.get("description"):
            print(f"    {tool['description']}")


# --- Server management ---

def is_http(config):
    """Check if server uses HTTP transport."""
    return config.get("type") == "http" or "url" in config


def list_servers(servers):
    """Print configured servers."""
    for name, cfg in servers.items():
        if is_http(cfg):
            print(f"  {name:20s} → {cfg['url']}  [http]")
        else:
            cmd = " ".join([cfg.get("command", "?")] + cfg.get("args", []))
            print(f"  {name:20s} → {cmd}  [stdio]")


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


# --- Main ---

def run_server(config, tool_name, tool_args):
    """Route to HTTP or stdio transport."""
    # tool discovery commands
    if tool_name in ("__tools__", "__discover__", "__schema__"):
        tools = fetch_tools(config)
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

    if server_name not in servers:
        print(f"Error: '{server_name}' not found. Available:", file=sys.stderr)
        list_servers(servers)
        sys.exit(1)

    run_server(servers[server_name], tool_name, tool_args)


if __name__ == "__main__":
    main()
