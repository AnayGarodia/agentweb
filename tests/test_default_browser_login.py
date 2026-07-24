"""Hermetic tests for default-browser login seeding.

These exercise the pure decision logic behind ``connect --use-default-browser``
(OS profile discovery, opt-in toggle, and session-file seeding) without
launching a browser.
"""

from __future__ import annotations

from pathlib import Path

import agentweb.connector as connector
from agentweb.connector import (
    BrowserProfile,
    default_chrome_user_data_dir,
    detect_default_browser,
    seed_profile_from_default_browser,
    use_default_browser_enabled,
)


def test_use_default_browser_enabled_precedence(monkeypatch) -> None:
    monkeypatch.delenv("AGENTWEB_USE_DEFAULT_BROWSER", raising=False)
    # Enabled by default now.
    assert use_default_browser_enabled(None) is True
    assert use_default_browser_enabled(True) is True
    assert use_default_browser_enabled(False) is False
    # The environment can force it off, or explicitly on.
    monkeypatch.setenv("AGENTWEB_USE_DEFAULT_BROWSER", "0")
    assert use_default_browser_enabled(None) is False
    monkeypatch.setenv("AGENTWEB_USE_DEFAULT_BROWSER", "1")
    assert use_default_browser_enabled(None) is True
    # An explicit flag always overrides the environment toggle.
    assert use_default_browser_enabled(False) is False


def test_default_chrome_user_data_dir_honors_override(monkeypatch, tmp_path) -> None:
    real = tmp_path / "User Data"
    real.mkdir()
    monkeypatch.setenv("AGENTWEB_CHROME_USER_DATA_DIR", str(real))
    assert default_chrome_user_data_dir() == real
    monkeypatch.setenv("AGENTWEB_CHROME_USER_DATA_DIR", str(tmp_path / "missing"))
    assert default_chrome_user_data_dir() is None


def test_seed_profile_copies_only_session_files(monkeypatch, tmp_path) -> None:
    source = tmp_path / "User Data"
    (source / "Default" / "Network").mkdir(parents=True)
    (source / "Local State").write_text("state", encoding="utf-8")
    (source / "Default" / "Cookies").write_text("cookies", encoding="utf-8")
    (source / "Default" / "Network" / "Cookies").write_text("net", encoding="utf-8")
    (source / "Default" / "Login Data").write_text("logins", encoding="utf-8")
    # Personal data that must NOT be copied into the per-site profile.
    (source / "Default" / "History").write_text("history", encoding="utf-8")
    monkeypatch.setenv("AGENTWEB_CHROME_USER_DATA_DIR", str(source))
    monkeypatch.delenv("AGENTWEB_CHROME_PROFILE_DIRECTORY", raising=False)

    profile_dir = tmp_path / "site-profile"
    result = seed_profile_from_default_browser(profile_dir, progress=lambda _msg: None)

    assert result["seeded"] is True
    assert (profile_dir / "Local State").read_text() == "state"
    assert (profile_dir / "Default" / "Cookies").read_text() == "cookies"
    assert (profile_dir / "Default" / "Network" / "Cookies").read_text() == "net"
    assert not (profile_dir / "Default" / "History").exists()
    # Second call is a no-op: an already-initialized profile is never reseeded.
    again = seed_profile_from_default_browser(profile_dir, progress=lambda _msg: None)
    assert again["seeded"] is False
    assert again["reason"] == "profile_already_initialized"


def test_seed_profile_reseeds_stale_logged_out_copy(monkeypatch, tmp_path) -> None:
    # Regression: a per-site profile left by an earlier connect (its own
    # Cookies file present) used to block default-browser reuse forever, so the
    # login window kept opening logged out. The login path reseeds instead.
    source = tmp_path / "User Data"
    (source / "Default").mkdir(parents=True)
    (source / "Local State").write_text("state", encoding="utf-8")
    (source / "Default" / "Cookies").write_text("fresh-cookies", encoding="utf-8")
    monkeypatch.setenv("AGENTWEB_CHROME_USER_DATA_DIR", str(source))
    monkeypatch.delenv("AGENTWEB_CHROME_PROFILE_DIRECTORY", raising=False)

    profile_dir = tmp_path / "site-profile"
    (profile_dir / "Default").mkdir(parents=True)
    (profile_dir / "Default" / "Cookies").write_text("stale", encoding="utf-8")
    (profile_dir / "Default" / "Cookies-wal").write_text("stale-wal", encoding="utf-8")

    blocked = seed_profile_from_default_browser(profile_dir, progress=lambda _m: None)
    assert blocked == {"seeded": False, "reason": "profile_already_initialized"}

    result = seed_profile_from_default_browser(
        profile_dir, reseed=True, progress=lambda _m: None
    )
    assert result["seeded"] is True
    assert (profile_dir / "Default" / "Cookies").read_text() == "fresh-cookies"
    # A leftover WAL from the stale copy must be dropped, not replayed.
    assert not (profile_dir / "Default" / "Cookies-wal").exists()


def test_seed_profile_no_default_browser(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENTWEB_CHROME_USER_DATA_DIR", str(tmp_path / "nope"))
    result = seed_profile_from_default_browser(
        tmp_path / "site-profile", progress=lambda _msg: None
    )
    assert result == {"seeded": False, "reason": "no_default_browser_profile"}


def test_seed_profile_uses_explicit_source(monkeypatch, tmp_path) -> None:
    # A caller (e.g. a detected Arc profile) can pass the source directory and
    # name directly; the env override is not consulted.
    monkeypatch.delenv("AGENTWEB_CHROME_USER_DATA_DIR", raising=False)
    monkeypatch.delenv("AGENTWEB_CHROME_PROFILE_DIRECTORY", raising=False)
    source = tmp_path / "Arc" / "User Data"
    (source / "Default").mkdir(parents=True)
    (source / "Local State").write_text("state", encoding="utf-8")
    (source / "Default" / "Cookies").write_text("arc-cookies", encoding="utf-8")

    profile_dir = tmp_path / "site-profile"
    result = seed_profile_from_default_browser(
        profile_dir,
        source_dir=source,
        source_name="Arc",
        progress=lambda _m: None,
    )
    assert result["seeded"] is True
    assert result["source_name"] == "Arc"
    assert (profile_dir / "Default" / "Cookies").read_text() == "arc-cookies"


def _profile(name: str, tmp_path: Path) -> BrowserProfile:
    data_dir = tmp_path / name
    data_dir.mkdir(exist_ok=True)
    return BrowserProfile(name=name, executable=f"/bin/{name}", user_data_dir=data_dir)


def test_detect_default_browser_prefers_os_default(monkeypatch, tmp_path) -> None:
    # Arc user with Chrome also installed: the OS default handler wins over the
    # Chrome-first fallback order, so the everyday browser (Arc) is reused.
    monkeypatch.delenv("AGENTWEB_CHROME_USER_DATA_DIR", raising=False)
    chrome = _profile("Google Chrome", tmp_path)
    arc = _profile("Arc", tmp_path)
    monkeypatch.setattr(
        connector, "installed_chromium_browsers", lambda: [chrome, arc]
    )
    monkeypatch.setattr(connector, "os_default_browser_key", lambda: "arc")
    assert detect_default_browser() == arc


def test_detect_default_browser_falls_back_to_first(monkeypatch, tmp_path) -> None:
    # When the OS default can't be read, the preference order (Chrome first) is
    # used rather than guessing.
    monkeypatch.delenv("AGENTWEB_CHROME_USER_DATA_DIR", raising=False)
    chrome = _profile("Google Chrome", tmp_path)
    arc = _profile("Arc", tmp_path)
    monkeypatch.setattr(
        connector, "installed_chromium_browsers", lambda: [chrome, arc]
    )
    monkeypatch.setattr(connector, "os_default_browser_key", lambda: None)
    assert detect_default_browser() == chrome


def test_detect_default_browser_none_without_chromium(monkeypatch) -> None:
    # Safari/Firefox-only machine: nothing to reuse, so login degrades to an
    # isolated window instead of failing.
    monkeypatch.delenv("AGENTWEB_CHROME_USER_DATA_DIR", raising=False)
    monkeypatch.setattr(connector, "installed_chromium_browsers", list)
    monkeypatch.setattr(connector, "os_default_browser_key", lambda: None)
    assert detect_default_browser() is None


def test_detect_default_browser_override(monkeypatch, tmp_path) -> None:
    data_dir = tmp_path / "custom"
    data_dir.mkdir()
    exe = tmp_path / "mybrowser"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("AGENTWEB_CHROME_USER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AGENTWEB_CHROME", str(exe))
    profile = detect_default_browser()
    assert profile is not None
    assert profile.user_data_dir == data_dir
    assert profile.executable == str(exe)
