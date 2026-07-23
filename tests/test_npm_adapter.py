from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

import agentweb.runtime as runtime_module
from agentweb.cli import main as cli_main
from agentweb.registry import Registry, audit_registry, bundled_registry
from agentweb.runtime import Runtime
from agentweb.sdk import AgentWebError, Response
from agentweb.storage import StatePaths


def npm_adapter(tmp_path: Path):
    paths = StatePaths(tmp_path)
    Registry(paths).sync(str(bundled_registry()))
    return Runtime(paths, fresh=True).adapter("npm")


def response(url: str, value, status: int = 200, content_type: str = "application/json"):
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    return Response(status, url, {"content-type": content_type}, body, 1.0)


def tarball(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, body in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(body)
            archive.addfile(item, io.BytesIO(body))
    return output.getvalue()


def test_npm_manifest_is_a_public_reference_adapter(tmp_path: Path) -> None:
    audit = audit_registry(bundled_registry(), "npm")["sites"][0]
    index = json.loads((bundled_registry() / "index.json").read_text())
    bundle = bundled_registry() / "sites" / "npm" / "0.1.0"
    manifest = json.loads((bundle / "manifest.json").read_text())
    evidence = json.loads((bundle / "flows" / "get_package.json").read_text())

    assert {entry["name"] for entry in index["sites"]} == {
        "arxiv",
        "npm",
        "wikipedia",
    }
    assert audit["exhaustive"] is True
    assert audit["typed_commands"] == 17
    assert audit["url_route_count"] == 5
    assert audit["runtime_browser_dependency"] is False
    assert audit["escape_hatches"] == []
    assert audit["unverified_commands"] == []
    assert manifest["compatibility"]["tested_runtimes"] == [
        "0.16.0",
        "0.16.3",
        "0.16.4",
        "0.16.5",
    ]
    assert evidence["observed"]["cases"] == [
        "success",
        "live_cli_success",
        "live_mcp_success",
        "missing_optional",
        "null_optional",
        "partial_record",
        "malformed_json",
        "unexpected_json_shape",
        "not_found",
        "rate_limited",
    ]
    assert evidence["verification"]["runner"]["runtime_matrix"]["0.16.5"] == [
        "adapter_import",
        "domain_first_cli",
        "mcp",
    ]


def test_npm_search_tolerates_normal_empty_and_missing_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = npm_adapter(tmp_path)
    payloads = [
        {
            "total": 1,
            "objects": [
                {
                    "package": {
                        "name": "example",
                        "version": "1.0.0",
                        "description": "Example package",
                        "keywords": None,
                        "publisher": {"username": "owner"},
                        "maintainers": [None, {"username": "owner"}],
                    },
                    "score": {"final": 0.9, "detail": {"quality": 0.8}},
                }
            ],
        },
        {"total": 0, "objects": []},
        {},
    ]
    monkeypatch.setattr(
        adapter.session(),
        "request",
        lambda _method, url, **_kwargs: response(url, payloads.pop(0)),
    )

    normal = adapter.search_packages("example", limit=10)["data"]
    empty = adapter.search_packages("nothing", limit=10)["data"]
    missing = adapter.search_packages("missing", limit=10)["data"]

    assert normal["packages"][0]["keywords"] == []
    assert normal["packages"][0]["publisher"] == {"username": "owner"}
    assert normal["packages"][0]["maintainers"] == [{"username": "owner"}]
    assert empty == {"query": "nothing", "packages": [], "total": 0}
    assert missing == {"query": "missing", "packages": [], "total": 0}


def test_npm_package_and_version_normalization_accepts_sparse_optional_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = npm_adapter(tmp_path)
    sparse = {
        "name": "sparse-package",
        "dist-tags": {"latest": "1.0.0"},
        "versions": {
            "1.0.0": {
                "name": "sparse-package",
                "version": "1.0.0",
                "description": None,
                "keywords": None,
                "dependencies": None,
                "peerDependencies": {},
                "maintainers": [None],
                "dist": {"tarball": "https://registry.npmjs.org/sparse-package/-/sparse-package-1.0.0.tgz"},
            }
        },
        "maintainers": None,
        "readme": None,
        "time": None,
    }
    monkeypatch.setattr(
        adapter.session(),
        "request",
        lambda _method, url, **_kwargs: response(url, sparse),
    )

    package = adapter.get_package("sparse-package")["data"]

    assert package["name"] == "sparse-package"
    assert package["version_count"] == 1
    assert package["maintainers"] == []
    assert package["latest"]["dependencies"] == {
        "runtime": {},
        "development": {},
        "peer": {},
        "optional": {},
        "bundled": [],
    }
    assert package["readme_preview"] == ""
    assert package["readme_truncated"] is False


def test_npm_empty_dependency_and_provenance_results_are_not_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = npm_adapter(tmp_path)

    def fake_request(_method: str, url: str, **_kwargs):
        if "attestations" in url:
            return response(url, {"attestations": []})
        return response(
            url,
            {
                "name": "empty-package",
                "version": "1.0.0",
                "dependencies": None,
                "optionalDependencies": {},
                "dist": {},
            },
        )

    monkeypatch.setattr(adapter.session(), "request", fake_request)

    dependencies = adapter.list_dependencies("empty-package")["data"]
    provenance = adapter.get_provenance("empty-package")["data"]

    assert dependencies["dependencies"] == []
    assert provenance["attestations"] == []
    assert provenance["verified_attestation_count"] == 0


def test_npm_dependents_uses_browser_compatible_public_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = npm_adapter(tmp_path)
    calls: list[tuple[str, str | None]] = []

    def fake_request(_method: str, url: str, **kwargs):
        calls.append((url, kwargs.get("impersonate")))
        if "/browse/depended/" in url:
            return response(
                url,
                b'<a href="/package/one">one</a><a href="/package/%40scope%2Ftwo">two</a><a href="?offset=36">Next Page</a>',
                content_type="text/html",
            )
        return response(
            url,
            b"<main><span>1,234 Dependents</span></main>",
            content_type="text/html",
        )

    monkeypatch.setattr(adapter.session(), "request", fake_request)
    response_value = adapter.list_dependents("react", limit=1)
    result = response_value["data"]

    assert result == {
        "package": "react",
        "dependents": ["one"],
        "total": 1234,
    }
    assert response_value["pagination"]["next_cursor"] == "1"
    assert all(impersonate == "chrome" for _url, impersonate in calls)


def test_npm_tarball_listing_and_reading_are_bounded_and_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = npm_adapter(tmp_path)
    archive = tarball(
        {
            "package/package.json": b'{"name":"fixture"}',
            "package/index.js": b"module.exports = 1;\n",
        }
    )

    def fake_request(_method: str, url: str, **_kwargs):
        if url.endswith(".tgz"):
            return response(url, archive, content_type="application/octet-stream")
        return response(
            url,
            {
                "name": "fixture",
                "version": "1.0.0",
                "dist": {"tarball": "https://registry.npmjs.org/fixture/-/fixture-1.0.0.tgz"},
            },
        )

    monkeypatch.setattr(adapter.session(), "request", fake_request)
    files = adapter.list_package_files("fixture", limit=10)["data"]["files"]
    source = adapter.read_package_file("fixture", "index.js")["data"]

    assert [item["path"] for item in files] == ["index.js", "package.json"]
    assert source["encoding"] == "utf-8"
    assert source["content"] == "module.exports = 1;\n"
    assert len(source["sha256"]) == 64

    unsafe = tarball({"package/../escape": b"no"})
    monkeypatch.setattr(
        adapter,
        "_tarball",
        lambda *_args: ({"name": "fixture", "version": "1.0.0"}, unsafe),
    )
    with pytest.raises(AgentWebError) as error:
        adapter.list_package_files("fixture")
    assert error.value.code == "unsafe_tarball_path"


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (404, {}, "package_not_found"),
        (429, {}, "npm_rate_limited"),
        (200, b"not-json", "invalid_npm_response"),
    ],
)
def test_npm_errors_are_structured_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body,
    code: str,
) -> None:
    adapter = npm_adapter(tmp_path)
    monkeypatch.setattr(
        adapter.session(),
        "request",
        lambda _method, url, **_kwargs: response(url, body, status=status),
    )

    with pytest.raises(AgentWebError) as error:
        adapter.get_package("missing-package")

    assert error.value.code == code
    assert str(tmp_path) not in str(error.value)


def test_npm_rejects_valid_json_with_an_unexpected_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = npm_adapter(tmp_path)
    monkeypatch.setattr(
        adapter.session(),
        "request",
        lambda _method, url, **_kwargs: response(url, []),
    )

    with pytest.raises(AgentWebError) as error:
        adapter.get_package("react")

    assert error.value.code == "invalid_npm_response"
    assert error.value.next_action == "npm.get_registry_status"


def test_npm_adapter_imports_without_new_sdk_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agentweb.sdk as sdk_module

    paths = StatePaths(tmp_path)
    Registry(paths).sync(str(bundled_registry()))
    monkeypatch.delattr(sdk_module, "parse_xml_feed")
    monkeypatch.delattr(sdk_module, "paginate_operation_output")

    adapter = Runtime(paths).adapter("npm")

    assert adapter.site_name == "npm"
    assert callable(adapter.search_packages)


def test_runtime_rejects_adapter_newer_than_running_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = StatePaths(tmp_path)
    Registry(paths).sync(str(bundled_registry()))
    runtime = Runtime(paths)
    monkeypatch.setattr(runtime_module, "__version__", "0.15.9")

    with pytest.raises(AgentWebError) as error:
        runtime.adapter("npm")

    assert error.value.code == "adapter_runtime_incompatible"
    assert error.value.details["minimum_runtime"] == "0.16.0"
    assert str(paths.root) not in str(error.value)


def test_npm_urls_and_common_search_resolve_to_typed_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = StatePaths(tmp_path)
    Registry(paths).sync(str(bundled_registry()))
    runtime = Runtime(paths)
    calls: list[tuple[str, dict]] = []

    def fake_call(operation: str, arguments: dict) -> dict:
        calls.append((operation, arguments))
        return {"ok": True}

    monkeypatch.setattr(runtime, "call", fake_call)
    runtime.get("https://www.npmjs.com/package/%40scope%2Fpackage")
    runtime.get("https://www.npmjs.com/~ljharb")
    runtime.get("https://docs.npmjs.com/about-npm/")
    runtime.execute("npmjs.com", "search", {"query": "markdown"})

    assert calls == [
        ("npm.get_package", {"package": "@scope/package"}),
        ("npm.list_maintainer_packages", {"username": "ljharb"}),
        ("npm.read_site_resource", {"url": "https://docs.npmjs.com/about-npm/"}),
        ("npm.search_packages", {"query": "markdown"}),
    ]


def test_dynamic_cli_does_not_consume_operation_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        Runtime,
        "resolve",
        lambda self, target: type(
            "Resolved", (), {"site": "npm", "domain": "npmjs.com"}
        )(),
    )
    monkeypatch.setattr(Runtime, "resolve_action", lambda self, site, action: action)
    monkeypatch.setattr(
        Runtime,
        "describe",
        lambda self, site: {
            "commands": {
                "get_version": {
                    "input_schema": {
                        "type": "object",
                        "required": ["package", "version", "profile"],
                        "properties": {
                            "package": {"type": "string"},
                            "version": {"type": "string"},
                            "profile": {"type": "string"},
                            "fresh": {"type": "boolean"},
                            "compact": {"type": "boolean"},
                            "mapping_mode": {"type": "boolean"},
                        },
                    }
                }
            }
        },
    )
    monkeypatch.setattr(
        Runtime,
        "execute",
        lambda self, site, action, arguments: {
            "site": site,
            "action": action,
            "arguments": arguments,
        },
    )

    assert (
        cli_main(
            [
                "npmjs.com",
                "get_version",
                "--package",
                "react",
                "--version",
                "latest",
                "--profile",
                "public-view",
                "--fresh",
                "--compact",
                "--mapping-mode",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["arguments"] == {
        "package": "react",
        "version": "latest",
        "profile": "public-view",
        "fresh": True,
        "compact": True,
        "mapping_mode": True,
    }
