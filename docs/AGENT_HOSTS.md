# Agent host setup

AgentWeb's CLI is the primary integration. A coding agent with shell access can
discover and call every installed adapter without loading hundreds of operation
schemas into its prompt.

Install it once for the current user:

```bash
curl -fsSL https://raw.githubusercontent.com/AnayGarodia/agentweb/main/install.sh | sh
```

Then verify that the same shell environment used by the agent can run:

```bash
agentweb sites
```

## Claude Code

Claude Code can call `agentweb` directly through its shell. The optional installer
adds a persistent AgentWeb instruction for it:

```bash
agentweb install-agent claude --scope user
```

The agent should follow `docs/AGENT_GUIDE.md`. A useful project instruction is:

```text
For supported websites, use the AgentWeb CLI. Start with `agentweb capabilities
DOMAIN --query TERM`, inspect one operation with `agentweb describe`, and then call
it. Attempt public operations before login. Ask me to run `agentweb connect DOMAIN`
only after authentication_required. Reconfirm writes after authorization.
```

## Codex

Codex can also call `agentweb` directly. Optional persistent setup:

```bash
agentweb install-agent codex --scope user
```

The same CLI sequence applies. Keep AgentWeb results as JSON rather than converting
them to screenshots or scraping them with regular expressions.

## Generic coding agents

If the host can run shell commands, no plugin is required. Verify that `agentweb`
is on `PATH`, give the host the short instruction above, and let it use `--help`,
`capabilities`, and `describe` for discovery.

## Optional MCP compatibility

For hosts that cannot call a CLI directly, AgentWeb exposes four stable MCP tools:

- `sites_list`
- `site_describe`
- `site_call`
- `site_connect`

Print a configuration snippet with:

```bash
agentweb mcp-config
```

The MCP surface intentionally stays small as more sites are installed. An agent
loads one operation contract only when it needs it.

## Installation checks

Run these in the same environment and user account as the agent host:

```bash
command -v agentweb
agentweb --version
agentweb sites
agentweb capabilities npmjs.com --query package
```

If a desktop app cannot find a command that works in your terminal, restart the
app after installation so it receives the updated `PATH`.

If `agentweb` is still not found, add the installer directory explicitly:

```bash
export PATH="$HOME/.local/bin:$PATH"
```
