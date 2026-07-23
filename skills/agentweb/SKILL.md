---
name: agentweb
description: Use browserless, typed website actions through the AgentWeb CLI and return bounded structured JSON instead of clicking through pages. Use for searching or acting on Amazon, arXiv, GitHub, GST, Hacker News, Hugging Face, LinkedIn, npm, Spotify, Stack Overflow, Wikipedia, or any site reported by AgentWeb; also use when a task mentions website automation, direct website APIs, saved login sessions, CAPTCHA handoff, or replacing browser automation.
license: Apache-2.0
compatibility: Requires macOS or Linux, network access, and a shell-capable agent. The AgentWeb installer provisions an isolated Python when Python 3.11+ is unavailable.
metadata:
  author: AnayGarodia
  homepage: https://github.com/AnayGarodia/agentweb
---

# AgentWeb

Use AgentWeb for a website task when it has a mapped operation. Prefer its typed,
browserless command over screenshots, DOM inspection, and repeated page parsing.

## Prepare the runtime

First check whether `agentweb` is available:

```bash
command -v agentweb
```

If it is missing, tell the user that the skill is installed but the AgentWeb
runtime is not. When the user asks to install it or approves installation, inspect
the installer and then run:

```bash
curl -fsSL https://github.com/AnayGarodia/agentweb/raw/refs/heads/main/install.sh | sh
```

Restart the agent host after installation so its generated global skill and CLI
path are available to new sessions.

## Discover before acting

Use this sequence instead of guessing commands:

```bash
agentweb sites
agentweb capabilities DOMAIN --query WORD
agentweb describe DOMAIN --operation ACTION
agentweb DOMAIN ACTION [arguments]
```

Normal website URLs can be passed where a domain is accepted. Read the operation's
declared gaps and risk metadata before promising coverage or taking action.

## Authentication and human handoff

Try public operations without login. If an operation returns
`authentication_required`, run:

```bash
agentweb connect DOMAIN
```

Let the user complete only the website checkpoint that AgentWeb reports, such as a
password, passkey, OTP, CAPTCHA, consent screen, or payment confirmation. Resume
the original operation after the connection succeeds.

Never expose cookies, tokens, or retained session data. Never treat an HTTPS
response, a browser fallback, or a successful login as proof that every website
operation has direct parity.

## Writes and fallback

For any mutating or consequential operation:

1. Inspect the operation description and exact target.
2. Obtain the user's confirmation when AgentWeb requires it.
3. Pass the explicit confirmation flag only for that approved action.
4. Verify the returned state rather than assuming success.

If the site or action is not mapped, say what is missing. Use another authorized
tool or a normal browser only for the uncovered portion instead of pretending
AgentWeb supports it.

For the full execution contract, read:
https://github.com/AnayGarodia/agentweb/blob/main/docs/AGENT_GUIDE.md
