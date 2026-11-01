import json

import pytest
from fastapi.testclient import TestClient
from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


@pytest.fixture
def store():
    s = Store(":memory:")
    try:
        s.init_schema()
        yield s
    finally:
        s.conn.close()


def _client(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    fake = FakeClient()
    return fake, TestClient(create_app(store, lambda: {iid: fake}, now_fn=lambda: 1000.0),
                            base_url="http://127.0.0.1")


def _synced_thin(store):
    from tests.conftest import seed_catalog_track
    seed_catalog_track(store,'a|x','T','A',video_id='v1')
    store.conn.execute("UPDATE recordings SET first_enriched_at=1 WHERE identity_key='a|x'")
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
    playlist = next(p for p in store.playlists.get_playlists() if p.provider_playlist_id == fake.created[0][0])
    html = client.get(f'/playlist/{playlist.id}').text
    assert 'Play on YouTube Music' in html
    assert 'Start radio from this playlist' not in html

    store.settings.set_setting(SETUP_COMPLETE, '1')
    store.settings.set_setting('onboard_format_pending', '1')
    html = client.get(f'/playlist/{playlist.id}').text
    assert f'href="/?radio_playlist={playlist.id}#radio"' in html
    assert store.settings.get_setting('home_format') is None
    assert store.settings.get_setting('onboard_listening_format') is None


@pytest.mark.parametrize('artist_set', [False, True])
def test_overlong_starter_has_an_actionable_length_error_without_remote_writes(store, caplog, artist_set):
    fake, client = _client(store)
    fake._song_durations['mix'] = 9000
    response = client.post('/home/generate', data={
        'name': 'Something to listen to',
        'tracks': json.dumps([{'video_id': 'mix', 'title': 'DJ mix', 'artist': 'Artist'}]),
        'artist_start': '1' if artist_set else '0'})
    assert response.status_code == 200
    assert '20-minute maximum track length set in Taste Model' in response.text
    assert 'Change track-length limit' in response.text
    assert 'href="/taste#recommendations/features"' in response.text
    assert 'unexpected response' not in response.text
    assert 'generate playlist' not in caplog.text
    assert not fake.created and not fake.added
    assert store.tracks.duration_for_video('mix') == 9000
    assert store.rec_query.tracks_total() == 0


def test_home_shows_onboarding_when_thin(store):
    _synced_thin(store)
    _, c = _client(store)
    html = c.get("/").text
    assert '<h1 id="arrival-title">Welcome to TuneConsole</h1>' in html
    assert html.count('class="festival-map"') == 1
    assert 'festival-grounds-v4.png' in html
    assert 'onboarding-arrival.png' not in html
    assert 'class="account-health"' not in html
    assert 'hx-get="/home/onboard/start"' in html          # imported music is immediately playable
    assert "onboard-progress" not in html
    assert 'hx-get="/home/cards"' not in html
    assert 'id="home-feed"' not in html


def test_first_format_welcome_rejects_invalid_input(store):
    _, client = _client(store)
    html = client.get('/').text
    assert 'id="arrival-title"' in html
    assert 'id="listening-format-title"' not in html
    assert store.settings.get_setting('onboard_format_pending') is None
    assert client.post('/home/onboard/format', data={'format': 'invalid'}).status_code == 422
    assert store.settings.get_setting('onboard_listening_format') is None


@pytest.mark.parametrize('format,panel', [('albums', 'album-choices'), ('mixes', 'directions'), ('radio', 'radio')])
def test_first_format_choice_persists_and_finishes_after_offers_arrive(store, format, panel):
    from tests.test_growth_destinations import publish_full_journey

    _, client = _client(store)
    assert client.post('/home/onboard/format', data={'format': format}).status_code == 409
    _synced_thin(store)
    publish_full_journey(store)
    response = client.post('/home/onboard/format', data={'format': format}, follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/'
    assert store.settings.get_setting('home_format') == format
    assert store.settings.get_setting('onboard_listening_format') is None
    status = client.get('/home/onboard/status').json()['setup']
    assert status['complete'] and status['albums_ready']
    assert status['listening_format'] == format
    html = client.get('/').text
    assert 'id="onboarding-feed"' not in html
    assert f"homePanel(location.hash.slice(1)) || '{panel}'" in html


def test_prepared_albums_cannot_skip_the_foundation_when_choosing_a_format(store):
    from yt_playlist.rec import account_growth, growth_readiness, onboarding

    _, client = _client(store)
    _synced_thin(store)
    store.rec_proposals.put_proposals('album_prepared', {
        'built_at': 1000, 'context': {}, 'items': [
            {'browse_id': f'MPRE{i}', 'title': f'Record {i}', 'artist': f'Artist {i}',
             'classification': 'discovery', 'score': .8} for i in range(2)]}, 1000)
    assert growth_readiness.albums(store, 1000)
    growth = account_growth.view(store, 1000)
    assert not growth['foundation_ready']
    assert not growth['ready_formats']['albums']
    assert client.get('/home/album-choices').status_code == 409
    response = client.post('/home/onboard/format', data={'format': 'albums'})
    assert response.status_code == 409
    assert store.settings.get_setting('home_format') is None
    assert not onboarding.setup_completed(store)


def test_legacy_pending_format_does_not_block_listening_and_settings_override_is_preserved(store):
    from tests.test_growth_destinations import publish_full_journey

    _, client = _client(store)
    store.settings.set_setting('onboard_format_pending', '1')
    client.get('/')
    store.settings.set_setting('last_sync_at', '1000')
    publish_full_journey(store)
    assert client.get('/home/onboard/status').json()['setup']['complete']
    assert 'id="welcome-title"' not in client.get('/').text
    assert 'id="listening-format-title"' not in client.get('/').text
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


def test_first_mix_polls_until_verified_songs_arrive(store):
    from yt_playlist.library.sync import sync_identity
    from tests.conftest import _track

    fake, c = _client(store)
    html = c.get("/").text
    assert 'id="arrival-title"' in html
    assert 'hx-get="/home/onboard/start"' in html
    assert "30 seconds" not in html
    empty = c.get("/home/onboard/start").text
    assert 'hx-trigger="every 3s"' in empty
    assert 'id="gen-onboard_library"' not in empty

    # Catalog observations from incomplete pages are not owned songs.
    store.tracks.observe_track_entry("partial", "Unverified song", "Artist", now=1000)
    assert 'id="gen-onboard_library"' not in c.get("/home/onboard/start").text

    iid = store.accounts.get_accounts()[0].id
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
    iid = store.accounts.get_accounts()[0].id
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
    from tests.conftest import seed_catalog_track
    from yt_playlist.core import sync_state

    _, c = _client(store)
    seed_catalog_track(store, "ready|artist", "Ready", "Artist", video_id="ready")
    assert c.get("/sync/recent").json() == {"events": [], "outcome": None}
    account_id = str(store.accounts.get_accounts()[0].id)
    result = {
        "status": "partial", "accounts": {account_id: {"label": "Main", "status": "partial", "resources": {
            "playlist_listing": {"status": "complete", "entries": [
                {"playlistId": "PL1", "title": "Alternative Mix"}]},
            "playlists": {"status": "partial", "items": {"PL1": {
                "status": "failed", "message": "YouTube did not return a track list"}}},
        }}}, "resources": {"album_tracks": {"status": "complete", "refreshed": 183}},
    }
    with store.conn:
        sync_state.write(store.conn, result, 1000)
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
    store.rec_proposals.put_proposals('listening_choices', {
        'home_format': {'episodes': {'mixes': 1}}}, 1000)
    store.settings.set_setting("last_sync_at", "1000")
    store.settings.set_setting("last_sync_outcome", outcome)
    html = client.get("/").text
    assert 'id="home-feed"' in html
    assert 'class="setup-banner"' not in html
    assert 'id="library-import-details"' not in html
    assert "Some items need a retry" not in html
    assert "Reconnect or sign in, then sync again" not in html

    # A confirmed account problem still has a specific action in Needs attention.
    identity = store.accounts.get_accounts()[0]
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
    from tests.test_growth_journey import early_journey
    early_journey(store)
    publish_personal_mix(store)
    assert client.get("/home/onboard/status").json()["setup"]["complete"]
    assert 'class="setup-banner"' not in client.get("/playlists").text


@pytest.mark.parametrize('format', ['mixes', 'albums'])
def test_partial_sync_with_playable_offers_finishes_setup_without_faking_sync_success(store, format):
    from tests.onboarding_support import finish_partial_import, publish_personal_album, publish_personal_mix
    from tests.test_growth_journey import early_journey
    from yt_playlist.core import sync_state

    _, client = _client(store)
    finish_partial_import(store)
    store.tracks.observe_track_entry('partial', 'Unverified song', 'Artist', now=1000)
    before = client.get('/home/onboard/status').json()
    assert not before['setup']['complete'] and before['songs'] == 0
    assert 'id="onboarding-feed"' in client.get('/').text

    (publish_personal_mix if format == 'mixes' else publish_personal_album)(store)
    # A downstream offer cannot skip prerequisite destinations. Their separate
    # preparation can finish even while the collection import needs retrying.
    assert not client.get('/home/onboard/status').json()['setup']['complete']
    early_journey(store)
    # Direct navigation must work before a status poll has remembered completion.
    html = client.get('/').text
    assert 'id="onboarding-feed"' not in html
    assert 'class="dash-nav"' in html
    assert 'class="setup-banner"' not in html
    setup = client.get('/home/onboard/status').json()['setup']
    assert setup['complete'] and setup['phase'] == 'ready' and setup['offers_ready']
    assert not setup['imported'] and setup['analyzed'] == 0 and setup['modes'] == 0
    assert store.settings.get_setting('last_sync_at') is None
    assert store.settings.get_setting('last_sync_outcome') == 'partial'
    state = sync_state.state(store)
    failed = state['accounts']['1']['resources']['playlists']['items']['PL_RETRY']
    assert failed['status'] == 'failed' and failed['retry_at'] > 1000
    assert any('Incomplete continuation page' in event['text']
               for event in client.get('/sync/recent').json()['events'])
    assert client.get('/home/growth').json()['nodes'][0]['ready']


def test_prepared_offer_readiness_agrees_between_home_and_setup_without_modes(store):
    from tests.onboarding_support import publish_personal_mix
    from tests.test_growth_journey import early_journey
    from yt_playlist.rec import onboarding

    _, client = _client(store)
    store.settings.set_setting("last_sync_at", "1000")
    early_journey(store)
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
    from tests.onboarding_support import publish_personal_mix
    from tests.test_growth_journey import early_journey
    from yt_playlist.core import home_outputs
    from yt_playlist.rec import onboarding

    path = tmp_path / 'library.db'
    first = Store(path)
    first.init_schema()
    try:
        first.settings.set_setting('last_sync_at', '1000')
        early_journey(first)
        if format == 'mixes':
            publish_personal_mix(first)
        client = TestClient(create_app(first, lambda: {}, now_fn=lambda: 1000,
                                       background_work=False), base_url='http://127.0.0.1')
        assert client.get('/home/onboard/status').json()['setup']['complete']
        assert first.settings.get_setting(onboarding.SETUP_COMPLETE) == '1'
        # Disposable projections and offer-deduplication rows are not setup state.
        with first.conn:
            first.conn.execute('BEGIN IMMEDIATE')
            home_outputs.clear(first.conn, ('home_prepared','home_tuned','home_choice','album_prepared'))
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
def test_empty_or_invalid_historical_offers_do_not_complete_setup(tmp_path, surface, payload):
    from yt_playlist.core.migrations import migrate, registry
    from yt_playlist.rec import onboarding

    # These malformed rows predate the typed offer owner; current writers reject them.
    store = Store(tmp_path / 'historical-offers.db')
    try:
        migrate(store.conn, registry=registry.MIGRATIONS[:82])
        store._bind_repositories()
        _synced_thin(store)
        store.rec_proposals.put_proposals(surface, payload, 999)
        assert onboarding.initial_setup_pending(store, 1000)
        assert store.settings.get_setting(onboarding.SETUP_COMPLETE) is None
    finally:
        store.conn.close()


def test_snapshot_readiness_does_not_write_setup_completion(store):
    from tests.onboarding_support import publish_personal_mix
    from tests.test_growth_journey import early_journey
    from yt_playlist.rec import onboarding

    store.settings.set_setting('last_sync_at', '1000')
    early_journey(store)
    publish_personal_mix(store)
    with store.read_snapshot() as snapshot:
        assert not onboarding.initial_setup_pending(snapshot.store, 1000)
    assert store.settings.get_setting(onboarding.SETUP_COMPLETE) is None
    assert not onboarding.initial_setup_pending(store, 1000)
    assert store.settings.get_setting(onboarding.SETUP_COMPLETE) == '1'


def test_setup_status_uses_live_import_step_and_empty_accounts_need_artists(store):
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
    setup = client.get("/home/onboard/status").json()["setup"]
    assert not setup['complete']
    assert setup['artists_required'] and setup['phase'] == 'artists'


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
