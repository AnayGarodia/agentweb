from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from agentweb.cli import BooleanFlagAction
from agentweb.cli import main as cli_main
from agentweb.registry import Registry, bundled_registry
from agentweb.runtime import Runtime
from agentweb.sdk import AgentWebError, Response
from agentweb.storage import StatePaths, write_json


def _parse_bool(args: list[str]) -> dict[str, Any]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public", dest="public", action=BooleanFlagAction, default=None)
    return vars(parser.parse_args(args))


def test_boolean_flag_explicit_value_forms() -> None:
    assert _parse_bool(["--public"])["public"] is True
    assert _parse_bool(["--public", "true"])["public"] is True
    assert _parse_bool(["--public", "false"])["public"] is False
    assert _parse_bool(["--public", "1"])["public"] is True
    assert _parse_bool(["--public", "0"])["public"] is False
    assert _parse_bool(["--no-public"])["public"] is False
    assert _parse_bool([])["public"] is None


def test_boolean_flag_rejects_non_boolean() -> None:
    with pytest.raises(SystemExit):
        _parse_bool(["--public", "maybe"])


def test_cli_public_false_reaches_adapter_as_false(
    tmp_path: Path, monkeypatch
) -> None:
    # Regression for the Spotify privacy Critical: `--public false` must arrive
    # at the adapter as public=False (not True, which the old
    # BooleanOptionalAction produced by treating "false" as a stray token).
    seen: dict[str, Any] = {}

    def fake_execute(
        self, target, action, arguments, *, idempotency_key=None, dry_run=False
    ):
        seen.update(arguments)
        return {"ok": True, "operation": f"{target}.{action}"}

    monkeypatch.setenv("AGENTWEB_HOME", str(tmp_path))
    monkeypatch.setattr(Runtime, "execute", fake_execute)
    assert (
        cli_main(
            [
                "spotify.com",
                "create_playlist",
                "--name",
                "qa",
                "--public",
                "false",
                "--confirm",
            ]
        )
        == 0
    )
    assert seen.get("public") is False


def test_registry_prefers_bundle_over_stray_packaged_source(tmp_path: Path) -> None:
    # QA High / 6-of-12 root cause: a stale sibling environment's packaged
    # registry must not be trusted over the bundle shipping with the running
    # code. A source under another env's site-packages/pipx resolves to bundle.
    paths = StatePaths(tmp_path)
    stray = "/home/other/.local/pipx/venvs/sitepack-lab/lib/python3.12/site-packages/agentweb/builtin_registry"
    write_json(paths.registry_config, {"source": stray})
    assert Registry(paths).configured_source() == str(bundled_registry())


def test_registry_prefers_bundle_over_missing_builtin_pointer(tmp_path: Path) -> None:
    paths = StatePaths(tmp_path)
    write_json(
        paths.registry_config,
        {"source": str(tmp_path / "gone" / "builtin_registry")},
    )
    assert Registry(paths).configured_source() == str(bundled_registry())


def test_registry_keeps_intentional_custom_local_source(tmp_path: Path) -> None:
    # A deliberate user registry (not a packaged builtin_registry) is preserved.
    paths = StatePaths(tmp_path)
    custom = tmp_path / "my_registry"
    custom.mkdir()
    write_json(paths.registry_config, {"source": str(custom)})
    assert Registry(paths).configured_source() == str(custom)


def _amazon_adapter(tmp_path: Path):
    paths = StatePaths(tmp_path)
    Registry(paths).sync(str(bundled_registry()))
    return Runtime(paths).adapter("amazon")


def _empty_html_request(self, method, url, **kwargs):
    return Response(
        status=200, url=url, headers={}, body=b"<html></html>", elapsed_ms=1.0
    )


def test_amazon_best_sellers_requires_department_when_empty(
    tmp_path: Path, monkeypatch
) -> None:
    # QA Medium: a departmentless best_sellers call parsed the category landing
    # page to an empty list that masqueraded as a valid result. It now errors.
    adapter = _amazon_adapter(tmp_path)
    monkeypatch.setattr(type(adapter), "_request", _empty_html_request)
    with pytest.raises(AgentWebError) as excinfo:
        adapter.best_sellers()
    assert excinfo.value.code == "department_required"


def test_amazon_best_sellers_empty_department_reports_empty(
    tmp_path: Path, monkeypatch
) -> None:
    adapter = _amazon_adapter(tmp_path)
    monkeypatch.setattr(type(adapter), "_request", _empty_html_request)
    with pytest.raises(AgentWebError) as excinfo:
        adapter.best_sellers(department="not-a-real-slug")
    assert excinfo.value.code == "empty_result"


def test_arxiv_search_accepts_positional_query() -> None:
    # QA Low: arXiv's search was flag-only; siblings accept a positional query.
    manifest = json.loads(
        (
            bundled_registry() / "sites" / "arxiv" / "0.1.5" / "manifest.json"
        ).read_text()
    )
    cli = manifest["commands"]["search_papers"].get("cli") or {}
    assert "query" in (cli.get("positionals") or [])


def _spotify_adapter(tmp_path: Path):
    paths = StatePaths(tmp_path)
    Registry(paths).sync(str(bundled_registry()))
    return Runtime(paths).adapter("spotify")


def test_spotify_web_playlist_read_exposes_public_field(
    tmp_path: Path, monkeypatch
) -> None:
    # The QA gap: a web-session playlist read exposed no `public` field, so the
    # tool's create/update privacy claims could not be verified from a read.
    adapter = _spotify_adapter(tmp_path)
    monkeypatch.setattr(type(adapter), "_has_oauth_tokens", lambda self: False)
    monkeypatch.setattr(type(adapter), "_has_web_session", lambda self: True)

    def fake_fetch(self, playlist_id, *, limit=100, offset=0):
        return (
            {
                "__typename": "Playlist",
                "uri": f"spotify:playlist:{playlist_id}",
                "name": "QA",
                "basePermission": "BLOCKED",
                "content": {"items": [], "totalCount": 0},
            },
            1.0,
        )

    monkeypatch.setattr(type(adapter), "_web_fetch_playlist", fake_fetch)
    result = adapter.playlist("37i9dQZF1DXcBWIGoYBM5M")
    assert result["playlist"]["public"] is False
    assert result["playlist"]["permission_level"] == "BLOCKED"


def test_spotify_web_playlist_read_reports_public_true(
    tmp_path: Path, monkeypatch
) -> None:
    adapter = _spotify_adapter(tmp_path)
    monkeypatch.setattr(type(adapter), "_has_oauth_tokens", lambda self: False)
    monkeypatch.setattr(type(adapter), "_has_web_session", lambda self: True)

    def fake_fetch(self, playlist_id, *, limit=100, offset=0):
        return (
            {
                "__typename": "Playlist",
                "uri": f"spotify:playlist:{playlist_id}",
                "name": "QA",
                "basePermission": "VIEWER",
                "content": {"items": [], "totalCount": 0},
            },
            1.0,
        )

    monkeypatch.setattr(type(adapter), "_web_fetch_playlist", fake_fetch)
    result = adapter.playlist("37i9dQZF1DXcBWIGoYBM5M")
    assert result["playlist"]["public"] is True


def _github_adapter(tmp_path: Path):
    paths = StatePaths(tmp_path)
    Registry(paths).sync(str(bundled_registry()))
    return Runtime(paths).adapter("github")


def test_github_create_issue_uses_token_rest_path(tmp_path: Path, monkeypatch) -> None:
    # QA Critical: create_issue demanded a website session even though a valid
    # token drove every other GitHub write. It now uses the same token REST path.
    adapter = _github_adapter(tmp_path)
    calls: list[tuple[str, str, dict]] = []

    def fake_token(self) -> str:
        return "ghp_dummy"

    def fake_write(self, method, path, *, body=None):
        calls.append((method, path, body or {}))
        return (
            {
                "number": 7,
                "title": body["title"],
                "html_url": "https://github.com/o/r/issues/7",
                "node_id": "abc",
            },
            object(),
        )

    monkeypatch.setattr(type(adapter), "_token", fake_token)
    monkeypatch.setattr(type(adapter), "_write", fake_write)
    monkeypatch.setattr(type(adapter), "_meta", lambda self, response: {})

    result = adapter.create_issue("o", "r", "Bug", body="details", confirm=True)
    assert calls == [("POST", "/repos/o/r/issues", {"title": "Bug", "body": "details"})]
    assert result["created"] is True
    assert result["issue"]["number"] == 7
    assert result["issue"]["url"] == "https://github.com/o/r/issues/7"
    assert result["token_exposed"] is False


def test_github_create_issue_requires_confirm(tmp_path: Path, monkeypatch) -> None:
    adapter = _github_adapter(tmp_path)
    monkeypatch.setattr(type(adapter), "_token", lambda self: "ghp_dummy")
    with pytest.raises(Exception):
        adapter.create_issue("o", "r", "Bug", confirm=False)


def test_spotify_web_playlist_read_falls_back_to_permission_service(
    tmp_path: Path, monkeypatch
) -> None:
    # When the fetch payload omits basePermission, the read consults the
    # permission service so `public` is still reliably reported.
    adapter = _spotify_adapter(tmp_path)
    monkeypatch.setattr(type(adapter), "_has_oauth_tokens", lambda self: False)
    monkeypatch.setattr(type(adapter), "_has_web_session", lambda self: True)

    def fake_fetch(self, playlist_id, *, limit=100, offset=0):
        return (
            {
                "__typename": "Playlist",
                "uri": f"spotify:playlist:{playlist_id}",
                "name": "QA",
                "content": {"items": [], "totalCount": 0},
            },
            1.0,
        )

    def fake_permission(self, playlist_id, public=None):
        return {"permission_level": "BLOCKED", "public": False}, 2.0

    monkeypatch.setattr(type(adapter), "_web_fetch_playlist", fake_fetch)
    monkeypatch.setattr(type(adapter), "_web_playlist_permission", fake_permission)
    result = adapter.playlist("37i9dQZF1DXcBWIGoYBM5M")
    assert result["playlist"]["public"] is False
    assert result["playlist"]["permission_level"] == "BLOCKED"
