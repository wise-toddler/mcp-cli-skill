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
mcp-call --servers                              # list configured servers
mcp-call <server> --tools                       # discover tools (human-readable)
mcp-call <server> --discover                    # discover tools as JSON with schemas
mcp-call <server> <tool> --schema               # show tool's input schema as JSON
mcp-call <server> <tool> --key=value ...        # call a tool
mcp-call <server> <tool> --json '{"key":"val"}' # call with JSON args
echo '{}' | mcp-call <server> <tool>            # call with stdin JSON
```

## Server Management

Config stored at `~/.mcp-cli/servers.json`. On first run, auto-seeds from `~/.claude/settings.json` and `~/.claude.json`. Supports both **stdio** and **HTTP** MCP transports.

```bash
mcp-call --add myserver uvx some-mcp --env API_KEY=abc123
mcp-call --add-http myapi http://localhost:8010/mcp
mcp-call --remove myserver
mcp-call --sync    # re-sync from Claude configs
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

## Multi-tool workflow example

A bash script that an LLM agent can generate and run via its shell tool — querying a database, reading files, and posting to Slack, all orchestrated through `mcp-call`:

```bash
#!/bin/bash
# Agent-generated script: fetch github issues, read related files, post to slack

# 1. Fetch open bugs from github
mcp-call github list_issues \
  --owner=acme --repo=backend --state=open --labels=bug \
  | jq '.[] | {number, title}' > /tmp/bugs.json

# 2. Read the project README for context
mcp-call filesystem read_file \
  --path=/projects/backend/README.md > /tmp/readme.txt

# 3. Search for related error patterns in code
for title in $(jq -r '.[].title' /tmp/bugs.json | head -5); do
  mcp-call github search_code \
    --query="$title repo:acme/backend" \
    | jq '.items[:2]'
done > /tmp/code_matches.txt

# 4. Post summary to slack
mcp-call slack send_message \
  --channel="#engineering" \
  --text="*Open Bugs Summary*

$(jq length /tmp/bugs.json) open bugs:
$(jq -r '.[] | "• #\(.number): \(.title)"' /tmp/bugs.json)

Related code matches: /tmp/code_matches.txt"
```

The key insight: an LLM agent writes this script in one shot, runs it via its Bash/shell tool, and gets the result — no need to make 4+ separate MCP tool calls with inline data. The agent can read files, pipe between tools, and use shell logic that MCP tool calls alone can't do.

## Requirements

- Python 3.10+

## How it works

Reads MCP server config from `~/.mcp-cli/servers.json` (standalone, agent-agnostic). On first run, seeds from `~/.claude/settings.json` and `~/.claude.json`. For stdio servers, spawns the server as a subprocess and speaks JSON-RPC over stdin/stdout. For HTTP servers, sends JSON-RPC over HTTP with session ID tracking. Zero dependencies — pure Python stdlib.
