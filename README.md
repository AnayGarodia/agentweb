# AgentWeb

**Use websites from coding agents without making the agent click through them.**

AgentWeb turns a website into simple commands that return clean JSON:

```bash
agentweb arxiv.org search-papers --query "graph neural networks" --limit 3
agentweb npmjs.com get-version --package react --version latest
agentweb wikipedia.org page --title "Alan Turing"
```

An agent can discover what a website supports, call the right command, and use the
result directly. It does not need to inspect screenshots, find buttons, or parse a
page again for a workflow that has already been mapped.

## Try it in one minute

AgentWeb needs macOS or Linux and Python 3.11 or newer.

```bash
curl -fsSL https://github.com/AnayGarodia/agentweb/raw/refs/heads/main/install.sh | sh
```

Restart Claude Code or Codex, then paste this prompt:

```text
Use AgentWeb to find the latest version of React on npm.
```

That is the intended experience. The installer detects Claude Code and Codex,
connects AgentWeb automatically, and installs everything in an isolated environment
under `~/.local/share/agentweb`. It does not change your system Python packages.

Prefer to inspect scripts before running them? [Read `install.sh`](install.sh), or
install from a checkout:

```bash
git clone https://github.com/AnayGarodia/agentweb.git
cd agentweb
python3 -m pip install -e .
agentweb setup
```

## Just ask for what you want

Once installed, prompts can be ordinary tasks:

```text
Use AgentWeb to find three recent arXiv papers about coding agents.

Use AgentWeb to compare the dependencies of React and Vue on npm.

Use AgentWeb to research Alan Turing on Wikipedia and follow the most relevant links.
```

The agent handles discovery and commands. You should not need to translate your
request into CLI syntax. If the installer could not detect your coding agent, the
manual connection commands are:

```bash
agentweb install-agent claude --scope user
agentweb install-agent codex --scope user
```

See [agent setup](docs/AGENT_HOSTS.md) for the exact behavior Claude Code and Codex
should follow around login, retries, and writes.

## See whether AgentWeb is being used

Open the private usage dashboard:

```bash
agentweb dashboard
```

It opens on `127.0.0.1` and immediately shows activity from this installation:
people, first-task activation, repeat use, website actions, success rate, latency,
common operations, agent paths, and failures. Connect a PostHog project from the
same page when you want the localhost dashboard to combine anonymous events from
public installations.

AgentWeb never records prompts, arguments, URLs, website responses, account
identities, cookies, or credentials. Inspect or disable analytics at any time:

```bash
agentweb telemetry inspect
agentweb telemetry disable
```

Read [usage analytics](docs/ANALYTICS.md) for the exact event schema and global
dashboard setup.

## What the agent does underneath

```bash
# See which websites are installed
agentweb sites

# Discover what one website supports
agentweb capabilities arxiv.org

# Run the selected action
agentweb arxiv.org search-papers --query "attention" --limit 3
```

Most users do not need to run these themselves. They are the predictable interface
the coding agent uses. If an argument is unclear, the agent can inspect only that
action:

```bash
agentweb describe arxiv.org --operation search_papers
```

Commands accept normal domains and many normal website URLs. Results and expected
errors are JSON, so agents do not need site-specific parsing code.

## What is included today?

This public repository includes three ready-to-use examples:

| Website | Examples |
| --- | --- |
| npm | Search packages, versions, downloads, dependencies, provenance, and files |
| arXiv | Search papers, metadata, authors, categories, BibTeX, PDFs, and source |
| Wikipedia | Search, pages, links, categories, revisions, images, and pageviews |

Run `agentweb capabilities DOMAIN` for the current, exact list.

AgentWeb's larger maintained website catalog is distributed separately. This
repository is the open core: the command-line tool, runtime, login/session system,
adapter format, signed updater, tests, and reference adapters. The automatic system
used to map and repair the official catalog is not in this repository.

## How it works

```text
website mapped once -> versioned AgentWeb adapter -> reusable commands for every agent
```

Each adapter describes the website's actions, inputs, output, login needs, and
known gaps. AgentWeb keeps the common behavior in one runtime: domains, sessions,
request limits, confirmation for writes, structured errors, and safe updates.

The browser can still appear when the website itself requires a person to log in,
solve a CAPTCHA, use a passkey, enter a one-time code, accept legal terms, or
confirm payment. It is not the normal path for already-mapped actions.

## Login only when needed

Try public commands without logging in. If an account action needs authorization,
AgentWeb returns `authentication_required`. Then the user runs:

```bash
agentweb connect example.com
```

Sessions stay on that user's device under `~/.agentweb`. They are not bundled into
an adapter or shared with other users. Read [security and trust](docs/SECURITY.md)
before installing an authenticated third-party adapter.

## Build an adapter

The open core includes the public pieces needed to build and distribute an adapter:

```bash
agentweb adapter-new example --base-url https://example.com --root ./registry
agentweb registry-build ./registry
agentweb audit example --root ./registry
```

Start with [building an adapter](docs/BUILDING_ADAPTERS.md). The documentation is
explicit about login, write confirmation, evidence, incomplete coverage, and what
must never be committed.

## Documentation

| If you want to… | Read… |
| --- | --- |
| Use AgentWeb from an agent | [Agent guide](docs/AGENT_GUIDE.md) |
| Connect Claude Code or Codex | [Agent setup](docs/AGENT_HOSTS.md) |
| Understand the code | [Architecture](docs/ARCHITECTURE.md) |
| Build a website adapter | [Adapter guide](docs/BUILDING_ADAPTERS.md) |
| Understand sessions and safety | [Security](docs/SECURITY.md) |
| Understand usage analytics | [Analytics](docs/ANALYTICS.md) |
| Contribute code | [Contributing](CONTRIBUTING.md) |

Coding agents working on this repository should read [AGENTS.md](AGENTS.md).
`llms.txt` provides a compact machine-readable map of the documentation.

## Current status

AgentWeb is an early public preview. Websites can change endpoints, revoke
sessions, add verification, or rate-limit requests. AgentWeb reports an adapter's
known gaps and verification state instead of claiming that every action will work
forever.

The public core is licensed under [Apache-2.0](LICENSE). Official adapter bundles,
hosted services, and the private mapping system may use separate terms.
