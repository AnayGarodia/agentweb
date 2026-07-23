# Agent host setup

AgentWeb's CLI is the primary integration. A coding agent with shell access can
discover and call every installed adapter without loading hundreds of operation
schemas into its prompt.

Install it once for the current user:

```bash
curl -fsSL https://github.com/AnayGarodia/agentweb/raw/refs/heads/main/install.sh | sh
```

The installer detects Claude Code and Codex, registers AgentWeb with each one, and
performs website setup automatically. Restart the coding agent, then ask a normal
question such as:

```text
Use AgentWeb to find the latest version of React on npm.
```

## Claude Code

The main installer connects Claude Code automatically when `claude` is on `PATH`.
If it was installed afterward or detection failed, connect it manually:

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

The main installer also connects Codex automatically when `codex` is on `PATH`.
Manual fallback:

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
