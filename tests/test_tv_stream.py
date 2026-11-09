"""TV continuations use confirmed, bounded playlists and replay-safe writes."""
import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.home_fixture_support import home_routes_only, without_metadata_providers
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema, file_consumer_store as store
from yt_playlist.library import tv_stream
from yt_playlist.library.playlist_generated import create_generated_playlist
from yt_playlist.rec import tv
from yt_playlist.web.app import create_app
from yt_playlist.util.matching import identity_key


pytestmark = pytest.mark.usefixtures('home_routes_only', 'without_metadata_providers')


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


def test_tv_stream_input_keeps_empty_receipts_and_real_guards(_pristine_current_schema):
    assert _pristine_current_schema.execute('PRAGMA query_only').fetchone()[0] == 1
    assert _pristine_current_schema._revision_script
    assert not _pristine_current_schema.execute('SELECT 1 FROM tv_continuation_receipts').fetchone()
    assert not _pristine_current_schema.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(autouse=True)
def catalog_inputs(request, monkeypatch):
    if 'store' not in request.fixturenames:
        return
    database = request.getfixturevalue('store')
    install_catalog_inputs(database, monkeypatch)


def install_catalog_inputs(database, monkeypatch):
    """Supply completed playable subjects beneath stream ordering and receipts."""
    def ingest(video_id, title, artist, album=None, duration_s=None, *, thumbnail=None, library=True):
        existing = database.conn.execute('SELECT id FROM catalog_entries WHERE video_id=?', (video_id,)).fetchone()
        if existing:
            return existing[0]
        key = identity_key(title, artist)
        item = dict(key=key, video_id=video_id, title=title, artist=artist, album=album, duration_s=duration_s)
        seed_publication_subjects(database, [item], unowned=() if library else (key,))
        return database.conn.execute('SELECT id FROM catalog_entries WHERE video_id=?', (video_id,)).fetchone()[0]

    # TV owns confirmed membership, ordering, receipts and replay safety. Keep
    # those SQL/files real; supply completed catalog subjects below the separate
    # provider-metadata capture and artist-inference boundary.
    monkeypatch.setattr(database.tracks, 'upsert_track', ingest)


def video(index, duration=200):
    return {'video_id': f'visual{index:05}', 'title': f'Video {index}', 'artist': f'Artist {index}',
            'source_artist': f'Artist {index}', 'source_key': f'source-{index}', 'duration': duration,
            'kind': 'Music video', 'group': tv.GROUPS[index % 3]}


@pytest.fixture
def stream(store):
    iid = store.accounts.upsert_account('TV account', 'cred', None, True)
    items = [video(i) for i in range(60)]
    client = FakeClient(catalog={t['video_id']: {'videoId': t['video_id'], 'title': t['title'],
                        'artists': [{'name': t['artist']}]} for t in items})
    recipe = {'lane': 'tv', 'media': 'video', 'tv_stream': 'live', 'max_minutes': 15, 'videos': items[:7]}
    result = create_generated_playlist(store, 'TV', items[:7], client, 1, iid, recipe=recipe)
    playlist = store.playlists.get_playlist(result['pid'])
    return iid, client, playlist, recipe, items


def test_multiple_batches_pass_seven_and_replays_never_duplicate_writes(store, stream):
    iid, client, playlist, recipe, items = stream
    for index in range(7, 27, 4):
        recipe = store.playlist_recipes.get_recipe(playlist.playlist_scope_id)
        result = tv_stream.refill(store, client, iid, playlist, recipe, 'live', str(index), items[index:index+4], index)
        assert result == (playlist.id, 'live', items[index:index+4])
        writes, draws = len(client.added), tv.draw_number(store)
        assert tv_stream.refill(store, client, iid, playlist, recipe, 'live', str(index), [], index) == result
        assert len(client.added) == writes and tv.draw_number(store) == draws
        assert [t['video_id'] for t in store.charts.playlist_tracks_detail(playlist.id)] == [
            t['video_id'] for t in items[:index+4]]
    assert len(client.created) == 1


def test_pending_append_is_confirmed_later_without_replaying_it(store, stream, monkeypatch):
    from yt_playlist.library import tv_launch

    iid, client, playlist, recipe, items = stream
    read = client.get_playlist
    lagging = [False]
    def lagged_read(pid, limit=None):
        if lagging[0]:
            return {'tracks': list(client._tracks[pid][:7])}
        return read(pid, limit)
    add = client.add_playlist_items
    def uncertain(pid, videos, **kwargs):
        add(pid, videos, **kwargs)
        lagging[0] = True
        raise ConnectionError('response lost after delivery')
    monkeypatch.setattr(client, 'get_playlist', lagged_read)
    monkeypatch.setattr(client, 'add_playlist_items', uncertain)
    with pytest.raises(tv_stream.PendingTV):
        tv_stream.refill(store, client, iid, playlist, recipe, 'live', 'pending', items[7:11], 2)
    assert len(store.charts.playlist_tracks_detail(playlist.id)) == 7
    writes = len(client.added)
    lagging[0] = False
    recipe = store.playlist_recipes.get_recipe(playlist.playlist_scope_id)
    result = tv_stream.refill(store, client, iid, playlist, recipe, 'live', 'pending', items[11:15], 3)
    assert result[2] == items[7:11]
    assert len(client.added) == writes
    assert len(store.charts.playlist_tracks_detail(playlist.id)) == 11

    # Initial launches have their own durable intent before creation, including
    # a lost create receipt and partial writes with never-attempted later videos.
    monkeypatch.setattr(client, 'get_playlist', read)
    original_create = client.create_playlist
    for failure in ('partial', 'create'):
        calls = []
        selected = items[20:32]
        lagging[0] = True

        def partial(pid, videos, **kwargs):
            calls.append(list(videos))
            if len(videos) > 1 or videos == [selected[1]['video_id']]:
                return {'status': 'STATUS_FAILED'}
            result = add(pid, videos, **kwargs)
            if videos == [selected[2]['video_id']]:
                raise ConnectionError('second successful addition lost its receipt')
            return result

        def partial_read(pid, limit=None):
            result = read(pid, limit)
            return {**result, 'tracks': result['tracks'][:1]} if lagging[0] else result

        def unknown_create(title, description):
            original_create(title, description)
            raise ConnectionError('committed create lost its receipt')

        monkeypatch.setattr(client, 'add_playlist_items', partial if failure == 'partial' else add)
        monkeypatch.setattr(client, 'get_playlist', partial_read)
        monkeypatch.setattr(client, 'create_playlist', unknown_create if failure == 'create' else original_create)
        with pytest.raises(tv_stream.PendingTV):
            tv_launch.start(store, {iid: client}, failure, selected, 10)
        retained = store.tv.launch(failure)
        count = (len(client.created), len(client.added))
        with pytest.raises(tv_stream.PendingTV):
            tv_launch.start(store, {iid: client}, failure, items[40:52], 11)
        assert store.tv.launch(failure) == retained
        assert (len(client.created), len(client.added)) == count
        lagging[0] = False
        if failure == 'partial':
            assert retained['outcome'] == {
                'confirmed': [selected[0]['video_id']], 'rejected': [selected[1]['video_id']],
                'uncertain': [selected[2]['video_id']], 'unattempted': [t['video_id'] for t in selected[3:]]}
            pid, token = tv_launch.start(store, {iid: client}, failure, [], 12)
            assert [t['video_id'] for t in store.charts.playlist_tracks_detail(pid)] == [
                selected[0]['video_id'], selected[2]['video_id']]
            assert tv_launch.start(store, {iid: client}, failure, [], 13) == (pid, token)
        else:
            assert retained['provider_playlist_id'] is None
            with pytest.raises(tv_stream.PendingTV):
                tv_launch.start(store, {iid: client}, failure, [], 12)
        assert (len(client.created), len(client.added)) == count
    # A separate explicit session keeps its own request, without rebinding the
    # unknown-create intent or repeating its provider call.
    monkeypatch.setattr(client, 'create_playlist', original_create)
    pid, _ = tv_launch.start(store, {iid: client}, 'intentional-new-session', items[40:52], 14)
    assert len(store.charts.playlist_tracks_detail(pid)) == 12
    assert store.tv.launch('create')['provider_playlist_id'] is None
    assert store.tv.launch('create')['state'] == 'pending'


@pytest.mark.parametrize('rollover', ['saved', 'full'])
def test_saved_sources_and_full_streams_roll_to_a_new_bounded_list(store, stream, rollover):
    iid, client, playlist, recipe, items = stream
    if rollover == 'saved':
        store.playlists.adopt_generated_playlist(playlist.playlist_scope_id)
    else:
        items += [video(i) for i in range(60, 64)]
        client._catalog.update({t['video_id']: {'videoId': t['video_id'], 'title': t['title']}
                                for t in items[60:]})
        tv_stream.refill(store, client, iid, playlist, recipe, 'live', 'fill', items[7:60], 2)
        recipe = store.playlist_recipes.get_recipe(playlist.playlist_scope_id)
    before = list(client._tracks[playlist.provider_playlist_id])
    next_items = items[-4:]
    result = tv_stream.refill(store, client, iid, playlist, recipe, 'live', 'roll', next_items, 3)
    assert result[0] != playlist.id
    assert store.playlists.get_playlist(result[0]).account_id == iid
    assert client._tracks[playlist.provider_playlist_id] == before
    created = len(client.created)
    assert tv_stream.refill(store, client, iid, playlist, recipe, 'live', 'roll', next_items, 4) == result
    assert len(client.created) == created
    assert len(store.rec_proposals.get_proposals('tv_continuations')) <= 60


def test_remote_edits_are_preserved_instead_of_appending_blindly(store, stream):
    iid, client, playlist, recipe, items = stream
    client._tracks[playlist.provider_playlist_id].reverse()
    writes = len(client.added)
    with pytest.raises(ValueError, match='changed elsewhere'):
        tv_stream.refill(store, client, iid, playlist, recipe, 'live', 'edited', items[7:11], 2)
    assert len(client.added) == writes


def test_refill_route_uses_playlist_account_and_confirmed_videos(store, stream):
    iid, client, playlist, recipe, items = stream
    other = store.accounts.upsert_account('another', 'cred2', None, False)
    wrong = FakeClient()
    store.rec_proposals.put_proposals(tv.SURFACE, {
        'schema': tv.SCHEMA, 'key': 'pool', 'at': 1, 'attempted': [], 'items': items}, 1)
    app = create_app(store, lambda: {other: wrong, iid: client}, now_fn=lambda: 2)
    with TestClient(app, base_url='http://127.0.0.1') as web:
        data = {'request_id': 'next', 'stream': 'live', 'exclude': json.dumps([t['video_id'] for t in items[:7]])}
        response = web.post(f'/home/tv/playlist/{playlist.id}/refill', data=data)
        assert response.status_code == 200, response.text
        assert len(response.json()['items']) == 4
        assert not wrong.added and not wrong.created
        assert web.post(f'/home/tv/playlist/{playlist.id}/refill', data=data).json() == response.json()
        assert len(client.added) == 2


@pytest.mark.parametrize('include_stream', [True, False], ids=['empty-stream', 'absent-stream'])
def test_saved_playlist_get_continues_once_with_the_browser_stream_token(store, stream, include_stream):
    from copy import deepcopy
    iid, client, playlist, recipe, items = stream
    store.playlists.adopt_generated_playlist(playlist.playlist_scope_id)
    original = (deepcopy(client._tracks[playlist.provider_playlist_id]),
                store.charts.playlist_tracks_detail(playlist.id),
                store.playlist_recipes.get_recipe(playlist.playlist_scope_id),
                store.playlists.playlist_provenance(playlist.playlist_scope_id))
    store.rec_proposals.put_proposals(tv.SURFACE, {
        'schema': tv.SCHEMA, 'key': 'saved-pool', 'at': 1, 'attempted': [], 'items': items}, 1)
    with TestClient(create_app(store, lambda: {iid: client}, now_fn=lambda: 2, background_work=False),
                    base_url='http://127.0.0.1') as web:
        saved = web.get(f'/home/tv/playlist/{playlist.id}')
        assert saved.status_code == 200, saved.text
        assert not saved.json().get('stream')
        data = {'request_id': 'saved-next',
                'exclude': json.dumps([t['video_id'] for t in saved.json()['items']])}
        if include_stream:
            data['stream'] = saved.json().get('stream') or ''
        response = web.post(f'/home/tv/playlist/{playlist.id}/refill', data=data)
        assert response.status_code == 200, response.text
        value = response.json()
        assert value['playlist_id'] != playlist.id and value['stream']
        assert len(value['items']) == 4
        videos = [t['video_id'] for t in value['items']]
        target = store.playlists.get_playlist(value['playlist_id'])
        assert target.account_id == iid
        assert store.playlists.is_quarantined_playlist(target.playlist_scope_id)
        assert [t['video_id'] for t in store.charts.playlist_tracks_detail(target.id)] == videos
        assert [t['videoId'] for t in client._tracks[target.provider_playlist_id]] == videos
        writes = (len(client.created), len(client.added), len(client.edited), len(client.removed))
        assert writes[0] == 2
        assert web.post(f'/home/tv/playlist/{playlist.id}/refill', data=data).json() == value
        assert (len(client.created), len(client.added), len(client.edited), len(client.removed)) == writes
    assert (client._tracks[playlist.provider_playlist_id],
            store.charts.playlist_tracks_detail(playlist.id),
            store.playlist_recipes.get_recipe(playlist.playlist_scope_id),
            store.playlists.playlist_provenance(playlist.playlist_scope_id)) == original


def test_unlimited_persists_and_admits_a_video_longer_than_two_hours(store, monkeypatch):
    from yt_playlist.rec import account_growth

    iid = store.accounts.upsert_account('main', 'cred', None, True)
    item = video(0, duration=3 * 3600)
    client = FakeClient(catalog={item['video_id']: {'videoId': item['video_id'], 'title': item['title']}})
    store.rec_proposals.put_proposals(tv.SURFACE, {
        'schema': tv.SCHEMA, 'key': 'long', 'at': 1, 'attempted': [], 'items': [item]}, 1)
    with TestClient(create_app(store, lambda: {iid: client}, now_fn=lambda: 1),
                    base_url='http://127.0.0.1') as web:
        assert not web.post('/home/tv', data={'max_minutes': '120'}).json()['ready']
        assert web.post('/home/tv', data={'max_minutes': 'unlimited'}).json() == {'ready': True, 'max_minutes': None}
        assert store.settings.get_setting(tv.SETTING) == 'unlimited'
        # Downstream output alone cannot unlock TV. Isolate the subsequent
        # duration check at the post-unlock boundary, keeping the real writer.
        assert web.post('/home/tv/play').status_code == 409
        assert not client.created and not client.added
        with monkeypatch.context() as unlocked:
            unlocked.setattr(account_growth, 'view', lambda *_args, **_kwargs: {'ready_areas': {'tv': True}})
            response = web.post('/home/tv/play')
        assert response.status_code == 200, response.text
        assert response.json()['items'][0]['duration'] == 10800
        saved = store.playlists.get_playlist(response.json()['playlist_id'])
        assert store.playlist_recipes.get_recipe(saved.playlist_scope_id)['max_minutes'] is None
        assert web.get('/home/tv').json()['max_minutes'] is None
        assert not web.post('/home/tv', data={'max_minutes': '1'}).json()['ready']
        with pytest.raises(ValueError, match='maximum track length'):
            create_generated_playlist(store, 'Music', [item], client, 1, iid)
