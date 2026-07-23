# Using AgentWeb as an agent

This is the shortest reliable procedure for Claude Code, Codex, and other agents
with shell access.

## 1. Check support

Do not guess whether a website is installed:

```bash
agentweb sites
```

Use the website's normal domain in new commands. AgentWeb also accepts adapter
aliases and supported resource URLs.

## 2. Find the operation

Search the small catalog before loading a full schema:

```bash
agentweb capabilities npmjs.com --query download
```

If one operation looks suitable, load only that operation:

```bash
agentweb describe npmjs.com --operation get_download_count
```

Do not load every schema preemptively. That wastes context and makes action
selection harder.

## 3. Execute the typed operation

The direct domain-first form is preferred:

```bash
agentweb npmjs.com get-download-count --package react --period last-week
```

For complicated nested values, use the stable JSON form:

```bash
agentweb run npmjs.com get_download_count \
  --input '{"package":"react","period":"last-week"}'
```

Use `--compact` before the command when another program will consume the result:

```bash
agentweb --compact npmjs.com get-package --package react
```

## 4. Interpret the result

Normal results contain the site, operation, data, verification information, and
request metadata. Do not treat a successful HTTP status alone as proof that a
write happened. Use operation-specific verification fields when available.

Expected failures are JSON on stderr. Important classes include:

| Error | Meaning | Agent response |
| --- | --- | --- |
| `missing_input`, `invalid_input`, or `invalid_request` | The call is malformed | Fix the arguments; do not ask the user unless the value is genuinely unknown |
| `authentication_required` | The operation needs the user's account | Ask permission, then tell the user to run the returned connection command |
| `configuration_required` | A site-specific non-secret setting is missing | Collect only the named setting and run the suggested operation |
| `rate_limited` | The website refused more requests temporarily | Respect the returned delay; do not create a retry loop |
| `human_required` | The site requires CAPTCHA, passkey, OTP, consent, or another human checkpoint | Hand off only that checkpoint and resume afterward |
| `flow_drift` or output mismatch | The website changed | Stop relying on the affected operation and report the adapter/version |
| `internal_error` | AgentWeb itself failed unexpectedly | Retry once only if the operation is read-only; report it with `agentweb --version` |

Never solve an authentication error by copying browser cookies into chat, logs, or
command arguments.

## 5. Authorization

Public operations should be attempted before login. When a protected operation
returns `authentication_required`, preserve the original arguments and ask the
user to complete:

```bash
agentweb connect example.com
```

After the command reports a verified account, retry the original read. For a
write, obtain confirmation again because authorization completion is not approval
for the pending mutation.

Named profiles keep different accounts separate:

```bash
agentweb --profile work auth status example.com
agentweb --profile personal example.com account-status
```

## 6. Writes and purchases

Treat an operation as mutating when its contract says so, even if its name sounds
harmless. Before a write:

1. Read the current state when practical.
2. Resolve ambiguous targets, quantities, recipients, addresses, and totals.
3. Confirm that the user's request authorizes this specific action.
4. Pass the operation's explicit `confirm` value.
5. Supply an idempotency key when the task interface supports it.
6. Inspect the returned verification instead of assuming success.

Do not invent a product policy that forbids an action the user explicitly requested.
Do not bypass a website-mandated human security or legal-consent checkpoint either.

## 7. URLs and workflows

Use `agentweb get URL` when an adapter declares a typed route for a normal website
URL:

```bash
agentweb get https://arxiv.org/abs/1706.03762
```

Use `workflow` when later steps need structured output from earlier steps:

```bash
agentweb workflow npmjs.com --steps '[
  {"name":"pkg","action":"get_package","arguments":{"package":"react"}},
  {"name":"versions","action":"list_versions","arguments":{"package":"$pkg.data.name"}}
]'
```

Keep workflows on one site. Avoid parallel calls when they share live playback,
cart, quota, or other state whose order matters.

## 8. Freshness and coverage

Use `--fresh` when the user asks for current account, cart, playback, price, quota,
or similarly volatile state:

```bash
agentweb --fresh example.com account-status
```

Before promising that AgentWeb can perform every website action, inspect:

```bash
agentweb audit example.com
agentweb describe example.com --parity-details
```

`browserless_replay` means normal execution does not need a browser. It does not
mean the site can never require a human checkpoint. `declared_gaps` and audit
failures are part of the result, not documentation to ignore.
