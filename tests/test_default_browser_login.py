"""Hermetic tests for default-browser login seeding.

These exercise the pure decision logic behind ``connect --use-default-browser``
(OS profile discovery, opt-in toggle, and session-file seeding) without
launching a browser.
"""

from __future__ import annotations

from agentweb.connector import (
    default_chrome_user_data_dir,
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


def test_seed_profile_no_default_browser(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENTWEB_CHROME_USER_DATA_DIR", str(tmp_path / "nope"))
    result = seed_profile_from_default_browser(
        tmp_path / "site-profile", progress=lambda _msg: None
    )
    assert result == {"seeded": False, "reason": "no_default_browser_profile"}
