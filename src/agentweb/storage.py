from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def safe_component(value: str, *, label: str) -> str:
    """Validate a user or registry value before using it as one path segment."""
    if not isinstance(value, str) or not SAFE_COMPONENT.fullmatch(value) or value in {".", ".."}:
        raise ValueError(
            f"{label} must be 1-128 letters, numbers, dots, underscores, or hyphens"
        )
    return value


def contained_path(root: Path, *components: str) -> Path:
    """Join validated components and prove the result stays below ``root``."""
    resolved_root = root.resolve()
    result = resolved_root.joinpath(*components).resolve()
    if result == resolved_root or resolved_root not in result.parents:
        raise ValueError("Path escaped its AgentWeb storage root")
    return result


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    atomic_write(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode,
    )


@contextmanager
def exclusive_path_lock(
    path: Path, *, timeout: float = 10, stale_after: float = 60
):
    """Portable inter-process lock based on atomic directory creation."""
    deadline = time.monotonic() + timeout
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            path.mkdir(mode=0o700)
            break
        except FileExistsError:
            try:
                stale = time.time() - path.stat().st_mtime > stale_after
            except FileNotFoundError:
                continue
            if stale:
                try:
                    path.rmdir()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for lock {path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            path.rmdir()
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class StatePaths:
    root: Path

    @classmethod
    def discover(cls) -> StatePaths:
        configured = os.environ.get("AGENTWEB_HOME") or os.environ.get("SITEPACK_HOME")
        root = Path(configured).expanduser() if configured else Path.home() / ".agentweb"
        legacy = Path.home() / ".sitepack"
        if not configured and not root.exists() and legacy.exists():
            # Reuse the legacy state in place. Moving or copying cookies and
            # credentials during a rename is both surprising and unnecessary.
            root = legacy
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        return cls(root=root)

    @property
    def sites(self) -> Path:
        return self.root / "sites"

    @property
    def installed(self) -> Path:
        return self.root / "installed.json"

    @property
    def registry_config(self) -> Path:
        return self.root / "registry.json"

    @property
    def cache_db(self) -> Path:
        return self.root / "cache.sqlite3"

    def authoring_dir(self, site: str, profile: str = "default") -> Path:
        safe_site = safe_component(site, label="site")
        safe_profile = safe_component(profile, label="profile")
        value = contained_path(self.root / "authoring", safe_site, safe_profile)
        value.mkdir(parents=True, exist_ok=True, mode=0o700)
        return value

    def profile_dir(self, site: str, profile: str) -> Path:
        safe_site = safe_component(site, label="site")
        safe_profile = safe_component(profile, label="profile")
        value = contained_path(self.root / "profiles", safe_site, safe_profile)
        value.mkdir(parents=True, exist_ok=True, mode=0o700)
        return value

    def cookie_file(self, site: str, profile: str) -> Path:
        return self.profile_dir(site, profile) / "cookies.txt"


class Cache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS responses (
              cache_key TEXT PRIMARY KEY,
              site TEXT NOT NULL,
              created_at REAL NOT NULL,
              expires_at REAL NOT NULL,
              payload BLOB NOT NULL
            )
            """
        )
        self.connection.commit()

    @staticmethod
    def key(
        site: str,
        action: str,
        arguments: dict[str, Any],
        *,
        profile: str = "default",
    ) -> str:
        canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(
            f"{site}\0{profile}\0{action}\0{canonical}".encode()
        ).hexdigest()

    def get(self, key: str) -> bytes | None:
        row = self.connection.execute(
            "SELECT expires_at, payload FROM responses WHERE cache_key = ?", (key,)
        ).fetchone()
        if not row:
            return None
        if row[0] <= time.time():
            self.connection.execute("DELETE FROM responses WHERE cache_key = ?", (key,))
            self.connection.commit()
            return None
        return bytes(row[1])

    def put(self, key: str, site: str, payload: bytes, ttl_seconds: int) -> None:
        now = time.time()
        self.connection.execute(
            """
            INSERT INTO responses(cache_key, site, created_at, expires_at, payload)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
              site=excluded.site,
              created_at=excluded.created_at,
              expires_at=excluded.expires_at,
              payload=excluded.payload
            """,
            (key, site, now, now + ttl_seconds, payload),
        )
        self.connection.commit()

    def clear(self, site: str | None = None) -> int:
        if site:
            cursor = self.connection.execute("DELETE FROM responses WHERE site = ?", (site,))
        else:
            cursor = self.connection.execute("DELETE FROM responses")
        self.connection.commit()
        return cursor.rowcount


def copy_tree_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        for item in source.iterdir():
            target = temporary / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
