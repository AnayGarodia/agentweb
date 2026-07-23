# AgentWeb documentation

You do not need to read every document.

## I want to use AgentWeb

Start with the [main README](../README.md), then use the
[agent guide](AGENT_GUIDE.md) when an agent needs exact rules for login, errors,
retries, workflows, and writes.

For Claude Code, Codex, or optional MCP setup, read [agent setup](AGENT_HOSTS.md).

## I want to understand or change the code

Read [architecture](ARCHITECTURE.md), then the repository's
[AGENTS.md](../AGENTS.md) for invariants and verification commands.

## I want to add a website

Read [building an adapter](BUILDING_ADAPTERS.md) and
[security and trust](SECURITY.md). Start with a public, read-only operation before
adding login or writes.

## I found a problem

Use a GitHub bug report for normal defects. Use the private security-advisory flow
for credential exposure, cross-profile access, request-allowlist escape,
signature bypass, or unintended remote writes. See [the security policy](../SECURITY.md).
