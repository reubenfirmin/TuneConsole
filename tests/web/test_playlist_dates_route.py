import json
import re
from urllib.parse import parse_qs, urlsplit

from starlette.testclient import TestClient

from yt_playlist.web.app import create_app
from yt_playlist.providers.playlist_dates import PlaylistDates, SCOPE
from tests.conftest import FakeClient
from tests.test_playlist_dates import CLIENT, NOW, Response, Session


def test_google_callback_backfills_new_and_old_without_marking_generated_new(store, tmp_path):
    iid = store.upsert_identity("main", "bridge", None, True)
    for ytm, title in (("PLnew", "Made in YouTube"), ("PLold", "Brexit List"),
                       ("PLgen", "Generated"), ("PLunknown", "Unknown")):
        store.upsert_playlist(iid, ytm, title, 0, "", NOW)
    store.set_playlist_group("PLgen", "Generated")
    session = Session([
        Response({"access_token": "access-secret", "refresh_token": "refresh-secret", "scope": SCOPE}),
        Response({"items": [{"id": "UCme", "snippet": {"title": "My channel"}}]}),
        Response({"items": [
            {"id": "PLnew", "snippet": {"publishedAt": "2026-09-19T12:00:00Z"}},
            {"id": "PLgen", "snippet": {"publishedAt": "2026-09-19T12:00:00Z"}},
            {"id": "PLold", "snippet": {"publishedAt": "2016-06-23T00:00:00Z"}},
        ]}),
    ])
    provider = PlaylistDates(tmp_path, session=session, now_fn=lambda: NOW)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: NOW, playlist_dates=provider)
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    r = c.post("/setup/playlist-dates/connect", files={"file": ("client.json", json.dumps(CLIENT))},
               follow_redirects=False)
    assert r.status_code == 303
    assert "httponly" in r.headers["set-cookie"].lower()
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    r = c.get("/setup/playlist-dates/callback", params={"state": state, "code": "code-secret"})
    assert r.status_code == 200
    assert "Updated creation dates for 3 playlists" in r.text
    assert "My channel" in r.text
    assert not any(secret in r.text for secret in ("access-secret", "refresh-secret", "code-secret"))
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    html = c.get("/playlists").text
    rows = json.loads(re.search(r"playlistsTab\((\[.*?\])\)", html).group(1))
    assert {row["ytm"] for row in rows if row["is_new"]} == {"PLnew"}
    # Local disconnect removes tokens, preserving dates that power New.
    assert c.post("/setup/playlist-dates/disconnect", data={"account": "UCme"}).status_code == 200
    assert provider.status()["accounts"] == []
    assert "refresh-secret" not in provider.path.read_text()
    assert store.get_playlists()[0].created_at == NOW - 43200


def test_callback_cannot_exchange_without_browser_cookie_and_valid_state(store, tmp_path):
    provider = PlaylistDates(tmp_path, session=Session([]), now_fn=lambda: NOW)
    c = TestClient(create_app(store, lambda: {}, playlist_dates=provider), base_url="http://127.0.0.1:8765")
    r = c.get("/setup/playlist-dates/callback?state=forged&code=secret")
    assert r.status_code == 200
    assert "request expired" in r.text
    assert provider.session.calls == []
    assert not provider.path.exists()


def test_connection_rejects_cross_origin_and_remote_redirect_hosts(store, tmp_path):
    provider = PlaylistDates(tmp_path, session=Session([]))
    c = TestClient(create_app(store, lambda: {}, playlist_dates=provider), base_url="http://127.0.0.1:8765")
    assert c.post("/setup/playlist-dates/connect", headers={"Origin": "https://evil.test"}).status_code == 403
    assert c.post("/setup/playlist-dates/connect", headers={"Host": "evil.test"}).status_code == 400
    assert provider.pending == {}
