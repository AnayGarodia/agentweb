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

AgentWeb supports macOS and Linux. If Python 3.11 or newer is not already
available, the installer provisions an isolated Python automatically.

```bash
curl -fsSL https://github.com/AnayGarodia/agentweb/raw/refs/heads/main/install.sh | sh
```

Restart Claude Code or Codex, then paste this prompt:

```text
Use AgentWeb to find the latest version of React on npm.
```

That is the intended experience. The installer adds a global AgentWeb skill for
Claude Code and Codex containing the CLI's absolute path, and installs everything
in an isolated environment under `~/.local/share/agentweb`. New agent sessions can
therefore discover AgentWeb even when the app does not inherit `~/.local/bin` in its
PATH. It does not change your system Python packages or register MCP automatically.

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

Use AgentWeb to find software engineering jobs on LinkedIn in San Francisco.
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

The installer currently ships these 11 mapped websites:

| Website | Domain | What an agent can do |
| --- | --- | --- |
| Amazon | `amazon.com` | Search and compare products, inspect reviews and deals, manage a cart and addresses, read orders, and complete a confirmed checkout |
| arXiv | `arxiv.org` | Search papers, inspect metadata and categories, get BibTeX, and download PDFs or source |
| GitHub | `github.com` | Work with repositories, files, commits, branches, releases, issues, pull requests, users, and authenticated API requests |
| GST | `gst.gov.in` | Search HSN/SAC data and browse practitioners, advisories, due dates, holidays, laws, statistics, and tools |
| Hacker News | `news.ycombinator.com` | Read and search stories, comments, users, and activity; submit, edit, vote, flag, and favorite when signed in |
| Hugging Face | `huggingface.co` | Explore models, datasets, Spaces, repositories, files, papers, documentation, collections, and community discussions |
| LinkedIn | `linkedin.com` | Search public jobs, inspect jobs and companies, keep a normal website login, and call approved official API endpoints |
| npm | `npmjs.com` | Search packages and inspect versions, dependencies, downloads, provenance, maintainers, tarballs, and package files |
| Spotify | `open.spotify.com` | Search and play music, control the desktop player, and manage playback, devices, queues, libraries, and playlists after login |
| Stack Overflow | `stackoverflow.com` | Search and read questions, answers, comments, users, and tags; ask, answer, comment, and vote when signed in |
| Wikipedia | `wikipedia.org` | Search and read pages, links, categories, revisions, languages, images, nearby pages, and pageviews; edit or upload when signed in |

Run `agentweb capabilities DOMAIN` for the current exact operation list and any
declared gaps. The installer ships every site above.

This public repository also includes three fully open reference adapters:

| Reference adapter | Examples |
| --- | --- |
| npm | Search packages, versions, downloads, dependencies, provenance, and files |
| arXiv | Search papers, metadata, authors, categories, BibTeX, PDFs, and source |
| Wikipedia | Search, pages, links, categories, revisions, images, and pageviews |

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

### Why Spotify uses the desktop app

This is a Spotify-specific path built into its AgentWeb adapter. For simple
playback on macOS, AgentWeb resolves a song to a Spotify URI, sends an Apple Event
to the installed Spotify app, and reads the player state back to verify what
happened. Account features such as playlists, the library, queue, and remote
devices use a saved Spotify Web Player session and Spotify Connect after one
normal login.

AgentWeb provides the common machinery for adapters to use direct website
requests, official APIs, saved sessions, or local app bridges. It does not
automatically control the desktop app for every website. A similar fast path can
be added when another service exposes a usable local protocol, URL scheme, or
operating-system automation interface, but it must be implemented and verified
for that service.

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
