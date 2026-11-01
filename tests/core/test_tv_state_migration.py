"""TV relocation preserves provider identity and fences optional network work."""
import sqlite3

import pytest

from yt_playlist.core import tv_recommendation_state as owner
from yt_playlist.core import entity_names
from yt_playlist.core.migrations import v095_recommendation_state as migration
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.store import Store
from yt_playlist.library import tv_stream
from yt_playlist.rec import tv_artwork
from tests.test_tv_stream import stream, video


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / 'tv.db')
    migrate(result.conn, registry=registry.MIGRATIONS[:94])
    admit(result, through=94)
    try:
        yield result
    finally:
        result.conn.close()


def history(store):
    return [tuple(row) for row in store.conn.execute(
        'SELECT * FROM schema_migrations ORDER BY version')]


def admit(store, *, through):
    installed = history(store)
    assert [(row[0], row[1]) for row in installed] == [
        (entry.version, entry.name) for entry in registry.MIGRATIONS[:through]]
    store.init_schema(defer_metadata=True)
    assert history(store) == installed
    # Admission supplies current reader names without converting the retained
    # physical owners or claiming that the later entity rename has run.
    assert not store.conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='accounts'").fetchone()
    assert store.conn.execute("SELECT 1 FROM sqlite_temp_schema WHERE name='accounts'").fetchone()


def cutover(store):
    # Exercise the registered TV-owner boundary even as later migrations evolve.
    previous = history(store)
    assert len(previous) in (94, 95)
    entity_names.clear_predecessor_reads(store.conn)
    migrate(store.conn, registry=registry.MIGRATIONS[:95])
    assert history(store)[:len(previous)] == previous
    # Start the genuine installed v95 layout through ordinary admission so its
    # connection-local predecessor read views match the running application.
    admit(store, through=95)


def test_cutover_retains_exact_shapes_clocks_and_explicit_unresolved_references(store):
    values = {
        'tv_pool': {'schema': 3, 'key': 'original', 'at': 8, 'items': [video(1)], 'attempted': ['missing|artist']},
        'tv_artist_artwork': {'unknown': {'images': ['https://example.test/photo'], 'retry_at': 100}},
        'tv_artist_artwork_requests': {'unknown': 'Unknown'},
        'tv_visual_checks': {'visual00001': {'version': 2, 'verdict': 'static', 'sample_times': [20, 60]}},
        'tv_continuations': {'10:old:request:with:colons': {'playlist_id': 20, 'stream': 'next',
            'items': [{**video(1), 'key': video(1)['video_id']}]}},
    }
    for surface, value in values.items():
        store.rec_proposals.put_proposals(surface, value, None)
    cutover(store)
    for surface, value in values.items():
        assert store.rec_proposals.get_proposals(surface) == value
        assert not store.conn.execute('SELECT 1 FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
    assert tuple(store.conn.execute('SELECT recording_id,source_recording_id,source_resolution,video_resolution FROM tv_pool_items').fetchone()) == (
        None, None, 'missing_recording', 'external_video')
    assert tuple(store.conn.execute('SELECT requested_at,resolution_reason FROM tv_artist_requests').fetchone()) == (None, 'missing_artist')
    assert tuple(store.conn.execute('SELECT source_ref,target_ref,source_resolution,target_resolution,built_at FROM tv_continuation_receipts').fetchone()) == (
        None, None, 'missing_playlist', 'missing_playlist', None)
    assert store.rec_proposals.tv_static_ids(2) == {'visual00001'}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_interrupted_cutover_preserves_original_tv_rows(store):
    store.rec_proposals.put_proposals('tv_pool', {'items': [video(1)]}, 9)
    before = list(store.conn.iterdump())
    with pytest.raises(InterruptedError), store.conn.atomic():
        migration.apply(store.conn, None)
        raise InterruptedError('Before TV cutover commit')
    assert list(store.conn.iterdump()) == before


def test_artwork_stale_completion_cannot_acknowledge_a_replaced_request(store):
    store.tracks.upsert_track('video', 'Song', 'Artist', None, 100)
    cutover(store)
    assert tv_artwork.request(store, 'Artist', 10)
    observed = store.rec_proposals.tv_artwork_observation('artist')
    assert tv_artwork.request(store, 'Artist', 11)
    assert store.rec_proposals.tv_artwork_observation('artist') == observed
    with store.conn.atomic():
        store.conn.execute('DELETE FROM tv_artist_requests')
    assert tv_artwork.request(store, 'Artist', 12)
    replacement = store.rec_proposals.tv_artwork_observation('artist')
    assert replacement[0] > observed[0]
    value = {'images': ['https://example.test/photo'], 'at': 10, 'retry_at': 20}
    assert not store.rec_proposals.publish_tv_artwork('artist', value, 13, observed, complete=True)
    assert store.rec_proposals.tv_artwork_observation('artist') == replacement
    assert store.rec_proposals.publish_tv_artwork('artist', value, 14, replacement, complete=True)
    assert store.rec_proposals.get_proposals('tv_artist_artwork_requests') == {}


def test_visual_publication_merges_videos_and_rejects_invalid_measurements(store):
    cutover(store)
    value = {'version': 2, 'verdict': 'static', 'difference': .2, 'changed_fraction': .01, 'sample_times': [20, 60]}
    store.rec_proposals.publish_tv_visual('visual00001', value, 10)
    store.rec_proposals.publish_tv_visual('visual00002', value, 11)
    assert store.rec_proposals.tv_static_ids(2) == {'visual00001', 'visual00002'}
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        store.rec_proposals.publish_tv_visual('visual00001', {**value, 'changed_fraction': 2}, 12)
    assert store.rec_proposals.get_proposals('tv_visual_checks')['visual00001'] == value


@pytest.mark.parametrize('input_stream', ['', 'live'])
def test_lost_create_response_cannot_create_a_second_playlist(store, stream, monkeypatch, input_stream):
    iid, client, playlist, recipe, items = stream
    store.playlists.adopt_generated_playlist(playlist.playlist_scope_id)
    cutover(store)
    create = client.create_playlist
    def response_lost(*args, **kwargs):
        create(*args, **kwargs)
        raise ConnectionError('Provider accepted creation before response was lost')
    monkeypatch.setattr(client, 'create_playlist', response_lost)
    with pytest.raises(ConnectionError):
        tv_stream.refill(store, client, iid, playlist, recipe, input_stream, 'lost', items[7:11], 10)
    created = len(client.created)
    with pytest.raises(ValueError, match='has not confirmed'):
        tv_stream.refill(store, client, iid, playlist, recipe, input_stream, 'lost', items[7:11], 11)
    assert len(client.created) == created
    assert store.rec_proposals.tv_receipt(f'{playlist.id}:{input_stream}:lost')['pending']
    with pytest.raises(sqlite3.IntegrityError, match='must be retained'), store.conn.atomic():
        store.conn.execute('DELETE FROM tv_continuation_receipts')


def test_legacy_saved_source_receipt_with_empty_stream_migrates_and_replays(store, stream):
    import json
    from fastapi.testclient import TestClient
    from yt_playlist.rec import tv
    from yt_playlist.web.app import create_app

    iid, client, playlist, recipe, items = stream
    store.playlists.adopt_generated_playlist(playlist.playlist_scope_id)
    before = list(client._tracks[playlist.provider_playlist_id])
    store.rec_proposals.put_proposals(tv.SURFACE, {
        'schema': tv.SCHEMA, 'key': 'saved-before-cutover', 'at': 1, 'attempted': [], 'items': items}, 1)
    with TestClient(create_app(store, lambda: {iid: client}, now_fn=lambda: 10, background_work=False),
                    base_url='http://127.0.0.1') as web:
        saved = web.get(f'/home/tv/playlist/{playlist.id}')
        assert saved.status_code == 200 and not saved.json().get('stream')
        response = web.post(f'/home/tv/playlist/{playlist.id}/refill', data={
            'stream': '', 'request_id': 'saved:retry',
            'exclude': json.dumps([item['video_id'] for item in saved.json()['items']])})
        assert response.status_code == 200, response.text
    value = response.json()
    assert len(value['items']) == 4
    created = len(client.created)
    original = store.rec_proposals.get_proposals('tv_continuations')
    receipt = original[f'{playlist.id}::saved:retry']
    result = receipt['playlist_id'], receipt['stream'], receipt['items']
    assert all(item['key'] == item['video_id'] for item in result[2])
    assert [item['video_id'] for item in result[2]] == [item['video_id'] for item in value['items']]
    with pytest.raises(InterruptedError), store.conn.atomic():
        migration.apply(store.conn, None)
        raise InterruptedError('Before receipt conversion commit')
    assert store.rec_proposals.get_proposals('tv_continuations') == original
    cutover(store)
    assert tv_stream.refill(store, client, iid, playlist, recipe, '', 'saved:retry', [], 11) == result
    assert client._tracks[playlist.provider_playlist_id] == before
    assert len(client.created) == created
    assert store.playlists.get_playlist(result[0]).account_id == iid
    assert tuple(store.conn.execute('SELECT input_stream,request_id,target_stream FROM tv_continuation_receipts').fetchone()) == (
        '', 'saved:retry', result[1])
    reopened = Store(store._db_path)
    try:
        cutover(reopened)
        assert reopened.rec_proposals.get_proposals('tv_continuations') == original
        assert tv_stream.refill(reopened, client, iid, playlist, recipe, '', 'saved:retry', [], 12) == result
        with TestClient(create_app(reopened, lambda: {iid: client}, now_fn=lambda: 12, background_work=False),
                        base_url='http://127.0.0.1') as web:
            replay = web.post(f'/home/tv/playlist/{playlist.id}/refill', data={
                'stream': '', 'request_id': 'saved:retry',
                'exclude': json.dumps([item['video_id'] for item in saved.json()['items']])})
            assert replay.status_code == 200, replay.text
            assert replay.json() == value
        assert len(client.created) == created
    finally:
        reopened.conn.close()
