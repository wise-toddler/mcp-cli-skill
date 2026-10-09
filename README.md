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
mcp-call <server> <tool> --input-json '{"k":"v"}' # call with JSON args
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

## OAuth servers

HTTP servers that use MCP OAuth (dynamic client registration + PKCE) need a one-time browser login:

```bash
mcp-call --add-http emergent https://mcp.emergent.sh/
mcp-call --login emergent     # opens the browser; approve, then "Logged in to emergent."
mcp-call emergent --tools     # bearer token is sent and refreshed automatically
mcp-call --logout emergent    # forget the local tokens (no server-side revocation)
```

Multiple accounts on one server: add it under one name per account, and log in with `--no-browser`, which only prints the URL so you can open it in the browser profile signed in to that account (the default browser may silently approve as whoever is already signed in):

```bash
mcp-call --add-http emergent-work https://mcp.emergent.sh/
mcp-call --login emergent-work --no-browser
```

Tokens live in `~/.mcp-cli/tokens.json` (mode 0600), bound to the server's URL. A call to a server that needs login exits with code 4 and says which `--login` to run. `--login` waits up to `MCP_CALL_LOGIN_TIMEOUT` seconds (default 300) for the browser; over SSH it prints the URL instead of opening a browser. A static `Authorization` header (`--header`) always wins over OAuth.

## Shell completion

Tab completion suggests server names, tool names, and flag names.

### One-time setup (required)

The shell needs to know how to ask `mcp-call` for completions. Same one-time eval pattern as `gh`, `kubectl`, `aws`:

```bash
# bash — add to ~/.bashrc
eval "$(mcp-call --completion bash)"

# zsh — add to ~/.zshrc
eval "$(mcp-call --completion zsh)"

# fish — write once
mcp-call --completion fish > ~/.config/fish/completions/mcp-call.fish
```

After this, `mcp-call <TAB>` immediately suggests server names (read live from `~/.mcp-cli/servers.json` — no cache involved).

### Tool-level completion (optional refresh)

`mcp-call <server> <TAB>` and `mcp-call <server> <tool> --<TAB>` read from a disk cache at `~/.mcp-cli/cache/tools-<server>.json`. The cache exists because fetching a server's tool list takes 200ms–1s (subprocess spawn or HTTP roundtrip) — too slow for TAB.

The cache populates **automatically** whenever you run `--tools`, `--discover`, `--help`, or `--schema`. So tool completion "just works" for any server you've used.

For instant completion on every server up front:

```bash
mcp-call --refresh-completions   # walks every configured server, caches its tools
mcp-call --clear-cache [server]  # bust a stale entry, or all entries
```

### Environment variables

`${VAR}` patterns in URLs, headers, command args, and env values are expanded at runtime:

```json
{
  "myapi": {
    "type": "http",
    "url": "https://${API_HOST}/mcp",
    "headers": { "X-API-Key": "${MY_API_KEY}" }
  }
}
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

## Releasing

Bump `__version__` in `src/mcp_cli_skill/__init__.py`, sync `scripts/mcp_call.py`, commit, then push a matching tag:

```bash
git tag v0.8.5 && git push origin v0.8.5
```

CI runs the tests, checks the tag matches `__version__`, and publishes to PyPI via Trusted Publishing (no token).
