# mcp-cli

Agent skill to call any MCP server tool as a CLI command with shell composition support.

## Install

```bash
npx skills add wise-toddler/mcp-cli-skill -g
```

## Usage

```bash
# List configured MCP servers
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py --servers

# List tools for a server
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py <server> --tools

# Call a tool with --flag=value style args
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py <server> <tool> --key=value ...
```

## Server Management

Config stored at `~/.mcp-cli/servers.json`. On first run, auto-seeds from `~/.claude/settings.json`.

```bash
# Add a new MCP server
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py --add myserver uvx some-mcp --env API_KEY=abc123

# Add npx-based server
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py --add github npx @modelcontextprotocol/server-github --env GITHUB_TOKEN=ghp_xxx

# Remove a server
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py --remove myserver

# Re-sync new servers from ~/.claude/settings.json (merges, won't overwrite)
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py --sync
```

## Why?

MCP tool calls can't use shell composition. This skill lets agents (or you) call MCP tools via CLI with:

- File content as args: `--query="$(cat /tmp/query.sql)"`
- Pipe output: `| jq '.results'`
- Shell variables: `--name="$VAR"`
- Chaining: `cmd1 && cmd2`

## Examples

```bash
# SQL from file
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py redash redash_query \
  --action=adhoc --query="$(cat /tmp/q.sql)" --data_source_id=1

# Slack message from file
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py slack slack_chat \
  --action=post --channel=C123 --text="$(cat /tmp/msg.txt)"

# Pipe through jq
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py redash redash_query \
  --action=list --page_size=5 | jq '.results[].name'
```

## Requirements

- Python 3.10+
- MCP servers configured in `~/.claude/settings.json` or `~/.mcp-cli/servers.json`

## How it works

Reads MCP server config from `~/.mcp-cli/servers.json` (standalone, agent-agnostic). On first run, seeds from `~/.claude/settings.json`. Spawns the server as a subprocess, speaks JSON-RPC over stdio, and prints the result. Zero dependencies — pure Python stdlib.
