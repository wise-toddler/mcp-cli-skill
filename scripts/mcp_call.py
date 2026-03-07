#!/usr/bin/env python3
"""Call any MCP server tool from CLI with --flag=value args."""
import json
import os
import subprocess
import sys


def read_config():
    """Read MCP server config from Claude settings."""
    for path in [
        os.path.expanduser("~/.claude/settings.json"),
        os.path.expanduser("~/.claude/settings.local.json"),
    ]:
        if os.path.exists(path):
            with open(path) as f:
                servers = json.load(f).get("mcpServers", {})
                if servers:
                    return servers
    return {}


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
        sys.exit(0 if args else 1)

    if args[0] == "--servers":
        return "__servers__", None, {}

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
        stderr=subprocess.DEVNULL, text=True, env=env
    )


def init_server(proc):
    """Initialize MCP handshake."""
    send(proc, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "mcp-cli", "version": "1.0"}
    }, msg_id=1)
    recv(proc, expected_id=1)
    send(proc, "notifications/initialized")


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


def main():
    servers = read_config()
    server_name, tool_name, tool_args = parse_args()

    if server_name == "__servers__":
        list_servers(servers)
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
