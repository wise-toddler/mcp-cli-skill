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
- MCP servers configured in `~/.claude/settings.json`

## How it works

Reads MCP server config from `~/.claude/settings.json`, spawns the server as a subprocess, speaks JSON-RPC over stdio, and prints the result. Zero dependencies — pure Python stdlib.
