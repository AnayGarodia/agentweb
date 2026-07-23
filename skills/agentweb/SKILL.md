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

Resolve the CLI without assuming the agent host inherited the user's shell PATH:

```bash
AGENTWEB_BIN="$(command -v agentweb || true)"
if [ -z "$AGENTWEB_BIN" ] && [ -x "$HOME/.local/bin/agentweb" ]; then
  AGENTWEB_BIN="$HOME/.local/bin/agentweb"
fi
```

If `AGENTWEB_BIN` is still empty, tell the user that the skill is installed but
the AgentWeb runtime is not. When the user asks to install it or approves
installation, inspect the installer and then run:

```bash
curl -fsSL https://github.com/AnayGarodia/agentweb/raw/refs/heads/main/install.sh | sh
AGENTWEB_BIN="$HOME/.local/bin/agentweb"
```

AgentWeb setup preserves a portable skill installed by GitHub CLI. Restart the
agent host only when it needs to reload a newly installed skill.

## Discover before acting

Use this sequence instead of guessing commands:

```bash
"$AGENTWEB_BIN" sites
"$AGENTWEB_BIN" capabilities DOMAIN --query WORD
"$AGENTWEB_BIN" describe DOMAIN --operation ACTION
"$AGENTWEB_BIN" DOMAIN ACTION [arguments]
```

Normal website URLs can be passed where a domain is accepted. Read the operation's
declared gaps and risk metadata before promising coverage or taking action.

## Authentication and human handoff

Try public operations without login. If an operation returns
`authentication_required`, preserve the original arguments and ask whether the
user wants to log in or sign up. Only after approval, run the matching mode:

```bash
"$AGENTWEB_BIN" connect DOMAIN --mode login
"$AGENTWEB_BIN" connect DOMAIN --mode signup
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
