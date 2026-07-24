# AgentWeb

**Let coding agents *use* websites — read data and take real actions — without opening a browser.**

AgentWeb turns a website into simple, typed commands that return clean JSON. There
are two kinds:

**Read** — look things up:

```bash
agentweb arxiv.org search-papers --query "graph neural networks" --limit 3
agentweb npmjs.com get-version --package react --version latest
agentweb wikipedia.org page --title "Alan Turing"
```

**Act** — actually do things on your own accounts. Impactful or irreversible
actions (checkout, posting, opening a PR) require an explicit `--confirm`:

```bash
agentweb spotify play --query "Massive Attack Teardrop"
agentweb amazon add-to-cart --asin B0XXXXXXXX
agentweb github create-pull-request --owner you --repo app --head fix --base main --confirm
```

An agent discovers what a site supports, calls the right command, and uses the
result directly — no screenshots, no hunting for buttons, no re-parsing a page for
a workflow that has already been mapped.

**Actions are the point.** Reads are often a web search away; *doing* things — play
a song, place an order, open a pull request, post a comment — is what a browserless
interface uniquely unlocks. Every mapped action is typed, state-changing ones are
confirmation-gated, and each is verified against the live site where possible;
where a site offers no safe way to prove an action, AgentWeb says so instead of
pretending.

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

### Upgrading an existing install

Already installed? Upgrade in place with one command — it upgrades the exact
launcher on your `PATH` (no second, drifting copy) and preserves your
`~/.agentweb` sessions and profiles:

```bash
agentweb upgrade          # checksum-verifies and installs the latest release
agentweb upgrade --check  # just report installed vs. latest; change nothing
```

`agentweb upgrade` re-runs the official installer after verifying it against its
published `install.sh.sha256`. Re-running the one-line installer from
[Try it in one minute](#try-it-in-one-minute) does the same thing.

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

Commands accept normal domains and many normal website URLs. A command's main
argument can be given either positionally or as a flag — `agentweb wikipedia.org
page "Alan Turing"` and `agentweb wikipedia.org page --title "Alan Turing"` are
equivalent — so copy-pasted examples work either way. Results and expected errors
are JSON, so agents do not need site-specific parsing code.

## What is included today?

This installer bundles **eight fully open adapters**, and `agentweb setup` (run for
you by the one-line installer) registers exactly those eight on first run:

| Adapter | Domain | Examples |
| --- | --- | --- |
| npm | `npmjs.com` | Search packages, versions, downloads, dependencies, provenance, and files |
| arXiv | `arxiv.org` | Search papers, metadata, authors, categories, BibTeX, PDFs, and source |
| Wikipedia | `wikipedia.org` | Search, pages, links, categories, revisions, images, and pageviews |
| PyPI | `pypi.org` | Search projects and inspect releases, files, dependencies, metadata, stats, and vulnerabilities |
| GitHub | `github.com` | Repositories, files, commits, branches, releases, issues, pull requests, users, and authenticated API requests |
| Hacker News | `news.ycombinator.com` | Read and search stories, comments, users, and activity; submit, edit, vote, flag, and favorite when signed in |
| Hugging Face | `huggingface.co` | Explore models, datasets, Spaces, repositories, files, papers, documentation, collections, and discussions |
| GST | `gst.gov.in` | Search HSN/SAC data and browse practitioners, advisories, due dates, holidays, laws, statistics, and tools |

Run `agentweb sites` to see exactly what is installed and `agentweb capabilities
DOMAIN` for a site's operation list and any declared gaps.

This repository is the open core: the command-line tool, runtime, login/session
system, adapter format, signed updater, tests, and the adapters above. The
automatic system used to map and repair the official catalog is **not** in this
repository.

### Login-required sites (distributed separately)

A few sites depend on a signed-in browser session and/or interact with a site in
ways governed by that site's terms of service, so they are **not** bundled in this
installer and are distributed separately as signed adapters:

| Website | Domain | What an agent can do |
| --- | --- | --- |
| Amazon | `amazon.com` | Search and compare products, inspect reviews and deals, manage a cart and addresses, read orders, and complete a confirmed checkout |
| LinkedIn | `linkedin.com` | Search public jobs, inspect jobs and companies, keep a normal website login, and call approved official API endpoints |
| Spotify | `open.spotify.com` | Search and play music, control the desktop player, and manage playback, devices, queues, libraries, and playlists after login |
| Stack Overflow | `stackoverflow.com` | Search and read questions, answers, comments, users, and tags; ask, answer, comment, and vote when signed in |

Once that catalog is configured, `agentweb sync` pulls the latest signed adapters
and `agentweb sites` shows what is currently installed.

## How it works

```text
website mapped once -> versioned AgentWeb adapter -> reusable commands for every agent
```

Each adapter describes the website's actions, inputs, output, login needs, and
known gaps. AgentWeb keeps the common behavior in one runtime: domains, sessions,
request limits, confirmation for writes, structured errors, and safe updates.

### Capture-once drift verification

Any operation can earn a keyless, credential-free drift check without a developer
key. Capture one real successful run as a **response oracle** — the redacted
response *shape* plus optional named assertions, never any captured values — then
replay it later to confirm the site has not changed underneath the adapter:

```bash
agentweb capture-oracle npmjs.com get_version \
  --input '{"package":"react"}' --assert '$.data.version' --out react.oracle.json
agentweb verify-capture react.oracle.json --strict   # capture_verified | drift
```

A single capture becomes a permanent regression oracle: when the site changes its
response, `verify-capture` reports `drift` (and exits non-zero under `--strict`
for CI) instead of silently returning degraded data. For a **mutating** operation
the oracle records the read-back that confirms the effect, not the mutation, so
replaying it never re-changes account state — `capture-oracle` refuses a mutating
op unless you pass `--confirm`.

#### Browser-assisted read-back (`--via-browser`)

A few sites actively refuse browserless replay: their anti-bot layer bounces a
plain HTTP request into a redirect/challenge loop even with a valid session, so
they can never earn a browserless `capture_verified`. For these, run the read
*inside* the site's already-authenticated Chrome via CDP:

```bash
agentweb capture-oracle SITE OP --via-browser --assert '$.data.x' --out o.json
agentweb verify-capture o.json --strict   # or --via-browser
```

The adapter builds and parses the request exactly as usual; only the transport
changes — the request is issued with `fetch(url, {credentials:"include"})` from a
page on the site's own origin, so first-party cookies and anti-bot context apply.
The response flows back through the normal envelope, so the oracle still stores
**only** the response shape and assertion types — never response values, cookies,
or the browser profile. Such an oracle records `"execution": "browser_assisted"`,
`verify-capture` re-runs it in the browser automatically, and a passing replay is
reported as `browser_capture_verified` to keep it distinct from browserless
`capture_verified`. This path is explicit-only (ordinary typed operations never
launch a browser), requires a prior `agentweb connect SITE`, and is **read-only**.

#### Continuous oracle drift (`verify-oracles`)

One capture is a receipt; a *directory* of captures replayed on a schedule is a
standing guarantee. `verify-oracles` discovers every `*.oracle.json` under a
directory and replays each, so a site that quietly changes its response shape is
caught automatically instead of at the next manual check:

```bash
agentweb verify-oracles --dir oracles --strict   # exits non-zero on any drift
```

- **Keyless, browserless, non-mutating** oracles are replayed against the live
  site and reported `capture_verified` or `drift`.
- **Mutating** oracles (which record a read-back) and **browser-assisted**
  oracles are account-tied, so they are `skipped` by default; pass
  `--via-browser` to also replay the browser ones inside their authenticated
  Chrome.
- A transient network/site error is reported as `inconclusive` and does **not**
  fail the run; only genuine drift or an unreadable oracle fails `--strict`.
- `--offline` validates each oracle's structure without making any request.

The committed [`oracles/`](oracles/) directory holds keyless public-read oracles
(npm, arXiv, Wikipedia) that the scheduled **Oracle drift** workflow replays
weekly. A passing oracle proves the captured response shape and assertions still
match; it does not prove every mutation or site action works.

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
