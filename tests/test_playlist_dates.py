"""Real Data API shapes, OAuth boundaries, and creation dates through to New."""
import hashlib
import base64
from datetime import datetime, timezone
import json
import stat
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from yt_playlist.providers.playlist_dates import PlaylistDates, PlaylistDatesError, SCOPE, published_at
from yt_playlist.library.sync import sync_all, sync_playlist_dates
from tests.conftest import FakeClient

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc).timestamp()
CLIENT = {"installed": {"client_id": "123-test.apps.googleusercontent.com", "client_secret": "client-secret"}}


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


class Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url, **kwargs):
        return self.request("POST", url, kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, kwargs)


def authorized(tmp_path, extra=()):
    session = Session([
        Response({"access_token": "access-secret", "refresh_token": "refresh-secret",
                  "expires_in": 3600, "scope": SCOPE}),
        Response({"items": [{"id": "UCme", "snippet": {"title": "My channel"}}]}),
        *extra,
    ])
    provider = PlaylistDates(tmp_path, session=session, now_fn=lambda: NOW)
    url = provider.begin(json.dumps(CLIENT), "http://127.0.0.1:8765/setup/playlist-dates/callback", "browser")
    state = parse_qs(urlsplit(url).query)["state"][0]
    provider.finish(state, "auth-code", "browser")
    return provider, session


def test_authorization_uses_pkce_readonly_and_private_storage(tmp_path):
    provider, session = authorized(tmp_path)
    assert stat.S_IMODE(provider.path.stat().st_mode) == 0o600
    assert provider.status() == {"configured": True, "accounts": [
        {"id": "UCme", "title": "My channel", "error": ""}]}
    assert "secret" not in json.dumps(provider.status())
    assert session.calls[0][1] == "https://oauth2.googleapis.com/token"
    assert len(session.calls[0][2]["data"]["code_verifier"]) >= 43
    assert all(c[2]["allow_redirects"] is False for c in session.calls)


def test_state_is_browser_bound_expiring_and_single_use(tmp_path):
    clock = [NOW]
    p = PlaylistDates(tmp_path, now_fn=lambda: clock[0], session=Session([]))
    url = p.begin(json.dumps(CLIENT), "http://127.0.0.1:8765/setup/playlist-dates/callback", "browser")
    q = parse_qs(urlsplit(url).query)
    assert q["scope"] == [SCOPE]
    assert q["code_challenge_method"] == ["S256"]
    state = q["state"][0]
    expected = base64.urlsafe_b64encode(hashlib.sha256(p.pending[state]["verifier"].encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == [expected]
    with pytest.raises(PlaylistDatesError):
        p.finish(state, "code", "another-browser")
    with pytest.raises(PlaylistDatesError, match="not granted"):
        p.finish(state, "", "browser")
    with pytest.raises(PlaylistDatesError, match="expired"):
        p.finish(state, "code", "browser")
    url = p.begin(json.dumps(CLIENT), "http://127.0.0.1:8765/setup/playlist-dates/callback", "browser")
    clock[0] += 601
    with pytest.raises(PlaylistDatesError, match="expired"):
        p.finish(parse_qs(urlsplit(url).query)["state"][0], "code", "browser")
    assert not p.path.exists()


@pytest.mark.parametrize("raw", ["no json", "{}", '{"web": {}}', '{"installed": null}',
                                  '{"installed": {"client_id": "https://evil.test", "client_secret": "x"}}'])
def test_invalid_client_config_does_not_write_or_make_requests(tmp_path, raw):
    p = PlaylistDates(tmp_path, session=Session([]))
    with pytest.raises(PlaylistDatesError, match="Desktop app"):
        p.begin(raw, "http://localhost:8765/callback", "browser")
    assert not p.path.exists()


@pytest.mark.parametrize("value", [None, "2026", "2026-09-19", "2026-09-19T12:00:00",
                                    "garbage", "2027-01-01T00:00:00Z"])
def test_unknown_partial_and_future_dates_stay_unknown(value):
    assert published_at(value, NOW) is None


def test_pagination_backfills_actual_dates_across_identities_without_resetting_sync_times(store, tmp_path):
    iid = store.upsert_identity("main", "bridge", None, True)
    other = store.upsert_identity("other", "bridge", None, False)
    old = store.upsert_playlist(iid, "PLold", "303", 0, "h", NOW)
    recent = store.upsert_playlist(iid, "PLnew", "Work in progress", 0, "h", NOW, created_at=NOW)
    duplicate = store.upsert_playlist(other, "PLnew", "Work in progress", 0, "h", NOW)
    unknown = store.upsert_playlist(iid, "PLunknown", "Unknown", 0, "h", NOW)
    provider, session = authorized(tmp_path, [
        Response({"items": [{"id": "PLold", "snippet": {"publishedAt": "2010-01-01T00:00:00Z"}}],
                  "nextPageToken": "page2"}),
        Response({"items": [{"id": "PLnew", "snippet": {"publishedAt": "2026-09-19T12:00:00Z"}},
                            {"id": "PLnotInLibrary", "snippet": {"publishedAt": "2026-09-18T00:00:00Z"}}]}),
    ])
    assert provider.refresh(store) == {"updated": 3, "errors": []}
    assert session.calls[-1][2]["params"]["pageToken"] == "page2"
    assert store.get_playlist(old).created_at == 1_262_304_000.0
    for pid in (recent, duplicate):
        p = store.get_playlist(pid)
        assert p.created_at == NOW - 43200
        assert p.created_at_source == "youtube"
        assert p.first_seen == p.last_seen == p.last_changed == NOW
    assert store.get_playlist(unknown).created_at is None
    assert len(store.get_playlists()) == 4
    store.upsert_playlist(iid, "PLnew", "Edited later", 1, "new", NOW + 60, created_at=NOW + 60)
    assert store.get_playlist(recent).created_at == NOW - 43200
    assert store.get_playlist(recent).created_at_source == "youtube"


def test_refresh_token_and_failure_preserve_dates(store, tmp_path):
    iid = store.upsert_identity("main", "bridge", None, True)
    pid = store.upsert_playlist(iid, "PLx", "Saved", 0, "h", NOW, created_at=NOW - 100)
    provider, session = authorized(tmp_path)
    provider.now = lambda: NOW + 7200
    session.responses.extend([Response({"access_token": "renewed", "expires_in": 3600}),
                              requests.ConnectionError("secret should not be reported")])
    report = provider.refresh(store)
    assert report["updated"] == 0
    assert "secret" not in str(report)
    assert report["errors"]
    assert session.calls[-2][2]["data"]["grant_type"] == "refresh_token"
    assert session.calls[-1][2]["headers"] == {"Authorization": "Bearer renewed"}
    assert store.get_playlist(pid).created_at == NOW - 100
    provider.disconnect("UCme")
    assert provider.status()["accounts"] == []
    assert "refresh-secret" not in provider.path.read_text()
    assert store.get_playlist(pid).created_at == NOW - 100


def test_partial_pagination_does_not_commit_dates(store, tmp_path):
    iid = store.upsert_identity("main", "bridge", None, True)
    pid = store.upsert_playlist(iid, "PLx", "Saved", 0, "h", NOW)
    provider, _ = authorized(tmp_path, [
        Response({"items": [{"id": "PLx", "snippet": {"publishedAt": "2026-09-19T00:00:00Z"}}],
                  "nextPageToken": "next"}), Response({"error": "denied"}, 403)])
    assert provider.refresh(store)["errors"]
    assert store.get_playlist(pid).created_at is None


def test_normal_sync_refreshes_dates_after_discovering_playlists(store, tmp_path):
    iid = store.upsert_identity("main", "bridge", None, True)
    provider, _ = authorized(tmp_path, [Response({"items": [
        {"id": "PLx", "snippet": {"publishedAt": "2026-09-19T00:00:00Z"}}]})])
    client = FakeClient(playlists=[{"playlistId": "PLx", "title": "Made in YouTube"}])
    sync_all(store, {iid: client}, NOW, playlist_dates=provider)
    assert store.get_playlists()[0].created_at == NOW - 86400


def test_disabled_connection_makes_no_requests(store, tmp_path):
    session = Session([])
    provider = PlaylistDates(tmp_path, session=session)
    assert sync_playlist_dates(store, provider) == {"updated": 0, "errors": []}
    assert session.calls == []
    assert not provider.path.exists()


def test_unexpected_401_refreshes_once_and_retries(store, tmp_path):
    provider, session = authorized(tmp_path, [Response({}, 401),
        Response({"access_token": "renewed", "expires_in": 3600}), Response({"items": []})])
    assert provider.refresh(store) == {"updated": 0, "errors": []}
    assert session.calls[-1][2]["headers"] == {"Authorization": "Bearer renewed"}


def test_one_expired_account_does_not_block_another(store, tmp_path):
    iid = store.upsert_identity("main", "bridge", None, True)
    pid = store.upsert_playlist(iid, "PLx", "Recent", 0, "h", NOW)
    provider, session = authorized(tmp_path)
    data = provider._load()
    data["accounts"]["UCother"] = {**data["accounts"]["UCme"], "title": "Other"}
    provider._save(data)
    session.responses.extend([Response({}, 401), Response({}, 400), Response({"items": [
        {"id": "PLx", "snippet": {"publishedAt": "2026-09-19T00:00:00Z"}}]})])
    report = provider.refresh(store)
    assert len(report["errors"]) == 1 and report["updated"] == 1
    assert store.get_playlist(pid).created_at == NOW - 86400
