# Security and trust

AgentWeb can hold website sessions and run account-changing operations. Treat its
runtime and every installed adapter as security-sensitive code.

## Local session storage

By default, AgentWeb stores state under `~/.agentweb`. The directory is created
with owner-only permissions, and cookie files and JSON state are written with mode
`0600` where the operating system supports Unix permissions.

Cookies are protected by filesystem permissions, not encrypted with an operating
system keychain in this version. Anyone who can read the user's account files or
run code as that user may be able to read the sessions. Do not place
`AGENTWEB_HOME` in a shared or synchronized directory.

Each site and named profile has its own cookie jar. Cache keys include the profile,
and authenticated reads bypass the shared response cache where required by the
runtime. Adapters must never copy credentials between profiles.

## Adapter boundaries

Every adapter declares an HTTPS host allowlist. The request runtime rejects hosts
outside it, manages cookies itself, strips dangerous caller-controlled headers,
and bounds returned data before it reaches the agent.

This reduces risk but does not make an untrusted adapter safe. Python adapter code
runs locally with the user's permissions. Review community adapters before
installation and prefer a registry whose signing key you trust.

## Registry trust

Local registries may be unsigned for development. Remote registries must use HTTPS
and an Ed25519-signed index. AgentWeb verifies the pinned public key, bundle paths,
SHA-256 hashes, manifest identity, and manifest contract before replacing an
installed version. Installation is staged so a failed update does not replace the
last valid bundle.

The signing private key must never appear in this repository, CI logs, release
artifacts, or user installations.

## Browser transport (declared per-operation exception)

Ordinary typed operations execute as direct HTTPS requests and never launch a
browser. A small number of operations are the exception: some sites (LinkedIn's
member API behind PerimeterX) reject a keyless browserless replay that carries
the login cookie but not the browser-established anti-bot context, bouncing it
into a redirect/429 loop. Those operations declare `"transport": "browser"` in
their manifest command entry and run the *same typed request* inside the user's
authenticated Chrome over CDP (`fetch(..., {credentials:"include"})` on the
site's own origin).

This is not browser automation of the DOM and not anti-bot evasion — no token
forging, fingerprint spoofing, or challenge bypass. The request simply rides a
genuine signed-in browser. Safety properties preserved:

- opt-in and declared per operation; never a silent, global behavior;
- the host allowlist still applies to the in-page request URL;
- only the target site's cookies are ever imported into its isolated profile;
- when no saved session exists, the per-site profile is seeded from the user's
  default browser (same mechanism as `agentweb connect`), and an honest,
  actionable error is raised when no default profile is available;
- preview (`--dry-run`) stays browserless;
- mutating browser-routed operations still require explicit confirmation.

## Write confirmation

AgentWeb requires explicit confirmation for operations declared as mutating and
for generic direct requests using write methods. Confirmation applies to one call;
login completion is not confirmation for a pending write.

An adapter must not disguise a write as a read to avoid confirmation. Pull requests
that add writes should include a negative test proving the unconfirmed call is
rejected before any network mutation.

## Human checkpoints

Passwords, CAPTCHA, OTP, passkeys, legal consent, and payment verification may be
required by the website. AgentWeb should identify and resume these checkpoints,
not bypass or weaken them. Secrets should be entered in the website's own trusted
surface and never returned in AgentWeb output.

## Captures and logs

The public capture analyzer redacts common credential names and response values,
but no automatic redactor is perfect. Inspect every trace before sharing it. Never
commit HAR files, screenshots containing personal information, browser profiles,
verification inputs, cookie files, authorization headers, or account identifiers.

The repository's `.gitignore` blocks common local artifacts, but it is not a
security boundary.

## Usage analytics

AgentWeb records a random installation ID plus fixed operational metadata: event
name, mapped site and operation, success, duration, interface, versions, cache use,
and a structured error code. Account-changing operations are reduced to the generic
name `account_write`.

It does not accept or store prompts, operation arguments, website responses, URLs,
exception messages, account identities, cookies, credentials, or IP addresses.
Events are written to `~/.agentweb/analytics.sqlite3`. Remote delivery is disabled
unless a PostHog project key is configured, and delivery runs outside the website
operation so analytics cannot make that operation fail.

Run `agentweb telemetry inspect` to see the complete event shape,
`agentweb telemetry disable` to stop local and remote recording, or
`agentweb telemetry reset-id` to replace the random identifier. The global
dashboard's personal PostHog key remains in owner-only local state and is never
sent to AgentWeb installations.

## Reporting a vulnerability

Do not open a public issue for a vulnerability involving credential exposure,
cross-profile access, host-allowlist escape, signature bypass, or unintended
remote mutation. Use GitHub's private security advisory flow for the repository.
Include the AgentWeb version, affected adapter and version, a minimal reproduction,
and whether any real account state changed.
