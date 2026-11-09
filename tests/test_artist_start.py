"""The empty-catalog path uses actual selections and retained catalog evidence."""
import json
from contextlib import ExitStack, nullcontext
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient, seed_catalog_track
from tests.home_fixture_support import home_routes_only, without_metadata_providers
from tests.store_fixture_support import (
    bounded_consumer_schema, uncontended_file_consumer_store as store, without_query_timing,
)
from yt_playlist.rec import artist_start
from yt_playlist.web.app import create_app


pytestmark = pytest.mark.usefixtures(
    'home_routes_only', 'without_metadata_providers', 'without_query_timing')


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture(autouse=True)
def client_lifecycle(request, monkeypatch):
    if 'store' not in request.fixturenames:
        yield
        return
    request.getfixturevalue('store')
    make_client = client_for
    with ExitStack() as clients:
        def managed_client(store, fake):
            return clients.enter_context(make_client(store, fake))

        monkeypatch.setattr(sys.modules[__name__], 'client_for', managed_client)
        yield


def page(name, index, related=()):
    return {'name': name, 'thumbnails': [{'url': f'https://example.com/{index}.jpg'}],
            'songs': {'results': [{'videoId': f'song{index}{n}', 'title': f'Song {index}-{n}',
                                  'artists': [{'name': name, 'id': f'UCartist{index}'}]}
                                 for n in range(4)]},
            'related': {'results': list(related)}}


class Catalog(FakeClient):
    def __init__(self):
        super().__init__(artists={f'UCartist{i}': page('Artist ' + str(i), i) for i in range(4)})
        self.calls = []

    def search(self, query, filter='songs', limit=8):
        self.calls.append(('search', query, filter))
        return [{'artist': 'Artist 0', 'browseId': 'UCartist0'},
                {'artist': 'Artist 0', 'browseId': 'UCartist0'},
                {'title': 'Not an artist', 'browseId': 'MPREwrong'}]

    def get_artist(self, browseId):
        self.calls.append(('artist', browseId))
        return super().get_artist(browseId)


class EmptyLibraryCatalog(Catalog):
    """The reported library: empty episodes, unread likes and one past listen."""
    history_timezone = 'UTC'
    likes_readable = False
    albums_readable = True

    def get_library_playlists(self, limit=25):
        return [{'playlistId': 'SE', 'title': 'Episodes for Later', 'count': 0},
                {'playlistId': 'LM', 'title': 'Liked Music', 'count': 1}]

    def get_playlist(self, playlistId, limit=100):
        if playlistId == 'LM' and not self.likes_readable:
            from yt_playlist.core.album_snapshot import LibrarySnapshotError
            raise LibrarySnapshotError('playlist', 'unsupported or incomplete track page (header_count=1)', {}, 1)
        return {'tracks': [], 'trackCount': 0}

    def get_history(self):
        return [{'videoId': 'past-song', 'title': 'One past song',
                 'artists': [{'name': 'Past artist'}], 'played': 'Today'}]

    def get_library_albums(self, limit=25):
        if not self.albums_readable:
            from yt_playlist.core.album_snapshot import LibrarySnapshotError
            raise LibrarySnapshotError('saved-album', 'snapshot has no complete grid', {}, 1)
        return []


def client_for(store, fake):
    identity = store.accounts.upsert_account('main', 'cred', None, True)
    return TestClient(create_app(store, lambda: {identity: fake}, now_fn=lambda: 1000.,
                                 background_work=False), base_url='http://127.0.0.1')


def test_empty_sync_requires_artist_setup_even_after_legacy_dismissal(store):
    fake = Catalog()
    client = client_for(store, fake)
    assert not artist_start.required(store)  # no false empty account during the initial sync
    store.settings.set_setting('last_sync_at', '999')
    store.settings.set_setting('onboard_setup_complete', '1')
    store.settings.set_setting('onboard_dismissed', '1')
    assert artist_start.required(store)
    html = client.get('/').text
    assert 'festival-intro' in html and 'id="onboarding-feed"' in html
    assert 'artist-start-page' not in html
    assert '/static/artist-start.js' in html and '/static/artist-start.css' in html
    fragment = client.get('/home/onboard/start').text
    assert 'Pick 3 artists you love.' in fragment
    assert 'Coming from Spotify?' in fragment
    assert 'data-onboarding-artists' in fragment and 'id="onboarding-start"' in fragment
    assert 'Your music starts here' not in fragment
    assert 'Start with a favorite.' not in fragment
    assert 'home-dashboard' not in html
    assert 'How do you like to listen?' not in html
    assert not fake.calls  # Home never waits on catalog calls


def test_partial_import_is_not_owned_music_and_saved_albums_can_start_normally(store):
    store.settings.set_setting('last_sync_at', '999')
    store.tracks.observe_track_entry('partial', 'Partial import', 'Artist', now=1000)
    assert artist_start.required(store)
    store.collection.add_saved_album({'browse': 'MPREready', 'title': 'My record', 'artist': 'Artist'})
    assert not artist_start.required(store)


@pytest.fixture
def immediate_sync_retries(monkeypatch):
    from functools import partial
    from yt_playlist.library import sync

    # Keep real attempts, failure classification and persisted retry times.
    # These setup checks do not own wall-clock backoff between provider reads.
    monkeypatch.setattr(sync, 'with_retry', partial(sync.with_retry, sleep=lambda seconds: None))


@pytest.mark.parametrize('likes_readable,albums_readable', [(False, False), (False, True), (True, True)])
@pytest.mark.parametrize('live_plays', [0, 1, 5])
def test_empty_owned_library_with_incidental_history_reaches_artist_setup(
        store, likes_readable, albums_readable, live_plays, immediate_sync_retries):
    from types import SimpleNamespace
    from yt_playlist.core import sync_state
    from yt_playlist.library import live_plays as live_ingestion
    from yt_playlist.library.sync import sync_all

    fake = EmptyLibraryCatalog()
    fake.likes_readable = likes_readable
    fake.albums_readable = albums_readable
    client = client_for(store, fake)
    result = sync_all(store, {1: fake}, 1000.)
    assert result['status'] == ('complete' if likes_readable else 'partial')
    assert set(dict(store.history.recent_plays_with_ts())) == {'one past song|past artist'}
    assert store.conn.execute('SELECT COUNT(*) FROM play_events').fetchone()[0] == 0
    assert store.conn.execute('SELECT COUNT(*) FROM playable_library_tracks').fetchone()[0] == 0
    for index in range(live_plays):
        assert live_ingestion.handle_play_event(SimpleNamespace(store=store), {
            'type': 'play', 'title': f'Incidental {index}', 'artist': 'Past artist',
            'videoId': f'incidental-{index}', 'brandId': '', 'likeStatus': 'INDIFFERENT',
        }, 900. + index)
    assert store.conn.execute('SELECT COUNT(*) FROM playable_library_tracks').fetchone()[0] == 0
    assert artist_start.required(store)
    status = client.get('/home/onboard/status').json()
    assert status['setup']['artists_required'] and not status['setup']['complete']
    assert status['songs'] == 0
    assert 'id="onboarding-feed"' in client.get('/').text
    assert 'Pick 3 artists you love.' in client.get('/home/onboard/start').text
    assert not fake.created
    if not likes_readable:
        assert store.settings.get_setting('last_sync_at') is None
        liked = sync_state.state(store)['accounts']['1']['resources']['playlists']['items']['LM']
        assert liked['status'] == 'failed' and liked['retry_at'] > 1000
    if not albums_readable:
        albums = sync_state.state(store)['accounts']['1']['resources']['saved_albums']
        assert albums['status'] == 'failed' and albums['retry_at'] > 1000
    assert client.get('/home/onboard/artists/state').json()['complete'] is False


def test_artist_setup_requires_picks_and_live_play_from_the_generated_first_set(store):
    from yt_playlist.library.playlist_generated import create_generated_playlist

    fake = Catalog()
    for page in fake._artists.values():
        for song in page['songs']['results']:
            fake._catalog[song['videoId']] = song
    client = client_for(store, fake)
    store.settings.set_setting('last_sync_at', '999')
    service = artist_start.ArtistCatalog(lambda: {1: fake})
    for index in range(3):
        service.select(store, f'UCartist{index}', True, 1000.)
    tracks = store.discovery.starter_tracks()
    first = tracks[0]
    # Even a selected song played elsewhere must not skip the first-set handoff.
    store.history.record_play_event(1, first['key'], first['video_id'], 1001.)
    assert artist_start.required(store)
    generated = create_generated_playlist(store, 'My first set', tracks, fake, 1002., 1)
    assert artist_start.required(store)  # creation alone is not listening
    store.history.record_play_event(1, 'unrelated|artist', 'unrelated', 1003.,
                                    playlist_ytm_id=generated['new_ytm'])
    assert artist_start.required(store)
    store.history.record_play_event(1, first['key'], first['video_id'], 1004.,
                                    playlist_ytm_id=generated['new_ytm'])
    assert not artist_start.required(store)
    assert client.get('/home/onboard/artists/state').json()['complete'] is True
    assert 'Pick 3 artists you love.' not in client.get('/home/onboard/start').text


@pytest.mark.parametrize('source', ['spotify_export', 'takeout'])
def test_imported_exact_history_is_not_a_live_first_listen(store, source):
    from yt_playlist.core import listening

    client_for(store, Catalog())
    store.settings.set_setting('last_sync_at', '999')
    with store.conn:
        event = listening.append_event(store.conn, 1, 'one past song|past artist', 100.)
        listening.project_event(store.conn, event, 1, 100., source=source)
    assert artist_start.required(store)


@pytest.mark.parametrize('unavailable', ['playlist_listing', 'saved_albums'])
def test_artist_setup_waits_for_a_connected_listing_but_not_unavailable_albums(
        unavailable):
    from yt_playlist.controllers.readiness import Readiness
    from yt_playlist.controllers.sync import LibrarySync
    from yt_playlist.repos.accounts import AccountRepo
    from yt_playlist.repos.collection import CollectionRepo
    from yt_playlist.repos.discovery import DiscoveryRepo
    from yt_playlist.repos.readiness import ReadinessRepo
    from yt_playlist.repos.settings import SettingsRepo
    from yt_playlist.repos.sync import SyncRepo

    # The setup decision consumes persisted import facts. Sync's provider
    # attempts and persistence are exercised by the import tests above.
    resources = {
        'playlist_listing': {'status': 'failed' if unavailable == 'playlist_listing' else 'complete'},
        'saved_albums': {'status': 'failed' if unavailable == 'saved_albums' else 'complete'},
    }
    sync = Mock(spec_set=SyncRepo)
    sync.state_facts.return_value = {'raw': json.dumps({'accounts': {'1': {'resources': resources}}})}
    settings = Mock(spec_set=SettingsRepo)
    settings.get_setting.return_value = None
    readiness = Mock(spec_set=ReadinessRepo)
    readiness.playable_owned.return_value = False
    collection = Mock(spec_set=CollectionRepo)
    collection.get_saved_albums.return_value = []
    discovery = Mock(spec_set=DiscoveryRepo)
    discovery.starter_artists.return_value = []
    accounts = Mock(spec_set=AccountRepo)
    accounts.get_accounts.return_value = [SimpleNamespace(id=1)]
    reader = SimpleNamespace(
        _read_only=True, settings=settings, collection=collection, discovery=discovery,
        accounts=accounts,
        readiness=Readiness(readiness, Mock(maximum_duration=Mock(return_value=1200))),
        sync=LibrarySync(sync, settings, Mock(retained=nullcontext), None))
    store = SimpleNamespace(_read_only=False, read_view=Mock(return_value=nullcontext(reader)))

    assert artist_start.required(store) == (unavailable == 'saved_albums')
    settings.get_setting.assert_called_once_with('last_sync_at')
    readiness.playable_owned.assert_called_once_with(exclude_generated=True, maximum_duration=1200)
    store.read_view.assert_called_once_with()


def test_search_deduplicates_provider_ids_and_does_not_select_suggestions(store):
    fake = Catalog()
    service = artist_start.ArtistCatalog(lambda: {1: fake})
    rows = service.search('artist')
    assert [(row['name'], row['browse_id']) for row in rows] == [('Artist 0', 'UCartist0')]
    assert not store.discovery.starter_artists()
    assert not store.discovery.starter_tracks()
    assert service.search('ARTIST') == rows
    assert len(fake.calls) == 1


def test_exact_artist_seeds_and_real_songs_survive_a_service_restart(store):
    fake = Catalog()
    fake._artists['UCartist0']['related']['results'] = [
        {'title': 'Related artist', 'browseId': 'UCrelated1'},
        {'title': 'Wrong result', 'browseId': 'MPREwrong'}]
    service = artist_start.ArtistCatalog(lambda: {1: fake})
    for i in range(3):
        related = service.select(store, f'UCartist{i}', True, 1000.)
        if i == 0:
            assert [row['browse_id'] for row in related] == ['UCrelated1']
    assert len(store.discovery.starter_artists()) == 3
    tracks = store.discovery.starter_tracks()
    assert len(tracks) == 12
    assert [row['artist'] for row in tracks[:3]] == ['Artist 0', 'Artist 1', 'Artist 2']
    assert store.rec_query.tracks_total() == 0
    assert store.conn.execute('SELECT COUNT(*) FROM play_events').fetchone()[0] == 0
    assert store.conn.execute('SELECT COUNT(*) FROM item_ratings').fetchone()[0] == 0
    assert {row['browse_id'] for row in store.discovery.interested_artists()} == {
        'UCartist0', 'UCartist1', 'UCartist2'}
    # Another process can reconstruct the state without session flags or caches.
    fresh = artist_start.ArtistCatalog(lambda: {})
    fresh.select(store, 'UCartist1', False, 1001.)
    assert len(store.discovery.starter_artists()) == 2
    assert len(store.discovery.starter_tracks()) == 8
    assert all(row['artist'] != 'Artist 1' for row in store.discovery.starter_tracks())


@pytest.mark.parametrize('duration_field,duration', [('duration', '2:30:00'), ('duration_seconds', 9000)])
def test_artist_starter_retains_lengths_and_excludes_dj_mixes(store, duration_field, duration):
    from yt_playlist.rec import rec_params

    fake = Catalog()
    service = artist_start.ArtistCatalog(lambda: {1: fake})
    for index in range(3):
        songs = fake._artists[f'UCartist{index}']['songs']['results']
        songs.insert(0, {'videoId': f'mix{index}', 'title': f'DJ mix {index}',
                         duration_field: duration})
        service.select(store, f'UCartist{index}', True, 1000.)
    tracks = store.discovery.starter_tracks()
    assert len(tracks) == 12  # short songs fill the slots removed before per-artist sampling
    assert all(not t['video_id'].startswith('mix') for t in tracks)
    assert store.tracks.duration_for_video('mix0') == 9000
    assert store.rec_query.tracks_total() == 0  # catalog duration is not library ownership
    assert artist_start.first_set(store, 1000.)

    # A user who deliberately allows long sets can still choose them, without refetching.
    rec_params.set_param(store, 'max_track_minutes', 180)
    assert {'mix0', 'mix1', 'mix2'} <= {t['video_id'] for t in store.discovery.starter_tracks()}


def test_only_overlong_owned_tracks_do_not_replace_artist_setup(store):
    from yt_playlist.rec import rec_params

    store.settings.set_setting('last_sync_at', '999')
    seed_catalog_track(store, 'dj-mix|artist', 'DJ mix', 'Artist', video_id='mix', duration_s=9000)
    assert artist_start.required(store)
    rec_params.set_param(store, 'max_track_minutes', 180)
    assert not artist_start.required(store)


def test_three_artist_choices_with_only_long_mixes_do_not_claim_a_ready_first_set(store):
    fake = Catalog()
    service = artist_start.ArtistCatalog(lambda: {1: fake})
    for index in range(3):
        for song in fake._artists[f'UCartist{index}']['songs']['results']:
            song['duration'] = '2:30:00'
        service.select(store, f'UCartist{index}', True, 1000.)
    assert len(store.discovery.starter_artists()) == 3
    assert store.tracks.duration_for_video('song00') == 9000
    assert store.discovery.starter_tracks() == []
    assert artist_start.first_set(store, 1000.) is None


def test_no_song_and_failed_catalog_calls_never_claim_first_set_ready(store):
    fake = Catalog()
    fake._artists['UCartist2']['songs']['results'] = []
    client = client_for(store, fake)
    for i in range(2):
        response = client.post('/home/onboard/artists/select', json={'browse_id': f'UCartist{i}', 'selected': True})
        assert response.status_code == 200 and not response.json()['ready']
    assert 'Play my first set' not in client.get('/home/onboard/artists/listen').text
    response = client.post('/home/onboard/artists/select', json={'browse_id': 'UCartist2', 'selected': True})
    assert response.json()['ready'] and response.json()['songs'] == 8
    assert 'Play my first set' in client.get('/home/onboard/artists/listen').text
    assert 'starter-tracks' not in client.get('/home/onboard/artists/listen').text
    assert client.post('/home/onboard/artists/select', json={'browse_id': 'MPREwrong', 'selected': True}).status_code == 422
    assert client.post('/home/onboard/artists/select', json={'browse_id': 'UCmissing', 'selected': True}).status_code == 503
    assert len(client.get('/home/onboard/artists/state').json()['selected']) == 3


def test_catalog_failure_is_bounded_and_existing_library_is_not_forced_into_picker(store):
    calls = []
    service = artist_start.ArtistCatalog(lambda: {})
    for _ in range(2):
        with pytest.raises(artist_start.CatalogUnavailable):
            service.search('artist')
    store.settings.set_setting('last_sync_at', '999')
    seed_catalog_track(store, 'song|artist', 'Song', 'Artist', video_id='actual-song')
    assert not artist_start.required(store)


def test_cleanup_retains_explicit_artist_input_and_playable_first_set(store, monkeypatch):
    from yt_playlist.core.db_revisions import Revisions

    # This case owns real cleanup publication and its source-change fence.
    store._snapshot_revisions = Revisions(store.conn)
    monkeypatch.delattr(store, 'read_snapshot')
    service = artist_start.ArtistCatalog(lambda: {1: Catalog()})
    for index in range(3):
        service.select(store, f'UCartist{index}', True, 1000.)
    with store.conn:
        store.conn.execute('UPDATE discovered_artists SET first_shown=1001')
        store.conn.execute('UPDATE discovery_memberships SET first_shown=1001')
    expired = store.discovery.gc_discovery_pool(1000 + 40 * 86400, 30, set())
    assert expired['artists'] == expired['tracks'] == 0
    assert len(store.discovery.starter_artists()) == 3
    assert len(store.discovery.starter_tracks()) == 12
    store.discovery.prune_discovered(set(), set(), {'artist 0', 'artist 1', 'artist 2'})
    assert len(store.discovery.starter_artists()) == 3
    assert all(row['thumbnail'] for row in store.discovery.starter_artists())
    service.select(store, 'UCartist1', False, 1001.)
    expired = store.discovery.gc_discovery_pool(1000 + 40 * 86400, 30, set())
    assert expired['artists'] == 1 and expired['tracks'] == 4
    assert len(store.discovery.starter_tracks()) == 8
