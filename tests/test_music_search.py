"""Native song saves retain ownership, account scope and uncertain-write safety."""
import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.home_fixture_support import without_metadata_providers
from tests.store_fixture_support import (
    bounded_consumer_schema, uncontended_file_consumer_store as store, without_query_timing,
)
from yt_playlist.rec import artist_start
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.usefixtures('without_metadata_providers', 'without_query_timing')


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture(autouse=True)
def routes(monkeypatch):
    from fastapi import APIRouter
    from yt_playlist.web import app
    from yt_playlist.web.routes import app_update, home, search
    monkeypatch.setattr(app, 'build_all', lambda ctx: [app_update.build(ctx), home.build(ctx), search.build(ctx)])
    monkeypatch.setattr(app, 'build_bridge_route', lambda ctx: APIRouter())
    monkeypatch.setattr(app, '_install_idle_shutdown', lambda *args: None)


class SongCatalog(FakeClient):
    def __init__(self):
        super().__init__()
        self.saved = set()
        self.writes = []
        self.uncertain = False
        self.missing_ack = False

    def search(self, query, filter='songs', limit=12, ignore_spelling=False):
        if filter == 'artists':
            return []
        return [dict(videoId='exact-song', title='A favorite song',
                     artists=[{'name': 'Favorite artist', 'id': 'UCfavorite'}],
                     album={'name': 'Favorite album', 'id': 'MPREfavorite'}, duration='3:42',
                     thumbnails=[{'url': 'https://example.com/song.jpg'}],
                     inLibrary='exact-song' in self.saved,
                     feedbackTokens={'add': 'private-add-token', 'remove': 'private-remove-token'})]

    def edit_song_library_status(self, tokens):
        self.writes.append(tokens)
        if self.uncertain:
            raise TimeoutError('Lost response')
        if tokens == ['private-remove-token']:
            self.saved.discard('exact-song')
        else:
            self.saved.add('exact-song')
        return {} if self.missing_ack else {'feedbackResponses': [{'isProcessed': True}]}


class EntityCatalog(SongCatalog):
    identities = {'artist': 'UCfavorite', 'album': 'MPREfavorite', 'playlist': 'VLPLfavorite'}

    def __init__(self):
        super().__init__()
        self.members = set()
        self.entity_writes = []
        self.outcome = 'confirmed'

    def search(self, query, filter='songs', **kwargs):
        if filter is not None:
            return super().search(query, filter=filter, **kwargs)
        return [dict(resultType=kind, browseId=browse, artist='Favorite artist', title='Favorite ' + kind,
                     inLibrary=kind in self.members, subscribed=kind in self.members)
                for kind, browse in self.identities.items()]

    def get_artist(self, browse):
        assert browse == 'UCfavorite'
        return dict(name='Favorite artist', channelId='UCsubscription', subscribed='artist' in self.members)

    def get_album(self, browse):
        assert browse == 'MPREfavorite'
        return dict(title='Favorite album', audioPlaylistId='OLAKfavorite',
                    artists=[{'name': 'Favorite artist', 'id': 'UCfavorite'}],
                    likeStatus='LIKE' if 'album' in self.members else 'INDIFFERENT', tracks=[])

    def get_playlist(self, playlistId, limit=100):
        if playlistId != 'PLfavorite':
            return super().get_playlist(playlistId, limit=limit)
        return dict(title='Favorite playlist', trackCount=1, tracks=[super().search('favorite')[0]])

    def get_library_playlists(self, limit=25):
        assert limit is None
        return [dict(playlistId='PLfavorite', title='Favorite playlist')] if 'playlist' in self.members else []

    def _entity_write(self, kind, target):
        from yt_playlist.integrations.youtube_music import YouTubeMusicUnavailable
        if self.outcome == 'not-dispatched':
            raise YouTubeMusicUnavailable('Not admitted', 1001., outcome='not-dispatched', mutation=True)
        self.entity_writes.append((kind, target))
        if self.outcome == 'uncertain':
            raise TimeoutError('Lost response')
        if self.outcome == 'confirmed':
            self.members.add(kind)
        return {}  # Empty acknowledgement alone never establishes success.

    def subscribe_artist(self, channelId):
        assert channelId == 'UCsubscription'  # Provider subscription identity differs from browse identity.
        return self._entity_write('artist', channelId)

    def rate_playlist(self, playlistId, rating):
        assert rating == 'LIKE'
        return self._entity_write('album' if playlistId == 'OLAKfavorite' else 'playlist', playlistId)


@pytest.mark.parametrize('kind', ['artist', 'album', 'playlist'])
def test_entity_save_confirms_native_membership_and_publishes_to_the_main_account(store, kind):
    account = store.accounts.upsert_account('main', None, True)
    other = store.accounts.upsert_account('other', None, False)
    fake, unused = EntityCatalog(), EntityCatalog()
    intent = dict(kind=kind, browse_id=fake.identities[kind])
    with TestClient(app_for(store, {account: fake, other: unused}), base_url='http://127.0.0.1') as client:
        assert client.get('/search/catalog/sections?q=favorite').status_code == 200
        assert not fake.entity_writes
        assert client.post('/search/entities/save', json=intent).json() == {'state': 'saved'}
        assert client.post('/search/entities/save', json=intent).json() == {'state': 'saved'}
        row = client.get('/search/catalog/sections?q=favorite').json()[kind + 's'][0]
        assert row['library_saved'] is True and row['can_remove'] is False
    assert len(fake.entity_writes) == 1 and not unused.entity_writes
    assert not store.actions.get_pending_actions('save_catalog_entity')
    assert not store.discovery.starter_artists()  # Subscriptions do not silently select onboarding picks.
    if kind == 'album':
        assert store.collection.saved_album_owners('MPREfavorite')['MPREfavorite'][0]['account_id'] == account
    elif kind == 'playlist':
        playlist = next(p for p in store.playlists.get_playlists() if p.provider_playlist_id == 'PLfavorite')
        assert playlist.account_id == account
        assert [t['video_id'] for t in store.charts.playlist_tracks_detail(playlist.id)] == ['exact-song']


@pytest.mark.parametrize('kind', ['artist', 'album', 'playlist'])
def test_uncertain_entity_save_survives_restart_and_never_replays(store, kind):
    account = store.accounts.upsert_account('main', None, True)
    fake = EntityCatalog()
    fake.outcome = 'uncertain'
    intent = dict(kind=kind, browse_id=fake.identities[kind])
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        assert client.post('/search/entities/save', json=intent).json()['state'] == 'pending'
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        row = client.get('/search/catalog/sections?q=favorite').json()[kind + 's'][0]
        assert row['action_state'] == 'pending'
        assert client.post('/search/entities/save', json=intent).json()['state'] == 'pending'
        if kind == 'playlist':
            assert client.post('/search/entities/save', json={**intent, 'browse_id': 'PLfavorite'}).json()['state'] == 'pending'
        fake.members.add(kind)
        assert client.post('/search/entities/save', json={**intent, 'check': True}).json()['state'] == 'saved'
    assert len(fake.entity_writes) == 1


@pytest.mark.parametrize('kind', ['artist', 'album', 'playlist'])
def test_entity_empty_ack_needs_membership_and_known_non_dispatch_allows_explicit_retry(store, kind):
    account = store.accounts.upsert_account('main', None, True)
    fake = EntityCatalog()
    intent = dict(kind=kind, browse_id=fake.identities[kind])
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        fake.outcome = 'not-dispatched'
        assert client.post('/search/entities/save', json=intent).json()['state'] == 'retry'
        fake.outcome = 'empty-ack'
        assert client.post('/search/entities/save', json=intent).json()['state'] == 'pending'
        assert client.post('/search/entities/save', json={**intent, 'check': True}).json()['state'] == 'pending'
        fake.members.add(kind)
        assert client.post('/search/entities/save', json={**intent, 'check': True}).json()['state'] == 'saved'
    assert len(fake.entity_writes) == 1


def test_entity_save_refuses_unidentified_membership_and_disconnected_main(store):
    main = store.accounts.upsert_account('main', None, True)
    other = store.accounts.upsert_account('other', None, False)
    fake = EntityCatalog()
    intent = dict(kind='artist', browse_id='UCfavorite')
    with TestClient(app_for(store, {other: fake}), base_url='http://127.0.0.1') as client:
        assert client.post('/search/entities/save', json=intent).status_code == 422
    fake.get_artist = lambda browse: {'name': 'Favorite artist', 'channelId': 'UCsubscription'}
    with TestClient(app_for(store, {main: fake}), base_url='http://127.0.0.1') as client:
        assert client.post('/search/entities/save', json=intent).status_code == 422
        assert client.post('/search/entities/save', json={**intent, 'browse_id': 'https://example.com'}).status_code == 422
    assert not fake.entity_writes


def test_confirmed_remote_entity_recovers_a_failed_local_publication_without_another_write(store):
    account = store.accounts.upsert_account('main', None, True)
    fake = EntityCatalog()
    app = app_for(store, {account: fake})
    entities = app.state.ctx.music_search().entities
    publish = entities.publish

    def interrupted(*args):
        raise OSError('Publication interrupted')

    entities.publish = interrupted
    intent = dict(kind='album', browse_id='MPREfavorite')
    with TestClient(app, base_url='http://127.0.0.1') as client:
        assert client.post('/search/entities/save', json=intent).json()['state'] == 'pending'
        assert 'album' in fake.members
        assert not store.collection.saved_album_ids()
        entities.publish = publish
        assert client.post('/search/entities/save', json={**intent, 'check': True}).json()['state'] == 'saved'
    assert len(fake.entity_writes) == 1
    assert store.collection.saved_album_ids()


def app_for(store, clients):
    return create_app(store, lambda: clients, now_fn=lambda: 1000., background_work=False)


def test_catalog_keeps_the_identified_artist_from_a_native_top_result(store):
    account = store.accounts.upsert_account('main', None, True)
    fake = SongCatalog()
    original = fake.search

    def search(query, filter='songs', **kwargs):
        if filter is not None:
            return original(query, filter=filter, **kwargs)
        # Shape observed in a read-only native search for "shpongle".
        return [dict(category='Top result', resultType='artist', subscribers='130K',
                     artists=[{'name': 'Shpongle', 'id': 'UCcN7KaN9M7ne22klvNUB7Mw'}]),
                dict(resultType='artist', browseId='UCnIasz5o3qoIllWu-SiL67w', artist='Hallucinogen')]

    fake.search = search
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        response = client.get('/search/catalog/sections?q=shpongle')
        assert response.status_code == 200
        artists = response.json()['artists']
        shpongle = next((row for row in artists if row['browse_id'] == 'UCcN7KaN9M7ne22klvNUB7Mw'), None)
        assert shpongle is not None, artists
        assert shpongle['title'] == 'Shpongle' and shpongle['can_add'] is True
        assert shpongle['href'] == '/artist?browse=UCcN7KaN9M7ne22klvNUB7Mw'
        assert shpongle['play_url'] == 'https://music.youtube.com/browse/UCcN7KaN9M7ne22klvNUB7Mw'
    assert not fake.writes and not fake.rated


@pytest.mark.parametrize('credits', [
    [], [None], ['Shpongle'], {'name': 'Shpongle', 'id': 'UCshpongle'},
    [{'name': 'Shpongle'}], [{'id': 'UCshpongle'}],
    [{'name': 'Shpongle', 'id': 'MPREwrong'}],
    [{'name': 'Shpongle', 'id': 'UCshpongle'}, {'name': 'Other artist', 'id': 'UCotherartist'}],
])
def test_top_artist_results_do_not_guess_missing_or_ambiguous_identities(credits):
    from yt_playlist.util.music_catalog import artist
    assert artist({'resultType': 'artist', 'title': 'Shpongle', 'artists': credits}) is None


def test_search_and_confirmed_save_without_liking_and_without_provider_tokens(store):
    account = store.accounts.upsert_account('main', None, True)
    fake = SongCatalog()
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        response = client.get('/search/catalog?q=favorite')
        assert response.status_code == 200
        assert response.json()['songs'][0]['owned'] is False
        assert 'private-add-token' not in response.text
        saved = client.post('/search/songs/save', json={'video_id': 'exact-song', 'query': 'favorite'})
        assert saved.json()['state'] == 'saved'
        assert client.get('/search/catalog?q=favorite').json()['songs'][0]['owned'] is True
        assert 'A favorite song' in client.get('/search/omni?q=favorite%20artist%20song').text
        # Repeated clicks and stale rendered results cannot send a second write.
        assert client.post('/search/songs/save', json={'video_id': 'exact-song', 'query': 'favorite'}).json()['state'] == 'saved'
    assert fake.writes == [['private-add-token']]
    assert fake.rated == []
    detail = store.tracks.track_detail('exact-song')
    assert detail['title'] == 'A favorite song'
    assert detail['duration_s'] == 222
    assert detail['playlists'] == []
    action = store.actions.get_actions()[0]
    assert action.status == 'executed'
    assert 'private-add-token' not in action.params_json


def test_catalog_sections_keep_native_identities_and_do_not_acquire_results(store):
    account = store.accounts.upsert_account('main', None, True)
    fake, calls = SongCatalog(), []
    original = fake.search

    def search(query, **kwargs):
        calls.append(kwargs['filter'])
        if kwargs['filter'] is not None:
            return original(query, **kwargs)
        return [
            {'resultType': 'artist', 'artist': 'Favorite artist', 'browseId': 'UCfavorite', 'shuffleId': 'RDAOshuffle'},
            {'resultType': 'artist', 'artist': 'Duplicate', 'browseId': 'UCfavorite'},
            {'resultType': 'album', 'title': 'Favorite album', 'browseId': 'MPREfavorite', 'artist': 'Favorite artist', 'year': '2000'},
            {'resultType': 'playlist', 'title': 'Evening mix', 'browseId': 'VLPLfavorite', 'author': {'name': 'Curator'}},
            {'resultType': 'video', 'videoId': 'not-a-song', 'title': 'A video'},
            {'resultType': 'album', 'browseId': 'https://example.com', 'title': 'Invalid identity'},
            {'resultType': 'artist', 'browseId': 'MPREwrong', 'artist': 'Not an artist'},
        ]

    fake.search = search
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        sections = client.get('/search/catalog/sections?q=favorite').json()
        assert [row['name'] for row in sections['artists']] == ['Favorite artist']
        assert sections['artists'][0]['play_url'] == 'https://music.youtube.com/playlist?list=RDAOshuffle'
        assert sections['artists'][0]['href'] == '/artist?browse=UCfavorite'
        assert sections['albums'][0]['play_url'] == 'https://music.youtube.com/browse/MPREfavorite'
        assert sections['albums'][0]['sub'] == 'Favorite artist · 2000'
        assert sections['playlists'][0]['play_url'] == 'https://music.youtube.com/playlist?list=PLfavorite'
        assert sections['playlists'][0]['sub'] == 'Curator'
        assert client.get('/search/catalog?q=favorite').json()['songs'][0]['video_id'] == 'exact-song'
        assert client.get('/search/catalog/sections?q=favorite').json() == sections
    assert calls == [None, 'songs']
    assert not store.tracks.library_identity_key_for_video('exact-song')
    assert not fake.writes and not fake.rated


def test_toggle_removes_standalone_save_but_blocks_playlist_owned_removal(store):
    account = store.accounts.upsert_account('main', None, True)
    fake = SongCatalog()
    intent = {'video_id': 'exact-song', 'query': 'favorite'}
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        assert client.post('/search/songs/save', json=intent).json()['state'] == 'saved'
        assert client.post('/search/songs/save', json={**intent, 'saved': False}).json()['state'] == 'removed'
        assert not store.tracks.library_identity_key_for_video('exact-song')
        assert 'A favorite song' not in client.get('/search/omni?q=favorite%20artist%20song').text
        assert client.post('/search/songs/save', json=intent).json()['state'] == 'saved'
        playlist = store.playlists.upsert_playlist(account, 'PLowned', 'Keep this playlist', 1, '', 999.)
        store.playlists.set_playlist_tracks(playlist, [store.tracks.track_detail('exact-song')['id']])
        writes = list(fake.writes)
        result = client.post('/search/songs/save', json={**intent, 'saved': False}).json()
        assert result['state'] == 'saved' and result['can_remove'] is False
        assert store.tracks.library_identity_key_for_video('exact-song')
        assert len(store.charts.playlist_tracks_detail(playlist)) == 1
        result = client.get('/search/catalog?q=favorite').json()['songs'][0]
        assert result['owned'] is True and result['library_saved'] is True and result['can_remove'] is False
        assert fake.writes == writes
    assert not fake.rated


@pytest.mark.parametrize('provider_saved', [False, None])
@pytest.mark.parametrize('owner', ['playlist', 'liked', 'album'])
def test_local_song_ownership_overrides_false_or_missing_native_membership(store, provider_saved, owner):
    account = store.accounts.upsert_account('main', None, True)
    fake = SongCatalog()
    original = fake.search

    def search(*args, **kwargs):
        rows = original(*args, **kwargs)
        if provider_saved is None:
            rows[0].pop('inLibrary')
        return rows

    fake.search = search
    track = store.tracks.upsert_track('exact-song', 'A favorite song', 'Favorite artist')
    key = store.tracks.library_identity_key_for_video('exact-song')
    if owner == 'album':
        store.collection.add_saved_album({'browse': 'MPREfavorite', 'title': 'Favorite album',
                                          'artist': 'Favorite artist'}, account_id=account)
        store.collection.cache_album_tracklist('MPREfavorite', 'Favorite album', 'Favorite artist',
                                              '', [key], entries=[{'entry_id': track}])
    else:
        playlist = store.playlists.upsert_playlist(account, 'LM' if owner == 'liked' else 'PLowned',
                                                  'Liked Music' if owner == 'liked' else 'My songs', 1, '', 999.)
        store.playlists.set_playlist_tracks(playlist, [track])
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        row = client.get('/search/catalog?q=favorite').json()['songs'][0]
        assert row['owned'] is True and row['library_saved'] is provider_saved and row['can_remove'] is False
        result = client.post('/search/songs/save', json={
            'video_id': 'exact-song', 'query': 'favorite', 'saved': False}).json()
        assert result['state'] == 'saved' and result['can_remove'] is False
    assert store.tracks.library_identity_key_for_video('exact-song') == key
    assert not fake.writes and not fake.rated and not store.actions.get_pending_actions('save_song')


@pytest.mark.parametrize('provider_saved', [False, None])
def test_album_bookmark_uses_saved_release_identity_and_excludes_cached_albums(store, provider_saved):
    account = store.accounts.upsert_account('main', None, True)
    fake = EntityCatalog()
    original = fake.search

    def search(query, filter='songs', **kwargs):
        if filter is not None:
            return original(query, filter=filter, **kwargs)
        rows = [dict(resultType='album', browseId=browse, title='Same album title')
                for browse in ['MPREfavorite', 'MPREcached', 'MPREanother']]
        if provider_saved is False:
            for row in rows:
                row['inLibrary'] = False
        return rows

    fake.search = search
    store.collection.add_saved_album({'browse': 'MPREfavorite', 'title': 'Same album title',
                                      'artist': 'Favorite artist'}, account_id=account)
    store.collection.ensure_album_identity('MPREcached')
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        rows = client.get('/search/catalog/sections?q=favorite').json()['albums']
    assert [(row['browse_id'], row['owned']) for row in rows] == [
        ('MPREfavorite', True), ('MPREcached', False), ('MPREanother', False)]
    assert all(row['library_saved'] is provider_saved for row in rows)
    assert not fake.entity_writes and not fake.writes


@pytest.mark.parametrize('phase', ['search', 'dispatch', 'pending'])
def test_removal_rechecks_ownership_acquired_while_the_action_is_running(store, phase):
    account = store.accounts.upsert_account('main', None, True)
    fake = SongCatalog()
    intent = {'video_id': 'exact-song', 'query': 'favorite'}
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        assert client.post('/search/songs/save', json=intent).json()['state'] == 'saved'

        def retain():
            playlist = store.playlists.upsert_playlist(account, 'PLnew', 'New owner', 1, '', 1001.)
            store.playlists.set_playlist_tracks(playlist, [store.tracks.track_detail('exact-song')['id']])

        if phase == 'search':
            original = fake.search

            def search(*args, **kwargs):
                retain()
                return original(*args, **kwargs)

            fake.search = search
        elif phase == 'dispatch':
            original = fake.edit_song_library_status

            def write(tokens):
                retain()
                return original(tokens)

            fake.edit_song_library_status = write
        else:
            fake.uncertain = True
            assert client.post('/search/songs/save', json={**intent, 'saved': False}).json()['state'] == 'pending'
            retain()
            assert client.post('/search/songs/save', json={**intent, 'check': True}).json()['state'] == 'pending'
            fake.saved.clear()
        result = client.post('/search/songs/save', json={**intent, 'saved': False}).json()
        assert result['state'] == 'saved' and result['owned'] is True and result['can_remove'] is False
    assert store.tracks.library_identity_key_for_video('exact-song')
    assert len(fake.writes) == (1 if phase == 'search' else 2)
    assert not fake.rated and not store.actions.get_pending_actions('save_song')


def test_uncertain_removal_only_reconciles_after_restart(store):
    account = store.accounts.upsert_account('main', None, True)
    fake = SongCatalog()
    intent = {'video_id': 'exact-song', 'query': 'favorite'}
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        assert client.post('/search/songs/save', json=intent).json()['state'] == 'saved'
        fake.uncertain = True
        assert client.post('/search/songs/save', json={**intent, 'saved': False}).json()['state'] == 'pending'
    assert store.tracks.library_identity_key_for_video('exact-song')
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        row = client.get('/search/catalog?q=favorite').json()['songs'][0]
        assert row['action_state'] == 'pending' and row['pending_saved'] is False
        assert client.post('/search/songs/save', json={**intent, 'saved': False}).json()['state'] == 'pending'
        fake.saved.clear()
        assert client.post('/search/songs/save', json={**intent, 'check': True}).json()['state'] == 'removed'
    assert not store.tracks.library_identity_key_for_video('exact-song')
    assert fake.writes == [['private-add-token'], ['private-remove-token']]


def test_interrupted_song_admission_keeps_the_onboarding_draft_and_reconciles_without_replay(store, monkeypatch):
    account = store.accounts.upsert_account('main', None, True)
    store.settings.set_setting('last_sync_at', '999')
    fake = SongCatalog()
    intent = {'video_id': 'exact-song', 'query': 'favorite', 'onboarding': True}

    def interrupted(*args):
        raise OSError('Pick publication interrupted')

    with monkeypatch.context() as patch:
        patch.setattr(artist_start, 'remember_song', interrupted)
        with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
            assert client.post('/search/songs/save', json=intent).json()['state'] == 'pending'
    assert store.tracks.library_identity_key_for_video('exact-song')
    assert artist_start.required(store)
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        assert client.post('/search/songs/save', json={**intent, 'check': True}).json()['state'] == 'saved'
        state = client.get('/home/onboard/artists/state').json()
        assert [row['video_id'] for row in state['saved_songs']] == ['exact-song']
        assert state['ready'] and not state['complete']
    assert fake.writes == [['private-add-token']]


def test_known_non_dispatched_save_can_be_retried(store):
    from yt_playlist.integrations.youtube_music import YouTubeMusicUnavailable

    account = store.accounts.upsert_account('main', None, True)
    fake = SongCatalog()
    write = fake.edit_song_library_status

    def refuse(tokens):
        raise YouTubeMusicUnavailable('Not admitted', 1001., outcome='not-dispatched', mutation=True)

    fake.edit_song_library_status = refuse
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        intent = {'video_id': 'exact-song', 'query': 'favorite'}
        assert client.post('/search/songs/save', json=intent).json()['state'] == 'retry'
        fake.edit_song_library_status = write
        assert client.post('/search/songs/save', json=intent).json()['state'] == 'saved'
    assert fake.writes == [['private-add-token']]


@pytest.mark.parametrize('refinement_available', [True, False])
def test_mixed_query_ranks_original_artist_above_covers_and_recovers_missing_title(store, refinement_available):
    account = store.accounts.upsert_account('main', None, True)
    fake = SongCatalog()
    calls = []

    def search(query, **kwargs):
        calls.append(query)
        if query == '"my life" Bon Jovi':
            if not refinement_available:
                raise TimeoutError('Refinement unavailable')
            return [dict(videoId='bon-original', title='It’s My Life', artists=[{'name': 'Bon Jovi'}]),
                    dict(videoId='bon-remix', title="It's My Life (Remix)", artists=[{'name': 'Bon Jovi'}])]
        return [dict(videoId='cover-song', title="It's My Life (Made famous by Bon Jovi)", artists=[{'name': 'Heaven is Shining'}]),
                dict(videoId='bon-other', title='Raise Your Hands', artists=[{'name': 'Bon Jovi'}])]

    fake.search = search
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        rows = client.get('/search/catalog?q=bon%20jovi%20my%20life').json()['songs']
    if refinement_available:
        assert [row['video_id'] for row in rows[:2]] == ['bon-original', 'bon-remix']
    else:
        assert {row['video_id'] for row in rows} == {'cover-song', 'bon-other'}
    assert calls == ['bon jovi my life', '"my life" Bon Jovi']


def test_unknown_save_survives_restart_and_only_reconciles(store):
    account = store.accounts.upsert_account('main', None, True)
    fake = SongCatalog()
    fake.uncertain = True
    intent = {'video_id': 'exact-song', 'query': 'favorite'}
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        assert client.post('/search/songs/save', json=intent).json()['state'] == 'pending'
    assert not store.tracks.library_identity_key_for_video('exact-song')
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        row = client.get('/search/catalog?q=favorite').json()['songs'][0]
        assert row['action_state'] == 'pending' and row['pending_saved'] is True
        assert client.post('/search/songs/save', json=intent).json()['state'] == 'pending'
        fake.saved.add('exact-song')  # the original remote request eventually becomes observable
        assert client.post('/search/songs/save', json={**intent, 'check': True}).json()['state'] == 'saved'
    assert len(fake.writes) == 1
    assert store.tracks.library_identity_key_for_video('exact-song')
    assert not store.actions.get_pending_actions('save_song')


def test_catalog_pending_save_matches_both_account_and_upload(store):
    account = store.accounts.upsert_account('main', None, True)
    other = store.accounts.upsert_account('other', None, False)
    for owner, video in [(other, 'exact-song'), (account, 'other-upload')]:
        params = dict(account_id=owner, query='favorite', song={'video_id': video}, onboarding=False, saved=False)
        store.actions.record_action('save_song', json.dumps(params), '{}', 'pending', '{}', 1000.)
    fake = SongCatalog()
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        row = client.get('/search/catalog?q=favorite').json()['songs'][0]
        assert row['video_id'] == 'exact-song'
        assert 'action_state' not in row and 'pending_saved' not in row
    assert not fake.writes and len(store.actions.get_pending_actions('save_song')) == 2


def test_song_only_onboarding_keeps_the_draft_and_builds_exact_first_set(store):
    from yt_playlist.library.playlist_generated import create_generated_playlist

    account = store.accounts.upsert_account('main', None, True)
    store.settings.set_setting('last_sync_at', '999')
    fake = SongCatalog()
    fake.missing_ack = True
    assert artist_start.required(store)
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        result = client.post('/search/songs/save', json={
            'video_id': 'exact-song', 'query': 'favorite', 'onboarding': True})
        assert result.json()['state'] == 'saved'
        state = client.get('/home/onboard/artists/state').json()
        assert state['selected'] == [] and state['ready'] and not state['complete']
        assert [row['video_id'] for row in state['saved_songs']] == ['exact-song']
        assert 'Start with songs or artists you love.' in client.get('/home/onboard/start').text
        assert 'data-vid="exact-song"' in client.get('/home/onboard/artists/listen').text
    proto = artist_start.first_set(store, 1000)
    assert [row['video_id'] for row in proto['tracks']] == ['exact-song']
    assert artist_start.required(store)
    fake._catalog['exact-song'] = fake.search('favorite')[0]
    generated = create_generated_playlist(store, 'My first set', proto['tracks'], fake, 1001., account)
    store.history.record_play_event(account, proto['tracks'][0]['key'], 'exact-song', 1002.,
                                    playlist_ytm_id=generated['new_ytm'])
    assert not artist_start.required(store)
    store.tracks.upsert_track('exact-song', 'A favorite song', 'Favorite artist', available=False)
    assert artist_start.saved_songs(store) == []
    assert not artist_start.required(store)


def test_catalog_never_writes_through_another_connected_account(store):
    main = store.accounts.upsert_account('main', None, True)
    other = store.accounts.upsert_account('other', None, False)
    fake = SongCatalog()
    with TestClient(app_for(store, {other: fake}), base_url='http://127.0.0.1') as client:
        assert client.get('/search/catalog?q=favorite').status_code == 503
        assert client.post('/search/songs/save', json={'video_id': 'exact-song', 'query': 'favorite'}).status_code == 422
    assert fake.writes == []


def test_query_cannot_supply_a_save_token_or_an_unreturned_video(store):
    account = store.accounts.upsert_account('main', None, True)
    fake = SongCatalog()
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        assert client.post('/search/songs/save', json={
            'video_id': 'another-upload', 'query': 'favorite'}).json()['state'] == 'unavailable'
    assert fake.writes == []


def test_native_song_sync_imports_ownership_without_inventing_ratings_and_retries_independently(store):
    from yt_playlist.core.album_snapshot import PlaylistSnapshot
    from yt_playlist.library.sync import sync_all
    from yt_playlist.library.sync_outcomes import sync_due

    account = store.accounts.upsert_account('main', None, True)
    fake = SongCatalog()
    fake.get_library_playlists = lambda limit=None: PlaylistSnapshot()
    reads = []
    failing = False

    def saved_songs(limit=None):
        reads.append(limit)
        if failing:
            raise TimeoutError('Native song listing unavailable')
        return [{**fake.search('favorite')[0], 'likeStatus': 'LIKE'}]

    fake.get_library_songs = saved_songs
    result = sync_all(store, {account: fake}, 1000.)
    assert result['status'] == 'complete'
    assert store.tracks.library_identity_key_for_video('exact-song')
    assert store.tracks.item_rating(account, 'exact-song') is None
    progress = store.sync.state()['accounts'][str(account)]['resources']['saved_songs']
    assert progress['status'] == 'complete' and progress['last_success_at'] == 1000.
    failing = True
    result = sync_all(store, {account: fake}, 1100.)
    assert result['status'] == 'partial'
    assert result['accounts'][str(account)]['resources']['saved_songs']['status'] == 'failed'
    progress = store.sync.state()['accounts'][str(account)]['resources']['saved_songs']
    assert progress['last_success_at'] == 1000. and progress['retry_at'] > 1100.
    assert not sync_due(store, 1101., 86400.)
    sync_all(store, {account: fake}, 1101., automatic=True)
    assert reads == [None, None]
    assert store.tracks.library_identity_key_for_video('exact-song')
    failing = False
    assert sync_due(store, progress['retry_at'], 86400.)
    repaired = sync_all(store, {account: fake}, progress['retry_at'], automatic=True)
    assert repaired['status'] == 'complete' and len(reads) == 3
    assert not fake.rated


@pytest.mark.parametrize('when', ['read', 'pending', 'import', 'partial'])
def test_native_snapshot_cannot_restore_a_later_confirmed_removal(store, monkeypatch, when):
    from yt_playlist.controllers.library_synchronization import _sync_saved_songs

    account = store.accounts.upsert_account('main', None, True)
    fake = SongCatalog()
    intent = {'video_id': 'exact-song', 'query': 'favorite'}
    removal = {**intent, 'saved': False}
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        assert client.post('/search/songs/save', json=intent).json()['state'] == 'saved'
        if when == 'pending':
            fake.uncertain = True
            assert client.post('/search/songs/save', json=removal).json()['state'] == 'pending'
            fake.uncertain = False

        def remove():
            if when == 'pending':
                fake.saved.clear()
            assert client.post('/search/songs/save', json={**removal, 'check': when == 'pending'}).json()['state'] == 'removed'
            assert not store.tracks.library_identity_key_for_video('exact-song')

        def captured_listing(limit=None):
            captured = fake.search('favorite')
            if when in ('read', 'pending'):
                remove()
            return captured + ([{'videoId': 'missing-title'}] if when == 'partial' else [])

        original = store.tracks.upsert_track

        def import_after_removal(*args, **kwargs):
            remove()
            return original(*args, **kwargs)

        fake.get_library_songs = captured_listing
        with monkeypatch.context() as patch:
            if when in ('import', 'partial'):
                patch.setattr(store.tracks, 'upsert_track', import_after_removal)
            outcome, auth_bad = _sync_saved_songs(store, account, fake, 1001.)
        assert outcome['status'] == ('failed' if when == 'partial' else 'complete')
        assert not auth_bad
        assert not store.tracks.library_identity_key_for_video('exact-song')
        fake.get_library_songs = lambda limit=None: []
        _sync_saved_songs(store, account, fake, 1002.)
        assert not store.tracks.library_identity_key_for_video('exact-song')
        assert 'A favorite song' not in client.get('/search/omni?q=favorite%20artist%20song').text

        # A later independent native add is fresh evidence and remains importable.
        fake.saved.add('exact-song')
        fake.get_library_songs = lambda limit=None: fake.search('favorite')
        _sync_saved_songs(store, account, fake, 1003.)
        assert store.tracks.library_identity_key_for_video('exact-song')
    assert not fake.rated


def test_exact_song_and_artist_picks_combine_without_three_artist_requirement(store):
    from tests.test_artist_start import Catalog

    account = store.accounts.upsert_account('main', None, True)
    store.settings.set_setting('last_sync_at', '999')
    with TestClient(app_for(store, {account: SongCatalog()}), base_url='http://127.0.0.1') as client:
        assert client.post('/search/songs/save', json={
            'video_id': 'exact-song', 'query': 'favorite', 'onboarding': True}).json()['state'] == 'saved'
    artists = artist_start.ArtistCatalog(lambda: {account: Catalog()})
    artists.choose(store, 'UCartist0', True, 1001.)
    state = artists.state(store)
    assert state['ready'] and len(state['selected']) == 1 and not state['complete']
    tracks = artist_start.first_set(store, 1002.)['tracks']
    assert tracks[0]['video_id'] == 'exact-song'
    assert any(row['video_id'] == 'song00' for row in tracks)
    assert len({row['video_id'] for row in tracks}) == len(tracks)


def test_liking_still_admits_a_song_through_liked_music(store):
    from yt_playlist.library.ops import PlaylistOps

    account = store.accounts.upsert_account('main', None, True)
    fake = SongCatalog()
    fake._catalog['exact-song'] = fake.search('favorite')[0]
    store.tracks.observe_track_entry('exact-song', 'A favorite song', 'Favorite artist', now=999.)
    liked = store.playlists.upsert_playlist(account, 'LM', 'Liked Music', 0, '', 999.)
    assert not store.tracks.library_identity_key_for_video('exact-song')
    PlaylistOps(store, lambda: {account: fake}, lambda: 1000.).set_rating('exact-song', 'LIKE', acquisition={
        'title': 'A favorite song', 'artist': 'Favorite artist',
        'identity_key': store.tracks.identity_key_for_video('exact-song')})
    assert store.tracks.library_identity_key_for_video('exact-song')
    assert [row['video_id'] for row in store.charts.playlist_tracks_detail(liked)] == ['exact-song']
    assert store.tracks.item_rating(account, 'exact-song')['status'] == 'LIKE'
    with TestClient(app_for(store, {account: fake}), base_url='http://127.0.0.1') as client:
        assert client.get('/search/catalog?q=favorite').json()['songs'][0]['owned'] is True
    assert not fake.writes
