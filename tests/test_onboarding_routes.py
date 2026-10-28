import json

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


def test_saved_album_starter_arrives_without_tracks_or_training(store):
    _, client = _client(store)
    store.settings.set_setting('onboard_listening_format', 'albums')
    assert 'hx-get="/home/onboard/start"' in client.get('/').text
    waiting = client.get('/home/onboard/start').text
    assert 'every 3s' in waiting and 'Play mix' not in waiting
    store.collection.add_saved_album({'browse': 'MPRE_first', 'title': 'First record',
                                      'artist': 'My artist', 'type': 'Album'})
    html = client.get('/home/onboard/start').text
    assert 'First record' in html
    assert "Here's an album to listen to while we perform initial customization." in html
    assert 'hx-post="/album/play"' in html
    assert 'data-onboarding-album="MPRE_first"' in html
    assert 'Play album' in html and 'Play mix' not in html
    assert 'every 3s' not in html
    assert store.settings.get_setting('last_sync_at') is None
    assert store.rec_proposals.get_proposals('album_prepared') is None
    assert store.rec_model.rec_content_vectors_count() == 0


def test_album_starter_can_use_familiar_artists_but_not_incomplete_imports(store):
    from yt_playlist.core import albums
    from tests.conftest import seed_catalog_track

    _, client = _client(store)
    store.settings.set_setting('onboard_listening_format', 'albums')
    store.tracks.observe_track_entry('partial', 'Unverified', 'Unknown artist', now=1000)
    with store.conn:
        albums.observe(store.conn, 'MPRE_unknown', title='Unverified album', artist='Unknown artist')
    assert 'Play album' not in client.get('/home/onboard/start').text
    for i in range(2):
        seed_catalog_track(store, f'familiar {i}|artist', f'Familiar {i}', 'Familiar artist',
                           video_id=f'familiar-{i}')
    with store.conn:
        albums.observe(store.conn, 'MPRE_familiar', title='Familiar record', artist='Familiar artist')
    html = client.get('/home/onboard/start').text
    assert 'Familiar record' in html and 'Unverified album' not in html
    assert "Here's an album to listen to while we perform initial customization." in html
    assert 'Play mix' not in html
    # A saved release takes priority over the familiar-artist fallback.
    store.collection.add_saved_album({'browse': 'MPRE_saved', 'title': 'Saved record', 'artist': 'Other artist'})
    html = client.get('/home/onboard/start').text
    assert 'Saved record' in html and 'Familiar record' not in html


def test_radio_starter_uses_ready_radio_and_falls_back_without_building_models(store, monkeypatch):
    from tests.conftest import seed_catalog_track

    _, client = _client(store)
    store.settings.set_setting('onboard_listening_format', 'radio')
    seed_catalog_track(store, 'ready|artist', 'Ready song', 'Artist', video_id='ready')
    radio = client.app.state.ctx.radio
    monkeypatch.setattr(radio, 'seeds', lambda: pytest.fail('Starter must only read prepared radio'))
    html = client.get('/home/onboard/start').text
    assert 'Play mix' in html
    assert 'while we prepare your Radio Session' in html
    radio.offer = [{'key': 'ready|artist', 'video_id': 'ready', 'title': 'Ready song', 'artist': 'Artist'}]
    radio.offer_id = 'prepared-radio-offer'
    html = client.get('/home/onboard/start').text
    assert 'Your first Radio Session' in html and 'Start radio' in html
    assert 'prepared-radio-offer' in html
    assert 'Play mix' not in html


def test_uploaded_starter_preserves_existing_browser_handoff(store):
    from yt_playlist.library.album_fetch import UPLOAD_RELEASE_PREFIX

    _, client = _client(store)
    store.settings.set_setting('onboard_listening_format', 'albums')
    store.collection.add_saved_album({'browse': UPLOAD_RELEASE_PREFIX + '_first',
                                      'title': 'My upload', 'artist': 'My artist'})
    html = client.get('/home/onboard/start').text
    assert 'My upload' in html and 'Play album' in html
    assert 'https://music.youtube.com/browse/' + UPLOAD_RELEASE_PREFIX in html
    assert 'Play mix' not in html


def test_generated_starter_hides_radio_until_setup_is_complete(store):
    from yt_playlist.rec.onboarding import SETUP_COMPLETE

    _synced_thin(store)
    fake, client = _client(store)
    response = client.post('/home/generate', data={
        'name': 'Something to listen to - January 1 1970',
        'tracks': json.dumps([{'video_id': 'v1', 'title': 'T', 'artist': 'A'}]),
    })
    assert response.status_code == 200 and 'Saved' in response.text
    playlist = next(p for p in store.playlists.get_playlists() if p.ytm_playlist_id == fake.created[0][0])
    html = client.get(f'/playlist/{playlist.id}').text
    assert 'Play on YouTube Music' in html
    assert 'Start radio from this playlist' not in html

    store.settings.set_setting(SETUP_COMPLETE, '1')
    html = client.get(f'/playlist/{playlist.id}').text
    assert f'href="/?radio_playlist={playlist.id}#radio"' in html


def test_home_shows_onboarding_when_thin(store):
    _synced_thin(store)
    _, c = _client(store)
    html = c.get("/").text
    assert "personal mixes and find your next favorites" in html
    assert 'hx-get="/home/onboard/start"' in html          # imported music is immediately playable
    assert "onboard-progress" not in html


@pytest.mark.parametrize('format,panel', [('albums', 'album-choices'), ('mixes', 'directions'), ('radio', 'radio')])
def test_first_format_choice_persists_and_finishes_after_offers_arrive(store, format, panel):
    from tests.onboarding_support import publish_personal_album, publish_personal_mix

    _, client = _client(store)
    html = client.get('/').text
    assert 'id="welcome-title"' in html
    assert 'id="listening-format-title"' not in html
    assert 'id="welcome-title"' in client.get('/').text
    html = client.post('/home/onboard/welcome').text
    assert 'id="listening-format-title"' in html
    assert 'id="welcome-title"' not in html
    assert 'Customize your landing page.' in html
    assert 'Taste Model settings' not in html
    assert 'id="arrival-title"' not in html
    assert store.settings.get_setting('onboard_format_pending') == '1'
    assert client.post('/home/onboard/format', data={'format': 'invalid'}).status_code == 422
    assert store.settings.get_setting('onboard_listening_format') is None
    response = client.post('/home/onboard/format', data={'format': format}, follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/'
    assert store.settings.get_setting('onboard_listening_format') == format
    assert store.settings.get_setting('onboard_format_pending') == '0'
    assert 'id="arrival-title"' in client.get('/').text
    store.settings.set_setting('last_sync_at', '1000')
    publish_personal_album(store)
    if format == 'mixes':
        publish_personal_mix(store)
    status = client.get('/home/onboard/status').json()['setup']
    assert status['complete'] and status['albums_ready']
    assert status['listening_format'] == format
    html = client.get('/').text
    assert 'id="onboarding-feed"' not in html
    assert f"location.hash.slice(1) || '{panel}'" in html


def test_pending_format_choice_survives_import_and_can_be_resolved_in_settings(store):
    from tests.onboarding_support import publish_personal_album

    _, client = _client(store)
    client.get('/')
    store.settings.set_setting('last_sync_at', '1000')
    publish_personal_album(store)
    assert client.get('/home/onboard/status').json()['setup']['complete']
    assert 'id="welcome-title"' in client.get('/').text
    client.post('/home/onboard/welcome')
    assert 'id="listening-format-title"' in client.get('/').text
    assert client.post('/home/format', data={'format': 'albums'}).status_code == 204
    assert 'id="onboarding-feed"' not in client.get('/').text


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
    assert 'id="welcome-title"' in html
    html = c.post('/home/onboard/format', data={'format': 'mixes'}).text
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
    from tests.conftest import seed_catalog_track

    _, c = _client(store)
    seed_catalog_track(store, "ready|artist", "Ready", "Artist", video_id="ready")
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
        assert 'aria-labelledby="setup-details-title"' in html
        assert 'id="library-import-details"' not in html


@pytest.mark.parametrize("outcome", ["partial", "skipped", "failed"])
def test_established_home_keeps_retry_diagnostics_out_of_the_dashboard(store, outcome):
    from tests.onboarding_support import publish_personal_mix

    _, client = _client(store)
    publish_personal_mix(store)
    store.settings.set_setting("last_sync_at", "1000")
    store.settings.set_setting("last_sync_outcome", outcome)
    html = client.get("/").text
    assert 'id="home-feed"' in html
    assert 'class="setup-banner"' not in html
    assert 'id="library-import-details"' not in html
    assert "Some items need a retry" not in html
    assert "Reconnect or sign in, then sync again" not in html

    # A confirmed account problem still has a specific action in Needs attention.
    identity = store.identities.get_identities()[0]
    client.app.state.ctx.flag_auth_expired(identity.id, "Main")
    html = client.get("/").text
    notices = html.split('<section class="dash-panel dash-notices"', 1)[1]
    assert 'class="alert-card card card--featured alert-high"' in notices
    assert "Sign in to YouTube Music (Main)" in notices
    assert 'href="https://music.youtube.com"' in notices
    assert "Open YouTube Music" in notices


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


def test_prepared_offer_readiness_agrees_between_home_and_setup_without_modes(store):
    from tests.onboarding_support import publish_personal_mix
    from yt_playlist.rec import onboarding

    _, client = _client(store)
    store.settings.set_setting("last_sync_at", "1000")
    publish_personal_mix(store)
    assert store.modes.list_modes(active_only=True) == []

    # A sparse account already has a usable personal mix on a direct Home visit.
    html = client.get("/").text
    assert 'id="home-feed"' in html
    assert 'id="onboarding-feed"' not in html
    assert 'class="setup-banner"' not in html
    assert not onboarding.initial_setup_pending(store, 1000)
    assert not onboarding.onboarding_active(store, 1000)
    setup = client.get("/home/onboard/status").json()["setup"]
    assert setup["modes"] == 0
    assert setup["mixes_ready"]
    assert setup["complete"]
    assert setup["phase"] == "ready"
    assert 'class="setup-banner"' not in client.get("/playlists").text


@pytest.mark.parametrize('format', ['mixes', 'albums'])
def test_completed_setup_survives_offer_loss_and_a_database_restart(tmp_path, format):
    from tests.onboarding_support import publish_personal_album, publish_personal_mix
    from yt_playlist.rec import onboarding

    path = tmp_path / 'library.db'
    first = Store(path)
    first.init_schema()
    try:
        first.settings.set_setting('last_sync_at', '1000')
        (publish_personal_mix if format == 'mixes' else publish_personal_album)(first)
        client = TestClient(create_app(first, lambda: {}, now_fn=lambda: 1000,
                                       background_work=False), base_url='http://127.0.0.1')
        assert client.get('/home/onboard/status').json()['setup']['complete']
        assert first.settings.get_setting(onboarding.SETUP_COMPLETE) == '1'
        # Disposable projections and offer-deduplication rows are not setup state.
        with first.conn:
            first.conn.execute("DELETE FROM rec_proposals WHERE surface IN "
                               "('home_prepared','home_tuned','home_choice','home_shown','album_prepared')")
    finally:
        first.conn.close()
    restored = Store(path)
    restored.init_schema()
    try:
        client = TestClient(create_app(restored, lambda: {}, now_fn=lambda: 1100,
                                       background_work=False), base_url='http://127.0.0.1')
        setup = client.get('/home/onboard/status').json()['setup']
        assert setup['complete'] and setup['phase'] == 'ready'
        assert not setup['offers_ready']
        assert not onboarding.initial_setup_pending(restored, 1100)
        assert not onboarding.onboarding_active(restored, 1100)
        html = client.get('/').text
        assert 'id="home-feed"' in html
        assert 'id="onboarding-feed"' not in html
        assert 'class="setup-banner"' not in html
    finally:
        restored.conn.close()


@pytest.mark.parametrize('surface,payload', [
    ('home_shown', {'epoch': 4, 'signatures': ['actual-offer']}),
    ('choice_window:mixes', {'window': '4', 'opportunity_id': 'actual-menu'}),
    ('choice_window:albums', {'window': '4', 'opportunity_id': 'actual-menu'}),
])
def test_existing_actual_home_offers_backfill_setup_completion(store, surface, payload):
    from yt_playlist.rec import onboarding

    _synced_thin(store)
    store.rec_proposals.put_proposals(surface, payload, 999)
    _, client = _client(store)
    assert store.settings.get_setting(onboarding.SETUP_COMPLETE) is None
    html = client.get('/').text
    assert 'id="onboarding-feed"' not in html
    assert 'id="home-feed"' in html
    assert store.settings.get_setting(onboarding.SETUP_COMPLETE) == '1'


@pytest.mark.parametrize('surface,payload', [
    ('home_shown', {'signatures': []}),
    ('home_shown', {'signatures': [None, '']}),
    ('home_shown', {'signatures': 'invalid'}),
    ('choice_window:albums', {'opportunity_id': None}),
    ('choice_window:mixes', {'opportunity_id': ''}),
])
def test_empty_or_invalid_historical_offers_do_not_complete_setup(store, surface, payload):
    from yt_playlist.rec import onboarding

    _synced_thin(store)
    store.rec_proposals.put_proposals(surface, payload, 999)
    assert onboarding.initial_setup_pending(store, 1000)
    assert store.settings.get_setting(onboarding.SETUP_COMPLETE) is None


def test_snapshot_readiness_does_not_write_setup_completion(store):
    from tests.onboarding_support import publish_personal_mix
    from yt_playlist.rec import onboarding

    store.settings.set_setting('last_sync_at', '1000')
    publish_personal_mix(store)
    with store.read_snapshot() as snapshot:
        assert not onboarding.initial_setup_pending(snapshot.store, 1000)
    assert store.settings.get_setting(onboarding.SETUP_COMPLETE) is None
    assert not onboarding.initial_setup_pending(store, 1000)
    assert store.settings.get_setting(onboarding.SETUP_COMPLETE) == '1'


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


@pytest.mark.parametrize("with_mode", [False, True])
def test_unusable_prepared_offers_do_not_end_setup(store, with_mode):
    import numpy as np
    from tests.onboarding_support import publish_personal_mix
    from yt_playlist.rec import onboarding

    _, client = _client(store)
    store.settings.set_setting("last_sync_at", "1000")
    if with_mode:
        store.modes.replace_modes([{"mode_id": 1, "label": "House", "families": [],
            "centroid": np.array([1.0]), "size": 5, "rep_keys": []}], [], 1000)
    tracks = publish_personal_mix(store)
    assert onboarding.prepared_mixes_ready(store, 1000)
    store.rec_model.record_dislike(tracks[0]["key"], until=None, now=1000)
    assert not onboarding.prepared_mixes_ready(store, 1000)
    assert onboarding.initial_setup_pending(store, 1000)
    assert not client.get("/home/onboard/status").json()["setup"]["complete"]
    assert 'id="onboarding-feed"' in client.get("/").text
    store.rec_proposals.put_proposals("home_tuned", {"menus": [{"protos": [None]}]}, 1000)
    assert not onboarding.prepared_mixes_ready(store, 1000)
    assert onboarding.initial_setup_pending(store, 1000)
    assert not client.get("/home/onboard/status").json()["setup"]["complete"]
