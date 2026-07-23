# AgentWeb

AgentWeb gives coding agents a fast command-line interface to websites.

Instead of repeatedly opening a browser, finding controls, and reading pages, an
agent can discover a website's available actions once and call them as structured
commands:

```bash
agentweb npmjs.com search-packages --query "react router" --limit 3
agentweb arxiv.org search-papers --query "graph neural networks" --limit 3
agentweb wikipedia.org page --title "Alan Turing"
```

The result is JSON, so the agent receives the useful data rather than screenshots,
HTML, or instructions for where to click.

> **Project boundary:** this repository is the Apache-2.0 licensed AgentWeb core.
> It includes the CLI, runtime, security model, adapter format, registry tooling,
> and three reference adapters. AgentWeb's private adapter factory and the
> separately distributed official adapter catalog are not part of this repository.

## Why AgentWeb exists

Browser tools are useful for teaching an integration and completing genuinely
human checkpoints. They are unnecessarily slow and expensive for a workflow that
has already been understood. AgentWeb turns the repeatable part into a named,
versioned operation that any connected agent can reuse.

This does not remove website security. A site may still require a login, CAPTCHA,
passkey, one-time code, consent screen, or payment confirmation. AgentWeb exposes
that requirement clearly and keeps each user's authorization on their own device.

## Install for development

AgentWeb requires Python 3.11 or newer. Until the first public-core release, install
directly from the repository:

```bash
git clone https://github.com/AnayGarodia/agentweb.git
cd agentweb
python3 -m pip install -e .
agentweb setup
```

For an isolated install with `uv`:

```bash
uv tool install --from . agentweb-cli
agentweb setup
```

Check the installation:

```bash
agentweb --version
agentweb sites
agentweb capabilities npmjs.com --query search
```

## The agent's normal loop

An agent only needs four ideas:

```bash
# 1. Find actions for a domain or URL
agentweb capabilities arxiv.org --query paper

# 2. Read the exact schema when necessary
agentweb describe arxiv --operation search_papers

# 3. Run the action using domain-first syntax
agentweb arxiv.org search-papers --query "attention is all you need" --limit 1

# 4. Connect an account only after a protected action asks for it
agentweb connect example.com
```

The CLI accepts a site name, a domain, or a supported URL. Action names may use
hyphens or underscores. Every successful call emits JSON. Every expected failure
also emits structured JSON on stderr with a stable error code, whether retrying is
safe, and the next useful action when one exists.

For exact examples and decision rules, read [Using AgentWeb as an agent](docs/AGENT_GUIDE.md).

## Included reference adapters

The public core intentionally ships only three reference integrations:

| Website | What it demonstrates |
| --- | --- |
| npm | A broad JSON API, package files, downloads, and provenance |
| arXiv | Search, XML feeds, identifiers, citations, and file downloads |
| Wikipedia | A mature read-only adapter with URL routing and semantic evidence |

List the exact operations available in your installed version instead of relying
on this table:

```bash
agentweb sites
agentweb capabilities wikipedia.org
agentweb audit
```

Official maintained adapters may be installed from a separate signed registry.
AgentWeb verifies the registry signature and every bundle hash before installing
anything. A remote registry can be selected explicitly:

```bash
agentweb sync https://registry.example/agentweb \
  --public-key ./registry-public-key.pem
```

## Login and local data

Public actions should not ask you to log in. Protected actions return an
`authentication_required` response that tells the agent what to ask the user to
do. The user then runs one connection command and completes the website's normal
login:

```bash
agentweb connect example.com
agentweb auth status example.com
agentweb auth disconnect example.com --confirm
```

Sessions, cookies, tokens, caches, and browser profiles stay under
`~/.agentweb` by default. Set `AGENTWEB_HOME` to use a different local state
directory. AgentWeb does not put personal sessions inside adapter bundles.

Read [Security and trust](docs/SECURITY.md) before using an authenticated or
write-capable third-party adapter.

## Mutating actions

Operations that create, change, send, purchase, vote, publish, or delete require
an explicit confirmation field. Discovering or reading an operation never counts
as confirmation. Agents should inspect the current state and important totals,
then run the write only after the user's request clearly authorizes it.

An idempotency key prevents a successful action from being accidentally repeated:

```bash
agentweb task example.com create-item \
  --input '{"name":"demo","confirm":true}' \
  --idempotency-key create-demo-1
```

## Using AgentWeb from an agent host

The CLI is the primary interface because every coding agent can run it without
loading a large tool schema into every prompt. MCP remains available as a compact
compatibility layer:

```bash
agentweb install-agent claude --scope user
agentweb install-agent codex --scope user
agentweb mcp-config
```

Agents should prefer the CLI when shell access exists. See
[Agent host setup](docs/AGENT_HOSTS.md) for Claude Code, Codex, and generic hosts.

## Building a community adapter

The public repository supports deliberate, reviewable adapter development. It
contains a safe request/session SDK, manifest validation, redacted trace analysis,
flow-capsule verification, scaffolding, registry hashing, and signing.

```bash
agentweb adapter-new example --base-url https://example.com --root ./registry
agentweb registry-build ./registry
agentweb audit example --root ./registry
```

The private AgentWeb factory that automatically inventories large websites,
coordinates mapping, generates operations, runs the full release matrix, detects
drift, and repairs official adapters is intentionally not included.

Read [Building an adapter](docs/BUILDING_ADAPTERS.md) before submitting one.

## Repository map

```text
src/agentweb/                 CLI, runtime, SDK, registry, auth, and storage
src/agentweb/builtin_registry Reference adapters included with the public core
src/sitepack/                 Compatibility namespace for older installs
docs/                         Human and agent documentation
tests/                        Core and reference-adapter tests
installer/                    Reproducible standalone installer template
```

Start with [Architecture](docs/ARCHITECTURE.md) when changing the runtime and with
[Contributing](CONTRIBUTING.md) when preparing a pull request. Coding agents should
also read [AGENTS.md](AGENTS.md), which is a short map of repository invariants and
verification commands.

## What AgentWeb does not promise

Websites change. They can revoke sessions, rotate tokens, rate-limit callers, alter
private endpoints, or add new security checks. An adapter is evidence about a
tested version of a website, not ownership of that website. AgentWeb reports known
coverage and verification state instead of silently claiming that every operation
will always work.

## License

The AgentWeb core and the reference adapters in this repository are licensed under
the [Apache License 2.0](LICENSE). Official adapter bundles, trademarks, hosted
services, and the private adapter factory may use separate terms.
