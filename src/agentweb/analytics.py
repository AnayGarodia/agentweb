from __future__ import annotations

import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .storage import StatePaths, read_json, write_json


PRIVACY_NOTICE = (
    "AgentWeb records anonymous counts, operation names, outcomes, and timing. "
    "It never records prompts, arguments, website responses, URLs, credentials, "
    "cookies, account names, or IP addresses."
)
EVENT_PREFIX = "agentweb_"
SAFE_EVENTS = {
    "setup_completed",
    "agent_connected",
    "operation_completed",
    "connection_requested",
    "connection_completed",
    "update_completed",
}
SAFE_INTERFACES = {"cli", "mcp", "workflow", "unknown"}


def _env_enabled() -> bool | None:
    value = os.environ.get("AGENTWEB_TELEMETRY")
    if value is None:
        return None
    return value.strip().lower() not in {"0", "false", "off", "no", "disabled"}


def _safe_label(value: Any, *, maximum: int = 128) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-/"
    cleaned = "".join(character for character in text if character in allowed)
    return cleaned[:maximum] or None


class Analytics:
    """Privacy-filtered local analytics with optional best-effort PostHog delivery."""

    def __init__(self, paths: StatePaths | None = None) -> None:
        self.paths = paths or StatePaths.discover()
        self.config_path = self.paths.root / "telemetry.json"
        self.database_path = self.paths.root / "analytics.sqlite3"
        self.defaults = self._load_defaults()
        self.config = self._load_config()
        self._initialize()

    def _load_config(self) -> dict[str, Any]:
        config = read_json(self.config_path, {}) or {}
        changed = False
        if not config.get("installation_id"):
            config["installation_id"] = str(uuid.uuid4())
            changed = True
        if "enabled" not in config:
            config["enabled"] = True
            changed = True
        if config.get("notice_version") != 1:
            config["notice_version"] = 1
            changed = True
        if changed:
            write_json(self.config_path, config)
        return config

    @staticmethod
    def _load_defaults() -> dict[str, Any]:
        try:
            return json.loads(
                files("agentweb").joinpath("telemetry-defaults.json").read_text()
            )
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    @property
    def enabled(self) -> bool:
        override = _env_enabled()
        return bool(self.config.get("enabled", True) if override is None else override)

    @property
    def installation_id(self) -> str:
        return str(self.config["installation_id"])

    @property
    def posthog_key(self) -> str | None:
        return os.environ.get("AGENTWEB_POSTHOG_KEY") or self.config.get(
            "posthog_project_key"
        ) or self.defaults.get("posthog_project_key")

    @property
    def posthog_host(self) -> str:
        return str(
            os.environ.get("AGENTWEB_POSTHOG_HOST")
            or self.config.get("posthog_host")
            or self.defaults.get("posthog_host")
            or "https://us.i.posthog.com"
        ).rstrip("/")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                  id TEXT PRIMARY KEY,
                  created_at REAL NOT NULL,
                  installation_id TEXT NOT NULL,
                  event TEXT NOT NULL,
                  site TEXT,
                  operation TEXT,
                  success INTEGER,
                  duration_ms REAL,
                  interface TEXT NOT NULL,
                  agentweb_version TEXT NOT NULL,
                  adapter_version TEXT,
                  error_code TEXT,
                  from_cache INTEGER,
                  sent_at REAL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS events_created_at ON events(created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS events_site ON events(site, operation)"
            )

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        self.config["enabled"] = bool(enabled)
        write_json(self.config_path, self.config)
        return self.status()

    def reset_installation_id(self) -> dict[str, Any]:
        self.config["installation_id"] = str(uuid.uuid4())
        write_json(self.config_path, self.config)
        return self.status()

    def configure_posthog(self, project_key: str, host: str | None = None) -> dict[str, Any]:
        self.config["posthog_project_key"] = project_key.strip()
        if host:
            self.config["posthog_host"] = host.rstrip("/")
        write_json(self.config_path, self.config)
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            local_events = int(
                connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            )
            pending = int(
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE sent_at IS NULL"
                ).fetchone()[0]
            )
        return {
            "enabled": self.enabled,
            "installation_id": self.installation_id,
            "local_events": local_events,
            "pending_remote_events": pending if self.posthog_key else 0,
            "remote_collection_configured": bool(self.posthog_key),
            "remote_host": self.posthog_host if self.posthog_key else None,
            "privacy": PRIVACY_NOTICE,
            "disable": "agentweb telemetry disable",
            "inspect": "agentweb telemetry inspect",
        }

    def inspect_event(self) -> dict[str, Any]:
        return {
            "event": "operation_completed",
            "properties": {
                "site": "wikipedia",
                "operation": "search",
                "success": True,
                "duration_ms": 420.0,
                "interface": "mcp",
                "agentweb_version": __version__,
                "adapter_version": "0.8.5",
                "error_code": None,
                "from_cache": False,
                "os_family": platform.system().lower(),
            },
            "never_collected": [
                "prompt",
                "arguments",
                "website_response",
                "url",
                "credentials",
                "cookies",
                "account_identity",
                "ip_address",
            ],
        }

    def record(
        self,
        event: str,
        *,
        site: str | None = None,
        operation: str | None = None,
        success: bool | None = None,
        duration_ms: float | None = None,
        interface: str = "unknown",
        adapter_version: str | None = None,
        error_code: str | None = None,
        from_cache: bool | None = None,
    ) -> None:
        if not self.enabled or event not in SAFE_EVENTS:
            return
        event_id = str(uuid.uuid4())
        created_at = time.time()
        safe_interface = interface if interface in SAFE_INTERFACES else "unknown"
        row = {
            "id": event_id,
            "created_at": created_at,
            "installation_id": self.installation_id,
            "event": event,
            "site": _safe_label(site),
            "operation": _safe_label(operation),
            "success": None if success is None else int(bool(success)),
            "duration_ms": (
                None
                if duration_ms is None
                else max(0.0, min(float(duration_ms), 86_400_000.0))
            ),
            "interface": safe_interface,
            "agentweb_version": __version__,
            "adapter_version": _safe_label(adapter_version),
            "error_code": _safe_label(error_code),
            "from_cache": None if from_cache is None else int(bool(from_cache)),
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO events (
                  id, created_at, installation_id, event, site, operation, success,
                  duration_ms, interface, agentweb_version, adapter_version,
                  error_code, from_cache
                ) VALUES (
                  :id, :created_at, :installation_id, :event, :site, :operation,
                  :success, :duration_ms, :interface, :agentweb_version,
                  :adapter_version, :error_code, :from_cache
                )
                """,
                row,
            )
        if self.posthog_key:
            if os.environ.get("AGENTWEB_TELEMETRY_SYNC") == "1":
                self.flush_pending(limit=100)
            else:
                self._start_uploader()

    @property
    def upload_marker(self) -> Path:
        return self.paths.root / ".telemetry-uploading"

    def _start_uploader(self) -> None:
        try:
            if self.upload_marker.exists():
                if time.time() - self.upload_marker.stat().st_mtime < 60:
                    return
                self.upload_marker.unlink(missing_ok=True)
            descriptor = os.open(
                self.upload_marker,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            os.close(descriptor)
        except (FileExistsError, OSError):
            return
        environment = dict(os.environ)
        environment["AGENTWEB_TELEMETRY_CHILD"] = "1"
        try:
            subprocess.Popen(
                [sys.executable, "-m", "agentweb.analytics", "--flush"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                env=environment,
            )
        except OSError:
            self.upload_marker.unlink(missing_ok=True)

    def flush_pending(self, *, limit: int = 100) -> dict[str, int]:
        if not self.posthog_key:
            return {"sent": 0, "remaining": 0}
        sent = 0
        with self._connect() as connection:
            records = connection.execute(
                "SELECT * FROM events WHERE sent_at IS NULL ORDER BY created_at LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        for record in records:
            row = dict(record)
            if not self._send(row):
                break
            sent += 1
            with self._connect() as connection:
                connection.execute(
                    "UPDATE events SET sent_at = ? WHERE id = ?",
                    (time.time(), row["id"]),
                )
        with self._connect() as connection:
            remaining = int(
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE sent_at IS NULL"
                ).fetchone()[0]
            )
        return {"sent": sent, "remaining": remaining}

    def _send(self, row: dict[str, Any]) -> bool:
        properties = {
            "distinct_id": row["installation_id"],
            "site": row["site"],
            "operation": row["operation"],
            "success": (
                None if row["success"] is None else bool(row["success"])
            ),
            "duration_ms": row["duration_ms"],
            "interface": row["interface"],
            "agentweb_version": row["agentweb_version"],
            "adapter_version": row["adapter_version"],
            "error_code": row["error_code"],
            "from_cache": (
                None if row["from_cache"] is None else bool(row["from_cache"])
            ),
            "os_family": platform.system().lower(),
            "$process_person_profile": False,
            "$ip": None,
        }
        body = json.dumps(
            {
                "api_key": self.posthog_key,
                "event": EVENT_PREFIX + row["event"],
                "timestamp": datetime.fromtimestamp(row["created_at"], UTC).isoformat(),
                "uuid": row["id"],
                "properties": properties,
            },
            separators=(",", ":"),
        ).encode()
        request = urllib.request.Request(
            self.posthog_host + "/capture/",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=0.8) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            return False

    def rows(self, *, days: int | None = 30) -> list[dict[str, Any]]:
        where = ""
        parameters: tuple[Any, ...] = ()
        if days is not None:
            where = "WHERE created_at >= ?"
            parameters = (time.time() - days * 86_400,)
        with self._connect() as connection:
            records = connection.execute(
                f"SELECT * FROM events {where} ORDER BY created_at DESC", parameters
            ).fetchall()
        return [dict(record) for record in records]


def _percent(numerator: int, denominator: int) -> float:
    return round((numerator / denominator) * 100, 1) if denominator else 0.0


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 1)


def summarize_events(
    rows: Iterable[dict[str, Any]], *, days: int | None = 30, source: str = "local"
) -> dict[str, Any]:
    now = time.time()
    threshold = None if days is None else now - days * 86_400
    filtered = [
        row
        for row in rows
        if threshold is None or float(row.get("created_at") or 0) >= threshold
    ]
    operations = [row for row in filtered if row.get("event") == "operation_completed"]
    installations = {str(row["installation_id"]) for row in filtered}
    successful_installations = {
        str(row["installation_id"])
        for row in operations
        if row.get("success") in {True, 1}
    }
    active_days: dict[str, set[str]] = defaultdict(set)
    for row in operations:
        day = datetime.fromtimestamp(float(row["created_at"]), UTC).date().isoformat()
        active_days[str(row["installation_id"])].add(day)
    returning = {key for key, values in active_days.items() if len(values) >= 2}
    successes = sum(row.get("success") in {True, 1} for row in operations)
    durations = [
        float(row["duration_ms"])
        for row in operations
        if row.get("duration_ms") is not None
    ]

    by_site: dict[str, dict[str, Any]] = {}
    for site, site_rows in _group(operations, "site"):
        site_successes = sum(row.get("success") in {True, 1} for row in site_rows)
        site_durations = [
            float(row["duration_ms"])
            for row in site_rows
            if row.get("duration_ms") is not None
        ]
        by_site[site] = {
            "site": site,
            "calls": len(site_rows),
            "success_rate": _percent(site_successes, len(site_rows)),
            "median_ms": (
                round(statistics.median(site_durations), 1) if site_durations else None
            ),
            "users": len({str(row["installation_id"]) for row in site_rows}),
        }

    operation_items: list[dict[str, Any]] = []
    grouped_operations: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in operations:
        grouped_operations[(str(row.get("site") or "unknown"), str(row.get("operation") or "unknown"))].append(row)
    for (site, operation), values in grouped_operations.items():
        operation_items.append(
            {
                "site": site,
                "operation": operation,
                "calls": len(values),
                "users": len({str(row["installation_id"]) for row in values}),
                "success_rate": _percent(
                    sum(row.get("success") in {True, 1} for row in values), len(values)
                ),
            }
        )
    operation_items.sort(key=lambda item: (-item["calls"], item["site"], item["operation"]))

    errors = Counter(
        (str(row.get("site") or "unknown"), str(row.get("error_code") or "unknown_error"))
        for row in operations
        if row.get("success") in {False, 0}
    )
    error_items = [
        {"site": site, "error_code": code, "count": count}
        for (site, code), count in errors.most_common(10)
    ]

    interfaces = Counter(str(row.get("interface") or "unknown") for row in operations)
    timeline_days = 14 if days is None else min(max(days, 7), 60)
    timeline: list[dict[str, Any]] = []
    for offset in range(timeline_days - 1, -1, -1):
        date = (datetime.now(UTC).date() - timedelta(days=offset)).isoformat()
        day_rows = [
            row
            for row in operations
            if datetime.fromtimestamp(float(row["created_at"]), UTC).date().isoformat()
            == date
        ]
        timeline.append(
            {
                "date": date,
                "calls": len(day_rows),
                "users": len({str(row["installation_id"]) for row in day_rows}),
                "failures": sum(row.get("success") in {False, 0} for row in day_rows),
            }
        )

    setup_users = {
        str(row["installation_id"])
        for row in filtered
        if row.get("event") == "setup_completed"
    }
    attempted_installations = {
        str(row["installation_id"]) for row in operations
    }
    recent = [
        {
            "created_at": datetime.fromtimestamp(
                float(row["created_at"]), UTC
            ).isoformat(),
            "event": row.get("event"),
            "site": row.get("site"),
            "operation": row.get("operation"),
            "success": (
                None if row.get("success") is None else bool(row.get("success"))
            ),
            "duration_ms": row.get("duration_ms"),
            "interface": row.get("interface"),
            "error_code": row.get("error_code"),
        }
        for row in filtered[:30]
    ]
    return {
        "source": source,
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": days,
        "privacy": PRIVACY_NOTICE,
        "totals": {
            "installations": len(installations),
            "activated_installations": len(successful_installations),
            "activation_rate": _percent(len(successful_installations), len(installations)),
            "returning_installations": len(returning),
            "return_rate": _percent(len(returning), len(successful_installations)),
            "operations": len(operations),
            "success_rate": _percent(successes, len(operations)),
            "median_ms": round(statistics.median(durations), 1) if durations else None,
            "p95_ms": _percentile(durations, 0.95),
            "active_24h": len(
                {
                    str(row["installation_id"])
                    for row in operations
                    if float(row["created_at"]) >= now - 86_400
                }
            ),
        },
        "funnel": [
            {"label": "Set up", "users": len(setup_users or installations)},
            {"label": "Tried a task", "users": len(attempted_installations)},
            {"label": "Useful result", "users": len(successful_installations)},
            {"label": "Returned", "users": len(returning)},
        ],
        "timeline": timeline,
        "sites": sorted(by_site.values(), key=lambda item: (-item["calls"], item["site"])),
        "operations": operation_items[:20],
        "errors": error_items,
        "interfaces": [
            {"name": name, "calls": count} for name, count in interfaces.most_common()
        ],
        "recent": recent,
    }


def _group(
    rows: list[dict[str, Any]], field: str
) -> Iterable[tuple[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(field) or "unknown")].append(row)
    return grouped.items()


def posthog_rows(
    *, host: str, project_id: str, personal_api_key: str, days: int = 90
) -> list[dict[str, Any]]:
    """Read only AgentWeb events through PostHog's HogQL query endpoint."""
    safe_project = "".join(character for character in project_id if character.isdigit())
    if not safe_project or safe_project != project_id:
        raise ValueError("PostHog project ID must be numeric")
    query = (
        "SELECT timestamp, distinct_id, event, properties "
        "FROM events WHERE event LIKE 'agentweb_%' "
        f"AND timestamp >= now() - INTERVAL {int(max(1, min(days, 365)))} DAY "
        "ORDER BY timestamp DESC LIMIT 50000"
    )
    body = json.dumps(
        {"query": {"kind": "HogQLQuery", "query": query}}, separators=(",", ":")
    ).encode()
    request = urllib.request.Request(
        host.rstrip("/") + f"/api/projects/{safe_project}/query/",
        data=body,
        headers={
            "Authorization": f"Bearer {personal_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read())
    columns = payload.get("columns") or [
        "timestamp",
        "distinct_id",
        "event",
        "properties",
    ]
    output: list[dict[str, Any]] = []
    for raw in payload.get("results") or []:
        record = dict(zip(columns, raw))
        properties = record.get("properties") or {}
        if isinstance(properties, str):
            try:
                properties = json.loads(properties)
            except json.JSONDecodeError:
                properties = {}
        event = str(record.get("event") or "").removeprefix(EVENT_PREFIX)
        if event not in SAFE_EVENTS:
            continue
        timestamp = record.get("timestamp")
        if isinstance(timestamp, str):
            created_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        else:
            created_at = float(timestamp or 0)
        output.append(
            {
                "created_at": created_at,
                "installation_id": str(record.get("distinct_id") or "unknown"),
                "event": event,
                "site": _safe_label(properties.get("site")),
                "operation": _safe_label(properties.get("operation")),
                "success": properties.get("success"),
                "duration_ms": properties.get("duration_ms"),
                "interface": _safe_label(properties.get("interface")) or "unknown",
                "agentweb_version": _safe_label(properties.get("agentweb_version")),
                "adapter_version": _safe_label(properties.get("adapter_version")),
                "error_code": _safe_label(properties.get("error_code")),
                "from_cache": properties.get("from_cache"),
            }
        )
    return output


def _uploader_main() -> int:
    analytics = Analytics()
    try:
        analytics.flush_pending(limit=100)
    finally:
        analytics.upload_marker.unlink(missing_ok=True)
    return 0


if __name__ == "__main__" and sys.argv[1:] == ["--flush"]:
    raise SystemExit(_uploader_main())
