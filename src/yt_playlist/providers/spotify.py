"""Transient, read-only Spotify boundary for playlist import (#22).

Spotify objects deliberately stop here: callers receive small neutral DTOs and must not persist API
payloads into TuneConsole's library, history, recommendation, or analytics tables.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import requests

ACCOUNTS_URL = "https://accounts.spotify.com"
API_URL = "https://api.spotify.com/v1"
IMPORT_SCOPES = ("playlist-read-private", "playlist-read-collaborative")


@dataclass(frozen=True)
class ImportTrack:
    external_id: str
    title: str
    artist: str
    album: str | None
    duration_s: int | None
    isrc: str | None = None


@dataclass(frozen=True)
class ImportPlaylist:
    external_id: str
    name: str
    track_count: int
    owner: str | None = None


class SpotifyError(RuntimeError):
    def __init__(self, status: int, message: str, *, reason=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.retry_after = retry_after


def new_pkce() -> tuple[str, str]:
    """Return (verifier, S256 challenge) for one authorization attempt."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorization_url(client_id, redirect_uri, state, challenge) -> str:
    params = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
              "scope": " ".join(IMPORT_SCOPES), "state": state,
              "code_challenge_method": "S256", "code_challenge": challenge}
    return f"{ACCOUNTS_URL}/authorize?{urlencode(params)}"


def exchange_code(session, client_id, redirect_uri, code, verifier) -> dict:
    response = session.post(f"{ACCOUNTS_URL}/api/token", data={
        "client_id": client_id, "grant_type": "authorization_code", "code": code,
        "redirect_uri": redirect_uri, "code_verifier": verifier,
    }, timeout=20)
    return _response_json(response)


def refresh_access_token(session, client_id, refresh_token) -> dict:
    response = session.post(f"{ACCOUNTS_URL}/api/token", data={
        "client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token,
    }, timeout=20)
    return _response_json(response)


def _response_json(response) -> dict:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        payload = {}
    if response.status_code >= 400:
        error = payload.get("error", {})
        if not isinstance(error, dict):
            error = {"message": str(error)}
        retry = response.headers.get("Retry-After")
        raise SpotifyError(response.status_code, error.get("message") or "Spotify request failed",
                           reason=error.get("reason") or payload.get("reason"),
                           retry_after=float(retry) if retry and retry.isdigit() else None)
    return payload


class SpotifyImportClient:
    """Minimal Spotify API reader. No cache and no Store dependency by design."""

    def __init__(self, access_token, session=None):
        self.session = session or requests.Session()
        self.access_token = access_token

    def _get(self, path_or_url, params=None):
        url = path_or_url if path_or_url.startswith("https://") else API_URL + path_or_url
        response = self.session.get(url, params=params,
                                    headers={"Authorization": f"Bearer {self.access_token}"},
                                    timeout=20)
        return _response_json(response)

    def _pages(self, path, params=None):
        page = self._get(path, params=params)
        while True:
            yield from page.get("items") or []
            nxt = page.get("next")
            if not nxt:
                return
            page = self._get(nxt)

    def profile(self) -> dict:
        data = self._get("/me")
        return {"id": data.get("account_id") or data.get("id"),
                "name": data.get("display_name") or data.get("id") or "Spotify"}

    def playlists(self) -> list[ImportPlaylist]:
        out = []
        for row in self._pages("/me/playlists", {"limit": 50}):
            if not row.get("id"):
                continue
            total = ((row.get("items") or row.get("tracks") or {}).get("total") or 0)
            out.append(ImportPlaylist(row["id"], row.get("name") or "Untitled playlist", int(total),
                                      (row.get("owner") or {}).get("display_name")))
        return out

    def playlist_tracks(self, playlist_id) -> list[ImportTrack]:
        out = []
        for row in self._pages(f"/playlists/{playlist_id}/items", {"limit": 50}):
            track = row.get("item") or row.get("track") or {}
            # Local files, podcasts, removed tracks, and unavailable placeholders have no catalog id.
            if track.get("type", "track") != "track" or not track.get("id"):
                continue
            artists = ", ".join(a.get("name", "") for a in track.get("artists") or [] if a.get("name"))
            out.append(ImportTrack(
                track["id"], track.get("name") or "", artists,
                (track.get("album") or {}).get("name"),
                round(track["duration_ms"] / 1000) if track.get("duration_ms") is not None else None,
                (track.get("external_ids") or {}).get("isrc")))
        return out
