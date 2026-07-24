# Instructions for coding agents

Read `README.md` first. Use `docs/ARCHITECTURE.md` for runtime changes,
`docs/BUILDING_ADAPTERS.md` for adapter changes, and `docs/SECURITY.md` for any
change involving sessions, requests, registries, files, or write confirmation.

The public repository must remain usable without AgentWeb's private adapter
factory. Do not import `workbench`, `authoring`, `author_mcp`, private release
gates, private captures, or official proprietary adapter bundles.

Preserve these invariants:

- ordinary typed operations do not launch a browser;
- adapter requests cannot escape their declared HTTPS host allowlist;
- profiles never share cookies, cached authenticated data, or tokens;
- every mutating operation requires explicit confirmation;
- remote registry updates require a pinned Ed25519 key and matching file hashes;
- returned data stays bounded before it reaches an agent;
- incomplete coverage is reported rather than described as full parity.

Before finishing a change, run:

```bash
python -m pytest -q
python -m build
PYTHONPATH=src python -m agentweb.cli audit
```

For package-boundary changes, also inspect the wheel and confirm that it contains
only the public modules and the bundled adapters (see the allowlist in
`scripts/check_public_release.py`). The private adapter factory (`workbench`,
`authoring`, `author_mcp`) must never appear in the wheel.
