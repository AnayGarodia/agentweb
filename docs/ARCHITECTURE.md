# Architecture

AgentWeb separates the stable agent interface from website-specific behavior.

```text
agent or human
      |
      v
CLI / four-tool MCP surface
      |
      v
Runtime: resolve domain -> validate input -> choose installed adapter
      |
      +--> Adapter SDK -> bounded HTTPS request -> normalized JSON
      |
      +--> Auth handoff when the website requires a human checkpoint
      |
      v
Local profile, cache, idempotency receipt, and verification metadata
```

## Public modules

| Module | Responsibility |
| --- | --- |
| `cli.py` | Domain-first command parsing and structured errors |
| `mcp.py` | Small compatibility surface for non-CLI agent hosts |
| `runtime.py` | Site resolution, dispatch, workflows, idempotency, and envelopes |
| `sdk.py` | Adapter base classes, HTTP sessions, schemas, normalization, and budgets |
| `registry.py` | Manifest validation, signed synchronization, audit, and rollback |
| `connector.py` | Login, OAuth, resumable human checkpoints, and host installation |
| `auth.py` | Authorization state model |
| `storage.py` | Profile isolation, atomic files, locks, and read cache |
| `targets.py` | Domain and URL routing |
| `capture.py` | Public redaction, trace analysis, and flow-capsule verification |
| `web_runtime.py` | Site-scoped browser used for authorization and opt-in mapping |
| `scaffold.py` | Minimal community adapter skeleton |

The private adapter factory is deliberately absent. The public package must import
and run without it.

## Request lifecycle

1. The runtime resolves a domain, alias, subdomain, or URL against installed
   manifests.
2. It locates the operation contract and rejects unknown or extra inputs.
3. The adapter runs through a site/profile-specific session.
4. The SDK enforces the allowed host, HTTPS, request limits, confirmation, and
   cancellation.
5. The adapter normalizes website output into the operation contract.
6. The runtime applies output budgets, records idempotency when requested, and
   returns verification and metadata with the data.

## Registry layout

```text
registry/
  index.json
  sites/
    example/
      0.1.0/
        manifest.json
        adapter.py
        evidence.json       # optional semantic evidence
        evidence/           # optional referenced fixtures
        flows/              # optional reviewed direct recipes
```

The index contains the active version and SHA-256 of every distributed file. Source
control may retain older versions, but package building includes only versions in
the index.

## Why the CLI is primary

A registry can contain hundreds of sites and thousands of operations. Publishing
each one as an always-visible tool would consume agent context and slow tool
selection. The CLI keeps discovery lazy: list sites, search one catalog, inspect
one schema, then execute. MCP mirrors the same approach with four tools.

## Public/private boundary

The open core defines how adapters run and how users can inspect and trust them.
The private factory handles large-scale website inventory, automated endpoint
discovery, generation, semantic release gates, drift monitoring, and repair of the
official catalog. No public-core module may require the factory at import time or
runtime.
