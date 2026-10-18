"""Read playlist creation timestamps from YouTube Data API v3.

Music's InnerTube playlist response has no creation timestamp. The Data API's
playlist.snippet.publishedAt does; never substitute video dates or sync times.
This optional connection uses youtube.readonly and keeps its tokens in a private
local file, separate from the browser bridge and from the library database.
"""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from urllib.parse import urlencode

import requests

SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_URL = "https://www.googleapis.com/youtube/v3/"


class PlaylistDatesError(ValueError):
    """A safe, user-facing error; never includes a token or raw Google response."""


class _ExpiredAccessToken(PlaylistDatesError):
    pass


def published_at(value, now):
    """Accept only a complete, timezone-qualified creation time."""
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            return None
        stamp = date.timestamp()
        return stamp if 0 < stamp <= now else None
    except (ValueError, OverflowError, OSError):
        return None


class PlaylistDates:
    def __init__(self, directory, *, now_fn=time.time, session=None):
        self.path = Path(directory) / "youtube-playlist-dates.json"
        self.now = now_fn
        self.session = session or requests.Session()
        self.lock = threading.RLock()
        self.pending = {}

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"accounts": {}}

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".playlist-dates-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as out:  # mkstemp creates mode 0600
                json.dump(data, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def status(self):
        with self.lock:
            data = self._load()
            return {"configured": bool(data.get("client")), "accounts": [
                {"id": key, "title": a["title"], "error": a.get("error", "")}
                for key, a in data["accounts"].items()]}

    def begin(self, raw, redirect_uri, browser_nonce):
        with self.lock:
            data = self._load()
            if raw:
                try:
                    client = json.loads(raw)["installed"]
                    cid, secret = client["client_id"], client["client_secret"]
                    if not re.fullmatch(r"[\w.-]+\.apps\.googleusercontent\.com", cid):
                        raise ValueError
                    if not isinstance(secret, str) or not secret.strip():
                        raise ValueError
                except (ValueError, TypeError, KeyError):
                    raise PlaylistDatesError("Choose the Google OAuth client JSON for a Desktop app.") from None
                client = {"client_id": cid, "client_secret": secret}
            else:
                client = data.get("client")
                if not client:
                    raise PlaylistDatesError("Choose your Google Desktop app client JSON first.")
            # Credentials are not persisted until authorization succeeds.
            self.pending = {s: p for s, p in self.pending.items() if p["expires"] > self.now()}
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
            self.pending[state] = {"client": client, "verifier": verifier,
                                   "redirect": redirect_uri, "browser": browser_nonce,
                                   "expires": self.now() + 600}
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
                "client_id": client["client_id"], "redirect_uri": redirect_uri,
                "response_type": "code", "scope": SCOPE, "access_type": "offline",
                "prompt": "consent select_account", "state": state,
                "code_challenge": challenge, "code_challenge_method": "S256"})

    def _token(self, fields):
        try:
            response = self.session.post(TOKEN_URL, data=fields, timeout=15, allow_redirects=False)
            if response.status_code != 200:
                raise PlaylistDatesError("Google authorization expired or was declined. Reconnect this account.")
            result = response.json()
            if not isinstance(result, dict) or not isinstance(result.get("access_token"), str):
                raise PlaylistDatesError("Google returned an unreadable authorization response. Please reconnect.")
            if not result["access_token"] or SCOPE not in str(result.get("scope", SCOPE)).split():
                raise PlaylistDatesError("Google did not grant read-only YouTube access. Please reconnect.")
            expires = float(result.get("expires_in", 3600))
            if not math.isfinite(expires) or expires <= 0:
                raise PlaylistDatesError("Google returned an invalid authorization lifetime. Please reconnect.")
            result["expires_in"] = expires
            return result
        except (requests.RequestException, ValueError, TypeError) as e:
            if isinstance(e, PlaylistDatesError):
                raise
            raise PlaylistDatesError("Could not reach Google authorization. Try again shortly.") from None

    def _get(self, resource, token, params):
        try:
            response = self.session.get(API_URL + resource, params=params,
                                        headers={"Authorization": "Bearer " + token},
                                        timeout=15, allow_redirects=False)
            if response.status_code == 401:
                raise _ExpiredAccessToken("Google authorization expired. Reconnect this account.")
            if response.status_code != 200:
                raise PlaylistDatesError(
                    "YouTube could not read playlist dates. Check that YouTube Data API v3 is enabled "
                    "in your Google project, or reconnect this account.")
            result = response.json()
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise PlaylistDatesError("YouTube returned an unreadable metadata response. Try again shortly.")
            return result
        except (requests.RequestException, ValueError) as e:
            if isinstance(e, PlaylistDatesError):
                raise
            raise PlaylistDatesError("Could not reach YouTube for playlist dates. Try again shortly.") from None

    def finish(self, state, code, browser_nonce):
        with self.lock:
            pending = self.pending.get(state)
            if (not pending or pending["expires"] <= self.now() or not browser_nonce
                    or not secrets.compare_digest(pending["browser"], browser_nonce)):
                raise PlaylistDatesError("This connection request expired. Start again from Playlist dates.")
            del self.pending[state]  # one use, including declined/failed exchanges
            if not code:
                raise PlaylistDatesError("Google access was not granted. Your library is unchanged.")
            client = pending["client"]
            token = self._token({**client, "grant_type": "authorization_code", "code": code,
                                 "redirect_uri": pending["redirect"], "code_verifier": pending["verifier"]})
            channels = self._get("channels", token["access_token"], {"part": "snippet", "mine": "true"})
            if not channels["items"]:
                raise PlaylistDatesError("That Google account has no YouTube channel. Choose your music account.")
            channel = channels["items"][0]
            if (not isinstance(channel, dict) or not isinstance(channel.get("id"), str)
                    or not isinstance(channel.get("snippet"), dict)
                    or not isinstance(channel["snippet"].get("title"), str)):
                raise PlaylistDatesError("YouTube returned unreadable account details. Please reconnect.")
            if not isinstance(token.get("refresh_token"), str) or not token["refresh_token"]:
                raise PlaylistDatesError("Google did not grant ongoing access. Please reconnect and allow access.")
            data = self._load()
            data["client"] = client
            data["accounts"][channel["id"]] = {
                "title": channel["snippet"]["title"], "client": client,
                "refresh_token": token["refresh_token"], "access_token": token["access_token"],
                "expires": self.now() + float(token.get("expires_in", 3600))}
            self._save(data)

    def disconnect(self, account_id):
        with self.lock:
            data = self._load()
            data["accounts"].pop(account_id, None)
            self._save(data)

    def _renew(self, account):
        token = self._token({**account["client"], "grant_type": "refresh_token",
                             "refresh_token": account["refresh_token"]})
        account.update(access_token=token["access_token"], expires=self.now() + token["expires_in"])
        if isinstance(token.get("refresh_token"), str) and token["refresh_token"]:
            account["refresh_token"] = token["refresh_token"]

    def _playlists(self, account, params):
        try:
            return self._get("playlists", account["access_token"], params)
        except _ExpiredAccessToken:
            self._renew(account)
            return self._get("playlists", account["access_token"], params)

    def refresh(self, store):
        """Fetch owned playlists with pagination; join strictly by YouTube ID.

        Each connected channel can supply its private playlists. An unavailable
        account never clears dates already learned from it. This never discovers
        or imports playlists into the library: normal Music sync owns that job.
        """
        with self.lock:
            data = self._load()
            count, errors = 0, []
            for account in data["accounts"].values():
                try:
                    if account["expires"] <= self.now() + 60:
                        self._renew(account)
                    dates, seen, page = {}, set(), None
                    while True:
                        params = {"part": "snippet", "mine": "true", "maxResults": 50,
                                  "fields": "nextPageToken,items(id,snippet(publishedAt))"}
                        if page:
                            params["pageToken"] = page
                        result = self._playlists(account, params)
                        for item in result["items"]:
                            if not isinstance(item, dict) or not isinstance(item.get("snippet"), dict):
                                continue
                            stamp = published_at(item.get("snippet", {}).get("publishedAt"), self.now())
                            if stamp is not None and isinstance(item.get("id"), str):
                                dates[item["id"]] = stamp
                        page = result.get("nextPageToken")
                        if not page:
                            break
                        if not isinstance(page, str) or page in seen:
                            raise PlaylistDatesError("YouTube repeated a page of playlist dates. Try again shortly.")
                        seen.add(page)
                    count += store.set_youtube_playlist_dates(dates)
                    account.pop("error", None)
                except PlaylistDatesError as e:
                    account["error"] = str(e)
                    errors.append(str(e))
            if data["accounts"]:
                self._save(data)
            return {"updated": count, "errors": errors}
