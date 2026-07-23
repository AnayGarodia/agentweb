from __future__ import annotations

import os
import signal
import time
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from .storage import StatePaths, read_json, write_json


class AuthState(StrEnum):
    DISCONNECTED = "disconnected"
    AUTHORIZING = "authorizing"
    HUMAN_REQUIRED = "human_required"
    VERIFYING = "verifying"
    CONNECTED = "connected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


class CheckpointKind(StrEnum):
    CAPTCHA = "captcha"
    OTP = "otp"
    PASSKEY = "passkey"
    EMAIL_VERIFICATION = "email_verification"
    PHONE_VERIFICATION = "phone_verification"
    CONSENT = "consent"
    ANTIBOT_INTERSTITIAL = "antibot_interstitial"
    SECURITY_CHECK = "security_check"


CHECKPOINT_INSTRUCTIONS = {
    CheckpointKind.CAPTCHA: "Complete the CAPTCHA in the open window.",
    CheckpointKind.OTP: "Enter the one-time verification code in the open window.",
    CheckpointKind.PASSKEY: "Complete the passkey or security-key prompt in the open window.",
    CheckpointKind.EMAIL_VERIFICATION: "Complete the email verification in the open window.",
    CheckpointKind.PHONE_VERIFICATION: "Complete the phone verification in the open window.",
    CheckpointKind.CONSENT: "Review and approve the requested permissions in the open window.",
    CheckpointKind.ANTIBOT_INTERSTITIAL: "Wait for the website security check in the open window.",
    CheckpointKind.SECURITY_CHECK: "Complete the website security check in the open window.",
}


@dataclass(frozen=True)
class HumanCheckpoint:
    kind: str
    instruction: str
    confidence: str = "high"
    source: str = "browser"

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> HumanCheckpoint | None:
        raw = snapshot.get("human_checkpoint")
        if not isinstance(raw, dict):
            return None
        try:
            kind = CheckpointKind(str(raw.get("kind")))
        except ValueError:
            kind = CheckpointKind.SECURITY_CHECK
        return cls(
            kind=kind.value,
            instruction=CHECKPOINT_INSTRUCTIONS[kind],
            confidence=str(raw.get("confidence") or "medium"),
            source=str(raw.get("source") or "browser"),
        )


@dataclass
class AuthAttempt:
    attempt_id: str
    site: str
    profile: str
    mode: str
    strategy: str
    state: str
    created_at: float
    updated_at: float
    expires_at: float
    pid: int | None = None
    debugger_port: int | None = None
    checkpoint: dict[str, Any] | None = None
    browser: dict[str, Any] | None = None
    error: str | None = None

    @classmethod
    def create(
        cls,
        *,
        site: str,
        profile: str,
        mode: str,
        strategy: str,
        timeout_seconds: int,
        pid: int | None = None,
        debugger_port: int | None = None,
    ) -> AuthAttempt:
        now = time.time()
        return cls(
            attempt_id=f"auth_{uuid.uuid4().hex}",
            site=site,
            profile=profile,
            mode=mode,
            strategy=strategy,
            state=AuthState.AUTHORIZING.value,
            created_at=now,
            updated_at=now,
            # The CLI's polling timeout is not the user's authorization deadline.
            # Keep a resumable browser attempt alive for at least one hour.
            expires_at=now + max(timeout_seconds, 3600),
            pid=pid,
            debugger_port=debugger_port,
        )

    def public(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "site": self.site,
            "profile": self.profile,
            "mode": self.mode,
            "strategy": self.strategy,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "checkpoint": self.checkpoint,
            "browser_open": process_is_alive(self.pid),
            "error": self.error,
        }


class AuthAttemptStore:
    def __init__(self, paths: StatePaths, site: str, profile: str) -> None:
        self.path = paths.profile_dir(site, profile) / "auth-attempt.json"

    def load(self) -> AuthAttempt | None:
        payload = read_json(self.path, None)
        if not isinstance(payload, dict):
            return None
        try:
            attempt = AuthAttempt(**payload)
        except (TypeError, ValueError):
            return None
        if (
            attempt.state
            in {AuthState.AUTHORIZING.value, AuthState.HUMAN_REQUIRED.value, AuthState.VERIFYING.value}
            and attempt.expires_at <= time.time()
        ):
            attempt.state = AuthState.EXPIRED.value
            attempt.updated_at = time.time()
            self.save(attempt)
        return attempt

    def save(self, attempt: AuthAttempt) -> None:
        attempt.updated_at = time.time()
        write_json(self.path, asdict(attempt))

    def update(self, attempt: AuthAttempt, **changes: Any) -> AuthAttempt:
        for key, value in changes.items():
            if not hasattr(attempt, key):
                raise ValueError(f"Unknown authentication attempt field {key!r}")
            setattr(attempt, key, value)
        self.save(attempt)
        return attempt


def process_is_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate_attempt(attempt: AuthAttempt) -> None:
    if not process_is_alive(attempt.pid):
        return
    assert attempt.pid is not None
    try:
        os.kill(attempt.pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and process_is_alive(attempt.pid):
        time.sleep(0.1)
    if process_is_alive(attempt.pid):
        try:
            os.kill(attempt.pid, signal.SIGKILL)
        except OSError:
            pass


def status_is_connected(value: dict[str, Any] | None) -> bool:
    """Normalize the legacy adapter status shapes at one compatibility boundary."""
    if not value:
        return False
    return any(
        bool(value.get(field))
        for field in (
            "authenticated",
            "signed_in",
            "website_authenticated",
            "api_authenticated",
            "website_session_available",
            "website_replay_ready",
        )
    )
