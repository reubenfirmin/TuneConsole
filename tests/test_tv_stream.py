"""TV continuations use confirmed, bounded playlists and replay-safe writes."""
import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.library import tv_stream
from yt_playlist.library.playlist_generated import create_generated_playlist
from yt_playlist.rec import tv
from yt_playlist.web.app import create_app


def video(index, duration=200):
    return {'video_id': f'visual{index:05}', 'title': f'Video {index}', 'artist': f'Artist {index}',
            'source_artist': f'Artist {index}', 'source_key': f'source-{index}', 'duration': duration,
            'kind': 'Music video', 'group': tv.GROUPS[index % 3]}


@pytest.fixture
def stream(store):
    iid = store.identities.upsert_identity('TV account', 'cred', None, True)
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
        recipe = store.playlist_recipes.get_recipe(playlist.scope_ref)
        result = tv_stream.refill(store, client, iid, playlist, recipe, 'live', str(index), items[index:index+4], index)
        assert result == (playlist.id, 'live', items[index:index+4])
        writes, draws = len(client.added), tv.draw_number(store)
        assert tv_stream.refill(store, client, iid, playlist, recipe, 'live', str(index), [], index) == result
        assert len(client.added) == writes and tv.draw_number(store) == draws
        assert [t['video_id'] for t in store.charts.playlist_tracks_detail(playlist.id)] == [
            t['video_id'] for t in items[:index+4]]
    assert len(client.created) == 1


def test_pending_append_is_confirmed_later_without_replaying_it(store, stream, monkeypatch):
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
    recipe = store.playlist_recipes.get_recipe(playlist.scope_ref)
    result = tv_stream.refill(store, client, iid, playlist, recipe, 'live', 'pending', items[11:15], 3)
    assert result[2] == items[7:11]
    assert len(client.added) == writes
    assert len(store.charts.playlist_tracks_detail(playlist.id)) == 11


@pytest.mark.parametrize('rollover', ['saved', 'full'])
def test_saved_sources_and_full_streams_roll_to_a_new_bounded_list(store, stream, rollover):
    iid, client, playlist, recipe, items = stream
    if rollover == 'saved':
        store.playlists.adopt_generated_playlist(playlist.scope_ref)
    else:
        items += [video(i) for i in range(60, 64)]
        client._catalog.update({t['video_id']: {'videoId': t['video_id'], 'title': t['title']}
                                for t in items[60:]})
        tv_stream.refill(store, client, iid, playlist, recipe, 'live', 'fill', items[7:60], 2)
        recipe = store.playlist_recipes.get_recipe(playlist.scope_ref)
    before = list(client._tracks[playlist.ytm_playlist_id])
    next_items = items[-4:]
    result = tv_stream.refill(store, client, iid, playlist, recipe, 'live', 'roll', next_items, 3)
    assert result[0] != playlist.id
    assert store.playlists.get_playlist(result[0]).identity_id == iid
    assert client._tracks[playlist.ytm_playlist_id] == before
    created = len(client.created)
    assert tv_stream.refill(store, client, iid, playlist, recipe, 'live', 'roll', next_items, 4) == result
    assert len(client.created) == created
    assert len(store.rec_proposals.get_proposals('tv_continuations')) <= 60


def test_remote_edits_are_preserved_instead_of_appending_blindly(store, stream):
    iid, client, playlist, recipe, items = stream
    client._tracks[playlist.ytm_playlist_id].reverse()
    writes = len(client.added)
    with pytest.raises(ValueError, match='changed elsewhere'):
        tv_stream.refill(store, client, iid, playlist, recipe, 'live', 'edited', items[7:11], 2)
    assert len(client.added) == writes


def test_refill_route_uses_playlist_account_and_confirmed_videos(store, stream):
    iid, client, playlist, recipe, items = stream
    other = store.identities.upsert_identity('another', 'cred2', None, False)
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


def test_unlimited_persists_and_admits_a_video_longer_than_two_hours(store):
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    item = video(0, duration=3 * 3600)
    client = FakeClient(catalog={item['video_id']: {'videoId': item['video_id'], 'title': item['title']}})
    store.rec_proposals.put_proposals(tv.SURFACE, {
        'schema': tv.SCHEMA, 'key': 'long', 'at': 1, 'attempted': [], 'items': [item]}, 1)
    with TestClient(create_app(store, lambda: {iid: client}, now_fn=lambda: 1),
                    base_url='http://127.0.0.1') as web:
        assert not web.post('/home/tv', data={'max_minutes': '120'}).json()['ready']
        assert web.post('/home/tv', data={'max_minutes': 'unlimited'}).json() == {'ready': True, 'max_minutes': None}
        assert store.settings.get_setting(tv.SETTING) == 'unlimited'
        response = web.post('/home/tv/play')
        assert response.status_code == 200, response.text
        assert response.json()['items'][0]['duration'] == 10800
        saved = store.playlists.get_playlist(response.json()['playlist_id'])
        assert store.playlist_recipes.get_recipe(saved.scope_ref)['max_minutes'] is None
        assert web.get('/home/tv').json()['max_minutes'] is None
        assert not web.post('/home/tv', data={'max_minutes': '1'}).json()['ready']
        with pytest.raises(ValueError, match='maximum track length'):
            create_generated_playlist(store, 'Music', [item], client, 1, iid)
