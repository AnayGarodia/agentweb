from __future__ import annotations

import json
from pathlib import Path
import pytest

from agentweb.analytics import Analytics, PRIVACY_NOTICE, summarize_events
from agentweb.dashboard import DashboardData
from agentweb.registry import Registry, bundled_registry
from agentweb.runtime import Runtime
from agentweb.sdk import AgentWebError
from agentweb.storage import StatePaths


def test_analytics_records_only_the_fixed_event_shape(tmp_path: Path) -> None:
    analytics = Analytics(StatePaths(tmp_path))
    analytics.record("setup_completed", success=True, interface="cli")
    analytics.record(
        "operation_completed",
        site="npm",
        operation="search_packages",
        success=True,
        duration_ms=125.5,
        interface="mcp",
        adapter_version="0.1.0",
    )

    rows = analytics.rows(days=None)
    summary = summarize_events(rows, days=None)

    assert len(rows) == 2
    assert set(rows[0]) == {
        "id",
        "created_at",
        "installation_id",
        "event",
        "site",
        "operation",
        "success",
        "duration_ms",
        "interface",
        "agentweb_version",
        "adapter_version",
        "error_code",
        "from_cache",
        "sent_at",
    }
    assert summary["totals"]["operations"] == 1
    assert summary["totals"]["activated_installations"] == 1
    assert summary["sites"][0]["site"] == "npm"
    assert "prompts" in PRIVACY_NOTICE


def test_disabling_telemetry_stops_local_recording(tmp_path: Path) -> None:
    analytics = Analytics(StatePaths(tmp_path))
    analytics.set_enabled(False)

    analytics.record(
        "operation_completed",
        site="npm",
        operation="search_packages",
        success=True,
    )

    assert analytics.rows(days=None) == []
    assert analytics.status()["enabled"] is False


def test_remote_event_contains_no_user_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTWEB_TELEMETRY_SYNC", "1")
    analytics = Analytics(StatePaths(tmp_path))
    analytics.configure_posthog("phc_test")
    captured: dict[str, object] = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("agentweb.analytics.urllib.request.urlopen", fake_urlopen)

    analytics.record(
        "operation_completed",
        site="wikipedia",
        operation="search",
        success=False,
        error_code="rate_limited",
        interface="mcp",
    )

    encoded = json.dumps(captured["payload"])
    assert "agentweb_operation_completed" in encoded
    assert "rate_limited" in encoded
    for private_field in (
        "prompt",
        "arguments",
        "website_response",
        "url",
        "cookie",
        "credential",
    ):
        assert private_field not in encoded


def test_runtime_records_success_and_structured_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = StatePaths(tmp_path)
    Registry(paths).sync(str(bundled_registry()))
    runtime = Runtime(paths, interface="mcp")

    monkeypatch.setattr(
        runtime,
        "_call_uninstrumented",
        lambda operation, arguments: {"ok": True},
    )
    assert runtime.call("npm.search_packages", {"query": "react"}) == {"ok": True}

    def fail(operation, arguments):
        raise AgentWebError("private detail", code="rate_limited")

    monkeypatch.setattr(runtime, "_call_uninstrumented", fail)
    with pytest.raises(AgentWebError):
        runtime.call("npm.search_packages", {"query": "private query"})

    rows = Analytics(paths).rows(days=None)
    assert [row["success"] for row in rows] == [0, 1]
    assert rows[0]["error_code"] == "rate_limited"
    assert rows[0]["interface"] == "mcp"
    assert "private" not in json.dumps(rows)


def test_dashboard_defaults_to_local_data(tmp_path: Path) -> None:
    paths = StatePaths(tmp_path)
    analytics = Analytics(paths)
    analytics.record(
        "operation_completed",
        site="arxiv",
        operation="search_papers",
        success=True,
        duration_ms=300,
        interface="cli",
    )

    dashboard = DashboardData(paths)
    result = dashboard.summary(30)

    assert result["source"] == "local"
    assert result["totals"]["operations"] == 1
    assert dashboard.status()["global_dashboard_connected"] is False


def test_posthog_configuration_stays_in_private_state(tmp_path: Path) -> None:
    paths = StatePaths(tmp_path)
    dashboard = DashboardData(paths)

    result = dashboard.connect_posthog(
        {
            "project_id": "123",
            "project_key": "phc_project",
            "personal_api_key": "phx_personal",
            "api_host": "https://us.posthog.com",
            "ingest_host": "https://us.i.posthog.com",
        }
    )

    assert result["global_dashboard_connected"] is True
    assert (tmp_path / "dashboard.json").stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path / "dashboard.json").read_text())[
        "posthog_personal_api_key"
    ] == "phx_personal"
    assert json.loads((tmp_path / "telemetry.json").read_text())[
        "posthog_project_key"
    ] == "phc_project"
