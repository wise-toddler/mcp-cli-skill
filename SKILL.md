---
name: mcp-cli
description: Call any MCP server tool as a CLI command with shell composition. Use when tool args come from files, pipes, or shell commands. Trigger on "mcp-call", "call mcp from cli", or when MCP tool content is in a file.
---

# MCP CLI

Call any configured MCP server tool from the command line with `--flag=value` style args and full shell composition support.

## Commands

```bash
# List configured MCP servers
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py --servers

# List tools for a server
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py <server> --tools

# Call a tool
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py <server> <tool> --key=value ...

# Add a new MCP server
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py --add <name> <command> [args...] [--env KEY=VAL ...]

# Remove an MCP server
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py --remove <name>
```

## Server Management

Config stored at `~/.mcp-cli/servers.json`. On first run, seeds from `~/.claude/settings.json`.

```bash
# Add a server
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py --add myredash uvx redash-mcp --env REDASH_URL=http://localhost --env REDASH_API_KEY=abc123

# Add npx-based server
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py --add github npx @modelcontextprotocol/server-github --env GITHUB_TOKEN=ghp_xxx

# Remove a server
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py --remove myredash

# Re-sync new servers from ~/.claude/settings.json (merges, won't overwrite existing)
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py --sync
```

## Examples

```bash
# Run SQL from a file
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py redash redash_query --action=adhoc --query="$(cat /tmp/query.sql)" --data_source_id=1

# List queries piped through jq
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py redash redash_query --action=list --page_size=5 | jq '.results[].name'

# Send slack message from file content
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py slack slack_chat --action=post --channel=C123 --text="$(cat /tmp/msg.txt)"

# Export query result to file
python3 ~/.claude/skills/mcp-cli/scripts/mcp_call.py redash redash_query --action=run --id=42 | jq '.query_result.data.rows' > /tmp/result.json
```

## When to Use

Prefer this over MCP tool calls when:
- Content comes from a file: `--arg="$(cat file)"`
- Output needs piping: `| jq`, `> file`, `| grep`
- Shell variable expansion needed: `--arg="$VAR"`
- Chaining multiple calls in one command

## Arg Types

Values auto-parse: `--id=42` → int, `--flag=true` → bool, `--name=hello` → string.
