---
name: testing-agentweb-cli
description: Test the agentweb CLI, installer, and auth/session behavior end-to-end. Use when verifying agentweb releases, installer changes, or login/session features.
---

# Testing the AgentWeb CLI

All testing is shell-based (the product is a CLI); do not record a screen — collect command output as evidence.

## Clean-install test (releases / install.sh changes)

```bash
rm -rf /tmp/aw-test /tmp/aw-bin
AGENTWEB_INSTALL_ROOT=/tmp/aw-test AGENTWEB_BIN_DIR=/tmp/aw-bin sh install.sh
/tmp/aw-bin/agentweb --version   # must print the release version exactly
```

- The installer verifies an embedded wheel sha256 and may bootstrap its own Python (system Python 3.10 is too old — the package needs 3.11+; never run `python3 -m agentweb.cli` with system Python).
- Regenerate the installer with `uv run python scripts/build_installer.py --version X --wheel dist/agentweb_cli-X-py3-none-any.whl --template installer/install.sh.template --output install.sh` (also writes `install.sh.sha256`).
- A release commit touches: `pyproject.toml`, `src/agentweb/__init__.py`, `install.sh`, `install.sh.sha256`, `uv.lock`.

## State isolation

Set `AGENTWEB_HOME=/tmp/aw-home` to keep profiles/cookies out of `~/.agentweb`. Per-site profile state lives at `$AGENTWEB_HOME/profiles/<site>/<profile>/` (e.g. `session-meta.json`, `auth-attempt.json`).

## Useful golden-path commands

- `agentweb sites` — JSON list of bundled sites with aliases and commands (command names differ from what you'd guess; check here first — e.g. Hacker News top stories is `hn stories`, not `hn top`).
- Browserless read regression: `agentweb hn stories --limit 3` (no auth, fast, deterministic shape).
- Auth surface: `agentweb auth status <site>`; disconnected status should include `reconnect_command`; a stored session shows a `session` block with `expired`/`expires_in_seconds`.
- To simulate an expired session without a real login, write `session-meta.json` into the profile dir in the shape `import_cdp_cookies` produces (`imported_at_unix`, `cookie_count`, `auth_cookies_captured`, `session_expires_at_unix` in the past), then run an authenticated write (e.g. `agentweb github create_gist --files '{"a.txt":"hi"}' --confirm true`) and check `details.session_expired` in the error JSON.
- Writes are confirm-gated: expect "repeat with confirm=true" unless `--confirm true` is passed.

## Login/connect behavior testing

- `agentweb connect <site>` returns a JSON result whose `default_browser` block reveals the seeding decision: `{"reason": "disabled"}` means the managed Chrome window was used without touching the user's browser (default since 0.26.1); any other reason (`seeded: true`, `no_default_browser_profile`, ...) means default-browser reuse was attempted (opt-in via `--use-default-browser` or `AGENTWEB_USE_DEFAULT_BROWSER=1`).
- Connect blocks; wrap it in `timeout 30 ... --timeout 12` for non-interactive assertions, then clean up with `agentweb auth cancel <site>`.
- A `Cookies` file inside `$AGENTWEB_HOME/profiles/<site>/<profile>/web-runtime/chrome-profile/` is created by the managed Chrome itself; only the `.agentweb-seeded` marker proves profile seeding happened.

## Validation commands (per AGENTS.md)

```bash
uv run pytest -q
uv run python -m build
uv run python -m agentweb.cli audit
uv run python scripts/check_public_release.py dist/*.whl   # package-boundary changes
```

## Devin Secrets Needed

None for public reads and installer testing. Real authenticated-session tests would need a user-provided login (interactive `agentweb connect <site>`) or a site API token (e.g. `github configure_token`).
