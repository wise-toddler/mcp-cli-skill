#!/usr/bin/env python3
"""Call any MCP server tool from CLI with --flag=value args."""
import json
import os
import subprocess
import sys

CONFIG_DIR = os.path.expanduser("~/.mcp-cli")
CONFIG_PATH = os.path.join(CONFIG_DIR, "servers.json")
CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")


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


def read_config():
    """Read MCP servers, seeding from Claude settings on first run."""
    if os.path.exists(CONFIG_PATH):
        return _load_json(CONFIG_PATH)
    # first run: seed from ~/.claude/settings.json
    servers = _load_json(CLAUDE_SETTINGS).get("mcpServers", {})
    if servers:
        _save_config(servers)
        print(f"Seeded {len(servers)} servers from {CLAUDE_SETTINGS}", file=sys.stderr)
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
        print("Usage: mcp_call.py <server> <tool> [--key=value ...]", file=sys.stderr)
        print("       mcp_call.py --servers", file=sys.stderr)
        print("       mcp_call.py <server> --tools", file=sys.stderr)
        print("       mcp_call.py --add <name> <command> [args...] [--env KEY=VAL ...]", file=sys.stderr)
        print("       mcp_call.py --remove <name>", file=sys.stderr)
        print("       mcp_call.py --sync", file=sys.stderr)
        sys.exit(0 if args else 1)

    if args[0] == "--servers":
        return "__servers__", None, {}
    if args[0] == "--add":
        return "__add__", None, {"_raw": args[1:]}
    if args[0] == "--remove":
        if len(args) < 2:
            print("Usage: mcp_call.py --remove <name>", file=sys.stderr)
            sys.exit(1)
        return "__remove__", args[1], {}
    if args[0] == "--sync":
        return "__sync__", None, {}

    server = args[0]
    if len(args) < 2 or args[1] == "--tools":
        return server, "__tools__", {}

    tool = args[1]
    tool_args = {}
    for arg in args[2:]:
        if arg.startswith("--") and "=" in arg:
            key, val = arg[2:].split("=", 1)
            tool_args[key] = parse_value(val)
        elif arg.startswith("--"):
            tool_args[arg[2:]] = True
    return server, tool, tool_args


def send(proc, method, params=None, msg_id=None):
    """Send JSON-RPC message."""
    msg = {"jsonrpc": "2.0", "method": method}
    if params:
        msg["params"] = params
    if msg_id is not None:
        msg["id"] = msg_id
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def recv(proc, expected_id=None):
    """Read JSON-RPC response, optionally matching by id."""
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
    """Initialize MCP handshake."""
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


def list_servers(servers):
    """Print configured servers."""
    for name, cfg in servers.items():
        cmd = " ".join([cfg["command"]] + cfg.get("args", []))
        print(f"  {name:20s} → {cmd}")


def list_tools(proc):
    """List tools from server."""
    send(proc, "tools/list", {}, msg_id=2)
    resp = recv(proc, expected_id=2)
    if not resp or "result" not in resp:
        return
    for tool in resp["result"].get("tools", []):
        schema = tool.get("inputSchema", {})
        props = schema.get("properties", {})
        flags = " ".join(f"--{k}" for k in props)
        print(f"  {tool['name']:30s} {flags}")
        if tool.get("description"):
            print(f"    {tool['description']}")


def call_tool(proc, tool_name, tool_args):
    """Call a tool and print result."""
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


def add_server(raw_args):
    """Add a new MCP server."""
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
    """Re-sync servers from ~/.claude/settings.json (merges, doesn't overwrite)."""
    claude_servers = _load_json(CLAUDE_SETTINGS).get("mcpServers", {})
    current = read_config()
    added = 0
    for name, cfg in claude_servers.items():
        if name not in current:
            current[name] = cfg
            added += 1
    _save_config(current)
    print(f"Synced: {added} new servers added, {len(current)} total")


def main():
    servers = read_config()
    server_name, tool_name, tool_args = parse_args()

    if server_name == "__servers__":
        list_servers(servers)
        return
    if server_name == "__add__":
        add_server(tool_args["_raw"])
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

    proc = spawn_server(servers[server_name])
    try:
        init_server(proc)
        if tool_name == "__tools__":
            list_tools(proc)
        else:
            call_tool(proc, tool_name, tool_args)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
