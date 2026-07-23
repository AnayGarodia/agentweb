# Contributing to AgentWeb

AgentWeb welcomes improvements to the open runtime, documentation, tests, and
reference adapters. Read [the architecture](docs/ARCHITECTURE.md) and
[security model](docs/SECURITY.md) before changing request, session, registry, or
write-confirmation behavior.

## Set up the repository

```bash
git clone https://github.com/AnayGarodia/agentweb.git
cd agentweb
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
```

`uv sync` and `uv run pytest -q` are equivalent if you use `uv`.

## Choose the right repository

This repository contains the public core and reference adapters. It does not
contain AgentWeb's private automatic adapter factory or official proprietary
adapter catalog. A contribution to the public core must not depend on those
components.

Community adapters are welcome when their code and evidence can be distributed
under Apache-2.0. Website terms, permissions, and local law still apply.

## Change an adapter

Read [Building an adapter](docs/BUILDING_ADAPTERS.md). Released bundles are
immutable: create a new version directory for behavior changes, update the active
index, and keep known gaps explicit.

```bash
agentweb registry-build src/agentweb/builtin_registry
agentweb audit <site>
python -m pytest -q
```

Never commit credentials, cookies, authorization headers, browser profiles,
private captures, account data, verification secrets, or signing private keys.

## Change the core

Keep the CLI and four-tool MCP surface lazy and stable. New sites should add
manifest data and adapter code, not site-specific branches in the core runtime.
Preserve domain resolution, host allowlists, profile isolation, bounded output,
structured errors, explicit write confirmation, cancellation, and signed atomic
registry updates.

Add focused tests that would fail without the change. For a bug fix, test the
incorrect behavior and the expected agent-facing error or result.

## Documentation for agents

Commands in documentation must exist and be copyable. State whether an example is
public, authenticated, mutating, or hypothetical. When behavior changes, update
`README.md`, `llms.txt`, `docs/AGENT_GUIDE.md`, and command help as appropriate.

Avoid broad claims such as “full parity” unless the audit has current evidence for
the declared surface. Documentation is part of the product contract.

## Before opening a pull request

```bash
python -m pytest -q
python -m build
PYTHONPATH=src python -m agentweb.cli audit
```

Inspect the built wheel for accidental private files, generated captures, and old
adapter versions. The wheel should contain only the public modules and active
reference bundles.

In the pull request, explain the user-visible problem, why the change belongs in
the public core, tests run, security implications, and remaining limitations.
