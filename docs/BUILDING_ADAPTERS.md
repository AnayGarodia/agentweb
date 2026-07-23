# Building a community adapter

An adapter is a versioned contract between an agent and one website. A good adapter
does more than fetch a page: it names user-level actions, validates inputs, returns
bounded structured data, protects writes, and says honestly what remains missing.

## Create the skeleton

Use a separate local registry while developing:

```bash
agentweb adapter-new example --base-url https://example.com --root ./registry
```

This creates `registry/sites/example/0.1.0/manifest.json` and `adapter.py` with a
safe public home-page operation. Point a test profile at it:

```bash
AGENTWEB_HOME="$(mktemp -d)" agentweb sync ./registry
```

## Define the contract first

Each command in `manifest.json` needs:

- a concrete description of what the operation returns or changes;
- a JSON input schema with required fields, types, ranges, and enums;
- CLI positional declarations only for arguments that are naturally positional;
- mutation and confirmation metadata for writes;
- known errors and human boundaries;
- bounded output expectations.

Use names such as `search_packages`, `get_paper`, or `create_playlist`. Avoid page
names, CSS selectors, and vague commands such as `do_action`.

`canonical_domain`, `aliases`, and declarative `url_routes` let agents use normal
domains and resource URLs without knowing the internal adapter name.

## Implement the direct operation

For straightforward HTTP calls, subclass `RequestRecipeAdapter` and add declarative
recipes. Add Python methods only when a workflow needs token exchange, pagination,
signing, error translation, or response normalization.

Never use shell commands or another site's official CLI as the adapter runtime.
Never accept an arbitrary host from operation input. Add every legitimate API host
to the fixed manifest and adapter allowlists.

## Authentication

Declare whether public operations require no account and which strategy protects
account operations. Keep credentials in the local profile through AgentWeb's
session APIs. Do not place cookies, access tokens, client secrets, or example user
accounts in manifests, fixtures, source, or documentation.

If a site requires CAPTCHA, passkey, OTP, consent, or payment verification, model
it as a resumable human checkpoint. Do not claim the adapter bypasses it.

## Writes

Set the operation's mutation metadata and require `confirm`. Validate all inputs
before making the request. When possible, read the resulting server state and
return a `verified` field rather than treating a 200 response as proof.

Support an idempotent server key when the website provides one. AgentWeb's task
receipt prevents local repetition after a recorded success, but it cannot make an
inherently non-idempotent upstream call safe after an ambiguous network failure.

## Captures and flow capsules

The public `--mapping-mode` and `capture-compile` commands can help inspect a flow
and produce a redacted structural capsule. They are deliberately basic tools, not
the private automatic AgentWeb factory.

```bash
agentweb --mapping-mode example web-start --visible
agentweb --mapping-mode example web-inspect
agentweb capture-compile capture.json --operation search --capsule-out search.flow.json
agentweb verify search.flow.json --offline
```

Inspect every capture manually before committing it. A structural match is not a
semantic test. Your regression test must assert user-visible meaning, not merely
status codes or field presence.

## Coverage and evidence

List known gaps in `coverage.not_mapped`. Do not mark an adapter exhaustive merely
because every implemented command works. Full parity means the declared website
surface was inventoried and every advertised stable command has current semantic
evidence across its stated roles and locales.

Evidence must not contain secrets or personal data. Prefer synthetic fixtures and
public read-only examples.

## Validate and package

```bash
agentweb registry-build ./registry
agentweb audit example --root ./registry
python -m pytest -q
python -m build
```

Create a new adapter version for behavior changes instead of rewriting a released
bundle in place. A pull request should explain the observed website behavior, the
direct protocol, tests run, mutation cleanup, and remaining gaps.

Read [Security and trust](SECURITY.md) before implementing authentication or any
operation that changes remote state.
