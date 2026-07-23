from __future__ import annotations

from http.cookiejar import Cookie
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentweb import connector
from agentweb.capture import compile_network_trace, verify_flow_capsule
from agentweb.cli import main as cli_main
from agentweb.mcp import dispatch
from agentweb.registry import (
    Registry,
    audit_registry,
    build_index,
    bundled_registry,
    generate_registry_keypair,
    verify_signed_index,
)
from agentweb.runtime import Runtime, WEB_COMMANDS
from agentweb.scaffold import create_adapter
from agentweb.sdk import (
    AdapterContext,
    AgentWebError,
    HttpSession,
    RequestRecipeAdapter,
    Response,
    _redirect_url,
)
from agentweb.storage import Cache, StatePaths


def synced_runtime(tmp_path: Path) -> Runtime:
    paths = StatePaths(tmp_path)
    Registry(paths).sync(str(bundled_registry()))
    return Runtime(paths)


def test_legacy_namespace_points_to_public_core() -> None:
    import agentweb
    import sitepack
    from sitepack.runtime import Runtime as LegacyRuntime

    assert sitepack.__version__ == agentweb.__version__
    assert LegacyRuntime is Runtime


def test_reference_registry_contains_only_public_adapters(tmp_path: Path) -> None:
    paths = StatePaths(tmp_path)
    result = Registry(paths).sync(str(bundled_registry()))

    assert result["available"] == ["arxiv", "npm", "wikipedia"]
    assert {item["name"] for item in Runtime(paths).sites()} == {
        "arxiv",
        "npm",
        "wikipedia",
    }


def test_domains_aliases_and_urls_resolve(tmp_path: Path) -> None:
    runtime = synced_runtime(tmp_path)

    assert runtime.resolve("npmjs.com").site == "npm"
    assert runtime.resolve("arxiv.org").site == "arxiv"
    assert runtime.resolve("https://en.wikipedia.org/wiki/Ada_Lovelace").site == "wikipedia"


def test_arxiv_url_uses_declared_typed_route(tmp_path: Path, monkeypatch) -> None:
    runtime = synced_runtime(tmp_path)
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        runtime,
        "call",
        lambda operation, arguments: calls.append((operation, arguments)) or {"ok": True},
    )

    result = runtime.get("https://arxiv.org/abs/1706.03762")

    assert result["data"] == {"ok": True}
    assert calls == [("arxiv.get_papers", {"ids": "1706.03762"})]


def test_public_runtime_never_inherits_mapping_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = synced_runtime(tmp_path)
    monkeypatch.setenv("AGENTWEB_MAPPING_MODE", "1")

    manifest = Runtime(runtime.paths, mapping_mode=False).describe("arxiv")

    assert set(WEB_COMMANDS).isdisjoint(manifest["commands"])


def test_runtime_rejects_unknown_inputs_before_network(tmp_path: Path) -> None:
    runtime = synced_runtime(tmp_path)

    with pytest.raises(AgentWebError) as error:
        runtime.call("npm.search_packages", {"query": "react", "surprise": True})

    assert error.value.code == "invalid_input"
    assert error.value.field == "surprise"


def test_network_trace_redacts_credentials() -> None:
    events = [
        {
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "1",
                "type": "Fetch",
                "initiator": {"type": "script"},
                "request": {
                    "method": "POST",
                    "url": "https://example.com/api?token=secret&query=music",
                    "headers": {"Cookie": "session=x", "Accept": "application/json"},
                    "postData": '{"password":"secret","name":"playlist"}',
                },
            },
        },
        {
            "method": "Network.responseReceived",
            "params": {
                "requestId": "1",
                "response": {
                    "status": 200,
                    "url": "https://example.com/api",
                    "headers": {"Set-Cookie": "private", "Content-Type": "application/json"},
                },
            },
        },
    ]

    trace = compile_network_trace(
        events,
        site="example",
        profile="default",
        allowed_domains=["example.com"],
        action_steps=[{"action": "fill", "value": "secret"}],
        page_before=None,
        page_after=None,
    )
    encoded = json.dumps(trace)

    assert trace["request_count"] == 1
    assert "session=x" not in encoded
    assert '"password": "secret"' not in encoded
    assert '"value": "secret"' not in encoded
    assert "playlist" in encoded


def test_flow_capsule_rejects_embedded_credentials() -> None:
    capsule = {
        "kind": "agentweb_flow_capsule",
        "schema_version": 1,
        "site": "example",
        "operation": "read",
        "recipe": {"steps": [{"method": "GET", "path": "/"}]},
        "observed": {"steps": [{"status": 200}]},
        "cookie": "private-value",
    }

    result = verify_flow_capsule(capsule)

    assert result["passed"] is False
    assert "capsule may contain a captured credential" in result["errors"]


def test_cache_and_cookie_state_are_profile_isolated(tmp_path: Path) -> None:
    arguments = {"owner": "private-org"}
    assert Cache.key("example", "read", arguments, profile="alice") != Cache.key(
        "example", "read", arguments, profile="bob"
    )

    alice = HttpSession(StatePaths(tmp_path), "example", "alice")
    bob = HttpSession(StatePaths(tmp_path), "example", "bob")
    alice.import_cookie_header("session=alice", ".example.com")

    assert {cookie.value for cookie in alice.cookies} == {"alice"}
    assert list(bob.cookies) == []


def test_concurrent_cookie_saves_merge_distinct_updates(tmp_path: Path) -> None:
    paths = StatePaths(tmp_path)
    first = HttpSession(paths, "example", "default")
    stale_second = HttpSession(paths, "example", "default")

    first.import_cookie_header("first=one", ".example.com")
    stale_second.import_cookie_header("second=two", ".example.com")

    values = {
        cookie.name: cookie.value
        for cookie in HttpSession(paths, "example", "default").cookies
    }
    assert values == {"first": "one", "second": "two"}


def test_profile_names_cannot_escape_storage(tmp_path: Path) -> None:
    paths = StatePaths(tmp_path / "state")

    for profile in ("../outside", "../../../outside", "/tmp/outside"):
        with pytest.raises((AgentWebError, ValueError), match="profile"):
            Runtime(paths, profile=profile)


def test_redirects_are_https_and_allowlisted() -> None:
    assert _redirect_url(
        "https://api.example.com/start",
        "https://www.example.com/next",
        allowed_domains=("example.com",),
    ) == "https://www.example.com/next"

    with pytest.raises(AgentWebError, match="outside the allowed domains"):
        _redirect_url(
            "https://api.example.com/start",
            "https://attacker.invalid/collect",
            allowed_domains=("example.com",),
        )
    with pytest.raises(AgentWebError, match="downgrade"):
        _redirect_url(
            "https://api.example.com/start",
            "http://api.example.com/collect",
            allowed_domains=("example.com",),
        )


def test_mcp_surface_stays_constant() -> None:
    initialized = dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    listed = dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert initialized["result"]["serverInfo"]["name"] == "agentweb"
    assert [tool["name"] for tool in listed["result"]["tools"]] == [
        "sites_list",
        "site_describe",
        "site_call",
        "site_connect",
    ]


def test_setup_connects_detected_agents_automatically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    class FakeRuntime:
        @staticmethod
        def sites() -> list[dict[str, str]]:
            return [{"name": "npm"}, {"name": "arxiv"}, {"name": "wikipedia"}]

    monkeypatch.setattr("agentweb.cli.StatePaths.discover", lambda: StatePaths(tmp_path))
    monkeypatch.setattr("agentweb.cli.Runtime", lambda *args, **kwargs: FakeRuntime())
    monkeypatch.setattr(
        "agentweb.cli.setup_detected_agents",
        lambda runtime: {
            "ready": True,
            "agentweb": "installed",
            "registry": {"available": ["arxiv", "npm", "wikipedia"]},
            "detected_agents": ["claude", "codex"],
            "agent_connections": [
                {"agent": "claude", "installed": True},
                {"agent": "codex", "installed": True},
            ],
            "errors": [],
        },
    )

    assert cli_main(["setup"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["ready"] is True
    assert result["detected_agents"] == ["claude", "codex"]
    assert result["sites"] == ["arxiv", "npm", "wikipedia"]
    assert "Restart any detected coding agent" in result["next"]


def test_detected_agent_setup_registers_claude_and_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRegistry:
        @staticmethod
        def sync() -> dict[str, list[str]]:
            return {"available": ["npm"]}

    class FakeRuntime:
        registry = FakeRegistry()

    binaries = {
        "agentweb": "/tools/agentweb",
        "claude": "/tools/claude",
        "codex": "/tools/codex",
    }
    calls: list[list[str]] = []
    monkeypatch.setattr(connector.shutil, "which", lambda name: binaries.get(name))
    monkeypatch.setattr(
        connector.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args)
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = connector.setup_detected_agents(FakeRuntime())

    assert result["ready"] is True
    assert result["detected_agents"] == ["claude", "codex"]
    assert [item["agent"] for item in result["agent_connections"]] == [
        "claude",
        "codex",
    ]
    assert ["/tools/claude", "mcp", "add", "--scope", "user", "agentweb", "--", "/tools/agentweb", "mcp"] in calls
    assert ["/tools/codex", "mcp", "add", "agentweb", "--", "/tools/agentweb", "mcp"] in calls


def test_scaffold_is_browserless_and_honest(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    created = create_adapter(root, "example", "https://example.com")
    source = Path(created["adapter"]).read_text()
    audit = audit_registry(root, "example")["sites"][0]

    assert "RequestRecipeAdapter" in source
    assert audit["browserless_replay"] is True
    assert audit["exhaustive"] is False
    assert audit["not_mapped"]
    assert build_index(root)["sites"][0]["name"] == "example"


def test_remote_registry_signature_detects_tampering(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    create_adapter(root, "example", "https://example.com")
    private_key = tmp_path / "registry.key"
    public_key = tmp_path / "registry.pub"
    generate_registry_keypair(private_key, public_key)
    index = build_index(root, signing_key=private_key)

    assert verify_signed_index(index, str(public_key)) == index["signature"]["key_id"]
    index["sites"][0]["version"] = "9.9.9"
    with pytest.raises(AgentWebError, match="signature verification failed"):
        verify_signed_index(index, str(public_key))


def test_recipe_write_requires_confirmation_and_hides_extracted_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Adapter(RequestRecipeAdapter):
        site_name = "example"
        base_url = "https://example.com"
        allowed_domains = ("example.com",)
        recipes = {
            "rename": {
                "steps": [
                    {
                        "name": "preflight",
                        "method": "GET",
                        "path": "/settings",
                        "extract": {
                            "csrf": {
                                "source": "html",
                                "selector": "input[name=csrf]",
                                "attribute": "value",
                            }
                        },
                    },
                    {
                        "name": "mutation",
                        "method": "POST",
                        "path": "/settings",
                        "form": {"csrf": "{csrf}", "name": "{name}"},
                    },
                ]
            }
        }

    adapter = Adapter(AdapterContext(StatePaths(tmp_path)))

    def request(method: str, url: str, **_kwargs) -> Response:
        if method == "GET":
            return Response(200, url, {}, b'<input name="csrf" value="private-token">', 1.0)
        return Response(200, url, {}, b'{"ok":true}', 1.0)

    monkeypatch.setattr(adapter.session(), "request", request)
    with pytest.raises(AgentWebError, match="confirm=true"):
        adapter.call("rename", {"name": "Ada"})

    result = adapter.call("rename", {"name": "Ada", "confirm": True})

    assert result["state_changed"] is True
    assert "private-token" not in json.dumps(result)


def test_authenticated_requests_are_not_cacheable(tmp_path: Path) -> None:
    session = HttpSession(StatePaths(tmp_path), "example", "default")
    assert session._has_request_credentials(
        "https://example.com/private", {"Authorization": "Bearer private"}
    )
    session.cookies.set_cookie(
        Cookie(
            version=0,
            name="session",
            value="private",
            port=None,
            port_specified=False,
            domain="example.com",
            domain_specified=True,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
    )
    assert session._has_request_credentials("https://example.com/private", None)


def test_public_registry_audit_reports_real_state() -> None:
    audit = audit_registry(bundled_registry())
    sites = {item["name"]: item for item in audit["sites"]}

    assert set(sites) == {"arxiv", "npm", "wikipedia"}
    assert sites["arxiv"]["exhaustive"] is True
    assert sites["npm"]["exhaustive"] is True
    assert sites["wikipedia"]["browserless_replay"] is True
