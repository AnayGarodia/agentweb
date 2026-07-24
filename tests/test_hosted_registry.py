"""The hosted registry keeps installs current without tool upgrades."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentweb.registry import (
    DEFAULT_REMOTE_PUBLIC_KEY,
    DEFAULT_REMOTE_REGISTRY,
    Registry,
    bundled_registry,
)
from agentweb.sdk import AgentWebError
from agentweb.storage import StatePaths, write_json


def test_fresh_install_defaults_to_hosted_registry(tmp_path: Path) -> None:
    assert (
        Registry(StatePaths(tmp_path)).configured_source() == DEFAULT_REMOTE_REGISTRY
    )


def test_builtin_config_defaults_to_hosted_registry(tmp_path: Path) -> None:
    paths = StatePaths(tmp_path)
    write_json(paths.registry_config, {"source": "builtin"})
    assert Registry(paths).configured_source() == DEFAULT_REMOTE_REGISTRY


def test_explicit_custom_source_is_preserved(tmp_path: Path) -> None:
    paths = StatePaths(tmp_path)
    custom = tmp_path / "my_registry"
    custom.mkdir()
    write_json(paths.registry_config, {"source": str(custom)})
    assert Registry(paths).configured_source() == str(custom)


def test_default_sync_pins_the_hosted_public_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry(StatePaths(tmp_path))
    seen: dict[str, str | None] = {}

    def fake_sync(source: str, *, trusted_public_key=None, prune=False):
        seen["source"] = source
        seen["key"] = trusted_public_key
        return {"available": []}

    monkeypatch.setattr(registry, "_sync", fake_sync)
    registry.sync()
    assert seen["source"] == DEFAULT_REMOTE_REGISTRY
    assert seen["key"] == DEFAULT_REMOTE_PUBLIC_KEY


def test_default_sync_falls_back_to_bundle_when_host_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry(StatePaths(tmp_path))
    real_sync = registry._sync

    def fake_sync(source: str, *, trusted_public_key=None, prune=False):
        if source == DEFAULT_REMOTE_REGISTRY:
            raise OSError("network unreachable")
        return real_sync(source, trusted_public_key=trusted_public_key, prune=prune)

    monkeypatch.setattr(registry, "_sync", fake_sync)
    result = registry.sync()
    assert "fallback" in result
    assert result["available"]


def test_explicit_remote_source_still_requires_a_trusted_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry(StatePaths(tmp_path))
    monkeypatch.setattr(
        "agentweb.registry._index_source",
        lambda source: ({"schema_version": 2, "sites": []}, source),
    )
    with pytest.raises(AgentWebError, match="trusted Ed25519 public key"):
        registry.sync("https://example.com/registry")


def test_bundled_sync_stays_local(tmp_path: Path) -> None:
    result = Registry(StatePaths(tmp_path)).sync(str(bundled_registry()))
    assert "fallback" not in result
    assert result["available"]
