from __future__ import annotations

import ast
import base64
from contextlib import contextmanager
import hashlib
import hmac
import html
import inspect
import json
import os
import re
import secrets
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, unquote, urlencode, urlparse

import websocket

from agentweb.sdk import (
    AdapterContext,
    AuthenticationRequired,
    ConfigurationRequired,
    Response,
    SiteAdapter,
    AgentWebError,
    bounded_data,
    enforce_data_budget,
    parse_query_pairs,
)
from agentweb.storage import exclusive_path_lock, read_json, write_json


API_URL = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{16,64}$")
RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{8,128}$")
SCOPES = [
    "user-read-private",
    "user-read-email",
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-modify-playback-state",
    "user-read-recently-played",
    "user-top-read",
    "user-library-read",
    "user-library-modify",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-follow-read",
    "user-follow-modify",
    "ugc-image-upload",
]
SEARCH_TYPES = {"track", "album", "artist", "playlist", "show", "episode", "audiobook"}
RESOURCE_PATHS = {
    "track": "tracks",
    "album": "albums",
    "artist": "artists",
    "playlist": "playlists",
    "show": "shows",
    "episode": "episodes",
    "audiobook": "audiobooks",
    "chapter": "chapters",
}
SPOTIFY_APP = Path("/Applications/Spotify.app")
PUBLIC_SEARCH_URL = "https://html.duckduckgo.com/html/"
OEMBED_URL = "https://open.spotify.com/oembed"
CLIENT_TOKEN_URL = "https://clienttoken.spotify.com/v1/clienttoken"
PARTNER_QUERY_URL = "https://api-partner.spotify.com/pathfinder/v2/query"
PLAYLIST_SERVICE_URL = "https://spclient.wg.spotify.com/playlist/v2"
PLAYLIST_PERMISSION_URL = "https://spclient.wg.spotify.com/playlist-permission/v1"
WEB_CLIENT_VERSION = "0.0.0"
FETCH_PLAYLIST_HASH = "a65e12194ed5fc443a1cdebed5fabe33ca5b07b987185d63c72483867ad13cb4"
ADD_TO_PLAYLIST_HASH = "47b2a1234b17748d332dd0431534f22450e9ecbb3d5ddcdacbd83368636a0990"
SEARCH_TOP_RESULTS_HASH = "738aa3e67e63f4b651de4c94a332a55a486eaa3043f0a7c3a2cb8376df7634d2"
RECENTS_HASH = "698be5892a3cc95331deebeff463d05dfdd5febf5254bea30b895b5a93dfb584"
ENTITY_LOOKUP_HASH = "5bb408450626d595cb24363104b612e14f9b966430f599121696e8996ea03794"
CONNECT_STATE_URL = "https://spclient.wg.spotify.com/connect-state/v1"
CONNECT_RESOLVER_URL = "https://apresolve.spotify.com/?type=dealer-g2&type=spclient"
PROFILE_ATTRIBUTES_HASH = "b197b5adb4b761690f76ad9d9fb278c14c14e7331f357c04a56e7001af7106e0"
ACCOUNT_ATTRIBUTES_HASH = "8ea75f2a2e357219328570ef35ec2d9c4db6089076908f59c6eb62348b225b55"
LIBRARY_V3_HASH = "973e511ca44261fda7eebac8b653155e7caee3675abb4fb110cc1b8c78b091c3"
LIBRARY_CONTAINS_HASH = "134337999233cc6fdd6b1e6dbf94841409f04a946c5c7b744b09ba0dfe5a85ed"
LIBRARY_MUTATION_HASH = "1ad0d40b3c09660d818b9e770eb1e84745dfbe941df159a64f8772b6fa2bfc3a"


class Adapter(SiteAdapter):
    site_name = "spotify"
    base_url = "https://open.spotify.com"
    allowed_domains = ("spotify.com",)

    def __init__(self, context: AdapterContext) -> None:
        super().__init__(context)
        self.profile_dir = context.paths.profile_dir("spotify", context.profile)
        self.config_path = self.profile_dir / "oauth-config.json"
        self.tokens_path = self.profile_dir / "oauth-tokens.json"
        self.web_tokens_path = self.profile_dir / "web-session-token.json"
        self.web_client_token_path = self.profile_dir / "web-client-token.json"
        self.api_backoff_path = self.profile_dir / "official-api-backoff.json"
        self.api_rate_lock_path = self.profile_dir / ".official-api-rate.lock"

    def _client_id(self) -> str | None:
        configured = read_json(self.config_path, {}) or {}
        return (
            os.environ.get("AGENTWEB_SPOTIFY_CLIENT_ID")
            or os.environ.get("SITEPACK_SPOTIFY_CLIENT_ID")
            or configured.get("client_id")
        )

    def _has_oauth_tokens(self) -> bool:
        tokens = read_json(self.tokens_path, {}) or {}
        return bool(tokens.get("refresh_token") or tokens.get("access_token"))

    def _has_web_session(self) -> bool:
        return any(
            cookie.name == "sp_dc" and bool(cookie.value)
            for cookie in self.session().cookies
        )

    def _has_account_access(self) -> bool:
        return self._has_oauth_tokens() or self._has_web_session()

    def _session_request(self, method: str, url: str, **kwargs: Any) -> Response:
        """Keep independently distributed adapters compatible with older runtimes."""
        request = self.session().request
        try:
            supports_impersonation = "impersonate" in inspect.signature(
                request
            ).parameters
        except (TypeError, ValueError):
            supports_impersonation = False
        if supports_impersonation:
            kwargs["impersonate"] = "chrome"
        response = request(method, url, **kwargs)
        if response.status != 429:
            return response
        retry_after = next(
            (
                value
                for name, value in response.headers.items()
                if name.lower() == "retry-after"
            ),
            None,
        )
        try:
            delay = min(max(float(retry_after or 1), 0.25), 30.0)
        except (TypeError, ValueError):
            delay = 1.0
        time.sleep(delay)
        return request(method, url, **kwargs)

    def direct_headers(self, url: str) -> dict[str, str]:
        if (urlparse(url).hostname or "").lower() == "api.spotify.com":
            return {"Authorization": f"Bearer {self._access_token()}"}
        return {}

    @staticmethod
    def _desktop_available() -> bool:
        return sys.platform == "darwin" and SPOTIFY_APP.is_dir()

    @staticmethod
    def _desktop_script(*statements: str) -> list[str]:
        command = ["osascript"]
        for statement in statements:
            command.extend(["-e", statement])
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            if "-1743" in detail or "Not authorized to send Apple events" in detail:
                raise ConfigurationRequired(
                    "macOS blocked Spotify desktop control. Allow your terminal or agent app under System Settings > Privacy & Security > Automation > Spotify, then retry. This is a one-time local permission and does not expose Spotify credentials.",
                    operation="spotify.desktop_status",
                )
            raise AgentWebError(
                f"Spotify desktop control failed: {detail or 'osascript exited unsuccessfully'}",
                code="desktop_control_failed",
                next_action="Open Spotify, sign in, and retry",
            )
        return [line for line in completed.stdout.strip().splitlines() if line]

    def _desktop_status(self) -> dict[str, Any]:
        if not self._desktop_available():
            return {
                "available": False,
                "platform": sys.platform,
                "reason": "Spotify.app is not installed on this Mac",
            }
        lines = self._desktop_script(
            'tell application "Spotify" to launch',
            'tell application "Spotify"',
            'set stateText to "unknown"',
            'if player state is playing then',
            'set stateText to "playing"',
            'else if player state is paused then',
            'set stateText to "paused"',
            'else',
            'set stateText to "stopped"',
            'end if',
            'set trackText to ""',
            'set artistText to ""',
            'set albumText to ""',
            'set positionText to ""',
            'set durationText to ""',
            'try',
            'set trackText to name of current track',
            'set artistText to artist of current track',
            'set albumText to album of current track',
            'set positionText to player position as text',
            'set durationText to duration of current track as text',
            'end try',
            'return "state=" & stateText & linefeed & "track=" & trackText & linefeed & "artist=" & artistText & linefeed & "album=" & albumText & linefeed & "position_seconds=" & positionText & linefeed & "duration_ms=" & durationText',
            'end tell',
        )
        fields = {}
        for line in lines:
            name, separator, value = line.partition("=")
            if separator:
                fields[name] = value or None

        def number(name: str, conversion):
            value = fields.get(name)
            try:
                return conversion(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        return {
            "available": True,
            "platform": "macos",
            "state": fields.get("state") or "unknown",
            "track": fields.get("track"),
            "artist": fields.get("artist"),
            "album": fields.get("album"),
            "position_seconds": number("position_seconds", float),
            "duration_ms": number("duration_ms", lambda value: int(float(value))),
        }

    def desktop_status(self) -> dict[str, Any]:
        return {"operation": "spotify.desktop_status", **self._desktop_status()}

    def _await_desktop_playing(
        self, attempts: int = 6, interval: float = 0.5
    ) -> dict[str, Any]:
        """Poll the desktop for a short window so a slow-to-start track is not
        misreported as stopped: Spotify.app often needs a couple of seconds to
        buffer and enter the ``playing`` state after a play command."""
        status = self._desktop_status()
        for _ in range(max(attempts - 1, 0)):
            if status.get("state") == "playing":
                return status
            time.sleep(interval)
            status = self._desktop_status()
        return status

    def _public_track_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        if not query.strip():
            raise AgentWebError("query cannot be empty")
        response = self.session().request(
            "GET",
            PUBLIC_SEARCH_URL,
            params={"q": f"site:open.spotify.com/track {query.strip()}"},
            headers={"Accept": "text/html"},
            cache_action="public_track_search",
            cache_arguments={"query": query, "limit": limit},
            cache_ttl=3600,
        )
        if response.status != 200:
            raise AgentWebError(
                f"Public Spotify track resolution returned HTTP {response.status}",
                code="track_resolution_failed",
                retryable=response.status >= 500,
            )
        decoded = unquote(html.unescape(response.text))
        ids: list[str] = []
        for track_id in re.findall(r"open\.spotify\.com/track/([A-Za-z0-9]{8,128})", decoded):
            if track_id not in ids:
                ids.append(track_id)
            if len(ids) >= limit:
                break
        results = []
        for track_id in ids:
            uri = f"spotify:track:{track_id}"
            metadata_response = self.session().request(
                "GET",
                OEMBED_URL,
                params={"url": f"https://open.spotify.com/track/{track_id}"},
                headers={"Accept": "application/json"},
                cache_action="spotify_oembed",
                cache_arguments={"track_id": track_id},
                cache_ttl=86400,
            )
            metadata: dict[str, Any] = {}
            if metadata_response.status == 200:
                try:
                    metadata = json.loads(metadata_response.text)
                except json.JSONDecodeError:
                    metadata = {}
            results.append(
                {
                    "id": track_id,
                    "uri": uri,
                    "type": "track",
                    "name": metadata.get("title"),
                    "artists": [],
                    "image": metadata.get("thumbnail_url"),
                    "url": f"https://open.spotify.com/track/{track_id}",
                    "resolution": "public_index_verified_by_spotify_oembed",
                }
            )
        if not results:
            raise AgentWebError(
                f"No public Spotify track result was found for {query!r}",
                code="empty_result",
                retryable=True,
                next_action="Retry with the song and artist, or connect Spotify OAuth",
            )
        return results

    def _desktop_play(self, uri: str | None = None) -> dict[str, Any]:
        if not self._desktop_available():
            raise ConfigurationRequired(
                "Zero-setup playback requires Spotify.app on macOS. Install and sign in to Spotify Desktop, or configure Spotify OAuth for remote and cross-platform playback.",
                operation="spotify.configure",
            )
        self._desktop_script('tell application "Spotify" to launch')
        if uri:
            normalized = self._uri(uri)
            if not normalized.startswith("spotify:track:"):
                raise AgentWebError("desktop playback currently requires a track URI")
            self._desktop_script(
                f'tell application "Spotify" to play track "{normalized}"'
            )
        else:
            self._desktop_script('tell application "Spotify" to play')
        status = self._await_desktop_playing()
        state = status.get("state")
        playing = state == "playing"
        response: dict[str, Any] = {
            "operation": "spotify.play",
            "command_sent": True,
            "control_path": "spotify_desktop_apple_events",
            # command_sent reports dispatch success; verified reports observed
            # playback. When the desktop has not yet reported "playing" the
            # command still dispatched fine, so this is "pending", not a failure.
            "verified": playing,
            "verification": "playing" if playing else "pending",
            "playback_state": state,
            "desktop": status,
            "uri": uri,
        }
        if not playing:
            response["note"] = (
                "Play command dispatched to Spotify Desktop; playback had not "
                "reported 'playing' within the confirmation window. Re-check with "
                "desktop_status in a moment."
            )
        return response

    def configure(self, client_id: str) -> dict[str, Any]:
        client_id = client_id.strip()
        if not CLIENT_ID_PATTERN.fullmatch(client_id):
            raise AgentWebError("client_id must be a valid Spotify developer Client ID")
        write_json(self.config_path, {"client_id": client_id, "configured_at": time.time()})
        return {
            "operation": "spotify.configure",
            "configured": True,
            "client_id_suffix": client_id[-6:],
            "client_secret_stored": False,
            "redirect_uri_to_register": "http://127.0.0.1/callback",
            "next_step": "Call site_connect for spotify; AgentWeb will use PKCE and a dynamic loopback port.",
        }

    def setup_status(self) -> dict[str, Any]:
        client_id = self._client_id()
        tokens = read_json(self.tokens_path, {}) or {}
        cookie_names = {cookie.name for cookie in self.session().cookies}
        return {
            "operation": "spotify.setup_status",
            "client_id_configured": bool(client_id),
            "client_id_source": (
                "AGENTWEB_SPOTIFY_CLIENT_ID"
                if os.environ.get("AGENTWEB_SPOTIFY_CLIENT_ID")
                else "SITEPACK_SPOTIFY_CLIENT_ID"
                if os.environ.get("SITEPACK_SPOTIFY_CLIENT_ID")
                else "profile"
                if client_id
                else None
            ),
            "oauth_tokens_saved": bool(tokens.get("refresh_token") or tokens.get("access_token")),
            "website_session_saved": "sp_dc" in cookie_names,
            "client_secret_stored": False,
            "redirect_uri_to_register": "http://127.0.0.1/callback",
            "developer_dashboard": "https://developer.spotify.com/dashboard",
            "premium_required_for_playback_api": True,
            "ready_for_site_connect": True,
            "normal_website_login_supported": True,
            "developer_client_id_required": False,
            "desktop_playback_available": self._desktop_available(),
            "desktop_playback_requires_developer_app": False,
            "recommended_playback_path": (
                "spotify_desktop" if self._desktop_available() else "spotify_web_session"
            ),
        }

    def _oauth_begin(self, redirect_uri: str) -> dict[str, str]:
        client_id = self._client_id()
        if not client_id:
            raise ConfigurationRequired(
                "Spotify's official API requires a developer Client ID. Create one at https://developer.spotify.com/dashboard, register http://127.0.0.1/callback, call spotify.configure once, then retry site_connect. On macOS, spotify.search and spotify.play can use the signed-in Spotify Desktop app without OAuth.",
                operation="spotify.configure",
            )
        verifier = secrets.token_urlsafe(72)[:96]
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        state = secrets.token_urlsafe(32)
        parameters = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "show_dialog": "false",
        }
        return {
            "authorization_url": AUTHORIZE_URL + "?" + urlencode(parameters),
            "code_verifier": verifier,
            "state": state,
        }

    def _token_request(self, form: dict[str, Any]) -> dict[str, Any]:
        response = self.session().request(
            "POST",
            TOKEN_URL,
            form=form,
            headers={"Accept": "application/json"},
        )
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise AgentWebError("Spotify authorization returned malformed JSON") from exc
        if response.status >= 400:
            detail = payload.get("error_description") or payload.get("error") or "authorization failed"
            raise AgentWebError(f"Spotify authorization returned HTTP {response.status}: {detail}")
        return payload

    def _oauth_exchange(
        self, *, code: str, redirect_uri: str, code_verifier: str
    ) -> dict[str, Any]:
        client_id = self._client_id()
        if not client_id:
            raise AgentWebError("Spotify Client ID disappeared during authorization")
        payload = self._token_request(
            {
                "client_id": client_id,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            }
        )
        self._save_tokens(payload)
        return payload

    def _save_tokens(
        self, payload: dict[str, Any], *, previous: dict[str, Any] | None = None
    ) -> None:
        previous = previous or {}
        access_token = payload.get("access_token")
        if not access_token:
            raise AgentWebError("Spotify token response did not contain an access token")
        expires_in = max(int(payload.get("expires_in") or 3600), 60)
        value = {
            "access_token": access_token,
            "refresh_token": payload.get("refresh_token") or previous.get("refresh_token"),
            "token_type": payload.get("token_type") or previous.get("token_type") or "Bearer",
            "scope": payload.get("scope") or previous.get("scope") or " ".join(SCOPES),
            "expires_at": time.time() + expires_in,
            "updated_at": time.time(),
        }
        write_json(self.tokens_path, value)

    def _refresh(self) -> str:
        tokens = read_json(self.tokens_path, {}) or {}
        refresh_token = tokens.get("refresh_token")
        client_id = self._client_id()
        if not refresh_token or not client_id:
            raise AuthenticationRequired(
                "Spotify API authorization is required. Configure a Client ID if needed, call site_connect for spotify, then retry."
            )
        payload = self._token_request(
            {
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        self._save_tokens(payload, previous=tokens)
        return str(payload["access_token"])

    @staticmethod
    def _totp(secret: bytes, timestamp: float) -> str:
        counter = int(timestamp // 30)
        digest = hmac.new(
            secret, struct.pack(">Q", counter), hashlib.sha1
        ).digest()
        offset = digest[-1] & 0x0F
        code = (
            int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
        ) % 1_000_000
        return f"{code:06d}"

    def _web_player_totp_config(self) -> tuple[bytes, str]:
        landing = self._session_request(
            "GET",
            "https://open.spotify.com/",
            headers={"Accept": "text/html"},
        )
        script_match = re.search(
            r"https://open\.spotifycdn\.com/cdn/build/web-player/"
            r"web-player\.[A-Za-z0-9_-]+\.js",
            landing.text,
        )
        if landing.status >= 400 or not script_match:
            raise AgentWebError(
                "Spotify's Web Player did not expose its current token bootstrap bundle",
                code="website_token_bootstrap_changed",
                retryable=True,
            )
        bundle_url = script_match.group(0)
        bundle = self._session_request(
            "GET", bundle_url, headers={"Accept": "application/javascript"}
        )
        config_match = re.search(
            r"\[\{secret:(['\"])(.*?)(?<!\\)\1,version:(\d+)",
            bundle.text,
        )
        if bundle.status >= 400 or not config_match:
            raise AgentWebError(
                "Spotify changed its Web Player token bootstrap format",
                code="website_token_bootstrap_changed",
                retryable=True,
            )
        quote, encoded_secret, version = config_match.groups()
        try:
            obfuscated_secret = ast.literal_eval(
                quote + encoded_secret + quote
            )
        except (SyntaxError, ValueError) as exc:
            raise AgentWebError(
                "Spotify's Web Player token secret could not be decoded",
                code="website_token_bootstrap_changed",
                retryable=True,
            ) from exc
        # Spotify's bundle XORs each character and then joins the resulting
        # integer array as decimal text before using those UTF-8 bytes as the
        # TOTP secret. This looks unusual but intentionally mirrors the live
        # Web Player implementation.
        secret = "".join(
            str(ord(character) ^ (index % 33 + 9))
            for index, character in enumerate(obfuscated_secret)
        ).encode("utf-8")
        return secret, version

    def _web_player_token_response(self) -> Response:
        secret, version = self._web_player_totp_config()
        server_time_response = self._session_request(
            "GET",
            "https://open.spotify.com/api/server-time",
            headers={"Accept": "application/json"},
        )
        try:
            server_time = float(json.loads(server_time_response.text)["serverTime"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            server_time = time.time()
        return self._session_request(
            "GET",
            "https://open.spotify.com/api/token",
            params={
                "reason": "transport",
                "productType": "web-player",
                "totp": self._totp(secret, time.time()),
                "totpServer": self._totp(secret, server_time),
                "totpVer": version,
            },
            headers={
                "Accept": "application/json",
                "Referer": "https://open.spotify.com/",
            },
        )

    def _web_access_token(self, *, force_refresh: bool = False) -> str:
        cached = read_json(self.web_tokens_path, {}) or {}
        if (
            not force_refresh
            and cached.get("access_token")
            and float(cached.get("expires_at") or 0) > time.time() + 60
        ):
            return str(cached["access_token"])
        if not self._has_web_session():
            raise AuthenticationRequired(
                "Spotify account access requires one normal Spotify website login. Run `agentweb connect spotify`, sign in, then retry. No developer Client ID is required."
            )
        response = self._web_player_token_response()
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise AgentWebError(
                f"Spotify's Web Player token bootstrap returned HTTP {response.status} with non-JSON content",
                code="website_session_rejected",
                retryable=True,
            ) from exc
        access_token = payload.get("accessToken")
        if response.status >= 400 or not access_token or payload.get("isAnonymous"):
            raise AuthenticationRequired(
                "The retained Spotify website session is signed out or expired. Run `agentweb connect spotify` and complete a normal login."
            )
        expiration_ms = float(payload.get("accessTokenExpirationTimestampMs") or 0)
        expires_at = expiration_ms / 1000 if expiration_ms else time.time() + 1800
        write_json(
            self.web_tokens_path,
            {
                "access_token": access_token,
                "expires_at": expires_at,
                "client_id": payload.get("clientId"),
                "source": "retained_spotify_web_player_session",
                "updated_at": time.time(),
            },
        )
        return str(access_token)

    def _web_client_token(self) -> str:
        cached = read_json(self.web_client_token_path, {}) or {}
        if (
            cached.get("token")
            and float(cached.get("expires_at") or 0) > time.time() + 60
        ):
            return str(cached["token"])
        web_tokens = read_json(self.web_tokens_path, {}) or {}
        client_id = web_tokens.get("client_id")
        if not client_id:
            self._web_access_token(force_refresh=True)
            web_tokens = read_json(self.web_tokens_path, {}) or {}
            client_id = web_tokens.get("client_id")
        if not client_id:
            raise AgentWebError(
                "Spotify's Web Player token response did not contain its client ID",
                code="website_token_bootstrap_changed",
                retryable=True,
            )
        device_id = str(cached.get("device_id") or uuid.uuid4())
        response = self._session_request(
            "POST",
            CLIENT_TOKEN_URL,
            json_body={
                "client_data": {
                    "client_id": client_id,
                    "client_version": WEB_CLIENT_VERSION,
                    "js_sdk_data": {
                        "device_brand": "Apple",
                        "device_id": device_id,
                        "device_model": "unknown",
                        "device_type": "computer",
                        "os": "macos",
                        "os_version": "10.15.7",
                    },
                }
            },
            headers={
                "Accept": "application/json",
                "Origin": "https://open.spotify.com",
                "Referer": "https://open.spotify.com/",
            },
        )
        try:
            payload = json.loads(response.text)
            granted = payload.get("granted_token") or {}
        except json.JSONDecodeError as exc:
            raise AgentWebError(
                f"Spotify client-token bootstrap returned HTTP {response.status} with non-JSON content",
                code="website_client_token_rejected",
                retryable=True,
            ) from exc
        token = granted.get("token")
        if response.status >= 400 or not token:
            raise AgentWebError(
                f"Spotify client-token bootstrap returned HTTP {response.status}",
                code="website_client_token_rejected",
                retryable=True,
            )
        expires_after = float(granted.get("expires_after_seconds") or 3600)
        write_json(
            self.web_client_token_path,
            {
                "token": token,
                "device_id": device_id,
                "expires_at": time.time() + expires_after,
                "updated_at": time.time(),
            },
        )
        return str(token)

    def _web_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._web_access_token()}",
            "client-token": self._web_client_token(),
            "App-Platform": "WebPlayer",
            "Spotify-App-Version": WEB_CLIENT_VERSION,
            "Origin": "https://open.spotify.com",
            "Referer": "https://open.spotify.com/",
        }

    @staticmethod
    def _json_response(response: Response, context: str) -> dict[str, Any]:
        try:
            payload = json.loads(response.text) if response.text.strip() else {}
        except json.JSONDecodeError as exc:
            raise AgentWebError(
                f"{context} returned HTTP {response.status} with non-JSON content",
                code="website_replay_changed",
                retryable=True,
            ) from exc
        if response.status >= 400:
            raise AgentWebError(
                f"{context} returned HTTP {response.status}: {payload}",
                code="website_replay_rejected",
                retryable=response.status in {408, 409, 425, 429} or response.status >= 500,
            )
        if not isinstance(payload, dict):
            raise AgentWebError(
                f"{context} returned an unexpected response shape",
                code="website_replay_changed",
                retryable=True,
            )
        return payload

    def _partner_query(
        self, operation_name: str, query_hash: str, variables: dict[str, Any]
    ) -> tuple[dict[str, Any], float]:
        response = self._session_request(
            "POST",
            PARTNER_QUERY_URL,
            json_body={
                "extensions": {
                    "persistedQuery": {"sha256Hash": query_hash, "version": 1}
                },
                "operationName": operation_name,
                "variables": variables,
            },
            headers=self._web_headers(),
        )
        payload = self._json_response(response, f"Spotify Web Player {operation_name}")
        if payload.get("errors"):
            raise AgentWebError(
                f"Spotify Web Player {operation_name} failed: {payload['errors']}",
                code="website_replay_rejected",
            )
        return payload, response.elapsed_ms

    def _connect_cluster(self) -> tuple[dict[str, Any], float]:
        response = self._session_request(
            "GET",
            CONNECT_STATE_URL + "/cluster",
            headers=self._web_headers(),
        )
        return self._json_response(response, "Spotify Web Player device state"), response.elapsed_ms

    @staticmethod
    def _connect_device(device: dict[str, Any], active_device_id: str | None) -> dict[str, Any]:
        capabilities = device.get("capabilities") or {}
        volume = device.get("volume")
        return {
            "id": device.get("device_id"),
            "is_active": device.get("device_id") == active_device_id,
            "is_private_session": False,
            "is_restricted": not bool(capabilities.get("is_controllable", True)),
            "name": device.get("name"),
            "type": str(device.get("device_type") or "unknown").title(),
            "volume_percent": (
                round(float(volume) * 100 / 65535)
                if isinstance(volume, (int, float))
                else None
            ),
            "supports_volume": not bool(capabilities.get("disable_volume")),
            "brand": device.get("brand"),
            "model": device.get("model"),
            "is_controllable": bool(capabilities.get("is_controllable", True)),
        }

    @staticmethod
    def _connect_target(
        cluster: dict[str, Any], device_id: str | None = None
    ) -> dict[str, Any]:
        devices = cluster.get("devices") or {}
        if device_id:
            device = devices.get(device_id)
            if not device:
                raise AgentWebError(
                    "Spotify device_id is not present in the current Connect device list; call spotify.devices again because device IDs are ephemeral"
                )
            return device
        active_id = cluster.get("active_device_id")
        if active_id and active_id in devices:
            return devices[active_id]
        candidates = list(devices.values())
        if not candidates:
            raise AgentWebError(
                "Spotify has no available playback device. Open Spotify on a device, then retry.",
                code="no_active_device",
                retryable=True,
            )
        controllable = [
            item
            for item in candidates
            if (item.get("capabilities") or {}).get("is_controllable", True)
        ] or candidates
        non_web = [item for item in controllable if item.get("model") != "web_player"]
        return (non_web or controllable)[0]

    @staticmethod
    def _connect_source(
        cluster: dict[str, Any], target: dict[str, Any]
    ) -> dict[str, Any]:
        devices = list((cluster.get("devices") or {}).values())
        target_id = target.get("device_id")
        remote_web_players = [
            item
            for item in devices
            if item.get("model") == "web_player" and item.get("device_id") != target_id
        ]
        if remote_web_players:
            return remote_web_players[0]
        active_id = cluster.get("active_device_id")
        return next(
            (item for item in devices if item.get("device_id") == active_id),
            target,
        )

    @contextmanager
    def _connect_command_source(
        self, cluster: dict[str, Any], target: dict[str, Any]
    ) -> Iterator[tuple[str, str]]:
        """Yield a live Connect controller without launching a browser.

        Spotify acknowledges player commands whose source equals their target,
        but often discards those commands. The Web Player solves that by keeping
        a Dealer websocket open and registering a hidden Connect observer. We
        reproduce that small transport directly from the retained website
        session instead of keeping Chrome alive.
        """
        target_id = str(target.get("device_id") or "")
        source = self._connect_source(cluster, target)
        source_id = str(source.get("device_id") or "")
        if source_id and source_id != target_id:
            yield source_id, "existing_connect_controller"
            return

        resolver = self._session_request(
            "GET", CONNECT_RESOLVER_URL, headers=self._web_headers()
        )
        resolved = self._json_response(resolver, "Spotify Connect resolver")
        dealer_hosts = resolved.get("dealer-g2") or []
        if not dealer_hosts:
            raise AgentWebError(
                "Spotify did not return a Connect controller endpoint",
                code="website_replay_changed",
                retryable=True,
            )
        dealer_host = str(dealer_hosts[0])
        for prefix in ("https://", "http://", "wss://", "ws://"):
            if dealer_host.startswith(prefix):
                dealer_host = dealer_host[len(prefix) :]
                break
        dealer_host = dealer_host.rstrip("/")
        parsed_dealer = urlparse("wss://" + dealer_host)
        if not parsed_dealer.hostname or not parsed_dealer.hostname.endswith(
            ".spotify.com"
        ):
            raise AgentWebError(
                "Spotify returned an invalid Connect controller host",
                code="website_replay_changed",
                retryable=True,
            )
        controller_id = secrets.token_hex(20)
        observer_id = "hobs_" + controller_id[:35]
        socket = None
        registered = False
        try:
            socket = websocket.create_connection(
                f"wss://{dealer_host}/?access_token={quote(self._web_access_token(), safe='')}",
                timeout=10,
                origin="https://open.spotify.com",
            )
            connection_id = ""
            for _ in range(5):
                raw_message = socket.recv()
                if isinstance(raw_message, bytes):
                    raw_message = raw_message.decode("utf-8", "replace")
                try:
                    message = json.loads(raw_message)
                except (TypeError, json.JSONDecodeError):
                    continue
                for name, value in (message.get("headers") or {}).items():
                    if str(name).lower() == "spotify-connection-id":
                        connection_id = str(value)
                        break
                if connection_id:
                    break
            if not connection_id:
                raise AgentWebError(
                    "Spotify's Connect controller did not provide a connection ID",
                    code="website_replay_changed",
                    retryable=True,
                )
            register = self._session_request(
                "PUT",
                f"{CONNECT_STATE_URL}/devices/{observer_id}",
                json_body={
                    "member_type": "CONNECT_STATE",
                    "device": {
                        "device_info": {
                            "capabilities": {
                                "can_be_player": False,
                                "hidden": True,
                                "needs_full_player_state": True,
                            }
                        }
                    },
                },
                headers=self._web_headers()
                | {"X-Spotify-Connection-Id": connection_id},
            )
            if register.status >= 400:
                raise AgentWebError(
                    f"Spotify Connect controller registration returned HTTP {register.status}",
                    code="website_replay_rejected",
                    retryable=register.status in {408, 409, 425, 429}
                    or register.status >= 500,
                )
            registered = True
            yield controller_id, "direct_dealer_controller"
        except AgentWebError:
            raise
        except Exception as exc:
            raise AgentWebError(
                f"Spotify Connect controller setup failed ({type(exc).__name__})",
                code="website_replay_rejected",
                retryable=True,
            ) from exc
        finally:
            if registered:
                try:
                    self._session_request(
                        "DELETE",
                        f"{CONNECT_STATE_URL}/devices/{observer_id}",
                        headers=self._web_headers(),
                    )
                except Exception:
                    pass
            if socket is not None:
                try:
                    socket.close()
                except Exception:
                    pass

    @staticmethod
    def _connect_restriction_reasons(
        endpoint: str, state: dict[str, Any]
    ) -> list[str]:
        key = {
            "pause": "disallow_pausing_reasons",
            "resume": "disallow_resuming_reasons",
            "skip_next": "disallow_skipping_next_reasons",
            "skip_prev": "disallow_skipping_prev_reasons",
            "seek_to": "disallow_seeking_reasons",
            "set_shuffling_context": "disallow_toggling_shuffle_reasons",
            "set_repeating_context": "disallow_toggling_repeat_context_reasons",
            "set_repeating_track": "disallow_toggling_repeat_track_reasons",
            "add_to_queue": "disallow_add_to_queue_reasons",
        }.get(endpoint)
        reasons = ((state.get("restrictions") or {}).get(key) or []) if key else []
        return [str(reason) for reason in reasons]

    @staticmethod
    def _connect_state_matches(
        endpoint: str, state: dict[str, Any], values: dict[str, Any]
    ) -> bool:
        options = state.get("options") or {}
        if endpoint == "pause":
            return bool(state.get("is_paused"))
        if endpoint == "resume":
            return bool(state.get("track")) and not bool(state.get("is_paused"))
        if endpoint == "seek_to":
            return abs(
                int(state.get("position") or state.get("position_as_of_timestamp") or 0)
                - int(values.get("value") or 0)
            ) < 5000
        if endpoint == "set_shuffling_context":
            return bool(options.get("shuffling_context")) == bool(values.get("value"))
        if endpoint == "set_repeating_context":
            return bool(options.get("repeating_context")) == bool(values.get("value"))
        if endpoint == "set_repeating_track":
            return bool(options.get("repeating_track")) == bool(values.get("value"))
        return False

    @staticmethod
    def _connect_track(track: dict[str, Any] | None) -> dict[str, Any] | None:
        if not track:
            return None
        metadata = track.get("metadata") or {}
        uri = str(track.get("uri") or "")
        return {
            "id": uri.rsplit(":", 1)[-1] if uri else None,
            "uri": uri or None,
            "type": "track",
            "name": metadata.get("title"),
            "artists": ([metadata["artist_name"]] if metadata.get("artist_name") else []),
            "album": metadata.get("album_title"),
        }

    def _web_lookup_tracks(self, uris: list[str]) -> dict[str, dict[str, Any]]:
        track_uris = list(dict.fromkeys(uri for uri in uris if uri.startswith("spotify:track:")))
        if not track_uris:
            return {}
        payload, _elapsed = self._partner_query(
            "fetchEntitiesForRecentlyPlayed",
            ENTITY_LOOKUP_HASH,
            {"uris": track_uris[:100]},
        )
        result: dict[str, dict[str, Any]] = {}
        for wrapper in ((payload.get("data") or {}).get("lookup") or []):
            data = wrapper.get("data") or {}
            uri = str(data.get("uri") or wrapper.get("_uri") or "")
            if not uri:
                continue
            album = data.get("albumOfTrack") or {}
            result[uri] = {
                "id": uri.rsplit(":", 1)[-1],
                "uri": uri,
                "type": "track",
                "name": data.get("name"),
                "artists": [
                    ((item.get("profile") or {}).get("name"))
                    for item in ((data.get("artists") or {}).get("items") or [])
                    if (item.get("profile") or {}).get("name")
                ],
                "album": album.get("name"),
                "album_uri": album.get("uri"),
                "explicit": (data.get("contentRating") or {}).get("label") not in {None, "NONE"},
            }
        return result

    @staticmethod
    def _web_compact_entity(data: dict[str, Any]) -> dict[str, Any] | None:
        typename = str(data.get("__typename") or "")
        uri = str(data.get("uri") or "")
        if not uri:
            return None
        kind = {
            "Track": "track",
            "Artist": "artist",
            "Album": "album",
            "Playlist": "playlist",
            "Podcast": "show",
            "Show": "show",
            "Episode": "episode",
            "Audiobook": "audiobook",
        }.get(typename)
        if not kind:
            return None
        profile = data.get("profile") or {}
        album = data.get("albumOfTrack") or {}
        owner = ((data.get("ownerV2") or {}).get("data") or {})
        artists = [
            ((item.get("profile") or {}).get("name"))
            for item in ((data.get("artists") or {}).get("items") or [])
            if (item.get("profile") or {}).get("name")
        ]
        images = (
            ((data.get("visuals") or {}).get("avatarImage") or {}).get("sources")
            or ((data.get("coverArt") or {}).get("sources"))
            or (((data.get("images") or {}).get("items") or [{}])[0].get("sources"))
            or []
        )
        return {
            "id": uri.rsplit(":", 1)[-1],
            "uri": uri,
            "type": kind,
            "name": data.get("name") or profile.get("name"),
            "artists": artists,
            "album": album.get("name"),
            "album_uri": album.get("uri"),
            "owner": owner.get("name") or owner.get("username"),
            "description": data.get("description"),
            "explicit": (data.get("contentRating") or {}).get("label") not in {None, "NONE"},
            "image": images[0].get("url") if images else None,
            "url": f"https://open.spotify.com/{kind}/{uri.rsplit(':', 1)[-1]}",
        }

    def _web_search(
        self, query: str, selected: list[str], limit: int, offset: int
    ) -> tuple[dict[str, list[dict[str, Any]]], float]:
        payload, elapsed = self._partner_query(
            "searchTopResultsList",
            SEARCH_TOP_RESULTS_HASH,
            {
                "includeAlbumPreReleases": True,
                "includeArtistHasConcertsField": False,
                "includeAudiobooks": True,
                "includeAuthors": False,
                "includeEpisodeContentRatingsV2": True,
                "includePreReleases": True,
                "isPrefix": None,
                "limit": min(50, max(limit + offset, 10)),
                "numberOfTopResults": min(50, max(limit + offset, 10)),
                "offset": offset,
                "query": query,
                "sectionFilters": ["GENERIC", "VIDEO_CONTENT"],
            },
        )
        wanted = set(selected)
        results = {kind + "s": [] for kind in selected}
        seen: set[tuple[str, str]] = set()

        def visit(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if not isinstance(value, dict):
                return
            compact = self._web_compact_entity(value)
            if compact and compact["type"] in wanted:
                key = (compact["type"], str(compact["uri"]))
                container = results[compact["type"] + "s"]
                if key not in seen and len(container) < limit:
                    seen.add(key)
                    container.append(compact)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)

        visit((payload.get("data") or {}).get("searchV2") or {})
        return results, elapsed

    def _web_library_page(
        self,
        *,
        limit: int,
        offset: int,
        filters: list[str] | None = None,
    ) -> tuple[dict[str, Any], float]:
        payload, elapsed = self._partner_query(
            "libraryV3",
            LIBRARY_V3_HASH,
            {
                "expandedFolders": [],
                "features": [
                    "LIKED_SONGS",
                    "YOUR_EPISODES_V2",
                    "PRERELEASES",
                    "PRERELEASES_V2",
                    "CLIPS",
                    "EVENTS",
                ],
                "filters": filters or [],
                "flatten": False,
                "folderUri": None,
                "includeFoldersWhenFlattening": True,
                "limit": limit,
                "offset": offset,
                "order": None,
                "textFilter": "",
            },
        )
        page = ((((payload.get("data") or {}).get("me") or {}).get("libraryV3")) or {})
        if page.get("__typename") != "LibraryPage":
            raise AgentWebError(
                f"Spotify Web Player library rejected the requested filter: {page}",
                code="website_replay_rejected",
            )
        return page, elapsed

    def _web_account(self) -> tuple[dict[str, Any], float]:
        profile_payload, profile_elapsed = self._partner_query(
            "profileAttributes", PROFILE_ATTRIBUTES_HASH, {}
        )
        account_payload, account_elapsed = self._partner_query(
            "accountAttributes", ACCOUNT_ATTRIBUTES_HASH, {}
        )
        profile = (((profile_payload.get("data") or {}).get("me") or {}).get("profile") or {})
        account = (((account_payload.get("data") or {}).get("me") or {}).get("account") or {})
        return {
            "id": profile.get("username"),
            "display_name": profile.get("name"),
            "uri": profile.get("uri"),
            "country": account.get("country"),
            "product": account.get("product"),
            "attributes": account.get("attributes") or {},
        }, profile_elapsed + account_elapsed

    def _connect_transfer(
        self,
        cluster: dict[str, Any],
        target: dict[str, Any],
        *,
        play: bool,
    ) -> tuple[dict[str, Any], float]:
        devices = cluster.get("devices") or {}
        target_id = str(target.get("device_id") or "")
        source_id = str(cluster.get("active_device_id") or target_id)
        if not target_id:
            raise AgentWebError("Spotify returned a device without an ID")
        response = self._session_request(
            "POST",
            f"{CONNECT_STATE_URL}/connect/transfer/from/{source_id}/to/{target_id}",
            json_body={
                "transfer_options": {"restore_paused": "resume" if play else "pause"},
                "command_id": uuid.uuid4().hex,
            },
            headers=self._web_headers(),
        )
        payload = self._json_response(response, "Spotify Web Player device transfer")
        if target_id not in devices:
            raise AgentWebError("Spotify target device disappeared during transfer", retryable=True)
        return payload, response.elapsed_ms

    def _connect_player_command(
        self,
        endpoint: str,
        *,
        device_id: str | None = None,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], float]:
        cluster, elapsed = self._connect_cluster()
        target = self._connect_target(cluster, device_id)
        target_id = str(target.get("device_id") or "")
        before_state = cluster.get("player_state") or {}
        before_uri = str((before_state.get("track") or {}).get("uri") or "")
        command_values = values or {}
        if self._connect_state_matches(endpoint, before_state, command_values):
            return {
                "_verified": True,
                "already_in_requested_state": True,
                "_controller": "not_needed",
            }, target, elapsed
        restriction_reasons = self._connect_restriction_reasons(
            endpoint, before_state
        )
        if restriction_reasons:
            raise AgentWebError(
                f"Spotify currently disallows {endpoint}: {', '.join(restriction_reasons)}",
                code="spotify_control_restricted",
                retryable=False,
                details={"reasons": restriction_reasons},
            )
        before_playback_id = str(before_state.get("playback_id") or "")
        before_queue_revision = str(before_state.get("queue_revision") or "")
        command = {
            "endpoint": endpoint,
            "logging_params": {"command_id": uuid.uuid4().hex},
        }
        command.update(command_values)
        with self._connect_command_source(cluster, target) as (
            source_id,
            controller_kind,
        ):
            response = self._session_request(
                "POST",
                f"{CONNECT_STATE_URL}/player/command/from/{source_id}/to/{target_id}",
                json_body={"command": command},
                headers=self._web_headers(),
            )
            payload = self._json_response(response, f"Spotify Web Player {endpoint}")
            elapsed += response.elapsed_ms
            verified = False
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                time.sleep(0.25)
                observed, observed_elapsed = self._connect_cluster()
                elapsed += observed_elapsed
                state = observed.get("player_state") or {}
                current_uri = str((state.get("track") or {}).get("uri") or "")
                if endpoint in {"skip_next", "skip_prev"}:
                    verified = bool(current_uri) and (
                        current_uri != before_uri
                        or str(state.get("playback_id") or "")
                        != before_playback_id
                        or str(state.get("queue_revision") or "")
                        != before_queue_revision
                        or str(state.get("session_command_id") or "")
                        == str(command["logging_params"]["command_id"])
                    )
                elif endpoint == "add_to_queue":
                    requested_uri = str(
                        ((command_values.get("track") or {}).get("uri")) or ""
                    )
                    verified = requested_uri in {
                        str(item.get("uri") or "")
                        for item in (state.get("next_tracks") or [])
                    } or (
                        bool(before_queue_revision)
                        and str(state.get("queue_revision") or "")
                        != before_queue_revision
                    )
                else:
                    verified = self._connect_state_matches(
                        endpoint, state, command_values
                    )
                    if endpoint not in {
                        "pause",
                        "resume",
                        "seek_to",
                        "set_shuffling_context",
                        "set_repeating_context",
                        "set_repeating_track",
                    }:
                        verified = bool(payload.get("ack_id"))
                if verified:
                    break
        payload["_verified"] = verified
        payload["_controller"] = controller_kind
        return payload, target, elapsed

    def _connect_play(
        self,
        *,
        track_uri: str | None = None,
        context_uri: str | None = None,
        device_id: str | None = None,
        position_ms: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], float]:
        cluster, elapsed = self._connect_cluster()
        target = self._connect_target(cluster, device_id)
        target_id = str(target.get("device_id") or "")
        if cluster.get("active_device_id") != target_id:
            _transfer, transfer_elapsed = self._connect_transfer(
                cluster, target, play=True
            )
            elapsed += transfer_elapsed
            cluster, cluster_elapsed = self._connect_cluster()
            elapsed += cluster_elapsed
        source = self._connect_source(cluster, target)
        resolved_context = context_uri
        skip_to: dict[str, Any] = {}
        if track_uri:
            details = self._web_lookup_tracks([track_uri]).get(track_uri) or {}
            resolved_context = str(details.get("album_uri") or track_uri)
            skip_to["track_uri"] = track_uri
        if not resolved_context:
            state = cluster.get("player_state") or {}
            if state.get("track") and not state.get("is_paused"):
                return {"already_playing": True, "_verified": True}, target, elapsed
            _payload, selected, command_elapsed = self._connect_player_command(
                "resume", device_id=target_id
            )
            return _payload, selected, elapsed + command_elapsed
        options: dict[str, Any] = {
            "license": source.get("license") or target.get("license") or "premium",
            "player_options_override": {},
            "skip_to": skip_to,
        }
        if position_ms is not None:
            options["skip_to"]["track_uri"] = track_uri
            options["player_options_override"]["start_position_ms"] = position_ms
        command = {
            "context": {
                "metadata": {},
                "uri": resolved_context,
                "url": "context://" + resolved_context,
            },
            "endpoint": "play",
            "logging_params": {"command_id": uuid.uuid4().hex},
            "options": options,
            "play_origin": {
                # This is an observed Spotify protocol value, not product branding.
                # Keep it stable until a fresh browser capture proves otherwise.
                "feature_identifier": "sitepack",
                "feature_version": "sitepack",
                "referrer_identifier": "cli",
            },
        }
        with self._connect_command_source(cluster, target) as (
            source_id,
            controller_kind,
        ):
            response = self._session_request(
                "POST",
                f"{CONNECT_STATE_URL}/player/command/from/{source_id}/to/{target_id}",
                json_body={"command": command},
                headers=self._web_headers(),
            )
            payload = self._json_response(response, "Spotify Web Player play")
            elapsed += response.elapsed_ms
            verified = False
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                time.sleep(0.25)
                observed, observed_elapsed = self._connect_cluster()
                elapsed += observed_elapsed
                state = observed.get("player_state") or {}
                observed_track = str((state.get("track") or {}).get("uri") or "")
                observed_context = str(state.get("context_uri") or "")
                verified = (
                    not bool(state.get("is_paused"))
                    and (
                        (bool(track_uri) and observed_track == track_uri)
                        or (bool(context_uri) and observed_context == context_uri)
                        or (not track_uri and not context_uri and bool(observed_track))
                    )
                )
                if verified:
                    break
        payload["_verified"] = verified
        payload["_controller"] = controller_kind
        return payload, target, elapsed

    def _web_fetch_playlist(
        self, playlist_id: str, *, limit: int = 100, offset: int = 0
    ) -> tuple[dict[str, Any], float]:
        payload, elapsed = self._partner_query(
            "fetchPlaylist",
            FETCH_PLAYLIST_HASH,
            {
                "enableWatchFeedEntrypoint": True,
                "includeEpisodeContentRatingsV2": True,
                "limit": limit,
                "offset": offset,
                "uri": f"spotify:playlist:{self._id(playlist_id)}",
            },
        )
        playlist = ((payload.get("data") or {}).get("playlistV2")) or {}
        if not playlist:
            raise AgentWebError("Spotify did not return the requested playlist")
        return playlist, elapsed

    def _web_create_playlist(self, name: str) -> tuple[dict[str, Any], float]:
        started = time.perf_counter()
        response = self._session_request(
            "POST",
            PLAYLIST_SERVICE_URL + "/playlist",
            json_body={
                "ops": [
                    {
                        "kind": "UPDATE_LIST_ATTRIBUTES",
                        "updateListAttributes": {
                            "newAttributes": {"values": {"name": name}}
                        },
                    }
                ]
            },
            headers=self._web_headers(),
        )
        created = self._json_response(response, "Spotify Web Player playlist creation")
        uri = str(created.get("uri") or "")
        playlist_id = uri.rsplit(":", 1)[-1] if uri.startswith("spotify:playlist:") else ""
        if not playlist_id:
            raise AgentWebError(
                "Spotify playlist creation did not return a playlist URI",
                code="website_replay_changed",
                retryable=True,
            )
        playlist, _fetch_elapsed = self._web_fetch_playlist(playlist_id, limit=1)
        owner = ((playlist.get("ownerV2") or {}).get("data") or {})
        username = owner.get("username") or str(owner.get("uri") or "").rsplit(":", 1)[-1]
        if not username:
            raise AgentWebError("Spotify did not return the new playlist owner")
        root_response = self._session_request(
            "POST",
            f"{PLAYLIST_SERVICE_URL}/user/{username}/rootlist/changes",
            json_body={
                "deltas": [
                    {
                        "info": {"source": {"client": "WEBPLAYER"}},
                        "ops": [
                            {
                                "add": {
                                    "addFirst": True,
                                    "items": [
                                        {
                                            "attributes": {
                                                "timestamp": str(int(time.time() * 1000))
                                            },
                                            "uri": uri,
                                        }
                                    ],
                                },
                                "kind": "ADD",
                            }
                        ],
                    }
                ]
            },
            headers=self._web_headers(),
        )
        self._json_response(root_response, "Spotify Web Player library update")
        return {
            "id": playlist_id,
            "uri": uri,
            "type": "playlist",
            "name": playlist.get("name") or name,
            "owner": owner.get("name"),
            "url": f"https://open.spotify.com/playlist/{playlist_id}",
        }, (time.perf_counter() - started) * 1000

    def _web_add_playlist_items(
        self, playlist_id: str, uris: list[str], position: int | None
    ) -> tuple[dict[str, Any], float]:
        move_type = "BOTTOM_OF_PLAYLIST" if position is None else "BEFORE"
        new_position: dict[str, Any] = {"fromUid": None, "moveType": move_type}
        if position is not None:
            new_position["position"] = position
        payload, elapsed = self._partner_query(
            "addToPlaylist",
            ADD_TO_PLAYLIST_HASH,
            {
                "newPosition": new_position,
                "playlistItemUris": uris,
                "playlistUri": f"spotify:playlist:{self._id(playlist_id)}",
            },
        )
        return payload, elapsed

    def _web_update_playlist(
        self, playlist_id: str, values: dict[str, Any]
    ) -> tuple[dict[str, Any], float]:
        response = self._session_request(
            "POST",
            f"{PLAYLIST_SERVICE_URL}/playlist/{self._id(playlist_id)}/changes",
            json_body={
                "deltas": [
                    {
                        "info": {"source": {"client": "WEBPLAYER"}},
                        "ops": [
                            {
                                "kind": "UPDATE_LIST_ATTRIBUTES",
                                "updateListAttributes": {
                                    "newAttributes": {"values": values}
                                },
                            }
                        ],
                    }
                ]
            },
            headers=self._web_headers(),
        )
        return self._json_response(response, "Spotify Web Player playlist update"), response.elapsed_ms

    def _web_playlist_permission(
        self, playlist_id: str, public: bool | None = None
    ) -> tuple[dict[str, Any], float]:
        identity = self._id(playlist_id)
        url = f"{PLAYLIST_PERMISSION_URL}/playlist/{identity}/permission/base"
        started = time.perf_counter()
        current_response = self._session_request(
            "GET", url, headers=self._web_headers()
        )
        current = self._json_response(
            current_response, "Spotify playlist base permission"
        )
        original_level = str(current.get("permissionLevel") or "UNKNOWN")
        target_level = "VIEWER" if public else "BLOCKED"
        changed = False
        if public is not None and original_level != target_level:
            update_response = self._session_request(
                "POST",
                url,
                json_body={**current, "permissionLevel": target_level},
                headers=self._web_headers(),
            )
            self._json_response(
                update_response, "Spotify playlist visibility update"
            )
            changed = True
        verified_response = self._session_request(
            "GET", url, headers=self._web_headers()
        )
        verified = self._json_response(
            verified_response, "Spotify playlist visibility verification"
        )
        observed_level = str(verified.get("permissionLevel") or "UNKNOWN")
        return {
            "permission_level": observed_level,
            "public": observed_level == "VIEWER",
            "collaborative": observed_level == "CONTRIBUTOR",
            "state_changed": changed,
            "verified": public is None or observed_level == target_level,
        }, (time.perf_counter() - started) * 1000

    def _web_remove_playlist_items(
        self, playlist_id: str, uris: list[str]
    ) -> tuple[dict[str, Any], float]:
        playlist, fetch_elapsed = self._web_fetch_playlist(playlist_id)
        wanted = set(uris)
        uids = [
            str(row.get("uid"))
            for row in ((playlist.get("content") or {}).get("items") or [])
            if str(((row.get("itemV2") or {}).get("data") or {}).get("uri") or "")
            in wanted
            and row.get("uid")
        ]
        observed = {
            str(((row.get("itemV2") or {}).get("data") or {}).get("uri") or "")
            for row in ((playlist.get("content") or {}).get("items") or [])
        }
        missing = sorted(wanted - observed)
        if missing:
            raise AgentWebError(
                "Spotify playlist does not contain requested URI(s): "
                + ", ".join(missing)
            )
        payload, elapsed = self._partner_query(
            "removeFromPlaylist",
            ADD_TO_PLAYLIST_HASH,
            {
                "playlistUri": f"spotify:playlist:{self._id(playlist_id)}",
                "uids": uids,
            },
        )
        return payload, fetch_elapsed + elapsed

    def _web_replace_playlist_items(
        self, playlist_id: str, uris: list[str]
    ) -> tuple[bool, float]:
        playlist, elapsed = self._web_fetch_playlist(playlist_id)
        current = [
            str(((row.get("itemV2") or {}).get("data") or {}).get("uri") or "")
            for row in ((playlist.get("content") or {}).get("items") or [])
        ]
        current = [uri for uri in current if uri]
        if current == uris:
            return True, elapsed
        if current:
            _payload, remove_elapsed = self._web_remove_playlist_items(
                playlist_id, sorted(set(current))
            )
            elapsed += remove_elapsed
        try:
            if uris:
                _payload, add_elapsed = self._web_add_playlist_items(
                    playlist_id, uris, None
                )
                elapsed += add_elapsed
        except Exception as exc:
            rollback_succeeded = False
            try:
                if current:
                    self._web_add_playlist_items(playlist_id, current, None)
                    rollback_succeeded = True
            except Exception:
                rollback_succeeded = False
            raise AgentWebError(
                "Spotify playlist replacement failed; original items "
                + ("were restored" if rollback_succeeded else "could not be restored")
            ) from exc
        verified_playlist, verify_elapsed = self._web_fetch_playlist(playlist_id)
        elapsed += verify_elapsed
        observed = [
            str(((row.get("itemV2") or {}).get("data") or {}).get("uri") or "")
            for row in ((verified_playlist.get("content") or {}).get("items") or [])
        ]
        return [uri for uri in observed if uri] == uris, elapsed

    def _web_track_search(self, query: str, limit: int) -> tuple[list[dict[str, Any]], float]:
        payload, elapsed = self._partner_query(
            "searchTopResultsList",
            SEARCH_TOP_RESULTS_HASH,
            {
                "includeAlbumPreReleases": True,
                "includeArtistHasConcertsField": False,
                "includeAudiobooks": True,
                "includeAuthors": False,
                "includeEpisodeContentRatingsV2": True,
                "includePreReleases": True,
                "isPrefix": None,
                "limit": max(limit, 10),
                "numberOfTopResults": max(limit, 10),
                "offset": 0,
                "query": query,
                "sectionFilters": ["GENERIC", "VIDEO_CONTENT"],
            },
        )
        items = (
            (((payload.get("data") or {}).get("searchV2") or {}).get("topResultsV2") or {}).get("itemsV2")
            or []
        )
        tracks: list[dict[str, Any]] = []
        for wrapper in items:
            data = ((wrapper.get("item") or {}).get("data") or {})
            if data.get("__typename") != "Track":
                continue
            album = data.get("albumOfTrack") or {}
            artists = [
                str((item.get("profile") or {}).get("name"))
                for item in ((data.get("artists") or {}).get("items") or [])
                if (item.get("profile") or {}).get("name")
            ]
            tracks.append(
                {
                    "id": data.get("id") or str(data.get("uri") or "").rsplit(":", 1)[-1],
                    "uri": data.get("uri"),
                    "type": "track",
                    "name": data.get("name"),
                    "artists": artists,
                    "album": album.get("name"),
                    "explicit": (data.get("contentRating") or {}).get("label") not in {None, "NONE"},
                    "url": (
                        f"https://open.spotify.com/track/{str(data.get('uri')).rsplit(':', 1)[-1]}"
                        if str(data.get("uri") or "").startswith("spotify:track:")
                        else None
                    ),
                }
            )
            if len(tracks) >= limit:
                break
        return tracks, elapsed

    def _access_token(self, *, force_refresh: bool = False) -> str:
        tokens = read_json(self.tokens_path, {}) or {}
        if (
            not force_refresh
            and tokens.get("access_token")
            and float(tokens.get("expires_at") or 0) > time.time() + 60
        ):
            return str(tokens["access_token"])
        if tokens.get("refresh_token") and self._client_id():
            return self._refresh()
        return self._web_access_token(force_refresh=force_refresh)

    def _api(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        retried: bool = False,
    ) -> tuple[Any, int, dict[str, str], float]:
        try:
            with exclusive_path_lock(
                self.api_rate_lock_path, timeout=90, stale_after=120
            ):
                return self._api_locked(
                    method,
                    path,
                    params=params,
                    body=body,
                    retried=retried,
                )
        except TimeoutError as exc:
            raise AgentWebError(
                "Spotify API request is waiting behind another local AgentWeb process; retry shortly",
                code="spotify_request_busy",
                retryable=True,
                details={"serialization": "per_profile_cross_process"},
            ) from exc

    def _api_locked(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        retried: bool = False,
    ) -> tuple[Any, int, dict[str, str], float]:
        backoff = read_json(self.api_backoff_path, {}) or {}
        blocked_until = float(backoff.get("until") or 0)
        if blocked_until > time.time():
            remaining = max(1, int(blocked_until - time.time()))
            raise AgentWebError(
                f"Spotify API is rate limited; retry after {remaining} seconds",
                code="spotify_rate_limited",
                retryable=True,
                details={"retry_after_seconds": remaining, "retry_at_unix": blocked_until},
            )
        token = self._access_token(force_refresh=retried)
        response = self.session().request(
            method,
            API_URL + path,
            params=params,
            json_body=body if method not in {"GET", "HEAD"} and body is not None else None,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        if response.status == 401 and not retried:
            return self._api_locked(
                method, path, params=params, body=body, retried=True
            )
        if not response.text.strip():
            payload: Any = None
        else:
            try:
                payload = json.loads(response.text)
            except json.JSONDecodeError:
                payload = response.text
        if response.status >= 400:
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    detail = error.get("message") or error.get("reason")
                else:
                    detail = payload.get("error_description") or error
            else:
                detail = payload
            retry_after = next(
                (value for key, value in response.headers.items() if key.lower() == "retry-after"),
                None,
            )
            if response.status == 429:
                try:
                    delay = max(1, int(float(retry_after or 1)))
                except (TypeError, ValueError):
                    delay = 1
                write_json(
                    self.api_backoff_path,
                    {"until": time.time() + delay, "retry_after_seconds": delay},
                )
            suffix = f"; retry after {retry_after} seconds" if retry_after else ""
            if response.status == 403:
                suffix += "; check Premium, app user allowlist, scopes, and device restrictions"
            raise AgentWebError(
                f"Spotify returned HTTP {response.status}: {detail or 'request failed'}{suffix}",
                code="spotify_rate_limited" if response.status == 429 else "spotify_http_error",
                retryable=response.status in {408, 409, 425, 429} or response.status >= 500,
                details={"status": response.status, "retry_after_seconds": retry_after},
            )
        return payload, response.status, response.headers, response.elapsed_ms

    @staticmethod
    def _playback_context(
        context_uri: Any, context_url: Any, current_item_uri: str
    ) -> dict[str, Any] | None:
        """Return a real collection context, never Spotify's internal context:// URI."""
        uri = str(context_uri or "").strip()
        if not uri or uri == current_item_uri:
            return None
        parts = uri.split(":")
        kind = parts[1] if len(parts) >= 3 and parts[0] == "spotify" else None
        # A retained single-track command is a playback origin, not useful
        # album/playlist/show context for an agent.
        if kind == "track":
            return None
        identifier = parts[-1] if kind else None
        public_url = str(context_url or "").strip()
        if not public_url.startswith(("https://open.spotify.com/", "https://play.spotify.com/")):
            public_url = (
                f"https://open.spotify.com/{kind}/{identifier}"
                if kind in {"album", "artist", "playlist", "show", "episode"}
                and identifier
                else ""
            )
        return {
            key: value
            for key, value in {"uri": uri, "url": public_url or None, "type": kind}.items()
            if value is not None
        }

    @staticmethod
    def _id(value: str) -> str:
        value = value.strip()
        if value.startswith("spotify:"):
            value = value.rsplit(":", 1)[-1]
        elif value.startswith("http://") or value.startswith("https://"):
            parsed = urlparse(value)
            if parsed.hostname not in {"open.spotify.com", "play.spotify.com"}:
                raise AgentWebError("Spotify URL must use open.spotify.com")
            value = parsed.path.strip("/").split("/")[-1]
        if not RESOURCE_ID_PATTERN.fullmatch(value):
            raise AgentWebError("Spotify resource ID is invalid")
        return value

    @staticmethod
    def _uri(value: str) -> str:
        value = value.strip()
        if not re.fullmatch(
            r"spotify:(track|episode|album|artist|playlist|show|audiobook|chapter):[A-Za-z0-9]{8,128}",
            value,
        ):
            raise AgentWebError("value must be a supported Spotify URI")
        return value

    @staticmethod
    def _confirm(confirm: bool, action: str) -> None:
        if not confirm:
            raise AgentWebError(f"{action} changes persistent Spotify state; repeat with confirm=true")

    @staticmethod
    def _page_params(limit: int, offset: int) -> dict[str, int]:
        if limit < 1 or limit > 50:
            raise AgentWebError("limit must be between 1 and 50")
        if offset < 0 or offset > 10000:
            raise AgentWebError("offset must be between 0 and 10000")
        return {"limit": limit, "offset": offset}

    @staticmethod
    def _artists(item: dict[str, Any]) -> list[str]:
        return [str(artist.get("name")) for artist in item.get("artists") or [] if artist.get("name")]

    @classmethod
    def _compact_item(cls, item: dict[str, Any] | None) -> dict[str, Any] | None:
        if not item:
            return None
        album = item.get("album") or {}
        images = item.get("images") or album.get("images") or []
        return {
            "id": item.get("id"),
            "uri": item.get("uri"),
            "type": item.get("type"),
            "name": item.get("name"),
            "artists": cls._artists(item),
            "album": album.get("name"),
            "duration_ms": item.get("duration_ms"),
            "explicit": item.get("explicit"),
            "release_date": item.get("release_date") or album.get("release_date"),
            "owner": (item.get("owner") or {}).get("display_name"),
            "description": item.get("description"),
            "image": images[0].get("url") if images else None,
            "url": (item.get("external_urls") or {}).get("spotify"),
        }

    def auth_status(self) -> dict[str, Any]:
        setup = self.setup_status()
        if not setup["oauth_tokens_saved"] and not setup["website_session_saved"]:
            return {
                "operation": "spotify.auth_status",
                "authenticated": False,
                "account": None,
                "setup": setup,
                "local_playback_ready": setup["desktop_playback_available"],
                "local_playback_auth": "uses the Spotify Desktop app's own signed-in session",
                "next_action": "agentweb connect spotify",
                "token_exposed": False,
                "session": self.session_freshness(False),
            }
        if setup["website_session_saved"] and not setup["oauth_tokens_saved"]:
            try:
                self._web_access_token()
            except AgentWebError as exc:
                return {
                    "operation": "spotify.auth_status",
                    "authenticated": False,
                    "website_replay_ready": False,
                    "account": None,
                    "setup": setup,
                    "error": str(exc),
                    "token_exposed": False,
                    "session": self.session_freshness(False),
                }
            return {
                "operation": "spotify.auth_status",
                "authenticated": True,
                "website_replay_ready": True,
                "account_api_ready": False,
                "account": None,
                "account_verification": "non_anonymous_web_player_token",
                "credential_source": "retained_web_session",
                "setup": setup,
                "token_exposed": False,
                "session": self.session_freshness(True),
            }
        try:
            account, _status, _headers, elapsed = self._api("GET", "/me")
        except AgentWebError as exc:
            web_token = read_json(self.web_tokens_path, {}) or {}
            website_session_verified = bool(
                setup["website_session_saved"]
                and web_token.get("access_token")
                and web_token.get("source")
                == "retained_spotify_web_player_session"
                and float(web_token.get("expires_at") or 0) > time.time()
            )
            if website_session_verified:
                return {
                    "operation": "spotify.auth_status",
                    "authenticated": True,
                    "account": None,
                    "account_api_ready": False,
                    "account_verification": "non_anonymous_web_player_token",
                    "credential_source": "retained_web_session",
                    "setup": setup,
                    "warning": str(exc),
                    "token_exposed": False,
                    "session": self.session_freshness(True),
                }
            return {
                "operation": "spotify.auth_status",
                "authenticated": False,
                "account": None,
                "setup": setup,
                "error": str(exc),
                "token_exposed": False,
                "session": self.session_freshness(False),
            }
        return {
            "operation": "spotify.auth_status",
            "authenticated": True,
            "account": self._compact_item(account),
            "account_id": account.get("id"),
            "display_name": account.get("display_name"),
            "elapsed_ms": round(elapsed, 1),
            "token_exposed": False,
            "session": self.session_freshness(True),
            "credential_source": (
                "oauth_pkce" if setup["oauth_tokens_saved"] else "retained_web_session"
            ),
        }

    def disconnect(self, confirm: bool = False) -> dict[str, Any]:
        self._confirm(confirm, "spotify.disconnect")
        existed = self.tokens_path.exists() or self.web_tokens_path.exists()
        self.tokens_path.unlink(missing_ok=True)
        self.web_tokens_path.unlink(missing_ok=True)
        self.web_client_token_path.unlink(missing_ok=True)
        return {
            "operation": "spotify.disconnect",
            "disconnected": existed,
            "website_session_preserved": True,
            "remote_grant_revoked": False,
        }

    def account(self) -> dict[str, Any]:
        if not self._has_oauth_tokens() and self._has_web_session():
            payload, elapsed = self._web_account()
            return {
                "operation": "spotify.account",
                "account": payload,
                "truncated": False,
                "control_path": "retained_spotify_web_player_session",
                "elapsed_ms": round(elapsed, 1),
            }
        payload, _status, _headers, elapsed = self._api("GET", "/me")
        safe, truncated = bounded_data(payload, max_items=50, max_string=4000)
        return {"operation": "spotify.account", "account": safe, "truncated": truncated, "elapsed_ms": round(elapsed, 1)}

    def search(
        self,
        query: str,
        types: list[str] | None = None,
        limit: int = 5,
        offset: int = 0,
        market: str | None = None,
    ) -> dict[str, Any]:
        if not query.strip():
            raise AgentWebError("query cannot be empty")
        selected = types or ["track"]
        if not selected or len(selected) > 7 or any(item not in SEARCH_TYPES for item in selected):
            raise AgentWebError("types must contain supported Spotify search types")
        if limit < 1 or limit > 10:
            raise AgentWebError("Spotify development-mode search limit must be between 1 and 10")
        if offset < 0 or offset > 10000:
            raise AgentWebError("offset must be between 0 and 10000")
        if not self._has_oauth_tokens():
            if self._has_web_session():
                results, elapsed = self._web_search(query, selected, limit, offset)
                control_path = "retained_web_session_replay"
            else:
                if selected != ["track"] or offset != 0:
                    raise AuthenticationRequired(
                        "Searching non-track Spotify catalog types requires one normal Spotify login. Call site_connect for spotify, then retry."
                    )
                started = time.perf_counter()
                tracks = self._public_track_search(query, limit)
                elapsed = (time.perf_counter() - started) * 1000
                control_path = "public_index_and_spotify_oembed"
                results = {"tracks": tracks}
            return {
                "operation": "spotify.search",
                "query": query,
                "types": selected,
                "limit": limit,
                "offset": offset,
                "results": results,
                "control_path": control_path,
                "oauth_required": False,
                "elapsed_ms": round(elapsed, 1),
            }
        params: dict[str, Any] = {"q": query, "type": ",".join(selected), "limit": limit, "offset": offset}
        if market:
            params["market"] = market
        payload, _status, _headers, elapsed = self._api("GET", "/search", params=params)
        results: dict[str, Any] = {}
        for kind in selected:
            container = payload.get(kind + "s") or {}
            results[kind + "s"] = [
                self._compact_item(item) for item in (container.get("items") or [])
            ]
        return {
            "operation": "spotify.search",
            "query": query,
            "types": selected,
            "limit": limit,
            "offset": offset,
            "results": results,
            "elapsed_ms": round(elapsed, 1),
        }

    def resource(self, kind: str, id: str, market: str | None = None) -> dict[str, Any]:
        path_kind = RESOURCE_PATHS.get(kind)
        if not path_kind:
            raise AgentWebError("kind is not supported")
        params = {"market": market} if market else None
        payload, _status, _headers, elapsed = self._api(
            "GET", f"/{path_kind}/{self._id(id)}", params=params
        )
        safe, truncated = bounded_data(payload, max_items=100, max_string=10000)
        safe, total_truncated, original_chars = enforce_data_budget(safe, max_total_chars=50000)
        return {
            "operation": "spotify.resource",
            "kind": kind,
            "resource": safe,
            "truncated": truncated or total_truncated,
            "original_chars": original_chars,
            "elapsed_ms": round(elapsed, 1),
        }

    def playback_state(self) -> dict[str, Any]:
        if not self._has_oauth_tokens() and self._has_web_session():
            cluster, elapsed = self._connect_cluster()
            state = cluster.get("player_state") or {}
            raw_track = state.get("track") or {}
            uri = str(raw_track.get("uri") or "")
            item = self._web_lookup_tracks([uri]).get(uri) or self._connect_track(raw_track)
            devices = cluster.get("devices") or {}
            active_id = cluster.get("active_device_id")
            active_device = devices.get(active_id) if active_id else None
            options = state.get("options") or {}
            progress_ms = int(state.get("position") or 0)
            if progress_ms == 0 and raw_track and self._desktop_available():
                try:
                    desktop = self._desktop_status()
                    if desktop.get("position_seconds") is not None:
                        progress_ms = int(float(desktop["position_seconds"]) * 1000)
                except AgentWebError:
                    pass
            return {
                "operation": "spotify.playback_state",
                "active": bool(raw_track),
                "is_playing": bool(raw_track) and not bool(state.get("is_paused")),
                "progress_ms": progress_ms,
                "duration_ms": int(state.get("duration") or (item or {}).get("duration_ms") or 0) or None,
                "item": item,
                "device": (
                    self._connect_device(active_device, active_id)
                    if active_device
                    else None
                ),
                "context": self._playback_context(
                    state.get("context_uri"), state.get("context_url"), uri
                ),
                "shuffle_state": bool(options.get("shuffling_context")),
                "repeat_state": (
                    "track"
                    if options.get("repeating_track")
                    else "context"
                    if options.get("repeating_context")
                    else "off"
                ),
                "control_path": "retained_spotify_web_player_connect",
                "elapsed_ms": round(elapsed, 1),
            }
        if not self._has_oauth_tokens() and self._desktop_available():
            desktop = self._desktop_status()
            return {
                "operation": "spotify.playback_state",
                "active": desktop.get("state") in {"playing", "paused"},
                "is_playing": desktop.get("state") == "playing",
                "item": {
                    "name": desktop.get("track"),
                    "artists": [desktop.get("artist")] if desktop.get("artist") else [],
                    "album": desktop.get("album"),
                },
                "progress_ms": (
                    int(desktop["position_seconds"] * 1000)
                    if desktop.get("position_seconds") is not None
                    else None
                ),
                "duration_ms": (
                    int(desktop["duration_seconds"] * 1000)
                    if desktop.get("duration_seconds") is not None
                    else None
                ),
                "device": {"type": "Computer", "name": "Spotify Desktop"},
                "control_path": "spotify_desktop_apple_events",
            }
        payload, status, _headers, elapsed = self._api("GET", "/me/player")
        if payload is None:
            return {"operation": "spotify.playback_state", "active": False, "status": status, "elapsed_ms": round(elapsed, 1)}
        return {
            "operation": "spotify.playback_state",
            "active": True,
            "is_playing": payload.get("is_playing"),
            "progress_ms": payload.get("progress_ms"),
            "duration_ms": (payload.get("item") or {}).get("duration_ms"),
            "item": self._compact_item(payload.get("item")),
            "device": payload.get("device"),
            "context": payload.get("context"),
            "shuffle_state": payload.get("shuffle_state"),
            "repeat_state": payload.get("repeat_state"),
            "elapsed_ms": round(elapsed, 1),
        }

    def currently_playing(self) -> dict[str, Any]:
        if not self._has_oauth_tokens() and self._has_web_session():
            state = self.playback_state()
            return {
                "operation": "spotify.currently_playing",
                **{key: value for key, value in state.items() if key != "operation"},
            }
        if not self._has_oauth_tokens() and self._desktop_available():
            state = self.playback_state()
            return {"operation": "spotify.currently_playing", **{
                key: value for key, value in state.items() if key != "operation"
            }}
        payload, status, _headers, elapsed = self._api("GET", "/me/player/currently-playing")
        return {
            "operation": "spotify.currently_playing",
            "active": payload is not None,
            "status": status,
            "is_playing": (payload or {}).get("is_playing"),
            "progress_ms": (payload or {}).get("progress_ms"),
            "item": self._compact_item((payload or {}).get("item")),
            "elapsed_ms": round(elapsed, 1),
        }

    def devices(self) -> dict[str, Any]:
        if not self._has_oauth_tokens() and self._has_web_session():
            cluster, elapsed = self._connect_cluster()
            active_id = cluster.get("active_device_id")
            devices = [
                self._connect_device(device, active_id)
                for device in (cluster.get("devices") or {}).values()
            ]
            devices.sort(key=lambda item: (not item["is_active"], str(item["name"] or "")))
            return {
                "operation": "spotify.devices",
                "count": len(devices),
                "devices": devices,
                "device_ids_are_ephemeral": True,
                "control_path": "retained_spotify_web_player_connect",
                "elapsed_ms": round(elapsed, 1),
            }
        payload, _status, _headers, elapsed = self._api("GET", "/me/player/devices")
        devices = payload.get("devices") or []
        return {"operation": "spotify.devices", "count": len(devices), "devices": devices, "device_ids_are_ephemeral": True, "elapsed_ms": round(elapsed, 1)}

    def play(
        self,
        query: str | None = None,
        uri: str | None = None,
        context_uri: str | None = None,
        device_id: str | None = None,
        position_ms: int | None = None,
        offset_uri: str | None = None,
        offset_position: int | None = None,
    ) -> dict[str, Any]:
        supplied = sum(value is not None for value in (query, uri, context_uri))
        if supplied > 1:
            raise AgentWebError("use only one of query, uri, or context_uri")
        if not self._has_oauth_tokens() and self._has_web_session():
            if offset_position is not None and offset_position < 0:
                raise AgentWebError("offset_position cannot be negative")
            if position_ms is not None and position_ms < 0:
                raise AgentWebError("position_ms cannot be negative")
            resolved_item = None
            if query is not None:
                resolved_item = self._web_track_search(query, 1)[0][0]
                uri = resolved_item["uri"]
            if offset_uri is not None:
                uri = self._uri(offset_uri)
            payload, target, elapsed = self._connect_play(
                track_uri=self._uri(uri) if uri else None,
                context_uri=self._uri(context_uri) if context_uri else None,
                device_id=device_id,
                position_ms=None,
            )
            if position_ms is not None:
                self.seek(position_ms, device_id=str(target.get("device_id") or ""))
            return {
                "operation": "spotify.play",
                "command_sent": True,
                "verified": bool(payload.get("_verified")),
                "device_id": target.get("device_id"),
                "device_name": target.get("name"),
                "resolved_item": resolved_item,
                "context_uri": context_uri,
                "position_ms": position_ms,
                "offset_position": offset_position,
                "control_path": "retained_spotify_web_player_connect",
                "controller": payload.get("_controller"),
                "elapsed_ms": round(elapsed, 1),
            }
        if not self._has_oauth_tokens() and device_id is None:
            if context_uri is not None or offset_uri is not None or offset_position is not None:
                raise AuthenticationRequired(
                    "Playing a context or offset requires one normal Spotify login. Call site_connect for spotify, then retry."
                )
            resolved_item = None
            desktop_uri = uri
            if query is not None:
                resolved_item = self._public_track_search(query, 1)[0]
                desktop_uri = resolved_item["uri"]
            result = self._desktop_play(desktop_uri)
            result["resolved_item"] = resolved_item
            if position_ms is not None:
                self.seek(position_ms)
                result["position_ms"] = position_ms
            return result
        resolved = None
        body: dict[str, Any] = {}
        if query is not None:
            result = self.search(query, types=["track"], limit=1)
            tracks = result["results"]["tracks"]
            if not tracks:
                raise AgentWebError("Spotify search returned no playable track")
            resolved = tracks[0]
            body["uris"] = [resolved["uri"]]
        elif uri is not None:
            body["uris"] = [self._uri(uri)]
        elif context_uri is not None:
            body["context_uri"] = self._uri(context_uri)
        if position_ms is not None:
            if position_ms < 0:
                raise AgentWebError("position_ms cannot be negative")
            body["position_ms"] = position_ms
        if offset_uri is not None and offset_position is not None:
            raise AgentWebError("use only one of offset_uri or offset_position")
        if offset_uri is not None:
            body["offset"] = {"uri": self._uri(offset_uri)}
        if offset_position is not None:
            if offset_position < 0:
                raise AgentWebError("offset_position cannot be negative")
            body["offset"] = {"position": offset_position}
        params = {"device_id": device_id} if device_id else None
        _payload, status, _headers, elapsed = self._api("PUT", "/me/player/play", params=params, body=body or None)
        return {"operation": "spotify.play", "command_sent": True, "status": status, "device_id": device_id, "resolved_item": resolved, "elapsed_ms": round(elapsed, 1)}

    def _player_command(
        self, operation: str, method: str, path: str, device_id: str | None = None
    ) -> dict[str, Any]:
        if not self._has_oauth_tokens() and self._has_web_session():
            endpoint = {"pause": "pause", "next": "skip_next", "previous": "skip_prev"}[operation]
            payload, target, elapsed = self._connect_player_command(
                endpoint, device_id=device_id
            )
            return {
                "operation": f"spotify.{operation}",
                "command_sent": True,
                "verified": bool(payload.get("_verified")),
                "device_id": target.get("device_id"),
                "control_path": "retained_spotify_web_player_connect",
                "controller": payload.get("_controller"),
                "elapsed_ms": round(elapsed, 1),
            }
        if not self._has_oauth_tokens() and device_id is None and self._desktop_available():
            statement = {
                "pause": 'tell application "Spotify" to pause',
                "next": 'tell application "Spotify" to next track',
                "previous": 'tell application "Spotify" to previous track',
            }.get(operation)
            if statement:
                self._desktop_script(statement)
                status = self._desktop_status()
                return {
                    "operation": f"spotify.{operation}",
                    "command_sent": True,
                    "verified": True,
                    "control_path": "spotify_desktop_apple_events",
                    "desktop": status,
                }
        params = {"device_id": device_id} if device_id else None
        _payload, status, _headers, elapsed = self._api(method, path, params=params)
        return {"operation": f"spotify.{operation}", "command_sent": True, "status": status, "device_id": device_id, "elapsed_ms": round(elapsed, 1)}

    def pause(self, device_id: str | None = None) -> dict[str, Any]:
        return self._player_command("pause", "PUT", "/me/player/pause", device_id)

    def next(self, device_id: str | None = None) -> dict[str, Any]:
        return self._player_command("next", "POST", "/me/player/next", device_id)

    def previous(self, device_id: str | None = None) -> dict[str, Any]:
        return self._player_command("previous", "POST", "/me/player/previous", device_id)

    def seek(self, position_ms: int, device_id: str | None = None) -> dict[str, Any]:
        if position_ms < 0:
            raise AgentWebError("position_ms cannot be negative")
        before = self.playback_state()
        duration_ms = before.get("duration_ms") or (before.get("item") or {}).get("duration_ms")
        if duration_ms is not None and position_ms > int(duration_ms):
            raise AgentWebError(
                f"position_ms exceeds the current item's duration ({duration_ms} ms)"
            )
        if not self._has_oauth_tokens() and self._has_web_session():
            payload, target, elapsed = self._connect_player_command(
                "seek_to", device_id=device_id, values={"value": position_ms}
            )
            observed = self.playback_state().get("progress_ms")
            return {
                "operation": "spotify.seek",
                "command_sent": True,
                "verified": observed is not None and abs(int(observed) - position_ms) <= 3000,
                "position_ms": position_ms,
                "observed_position_ms": observed,
                "device_id": target.get("device_id"),
                "control_path": "retained_spotify_web_player_connect",
                "controller": payload.get("_controller"),
                "elapsed_ms": round(elapsed, 1),
            }
        if not self._has_oauth_tokens() and device_id is None and self._desktop_available():
            self._desktop_script(
                f'tell application "Spotify" to set player position to {position_ms / 1000.0}'
            )
            return {
                "operation": "spotify.seek",
                "command_sent": True,
                "verified": True,
                "position_ms": position_ms,
                "control_path": "spotify_desktop_apple_events",
            }
        params: dict[str, Any] = {"position_ms": position_ms}
        if device_id:
            params["device_id"] = device_id
        _payload, status, _headers, elapsed = self._api("PUT", "/me/player/seek", params=params)
        return {"operation": "spotify.seek", "command_sent": True, "status": status, "position_ms": position_ms, "device_id": device_id, "elapsed_ms": round(elapsed, 1)}

    def volume(self, volume_percent: int, device_id: str | None = None) -> dict[str, Any]:
        if volume_percent < 0 or volume_percent > 100:
            raise AgentWebError("volume_percent must be between 0 and 100")
        if not self._has_oauth_tokens() and self._has_web_session():
            cluster, elapsed = self._connect_cluster()
            target = self._connect_target(cluster, device_id)
            target_id = str(target.get("device_id") or "")
            with self._connect_command_source(cluster, target) as (
                source_id,
                controller_kind,
            ):
                response = self._session_request(
                    "PUT",
                    f"{CONNECT_STATE_URL}/connect/volume/from/{source_id}/to/{target_id}",
                    json_body={"volume": round(65535 * volume_percent / 100)},
                    headers=self._web_headers(),
                )
                self._json_response(response, "Spotify Web Player volume")
                time.sleep(0.3)
                observed, observed_elapsed = self._connect_cluster()
            observed_device = (observed.get("devices") or {}).get(target_id) or {}
            observed_percent = round(float(observed_device.get("volume") or 0) * 100 / 65535)
            return {
                "operation": "spotify.volume",
                "command_sent": True,
                "verified": abs(observed_percent - volume_percent) <= 1,
                "volume_percent": volume_percent,
                "device_id": target_id,
                "control_path": "retained_spotify_web_player_connect",
                "controller": controller_kind,
                "elapsed_ms": round(elapsed + response.elapsed_ms + observed_elapsed, 1),
            }
        if not self._has_oauth_tokens() and device_id is None and self._desktop_available():
            self._desktop_script(
                f'tell application "Spotify" to set sound volume to {volume_percent}'
            )
            return {
                "operation": "spotify.volume",
                "command_sent": True,
                "verified": True,
                "volume_percent": volume_percent,
                "control_path": "spotify_desktop_apple_events",
            }
        params: dict[str, Any] = {"volume_percent": volume_percent}
        if device_id:
            params["device_id"] = device_id
        _payload, status, _headers, elapsed = self._api("PUT", "/me/player/volume", params=params)
        return {"operation": "spotify.volume", "command_sent": True, "status": status, "volume_percent": volume_percent, "device_id": device_id, "elapsed_ms": round(elapsed, 1)}

    def shuffle(self, state: bool, device_id: str | None = None) -> dict[str, Any]:
        if not self._has_oauth_tokens() and self._has_web_session():
            payload, target, elapsed = self._connect_player_command(
                "set_shuffling_context", device_id=device_id, values={"value": state}
            )
            return {
                "operation": "spotify.shuffle",
                "command_sent": True,
                "verified": bool(payload.get("_verified")),
                "state": state,
                "device_id": target.get("device_id"),
                "control_path": "retained_spotify_web_player_connect",
                "controller": payload.get("_controller"),
                "elapsed_ms": round(elapsed, 1),
            }
        if not self._has_oauth_tokens() and device_id is None and self._desktop_available():
            value = "true" if state else "false"
            self._desktop_script(
                f'tell application "Spotify" to set shuffling to {value}'
            )
            return {
                "operation": "spotify.shuffle",
                "command_sent": True,
                "verified": True,
                "state": state,
                "control_path": "spotify_desktop_apple_events",
            }
        params: dict[str, Any] = {"state": "true" if state else "false"}
        if device_id:
            params["device_id"] = device_id
        _payload, status, _headers, elapsed = self._api("PUT", "/me/player/shuffle", params=params)
        return {"operation": "spotify.shuffle", "command_sent": True, "status": status, "state": state, "device_id": device_id, "elapsed_ms": round(elapsed, 1)}

    def repeat(self, state: str, device_id: str | None = None) -> dict[str, Any]:
        if state not in {"track", "context", "off"}:
            raise AgentWebError("state must be track, context, or off")
        if not self._has_oauth_tokens() and self._has_web_session():
            values = {"value": state != "off"}
            endpoint = "set_repeating_track" if state == "track" else "set_repeating_context"
            if state == "track":
                self._connect_player_command(
                    "set_repeating_context", device_id=device_id, values={"value": False}
                )
            elif state == "context":
                self._connect_player_command(
                    "set_repeating_track", device_id=device_id, values={"value": False}
                )
            else:
                self._connect_player_command(
                    "set_repeating_track", device_id=device_id, values={"value": False}
                )
            payload, target, elapsed = self._connect_player_command(
                endpoint, device_id=device_id, values=values
            )
            return {
                "operation": "spotify.repeat",
                "command_sent": True,
                "verified": bool(payload.get("_verified")),
                "state": state,
                "device_id": target.get("device_id"),
                "control_path": "retained_spotify_web_player_connect",
                "controller": payload.get("_controller"),
                "elapsed_ms": round(elapsed, 1),
            }
        if not self._has_oauth_tokens() and device_id is None and self._desktop_available():
            if state == "track":
                raise AuthenticationRequired(
                    "Spotify Desktop Apple Events exposes context repeat on/off but not repeat-one. Connect OAuth for repeat track mode."
                )
            value = "true" if state == "context" else "false"
            self._desktop_script(
                f'tell application "Spotify" to set repeating to {value}'
            )
            return {
                "operation": "spotify.repeat",
                "command_sent": True,
                "verified": True,
                "state": state,
                "control_path": "spotify_desktop_apple_events",
            }
        params: dict[str, Any] = {"state": state}
        if device_id:
            params["device_id"] = device_id
        _payload, status, _headers, elapsed = self._api("PUT", "/me/player/repeat", params=params)
        return {"operation": "spotify.repeat", "command_sent": True, "status": status, "state": state, "device_id": device_id, "elapsed_ms": round(elapsed, 1)}

    def transfer_playback(self, device_id: str, play: bool = False) -> dict[str, Any]:
        if not device_id.strip():
            raise AgentWebError("device_id cannot be empty")
        if not self._has_oauth_tokens() and self._has_web_session():
            cluster, elapsed = self._connect_cluster()
            target = self._connect_target(cluster, device_id)
            payload, transfer_elapsed = self._connect_transfer(
                cluster, target, play=play
            )
            time.sleep(0.3)
            observed, observed_elapsed = self._connect_cluster()
            return {
                "operation": "spotify.transfer_playback",
                "command_sent": True,
                "verified": observed.get("active_device_id") == device_id,
                "device_id": device_id,
                "play": play,
                "control_path": "retained_spotify_web_player_connect",
                "elapsed_ms": round(elapsed + transfer_elapsed + observed_elapsed, 1),
            }
        _payload, status, _headers, elapsed = self._api(
            "PUT", "/me/player", body={"device_ids": [device_id], "play": play}
        )
        return {"operation": "spotify.transfer_playback", "command_sent": True, "status": status, "device_id": device_id, "play": play, "elapsed_ms": round(elapsed, 1)}

    def queue(self) -> dict[str, Any]:
        if not self._has_oauth_tokens() and self._has_web_session():
            cluster, elapsed = self._connect_cluster()
            state = cluster.get("player_state") or {}
            current = state.get("track") or {}
            queued_raw = [
                item
                for item in (state.get("next_tracks") or [])
                if str(item.get("uri") or "").startswith("spotify:track:")
            ]
            uris = [str(current.get("uri") or "")] + [
                str(item.get("uri") or "") for item in queued_raw
            ]
            details = self._web_lookup_tracks(uris)
            queued = [
                details.get(str(item.get("uri") or "")) or self._connect_track(item)
                for item in queued_raw
            ]
            current_uri = str(current.get("uri") or "")
            return {
                "operation": "spotify.queue",
                "currently_playing": details.get(current_uri) or self._connect_track(current),
                "count": len(queued),
                "queue": queued,
                "control_path": "retained_spotify_web_player_connect",
                "elapsed_ms": round(elapsed, 1),
            }
        payload, _status, _headers, elapsed = self._api("GET", "/me/player/queue")
        queued = [self._compact_item(item) for item in payload.get("queue") or []]
        return {"operation": "spotify.queue", "currently_playing": self._compact_item(payload.get("currently_playing")), "count": len(queued), "queue": queued, "elapsed_ms": round(elapsed, 1)}

    def add_to_queue(self, uri: str, device_id: str | None = None) -> dict[str, Any]:
        if not self._has_oauth_tokens() and self._has_web_session():
            normalized = self._uri(uri)
            payload, target, elapsed = self._connect_player_command(
                "add_to_queue",
                device_id=device_id,
                values={"track": {"uri": normalized}},
            )
            return {
                "operation": "spotify.add_to_queue",
                "command_sent": True,
                "verified": bool(payload.get("_verified")),
                "uri": normalized,
                "device_id": target.get("device_id"),
                "control_path": "retained_spotify_web_player_connect",
                "controller": payload.get("_controller"),
                "elapsed_ms": round(elapsed, 1),
            }
        params: dict[str, Any] = {"uri": self._uri(uri)}
        if device_id:
            params["device_id"] = device_id
        _payload, status, _headers, elapsed = self._api("POST", "/me/player/queue", params=params)
        return {"operation": "spotify.add_to_queue", "command_sent": True, "status": status, "uri": uri, "device_id": device_id, "elapsed_ms": round(elapsed, 1)}

    def recently_played(
        self, limit: int = 20, after: int | None = None, before: int | None = None
    ) -> dict[str, Any]:
        if after is not None and before is not None:
            raise AgentWebError("use only one of after or before")
        if limit < 1 or limit > 50:
            raise AgentWebError("limit must be between 1 and 50")
        if not self._has_oauth_tokens() and self._has_web_session():
            if after is not None or before is not None:
                raise AgentWebError(
                    "after and before are exact-play-history cursors exposed only by Spotify's optional official OAuth transport. A normal Web Player login exposes the website's Recents activity instead."
                )
            payload, elapsed = self._partner_query(
                "recents",
                RECENTS_HASH,
                {
                    "limit": min(100, max(limit * 3, 20)),
                    "offset": 0,
                    "uris": ["spotify:list:recents:page"],
                },
            )
            lists = (payload.get("data") or {}).get("lists") or []
            container = lists[0] if lists else {}
            rows = ((container.get("items") or {}).get("items") or [])
            items: list[dict[str, Any]] = []
            for row in rows:
                entity = row.get("entity") or {}
                data = entity.get("data") or {}
                identity = data.get("identityTrait") or {}
                uri = str(data.get("uri") or entity.get("_uri") or "")
                if not uri:
                    continue
                contributors = [
                    {"name": item.get("name"), "uri": item.get("uri")}
                    for item in ((identity.get("contributors") or {}).get("items") or [])
                ]
                parent = (identity.get("contentHierarchyParent") or {}).get("identityTrait") or {}
                attributes = [
                    str(item.get("key") or "")
                    for item in (row.get("formatListAttributes") or [])
                ]
                activity = next(
                    (
                        value.removeprefix("recent_type_")
                        for value in attributes
                        if value.startswith("recent_type_")
                    ),
                    None,
                )
                image_sources = (
                    (((data.get("visualIdentityTrait") or {}).get("squareCoverImage") or {}).get("image") or {}).get("data") or {}
                ).get("sources") or []
                items.append(
                    {
                        "uri": uri,
                        "type": str((data.get("entityTypeTrait") or {}).get("type") or "")
                        .removeprefix("ENTITY_TYPE_")
                        .lower()
                        or None,
                        "name": identity.get("name"),
                        "subtitle": identity.get("type"),
                        "contributors": contributors,
                        "parent": parent.get("name"),
                        "activity": activity,
                        "date": row.get("addedAt"),
                        "image": image_sources[0].get("url") if image_sources else None,
                    }
                )
                if len(items) >= limit:
                    break
            paging = (container.get("items") or {}).get("pagingInfo") or {}
            return {
                "operation": "spotify.recently_played",
                "count": len(items),
                "items": items,
                "total": (container.get("items") or {}).get("totalCount"),
                "cursors": {"offset": paging.get("offset"), "limit": paging.get("limit")},
                "semantics": "Spotify Web Player Recents activity; dates and played/saved activity match the website, but exact played_at timestamps are not exposed by this website route",
                "control_path": "retained_spotify_web_player_session",
                "elapsed_ms": round(elapsed, 1),
            }
        params = self._page_params(limit, 0)
        params.pop("offset")
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        payload, _status, _headers, elapsed = self._api(
            "GET", "/me/player/recently-played", params=params
        )
        items = [
            {"played_at": row.get("played_at"), "track": self._compact_item(row.get("track")), "context": row.get("context")}
            for row in payload.get("items") or []
        ]
        return {"operation": "spotify.recently_played", "count": len(items), "items": items, "cursors": payload.get("cursors"), "elapsed_ms": round(elapsed, 1)}

    def top_items(
        self,
        kind: str,
        time_range: str = "medium_term",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        if kind not in {"tracks", "artists"}:
            raise AgentWebError("kind must be tracks or artists")
        if time_range not in {"short_term", "medium_term", "long_term"}:
            raise AgentWebError("time_range is invalid")
        params = self._page_params(limit, offset)
        params["time_range"] = time_range
        payload, _status, _headers, elapsed = self._api("GET", f"/me/top/{kind}", params=params)
        items = [self._compact_item(item) for item in payload.get("items") or []]
        return {"operation": "spotify.top_items", "kind": kind, "time_range": time_range, "count": len(items), "items": items, "total": payload.get("total"), "elapsed_ms": round(elapsed, 1)}

    def library(self, kind: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        allowed = {"tracks", "albums", "shows", "episodes", "audiobooks", "following", "playlists"}
        if kind not in allowed:
            raise AgentWebError("library kind is invalid")
        self._page_params(limit, offset)
        if not self._has_oauth_tokens() and self._has_web_session() and kind in {"playlists", "following"}:
            # Spotify's Playlists-filtered Web Player collection includes a
            # synthetic Liked Songs row at raw offset zero. It is not a
            # playlist and was silently consuming one requested result.
            web_limit = limit
            web_offset = offset + 1 if kind == "playlists" else offset
            page, elapsed = self._web_library_page(
                limit=web_limit,
                offset=web_offset,
                filters=["Playlists" if kind == "playlists" else "Artists"],
            )
            rows = []
            for row in page.get("items") or []:
                wrapper = row.get("item") or {}
                data = wrapper.get("data") or {}
                compact = self._web_compact_entity(data)
                if compact:
                    rows.append(
                        {
                            "added_at": (row.get("addedAt") or {}).get("isoString"),
                            "played_at": (row.get("playedAt") or {}).get("isoString"),
                            "item": compact,
                        }
                    )
            return {
                "operation": "spotify.library",
                "kind": kind,
                "count": len(rows),
                "items": rows,
                "total": max(int(page.get("totalCount") or 0) - 1, 0)
                if kind == "playlists"
                else page.get("totalCount"),
                "control_path": "retained_spotify_web_player_session",
                "elapsed_ms": round(elapsed, 1),
            }
        params: dict[str, Any] = self._page_params(limit, offset)
        if kind == "following":
            params = {"type": "artist", "limit": limit}
            path = "/me/following"
        elif kind == "playlists":
            path = "/me/playlists"
        else:
            path = f"/me/{kind}"
        payload, _status, _headers, elapsed = self._api("GET", path, params=params)
        container = payload.get("artists") if kind == "following" else payload
        rows = []
        for row in (container or {}).get("items") or []:
            item = row.get("track") or row.get("album") or row.get("show") or row.get("episode") or row.get("audiobook") or row
            rows.append({"added_at": row.get("added_at"), "item": self._compact_item(item)})
        return {"operation": "spotify.library", "kind": kind, "count": len(rows), "items": rows, "total": (container or {}).get("total"), "elapsed_ms": round(elapsed, 1)}

    def _library_mutation(self, method: str, uris: list[str], confirm: bool) -> dict[str, Any]:
        action = "save_library" if method == "PUT" else "remove_library"
        self._confirm(confirm, f"spotify.{action}")
        if not uris or len(uris) > 40:
            raise AgentWebError("uris must contain between 1 and 40 Spotify URIs")
        normalized = [self._uri(uri) for uri in uris]
        if not self._has_oauth_tokens() and self._has_web_session():
            operation = "addToLibrary" if method == "PUT" else "removeFromLibrary"
            _payload, elapsed = self._partner_query(
                operation,
                LIBRARY_MUTATION_HASH,
                {"libraryItemUris": normalized},
            )
            observed = self.library_contains(normalized)
            expected = method == "PUT"
            verified = all(
                bool((observed.get("contains") or {}).get(uri)) is expected
                for uri in normalized
            )
            return {
                "operation": f"spotify.{action}",
                "changed": True,
                "verified": verified,
                "uris": normalized,
                "control_path": "retained_spotify_web_player_session",
                "elapsed_ms": round(
                    elapsed + float(observed.get("elapsed_ms") or 0), 1
                ),
            }
        _payload, status, _headers, elapsed = self._api(method, "/me/library", params={"uris": ",".join(normalized)})
        return {"operation": f"spotify.{action}", "changed": True, "status": status, "uris": normalized, "elapsed_ms": round(elapsed, 1)}

    def save_library(self, uris: list[str], confirm: bool = False) -> dict[str, Any]:
        return self._library_mutation("PUT", uris, confirm)

    def remove_library(self, uris: list[str], confirm: bool = False) -> dict[str, Any]:
        return self._library_mutation("DELETE", uris, confirm)

    def library_contains(self, uris: list[str]) -> dict[str, Any]:
        if not uris or len(uris) > 40:
            raise AgentWebError("uris must contain between 1 and 40 Spotify URIs")
        normalized = [self._uri(uri) for uri in uris]
        if not self._has_oauth_tokens() and self._has_web_session():
            payload, elapsed = self._partner_query(
                "areEntitiesInLibrary",
                LIBRARY_CONTAINS_HASH,
                {"uris": normalized},
            )
            lookup = (payload.get("data") or {}).get("lookup") or []
            contains = {
                uri: bool(((wrapper.get("data") or {}).get("saved")))
                for uri, wrapper in zip(normalized, lookup)
            }
            return {
                "operation": "spotify.library_contains",
                "contains": contains,
                "control_path": "retained_spotify_web_player_session",
                "elapsed_ms": round(elapsed, 1),
            }
        payload, _status, _headers, elapsed = self._api("GET", "/me/library/contains", params={"uris": ",".join(normalized)})
        if isinstance(payload, list):
            contains = dict(zip(normalized, payload))
        else:
            contains = payload
        return {"operation": "spotify.library_contains", "contains": contains, "elapsed_ms": round(elapsed, 1)}

    def playlists(self, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        self._page_params(limit, offset)
        if not self._has_oauth_tokens() and self._has_web_session():
            page, elapsed = self._web_library_page(
                limit=limit, offset=offset + 1, filters=["Playlists"]
            )
            items = []
            for row in page.get("items") or []:
                data = ((row.get("item") or {}).get("data") or {})
                compact = self._web_compact_entity(data)
                if compact and compact.get("type") == "playlist":
                    items.append(compact)
            return {
                "operation": "spotify.playlists",
                "count": len(items),
                "playlists": items,
                "total": max(int(page.get("totalCount") or 0) - 1, 0),
                "control_path": "retained_spotify_web_player_session",
                "elapsed_ms": round(elapsed, 1),
            }
        payload, _status, _headers, elapsed = self._api("GET", "/me/playlists", params=self._page_params(limit, offset))
        items = [self._compact_item(item) for item in payload.get("items") or []]
        return {"operation": "spotify.playlists", "count": len(items), "playlists": items, "total": payload.get("total"), "elapsed_ms": round(elapsed, 1)}

    def playlist(
        self,
        playlist_id: str,
        market: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        page = self._page_params(limit, offset)
        normalized_id = self._id(playlist_id)
        if not self._has_oauth_tokens() and self._has_web_session():
            payload, elapsed = self._web_fetch_playlist(
                normalized_id, limit=limit, offset=offset
            )
            content = payload.get("content") or {}
            summary = self._web_compact_entity(payload) or {
                "id": normalized_id,
                "uri": f"spotify:playlist:{normalized_id}",
                "type": "playlist",
                "name": payload.get("name"),
            }
            items = []
            for row in content.get("items") or []:
                data = ((row.get("itemV2") or {}).get("data") or {})
                compact = self._web_compact_entity(data)
                if compact:
                    added_at = row.get("addedAt")
                    if isinstance(added_at, dict):
                        added_at = added_at.get("isoString")
                    compact["added_at"] = added_at
                    items.append(compact)
            total = int(content.get("totalCount") or len(items))
            next_offset = offset + len(items) if offset + len(items) < total else None
            return {
                "operation": "spotify.playlist",
                "playlist": summary,
                "count": len(items),
                "items": items,
                "total": total,
                "page": {
                    "limit": limit,
                    "offset": offset,
                    "next_offset": next_offset,
                    "has_more": next_offset is not None,
                },
                "control_path": "retained_web_session_replay",
                "elapsed_ms": round(elapsed, 1),
            }
        summary_params: dict[str, Any] = {
            "fields": "id,uri,name,description,public,collaborative,owner,images,external_urls,followers,tracks.total"
        }
        if market:
            summary_params["market"] = market
        summary_payload, _status, _headers, summary_elapsed = self._api(
            "GET", f"/playlists/{normalized_id}", params=summary_params
        )
        item_params: dict[str, Any] = dict(page)
        if market:
            item_params["market"] = market
        item_payload, _status, _headers, item_elapsed = self._api(
            "GET", f"/playlists/{normalized_id}/items", params=item_params
        )
        summary = self._compact_item(summary_payload) or {
            "id": normalized_id,
            "uri": f"spotify:playlist:{normalized_id}",
            "type": "playlist",
        }
        summary.update(
            {
                "public": summary_payload.get("public"),
                "collaborative": summary_payload.get("collaborative"),
                "followers": (summary_payload.get("followers") or {}).get("total"),
            }
        )
        items = [
            compact
            for row in item_payload.get("items") or []
            if (
                compact := self._compact_item(
                    row.get("track") or row.get("item") or row
                )
            )
        ]
        total = int(
            item_payload.get("total")
            or ((summary_payload.get("tracks") or {}).get("total"))
            or len(items)
        )
        next_offset = offset + len(items) if offset + len(items) < total else None
        return {
            "operation": "spotify.playlist",
            "playlist": summary,
            "count": len(items),
            "items": items,
            "total": total,
            "page": {
                "limit": limit,
                "offset": offset,
                "next_offset": next_offset,
                "has_more": next_offset is not None,
            },
            "elapsed_ms": round(summary_elapsed + item_elapsed, 1),
        }

    def create_playlist(
        self,
        name: str,
        public: bool = True,
        collaborative: bool = False,
        description: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        self._confirm(confirm, "spotify.create_playlist")
        if not name.strip() or len(name) > 100:
            raise AgentWebError("name must contain between 1 and 100 characters")
        if collaborative and public:
            raise AgentWebError("collaborative playlists must be private")
        body: dict[str, Any] = {"name": name, "public": public, "collaborative": collaborative}
        if description is not None:
            body["description"] = description
        if not self._has_oauth_tokens() and self._has_web_session():
            playlist, elapsed = self._web_create_playlist(name)
            playlist_id = str(playlist.get("id") or "")
            update_elapsed = 0.0
            permission: dict[str, Any] = {
                "permission_level": "VIEWER",
                "public": True,
                "collaborative": False,
                "verified": True,
            }
            try:
                metadata = {
                    key: value
                    for key, value in {
                        "description": description,
                        "collaborative": collaborative if collaborative else None,
                    }.items()
                    if value is not None
                }
                if metadata:
                    _payload, metadata_elapsed = self._web_update_playlist(
                        playlist_id, metadata
                    )
                    update_elapsed += metadata_elapsed
                permission_target = False if not public and not collaborative else None
                permission, permission_elapsed = self._web_playlist_permission(
                    playlist_id, permission_target
                )
                update_elapsed += permission_elapsed
                fetched, verify_elapsed = self._web_fetch_playlist(
                    playlist_id, limit=1
                )
                update_elapsed += verify_elapsed
                verified = (
                    permission.get("verified") is True
                    and (description is None or fetched.get("description") == description)
                    and (
                        not collaborative
                        or permission.get("permission_level") == "CONTRIBUTOR"
                    )
                )
            except Exception as exc:
                cleanup_verified = False
                try:
                    cleanup = self.delete_playlist(playlist_id, confirm=True)
                    cleanup_verified = bool(cleanup.get("verified"))
                except Exception:
                    pass
                raise AgentWebError(
                    "Spotify created the playlist but its requested metadata update failed; AgentWeb attempted to remove the partial playlist.",
                    code="spotify_playlist_create_incomplete",
                    retryable=False,
                    details={
                        "partial_playlist_removed": cleanup_verified,
                        "cause": str(exc),
                    },
                ) from exc
            return {
                "operation": "spotify.create_playlist",
                "created": True,
                "status": 200,
                "playlist": playlist,
                "public": bool(permission.get("public")),
                "collaborative": bool(permission.get("collaborative")),
                "description": description,
                "control_path": "retained_web_session_replay",
                "verified": verified,
                "elapsed_ms": round(elapsed + update_elapsed, 1),
            }
        account, _account_status, _account_headers, account_elapsed = self._api(
            "GET", "/me"
        )
        account_id = account.get("id")
        if not account_id:
            raise AgentWebError("Spotify account response did not contain a user ID")
        payload, status, _headers, elapsed = self._api(
            "POST", f"/users/{account_id}/playlists", body=body
        )
        return {
            "operation": "spotify.create_playlist",
            "created": True,
            "status": status,
            "playlist": self._compact_item(payload),
            "elapsed_ms": round(account_elapsed + elapsed, 1),
        }

    def delete_playlist(
        self, playlist_id: str, confirm: bool = False
    ) -> dict[str, Any]:
        self._confirm(confirm, "spotify.delete_playlist")
        identity = self._id(playlist_id)
        uri = f"spotify:playlist:{identity}"
        if not self._has_oauth_tokens() and self._has_web_session():
            account, account_elapsed = self._web_account()
            username = str(account.get("id") or "")
            if not username:
                raise AgentWebError("Spotify account did not provide a rootlist owner")
            response = self._session_request(
                "POST",
                f"{PLAYLIST_SERVICE_URL}/user/{username}/rootlist/changes",
                json_body={
                    "deltas": [
                        {
                            "info": {"source": {"client": "WEBPLAYER"}},
                            "ops": [
                                {
                                    "kind": "REM",
                                    "rem": {
                                        "items": [{"uri": uri}],
                                        "itemsAsKey": True,
                                    },
                                }
                            ],
                        }
                    ]
                },
                headers=self._web_headers(),
            )
            payload = self._json_response(
                response, "Spotify Web Player playlist deletion"
            )
            contains = self.library_contains([uri])
            verified = not bool((contains.get("contains") or {}).get(uri))
            return {
                "operation": "spotify.delete_playlist",
                "deleted": True,
                "playlist_id": identity,
                "revision": payload.get("revision"),
                "verified": verified,
                "control_path": "retained_web_session_replay",
                "elapsed_ms": round(
                    account_elapsed
                    + response.elapsed_ms
                    + float(contains.get("elapsed_ms") or 0),
                    1,
                ),
            }
        _payload, status, _headers, elapsed = self._api(
            "DELETE", f"/playlists/{identity}/followers"
        )
        return {
            "operation": "spotify.delete_playlist",
            "deleted": True,
            "playlist_id": identity,
            "status": status,
            "elapsed_ms": round(elapsed, 1),
        }

    def update_playlist(
        self,
        playlist_id: str,
        name: str | None = None,
        public: bool | None = None,
        collaborative: bool | None = None,
        description: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        self._confirm(confirm, "spotify.update_playlist")
        if collaborative is True and public is True:
            raise AgentWebError("collaborative playlists must be private")
        body = {key: value for key, value in {"name": name, "public": public, "collaborative": collaborative, "description": description}.items() if value is not None}
        if not body:
            raise AgentWebError("provide at least one playlist field to update")
        if not self._has_oauth_tokens() and self._has_web_session():
            metadata = {key: value for key, value in body.items() if key != "public"}
            payload: dict[str, Any] = {}
            elapsed = 0.0
            if metadata:
                payload, elapsed = self._web_update_playlist(
                    playlist_id, metadata
                )
            permission, permission_elapsed = self._web_playlist_permission(
                playlist_id, public
            )
            playlist, verify_elapsed = self._web_fetch_playlist(playlist_id, limit=1)
            metadata_verified = all(
                playlist.get(key) == value
                for key, value in metadata.items()
                if key != "collaborative"
            )
            collaborative_verified = (
                collaborative is None
                or bool(permission.get("collaborative")) is collaborative
            )
            public_verified = (
                public is None or bool(permission.get("public")) is public
            )
            verified = metadata_verified and collaborative_verified and public_verified
            return {
                "operation": "spotify.update_playlist",
                "updated": True,
                "status": 200,
                "playlist_id": self._id(playlist_id),
                "fields": sorted(body),
                "revision": payload.get("revision"),
                "verified": verified,
                "public": permission.get("public"),
                "collaborative": permission.get("collaborative"),
                "permission_level": permission.get("permission_level"),
                "control_path": "retained_web_session_replay",
                "elapsed_ms": round(
                    elapsed + permission_elapsed + verify_elapsed, 1
                ),
            }
        _payload, status, _headers, elapsed = self._api("PUT", f"/playlists/{self._id(playlist_id)}", body=body)
        return {"operation": "spotify.update_playlist", "updated": True, "status": status, "playlist_id": self._id(playlist_id), "fields": sorted(body), "elapsed_ms": round(elapsed, 1)}

    @staticmethod
    def _playlist_uris(uris: list[str], *, allow_empty: bool = False) -> list[str]:
        if (not uris and not allow_empty) or len(uris) > 100:
            minimum = 0 if allow_empty else 1
            raise AgentWebError(
                f"uris must contain between {minimum} and 100 Spotify URIs"
            )
        return [Adapter._uri(uri) for uri in uris]

    def add_playlist_items(self, playlist_id: str, uris: list[str], position: int | None = None, confirm: bool = False) -> dict[str, Any]:
        self._confirm(confirm, "spotify.add_playlist_items")
        body: dict[str, Any] = {"uris": self._playlist_uris(uris)}
        if position is not None:
            if position < 0:
                raise AgentWebError("position cannot be negative")
            body["position"] = position
        if not self._has_oauth_tokens() and self._has_web_session():
            if position is not None:
                raise AgentWebError(
                    "Positioned insertion has not yet been verified against Spotify's Web Player protocol; omit position to append safely."
                )
            payload, elapsed = self._web_add_playlist_items(
                playlist_id, body["uris"], position
            )
            playlist, verify_elapsed = self._web_fetch_playlist(playlist_id)
            content = playlist.get("content") or {}
            items = content.get("items") or []
            observed_uris = {
                str(
                    ((item.get("itemV2") or {}).get("data") or {}).get("uri")
                    or (item.get("item") or {}).get("uri")
                    or item.get("uri")
                    or ""
                )
                for item in items
            }
            verified = all(uri in observed_uris for uri in body["uris"])
            return {
                "operation": "spotify.add_playlist_items",
                "changed": True,
                "status": 200,
                "snapshot_id": (
                    ((payload.get("data") or {}).get("addToPlaylist") or {}).get(
                        "newRevision"
                    )
                ),
                "control_path": "retained_web_session_replay",
                "verified": verified,
                "added_uris": body["uris"],
                "elapsed_ms": round(elapsed + verify_elapsed, 1),
            }
        payload, status, _headers, elapsed = self._api("POST", f"/playlists/{self._id(playlist_id)}/items", body=body)
        return {"operation": "spotify.add_playlist_items", "changed": True, "status": status, "snapshot_id": payload.get("snapshot_id"), "elapsed_ms": round(elapsed, 1)}

    def remove_playlist_items(self, playlist_id: str, uris: list[str], snapshot_id: str | None = None, confirm: bool = False) -> dict[str, Any]:
        self._confirm(confirm, "spotify.remove_playlist_items")
        normalized = self._playlist_uris(uris)
        body: dict[str, Any] = {"items": [{"uri": uri} for uri in normalized]}
        if snapshot_id:
            body["snapshot_id"] = snapshot_id
        if not self._has_oauth_tokens() and self._has_web_session():
            if snapshot_id:
                raise AgentWebError(
                    "snapshot_id belongs to Spotify's official Web API and is not used by the retained Web Player replay"
                )
            payload, elapsed = self._web_remove_playlist_items(
                playlist_id, normalized
            )
            playlist, verify_elapsed = self._web_fetch_playlist(playlist_id)
            remaining = {
                str(((row.get("itemV2") or {}).get("data") or {}).get("uri") or "")
                for row in ((playlist.get("content") or {}).get("items") or [])
            }
            return {
                "operation": "spotify.remove_playlist_items",
                "changed": True,
                "status": 200,
                "snapshot_id": None,
                "verified": not any(uri in remaining for uri in normalized),
                "removed_uris": normalized,
                "control_path": "retained_web_session_replay",
                "elapsed_ms": round(elapsed + verify_elapsed, 1),
                "website_response": bool(
                    ((payload.get("data") or {}).get("removeItemsFromPlaylist"))
                ),
            }
        payload, status, _headers, elapsed = self._api("DELETE", f"/playlists/{self._id(playlist_id)}/items", body=body)
        return {"operation": "spotify.remove_playlist_items", "changed": True, "status": status, "snapshot_id": payload.get("snapshot_id"), "elapsed_ms": round(elapsed, 1)}

    def reorder_playlist_items(self, playlist_id: str, range_start: int, insert_before: int, range_length: int = 1, snapshot_id: str | None = None, confirm: bool = False) -> dict[str, Any]:
        self._confirm(confirm, "spotify.reorder_playlist_items")
        if min(range_start, insert_before) < 0 or range_length < 1:
            raise AgentWebError("playlist positions must be non-negative and range_length positive")
        body: dict[str, Any] = {"range_start": range_start, "insert_before": insert_before, "range_length": range_length}
        if snapshot_id:
            body["snapshot_id"] = snapshot_id
        if not self._has_oauth_tokens() and self._has_web_session():
            if snapshot_id:
                raise AgentWebError(
                    "snapshot_id belongs to Spotify's official Web API and is not used by the retained Web Player replay"
                )
            playlist, fetch_elapsed = self._web_fetch_playlist(playlist_id)
            current = [
                str(((row.get("itemV2") or {}).get("data") or {}).get("uri") or "")
                for row in ((playlist.get("content") or {}).get("items") or [])
            ]
            current = [uri for uri in current if uri]
            if range_start + range_length > len(current) or insert_before > len(current):
                raise AgentWebError("playlist reorder range is outside the current playlist")
            segment = current[range_start : range_start + range_length]
            remaining = current[:range_start] + current[range_start + range_length :]
            target = insert_before
            if insert_before > range_start:
                target -= range_length
            reordered = remaining[:target] + segment + remaining[target:]
            verified, replace_elapsed = self._web_replace_playlist_items(
                playlist_id, reordered
            )
            return {
                "operation": "spotify.reorder_playlist_items",
                "changed": current != reordered,
                "status": 200,
                "snapshot_id": None,
                "verified": verified,
                "items": reordered,
                "control_path": "retained_web_session_replay",
                "elapsed_ms": round(fetch_elapsed + replace_elapsed, 1),
            }
        payload, status, _headers, elapsed = self._api("PUT", f"/playlists/{self._id(playlist_id)}/items", body=body)
        return {"operation": "spotify.reorder_playlist_items", "changed": True, "status": status, "snapshot_id": payload.get("snapshot_id"), "elapsed_ms": round(elapsed, 1)}

    def replace_playlist_items(self, playlist_id: str, uris: list[str], confirm: bool = False) -> dict[str, Any]:
        self._confirm(confirm, "spotify.replace_playlist_items")
        normalized = self._playlist_uris(uris, allow_empty=True)
        if not self._has_oauth_tokens() and self._has_web_session():
            verified, elapsed = self._web_replace_playlist_items(
                playlist_id, normalized
            )
            return {
                "operation": "spotify.replace_playlist_items",
                "changed": True,
                "status": 200,
                "snapshot_id": None,
                "verified": verified,
                "items": normalized,
                "control_path": "retained_web_session_replay",
                "elapsed_ms": round(elapsed, 1),
            }
        payload, status, _headers, elapsed = self._api("PUT", f"/playlists/{self._id(playlist_id)}/items", body={"uris": normalized})
        return {"operation": "spotify.replace_playlist_items", "changed": True, "status": status, "snapshot_id": (payload or {}).get("snapshot_id"), "elapsed_ms": round(elapsed, 1)}

    def api_request(
        self,
        path: str,
        method: str = "GET",
        query: list[str] | None = None,
        body: dict[str, Any] | None = None,
        confirm: bool = False,
        max_items: int = 100,
        max_string: int = 10000,
        max_total_chars: int = 50000,
    ) -> dict[str, Any]:
        if not path.startswith("/") or "://" in path or ".." in path.split("/"):
            raise AgentWebError("path must be a safe Spotify API path beginning with /")
        method = method.upper()
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise AgentWebError("method is not supported")
        mutating = method not in {"GET", "HEAD"}
        if mutating:
            self._confirm(confirm, f"spotify.api_request {method}")
        if max_items < 1 or max_items > 500 or max_string < 100 or max_string > 50000:
            raise AgentWebError("max_items or max_string is out of range")
        if max_total_chars < 1000 or max_total_chars > 200000:
            raise AgentWebError("max_total_chars must be between 1000 and 200000")
        params = parse_query_pairs(query)
        payload, status, headers, elapsed = self._api(method, path, params=params, body=body)
        safe, truncated = bounded_data(payload, max_items=max_items, max_string=max_string)
        safe, total_truncated, original_chars = enforce_data_budget(safe, max_total_chars=max_total_chars)
        retry_after = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
        return {
            "operation": "spotify.api_request",
            "method": method,
            "path": path,
            "query": params,
            "state_changed": mutating,
            "status": status,
            "data": safe,
            "truncated": truncated or total_truncated,
            "original_chars": original_chars,
            "retry_after_seconds": retry_after,
            "elapsed_ms": round(elapsed, 1),
        }
