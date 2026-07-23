from __future__ import annotations

import json
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlparse

from .analytics import Analytics, posthog_rows, summarize_events
from .storage import StatePaths, read_json, write_json


class DashboardData:
    def __init__(self, paths: StatePaths) -> None:
        self.paths = paths
        self.analytics = Analytics(paths)
        self.config_path = paths.root / "dashboard.json"

    def config(self) -> dict[str, Any]:
        file_config = read_json(self.config_path, {}) or {}
        return {
            "api_host": os.environ.get("AGENTWEB_POSTHOG_API_HOST")
            or file_config.get("posthog_api_host")
            or "https://us.posthog.com",
            "project_id": os.environ.get("AGENTWEB_POSTHOG_PROJECT_ID")
            or file_config.get("posthog_project_id"),
            "personal_api_key": os.environ.get("AGENTWEB_POSTHOG_PERSONAL_API_KEY")
            or file_config.get("posthog_personal_api_key"),
        }

    def status(self) -> dict[str, Any]:
        config = self.config()
        telemetry = self.analytics.status()
        return {
            "telemetry": telemetry,
            "global_dashboard_connected": bool(
                config.get("project_id") and config.get("personal_api_key")
            ),
            "posthog_project_id": config.get("project_id"),
            "posthog_api_host": config.get("api_host"),
        }

    def connect_posthog(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "").strip()
        personal_api_key = str(payload.get("personal_api_key") or "").strip()
        project_key = str(payload.get("project_key") or "").strip()
        api_host = str(payload.get("api_host") or "https://us.posthog.com").rstrip("/")
        ingest_host = str(
            payload.get("ingest_host") or "https://us.i.posthog.com"
        ).rstrip("/")
        if not project_id.isdigit():
            raise ValueError("Project ID must be numeric")
        if not personal_api_key:
            raise ValueError("Personal API key is required to read global analytics")
        if not project_key:
            raise ValueError("Project key is required to send analytics")
        if not api_host.startswith("https://") or not ingest_host.startswith("https://"):
            raise ValueError("PostHog hosts must use HTTPS")
        write_json(
            self.config_path,
            {
                "posthog_api_host": api_host,
                "posthog_project_id": project_id,
                "posthog_personal_api_key": personal_api_key,
            },
        )
        self.analytics.configure_posthog(project_key, ingest_host)
        return self.status()

    def summary(self, days: int | None) -> dict[str, Any]:
        config = self.config()
        if config.get("project_id") and config.get("personal_api_key"):
            try:
                rows = posthog_rows(
                    host=str(config["api_host"]),
                    project_id=str(config["project_id"]),
                    personal_api_key=str(config["personal_api_key"]),
                    days=90 if days is None else max(days, 7),
                )
                return summarize_events(rows, days=days, source="global")
            except Exception as exc:
                result = summarize_events(
                    self.analytics.rows(days=days), days=days, source="local"
                )
                result["source_error"] = (
                    "Global analytics could not be loaded. Showing this device only: "
                    + type(exc).__name__
                )
                return result
        return summarize_events(
            self.analytics.rows(days=days), days=days, source="local"
        )


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        data: DashboardData,
        session_token: str,
    ) -> None:
        super().__init__(address, DashboardHandler)
        self.data = data
        self.session_token = session_token


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            template = (
                files("agentweb").joinpath("dashboard/index.html").read_text()
            )
            body = template.replace(
                "__AGENTWEB_DASHBOARD_TOKEN__", self.server.session_token
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/status":
            self._json(self.server.data.status())
            return
        if parsed.path == "/api/summary":
            raw = (parse_qs(parsed.query).get("days") or ["30"])[0]
            try:
                days = None if raw == "all" else max(1, min(int(raw), 365))
            except ValueError:
                self._json({"error": "invalid_window"}, 400)
                return
            self._json(self.server.data.summary(days))
            return
        self._json({"error": "not_found"}, 404)

    def do_POST(self) -> None:
        if self.headers.get("X-AgentWeb-Dashboard-Token") != self.server.session_token:
            self._json({"error": "forbidden"}, 403)
            return
        if self.path != "/api/connect-posthog":
            self._json({"error": "not_found"}, 404)
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 16_384)
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be an object")
            self._json(self.server.data.connect_posthog(payload))
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"error": "invalid_configuration", "message": str(exc)}, 400)


def serve_dashboard(
    paths: StatePaths | None = None,
    *,
    port: int = 0,
    open_browser: bool = True,
    ready_event: threading.Event | None = None,
) -> int:
    state_paths = paths or StatePaths.discover()
    token = secrets.token_urlsafe(24)
    server = DashboardServer(
        ("127.0.0.1", port), DashboardData(state_paths), token
    )
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"AgentWeb dashboard: {url}", flush=True)
    if ready_event:
        ready_event.set()
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
