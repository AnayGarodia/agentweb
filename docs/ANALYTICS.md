# Usage analytics

AgentWeb includes a localhost dashboard for answering a small set of product
questions quickly:

- Did a new installation reach a successful website task?
- Did that installation use AgentWeb again on another day?
- Which mapped sites and operations are used?
- Which adapters fail, how often, and with which structured error code?
- How fast are successful and failed operations?
- Did the call come from an agent through MCP or directly through the CLI?

Run:

```bash
agentweb dashboard
```

The server binds only to `127.0.0.1`, chooses an available port by default, opens
the system browser, and keeps dashboard credentials outside the page. Use
`--no-open` on headless machines or `--port PORT` for a stable local address.

## Local and global views

The dashboard always works with this installation's local SQLite events. Its
source label says **This device**.

To see anonymous aggregate activity from public installations, create a PostHog
project and expand **Connect all installations** at the bottom of the dashboard.
Enter:

- The numeric project ID.
- The public project key used for event ingestion.
- A personal API key that can query that project.
- The PostHog API and ingestion hosts for the selected region.

The public project key must also be configured in the AgentWeb build distributed
to users, or provided as `AGENTWEB_POSTHOG_KEY`. The personal API key is different:
it stays under `~/.agentweb/dashboard.json` on the maintainer's computer and must
never be shipped. Once global reads work, the source label changes to **All
installations**. If PostHog is unavailable, the dashboard falls back to local data
and says so explicitly.

For a local installation, configure event delivery with:

```bash
agentweb telemetry configure-posthog \
  --project-key PROJECT_KEY \
  --host https://us.i.posthog.com
```

Set `posthog_project_key` in `src/agentweb/telemetry-defaults.json` before a public
release to configure every installation. `AGENTWEB_POSTHOG_KEY` and
`AGENTWEB_POSTHOG_HOST` remain available as deployment overrides.

## What an event contains

An operation event has this fixed shape:

```json
{
  "event": "operation_completed",
  "site": "wikipedia",
  "operation": "search",
  "success": true,
  "duration_ms": 420.0,
  "interface": "mcp",
  "agentweb_version": "0.18.2",
  "adapter_version": "0.8.5",
  "error_code": null,
  "from_cache": false
}
```

Events also carry a random installation ID and operating-system family. PostHog
person profiles and IP capture are disabled. Account-changing operation names are
replaced with `account_write` before storage.

AgentWeb never records prompts, arguments, search terms, product or song names,
repository names, URLs, website responses, account identities, exception messages,
cookies, tokens, or credentials. The telemetry API does not accept these fields.

Inspect the representative payload directly:

```bash
agentweb telemetry inspect
```

## Controls

```bash
agentweb telemetry status
agentweb telemetry enable
agentweb telemetry disable
agentweb telemetry reset-id
agentweb telemetry inspect
```

`AGENTWEB_TELEMETRY=0` disables recording for one process or environment regardless
of the saved setting. Disabling telemetry stops both local recording and optional
remote delivery. Existing local events remain available in the dashboard until the
user removes their AgentWeb state.

## Metric definitions

**People** means distinct random installation IDs in the selected window. It does
not mean accounts or named individuals.

**Got a result** means an installation completed at least one successful mapped
website operation. **Came back** means an activated installation ran an operation
on at least two distinct UTC dates.

**Website actions** counts logical AgentWeb operations. Internal HTTP requests,
retries, schema discovery, and dashboard refreshes are not counted as separate
actions.
