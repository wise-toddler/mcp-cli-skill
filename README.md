# mcp-cli

Call any MCP server tool from the command line with shell composition support.

## Install

```bash
# As a CLI tool (recommended)
pipx install mcp-cli-skill

# Or run directly without installing
uvx mcp-cli-skill --servers

# As a Claude Code skill
npx skills add wise-toddler/mcp-cli-skill -g
```

## Usage

```bash
mcp-call --servers                          # list configured servers
mcp-call <server> --tools                   # discover tools
mcp-call <server> <tool> --key=value ...    # call a tool
```

## Server Management

Config stored at `~/.mcp-cli/servers.json`. On first run, auto-seeds from `~/.claude/settings.json`.

```bash
mcp-call --add myserver uvx some-mcp --env API_KEY=abc123
mcp-call --remove myserver
mcp-call --sync    # re-sync from ~/.claude/settings.json
```

## Why?

MCP tool calls can't use shell composition. This CLI lets agents (or you) use:

- File content as args: `--query="$(cat /tmp/query.sql)"`
- Pipe output: `| jq '.results'`
- Shell variables: `--name="$VAR"`
- Chaining: `cmd1 && cmd2`

## Examples

```bash
mcp-call redash redash_query \
  --action=adhoc --query="$(cat /tmp/q.sql)" --data_source_id=1

mcp-call slack slack_chat \
  --action=post --channel=C123 --text="$(cat /tmp/msg.txt)"

mcp-call redash redash_query \
  --action=list --page_size=5 | jq '.results[].name'
```

## Requirements

- Python 3.10+

## How it works

Reads MCP server config from `~/.mcp-cli/servers.json` (standalone, agent-agnostic). On first run, seeds from `~/.claude/settings.json`. Spawns the server as a subprocess, speaks JSON-RPC over stdio, prints the result. Zero dependencies — pure Python stdlib.
