import pytest
from fastapi.testclient import TestClient
from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


@pytest.fixture
def store():
    s = Store(":memory:"); s.init_schema(); return s


def _client(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    fake = FakeClient()
    return fake, TestClient(create_app(store, lambda: {iid: fake}, now_fn=lambda: 1000.0),
                            base_url="http://127.0.0.1")


def _synced_thin(store):
    from tests.conftest import seed_catalog_track
    seed_catalog_track(store,'a|x','T','A',video_id='v1')
    store.conn.execute("UPDATE tracks SET first_enriched_at=1 WHERE identity_key='a|x'")
    store.conn.commit()
    store.settings.set_setting("last_sync_at", "999")   # makes sync.last_synced_ago truthy so home enters the synced branch


def test_home_shows_onboarding_when_thin(store):
    _synced_thin(store)
    _, c = _client(store)
    html = c.get("/").text
    assert "personal mixes and find your next favorites" in html
    assert 'hx-get="/home/onboard/start"' in html          # imported music is immediately playable
    assert "onboard-progress" not in html


def test_onboard_radio_fragment_ok(store):
    _synced_thin(store)
    _, c = _client(store)
    assert c.get("/home/onboard/radio").status_code == 200


def test_onboard_done_dismisses(store):
    _synced_thin(store)
    _, c = _client(store)
    assert c.post("/onboard/done").status_code in (200, 204)
    assert store.settings.get_setting("onboard_dismissed") == "1"


def test_onboarding_hides_taste_panel_and_mode_cards(store):
    _synced_thin(store)
    _, c = _client(store)
    html = c.get("/").text
    assert 'hx-get="/home/cards"' not in html               # mode-card row hidden during onboarding
    assert 'id="home-feed"' not in html                     # the normal feed partial is not included


def test_first_mix_polls_until_verified_songs_arrive(store):
    from yt_playlist.library.sync import sync_identity
    from tests.conftest import _track

    fake, c = _client(store)
    html = c.get("/").text
    assert 'hx-get="/home/onboard/radio"' in html
    assert "30 seconds" not in html
    empty = c.get("/home/onboard/start").text
    assert 'hx-trigger="every 3s"' in empty
    assert 'id="gen-onboard_library"' not in empty

    # Catalog observations from incomplete pages are not owned songs.
    store.tracks.observe_track_entry("partial", "Unverified song", "Artist", now=1000)
    assert 'id="gen-onboard_library"' not in c.get("/home/onboard/start").text

    iid = store.identities.get_identities()[0].id
    fake._playlists = [{"playlistId": "PL1", "title": "My songs", "count": 1}]
    fake._tracks = {"PL1": [_track("ready", "Verified song", "Artist")]}
    sync_identity(store, iid, fake, now=1000)
    assert store.settings.get_setting("last_sync_at") is None
    ready = c.get("/home/onboard/start").text
    assert 'id="gen-onboard_library"' in ready
    assert 'hx-trigger="every 3s"' not in ready
    assert "Verified song" in ready and "Unverified song" not in ready
    assert "Play mix" in ready
    assert "Something to listen to" in ready
    assert "while we complete setup for you." in ready
    assert c.post("/home/refresh-card/onboard_library").status_code == 200
    status = c.get("/home/onboard/status").json()
    assert status["songs"] == 1
    assert status["playlists"] == 1
    assert status["albums"] == 0
    assert len(status["covers"]) == 1
    assert "partial" not in status["covers"][0]


def test_first_mix_is_available_while_album_sync_is_blocked(store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from tests.conftest import _track
    from yt_playlist.library.sync import sync_all

    _, c = _client(store)
    iid = store.identities.get_identities()[0].id
    album_started, release_album = Event(), Event()

    class SlowAlbumClient(FakeClient):
        def get_library_albums(self, limit=None):
            return [{"browseId": "ALBUM", "title": "Slow album", "artists": [{"name": "Artist"}]}]

        def get_album(self, browseId):
            album_started.set()
            assert release_album.wait(timeout=10)
            return {"title": "Slow album", "tracks": [_track("album", "Album song", "Artist")]}

    fake = SlowAlbumClient(playlists=[{"playlistId": "PL1", "title": "Ready", "count": 1}],
                           tracks={"PL1": [_track("ready", "Ready before albums", "Artist")]})
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(sync_all, store, {iid: fake}, 1000)
        try:
            assert album_started.wait(timeout=5)
            response = c.get("/home/onboard/start")
            assert response.status_code == 200
            assert "Ready before albums" in response.text
            assert "Album song" not in response.text
            assert not pending.done()
            assert store.settings.get_setting("last_sync_at") is None
        finally:
            release_album.set()
            pending.result(timeout=5)


def test_import_details_remain_available_after_restart(store):
    import json

    _, c = _client(store)
    assert c.get("/sync/recent").json() == {"events": [], "outcome": None}
    store.settings.set_setting("library_sync_state", json.dumps({
        "status": "partial", "accounts": {"1": {"label": "Main", "resources": {
            "playlist_listing": {"status": "complete", "entries": [
                {"playlistId": "PL1", "title": "Alternative Mix"}]},
            "playlists": {"status": "partial", "items": {"PL1": {
                "status": "failed", "message": "YouTube did not return a track list"}}},
        }}}, "resources": {"album_tracks": {"status": "complete", "refreshed": 183}},
    }))
    restored = c.get("/sync/recent").json()
    assert restored["outcome"] == "partial"
    assert any(event["type"] == "err" and "Alternative Mix" in event["text"]
               and "did not return a track list" in event["text"] for event in restored["events"])
    for stamp in (None, "1000"):
        if stamp:
            store.settings.set_setting("last_sync_at", stamp)
        html = c.get("/").text
        assert ('aria-labelledby="setup-details-title"' in html
                or 'id="library-import-details"' in html)
        assert 'x-show="running || lines.length"' not in html


def test_setup_banner_tracks_import_then_models_across_pages(store):
    import numpy as np
    from tests.conftest import seed_catalog_track

    _, client = _client(store)
    setup = client.get("/home/onboard/status").json()["setup"]
    assert setup["phase"] == "import" and not setup["complete"]
    seed_catalog_track(store, "ready|artist", "Ready", "Artist", video_id="ready", genre="house")
    store.settings.set_setting("last_sync_at", "1000")
    # Finishing / dismissing the Home introduction does not finish actual model setup.
    store.settings.set_setting("onboard_dismissed", "1")
    for path in ("/", "/playlists", "/albums"):
        html = client.get(path).text
        assert 'class="setup-banner"' in html
        assert 'aria-labelledby="setup-details-title"' in html
    setup = client.get("/home/onboard/status").json()["setup"]
    assert setup["phase"] == "learn" and not setup["complete"]
    assert setup["total"] == 1 and setup["genres"] == 1
    assert setup["modes"] == 0
    store.modes.replace_modes([{"mode_id": 1, "label": "House", "families": [["house", 1]],
        "centroid": np.array([1.0], dtype=np.float32), "size": 5, "rep_keys": []}], [], 1000)
    assert not client.get("/home/onboard/status").json()["setup"]["complete"]
    assert 'id="onboarding-feed"' in client.get("/").text
    from tests.onboarding_support import publish_personal_mix
    publish_personal_mix(store)
    assert client.get("/home/onboard/status").json()["setup"]["complete"]
    assert 'class="setup-banner"' not in client.get("/playlists").text


def test_setup_status_uses_live_import_step_and_empty_accounts_finish(store):
    _, client = _client(store)
    job = client.app.state.ctx.jobs.create(kind="library")
    job.events.append({"type": "info", "text": "Reading playlist 2 of 5: Night music",
                       "stage": "playlists", "i": 2, "total": 5})
    setup = client.get("/home/onboard/status").json()["setup"]
    assert setup["running"]
    assert setup["step"] == "Reading playlist 2 of 5: Night music"
    assert (setup["activity"], setup["current"], setup["target"]) == ("playlists", 2, 5)
    job.done = True
    store.settings.set_setting("last_sync_at", "1000")
    assert client.get("/home/onboard/status").json()["setup"]["complete"]


def test_imported_likes_and_dismissal_cannot_open_an_empty_home(store):
    from tests.conftest import seed_catalog_track

    _, client = _client(store)
    store.settings.set_setting("last_sync_at", "1000")
    store.settings.set_setting("onboard_dismissed", "1")
    for i in range(30):
        key = f"song {i}|artist"
        seed_catalog_track(store, key, f"Song {i}", "Artist", video_id=f"v{i}")
        store.rec_model.record_like(key, now=1000)
    for _ in range(2):
        html = client.get("/").text
        assert 'id="onboarding-feed"' in html
        assert 'id="home-feed"' not in html
        assert 'class="dash-nav"' not in html
        assert "arrival-feature" not in html


def test_unusable_prepared_offers_do_not_end_setup(store):
    import numpy as np
    from tests.onboarding_support import publish_personal_mix
    from yt_playlist.rec import onboarding

    _, client = _client(store)
    store.settings.set_setting("last_sync_at", "1000")
    store.modes.replace_modes([{"mode_id": 1, "label": "House", "families": [],
        "centroid": np.array([1.0]), "size": 5, "rep_keys": []}], [], 1000)
    tracks = publish_personal_mix(store)
    assert onboarding.prepared_mixes_ready(store, 1000)
    store.rec_model.record_dislike(tracks[0]["key"], until=None, now=1000)
    assert not onboarding.prepared_mixes_ready(store, 1000)
    assert 'id="onboarding-feed"' in client.get("/").text
    store.rec_proposals.put_proposals("home_tuned", {"menus": [{"protos": [None]}]}, 1000)
    assert not onboarding.prepared_mixes_ready(store, 1000)
