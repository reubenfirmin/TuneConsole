"""Background execution preserves the established models and last-good serving."""
import asyncio
from copy import deepcopy
import logging
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from yt_playlist.core.activity import Activity, WorkClosing
from yt_playlist.core.store import Store
from yt_playlist.rec import artist_model, embed, model_refresh, mode_surfaces, rec_params, taste_modes
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.web.app import create_app
from tests import model_support


def seed(store):
    iid = store.identities.upsert_identity('Listener', 'fixture', None, True)
    tids = []
    for i in range(96):
        tid = store.tracks.upsert_track(f'v{i}', f'Song {i:03}', f'Artist {i}', 'Album', None)
        store.tracks.set_track_genre(tid, ('Techno', 'Jazz', 'Rock')[i // 32])
        store.tracks.set_track_audio(tid, bpm=80 + i, energy=(i % 10) / 10)
        tids.append(tid)
    for i in range(8):
        pid = store.playlists.upsert_playlist(iid, f'p{i}', f'Playlist {i}', 24, '', 1)
        store.playlists.set_playlist_tracks(pid, (tids + tids)[i * 12:i * 12 + 24])
    store.history.import_play_events(iid, [(store.tracks.identity_key_for_video(f'v{i}'), f'v{i}', 1000 + i)
                                         for i in range(8)])
    store.settings.set_setting('rec_dim', '16')
    rec_params.set_param(store, 'modes_k', 3)
    rec_params.set_param(store, 'modes_min_members', 8)
    return tids


def established_pipeline(store, now):
    model_support.build_track_model(store)
    model_support.build_content_model(store)
    model_support.build_artist_model(store)
    taste_modes.recompute(store, now)
    return mode_surfaces.prepare_bundles(store, now)


@pytest.mark.parametrize('method', ['svd', 'item2vec'])
def test_background_generation_matches_established_models_and_ranked_pools(store, method):
    seed(store)
    store.settings.set_setting('rec_embed_method', method)
    expected_pools = established_pipeline(store, 2000)
    expected = {table: dict(getattr(store.rec_model, getter)()) for table, getter in (
        ('rec_vectors', 'get_rec_vectors'), ('rec_content_vectors', 'get_rec_content_vectors'),
        ('rec_artist_vectors', 'get_rec_artist_vectors'),
        ('rec_artist_content_vectors', 'get_rec_artist_content_vectors'))}
    assert expected['rec_vectors'] and expected['rec_content_vectors']
    assert expected_pools
    prepared = model_refresh.rebuild(store, 2000)
    assert prepared is not None
    for table, rows in prepared['vectors'].items():
        if table in expected:
            assert dict(rows).keys() == expected[table].keys()
            for key, blob in rows:
                np.testing.assert_allclose(np.frombuffer(blob, np.float32),
                                           np.frombuffer(expected[table][key], np.float32), atol=1e-6)
    assert mode_surfaces.prepare_bundles(store, 2000) == expected_pools
    assert store.settings.get_setting('rec_embed_method') == method
    assert store.settings.get_setting('rec_dim') == '16'
    assert not store.rec_refresh.has_pending_changes()


def test_unchanged_inputs_reuse_fits_but_metadata_and_baskets_invalidate_their_models(store, monkeypatch):
    tids = seed(store)
    assert model_refresh.rebuild(store, 2000)
    assert model_refresh.rebuild(store, 2001)['vectors'] == {}
    store.tracks.set_track_genre(tids[0], 'Classical')
    prepared = model_refresh.rebuild(store, 2002)
    assert 'rec_content_vectors' in prepared['vectors']
    assert 'rec_vectors' in prepared['vectors']  # genre baskets are collaborative inputs too
    assert not store.rec_refresh.has_pending_changes()


@pytest.mark.parametrize('change', [
    'genre', 'year', 'audio', 'membership', 'empty-library', 'missing-model',
    'genre-revision', 'missing-revision',
])
def test_production_refresh_invalidates_content_without_a_coverage_gate(store, monkeypatch, change):
    from yt_playlist.util import genre_catalog

    tid = store.tracks.upsert_track('one', 'One', 'Artist', None, None)
    store.tracks.set_track_genre(tid, 'Techno')
    assert model_refresh.rebuild(store, 1000)
    before = store.rec_model.content_generation()
    assert model_refresh.rebuild(store, 1001)['vectors'] == {}
    if change == 'genre':
        store.tracks.set_track_genre(tid, 'Folk')
    elif change == 'year':
        store.tracks.set_track_year(tid, '1995')
    elif change == 'audio':
        store.tracks.set_track_audio(tid, bpm=120)
    elif change == 'membership':
        store.tracks.upsert_track('two', 'Two', 'Artist', None, None)
    elif change == 'empty-library':
        with store.conn:
            store.conn.execute('DELETE FROM library_memberships')
    elif change == 'missing-model':
        store.settings.delete_setting('rec_content_model')
    elif change == 'genre-revision':
        monkeypatch.setattr(genre_catalog, 'REVISION', 'next-catalog')
    elif change == 'missing-revision':
        store.settings.delete_setting('rec_content_genre_revision')
    prepared = model_refresh.rebuild(store, 1002)
    assert 'rec_content_vectors' in prepared['vectors']
    after = store.rec_model.content_generation()
    assert after['generation'] != before['generation']
    assert after['revision'] == genre_catalog.REVISION
    if change == 'empty-library':
        assert after['library'] == []
    assert model_refresh.rebuild(store, 1003)['vectors'] == {}


@pytest.mark.parametrize('external', [False, True])
def test_changed_source_keeps_last_good_generation_and_notification(tmp_path, monkeypatch, external):
    store = Store(tmp_path / 'state.db')
    try:
        store.init_schema()
        tids = seed(store)
        model_refresh.rebuild(store, 2000)
        before = deepcopy(store.rec_model.content_generation())
        store.tracks.set_track_genre(tids[0], 'Classical')
        original = model_refresh.prepare
        def changed(reader, now, **kwargs):
            prepared = original(reader, now, **kwargs)
            if external:
                with sqlite3.connect(store._db_path) as writer:
                    writer.execute('UPDATE recordings SET genre=? WHERE id='
                        '(SELECT recording_id FROM catalog_entries WHERE id=?)', ('Ambient', tids[1]))
            else:
                store.tracks.set_track_genre(tids[1], 'Ambient')
            return prepared
        monkeypatch.setattr(model_refresh, 'prepare', changed)
        assert model_refresh.rebuild(store, 2001) is None
        assert store.rec_model.content_generation() == before
        assert store.rec_refresh.has_pending_changes()
    finally:
        store.conn.close()


def test_enrichment_stamps_allow_full_model_and_pool_publication(store, monkeypatch):
    seed(store)
    original_prepare = model_refresh.prepare
    original_bundles = mode_surfaces._calculate_bundles

    def stamp():
        with store.conn:
            store.conn.execute('UPDATE recordings SET first_enriched_at=COALESCE(first_enriched_at,1),'
                               'last_enriched_at=COALESCE(last_enriched_at,0)+1')

    def fitting(reader, now, **kwargs):
        result = original_prepare(reader, now, **kwargs)
        stamp()
        return result

    def ranking(reader, now, policies):
        result = original_bundles(reader, now, policies)
        stamp()
        return result

    monkeypatch.setattr(model_refresh, 'prepare', fitting)
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', ranking)
    assert model_refresh.rebuild(store, 2000) is not None
    assert not store.rec_refresh.has_pending_changes()
    assert mode_surfaces.prepare_bundles(store, 2000) is not None


def test_failed_publication_rolls_back_models_settings_clusters_and_notification(store):
    tids = seed(store)
    model_refresh.rebuild(store, 2000)
    before = store.rec_model.content_generation()
    modes = [(m['mode_id'], m['centroid'].tobytes()) for m in store.modes.list_modes()]
    store.tracks.set_track_genre(tids[0], 'Classical')
    revision = store.rec_refresh.revision()
    store.conn.execute("CREATE TRIGGER fail_training BEFORE INSERT ON rec_artist_content_vectors "
                       "BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected failure'):
        model_refresh.rebuild(store, 2001)
    store.settings.set_setting('unrelated', 'commit after failure')
    assert store.rec_model.content_generation() == before
    assert [(m['mode_id'], m['centroid'].tobytes()) for m in store.modes.list_modes()] == modes
    assert store.rec_refresh.revision() == revision


def test_coalesced_notifications_survive_restart_and_do_not_acknowledge_later_changes(tmp_path):
    path = tmp_path / 'state.db'
    store = Store(path)
    store.init_schema()
    seed(store)
    assert store.conn.execute('SELECT COUNT(*) FROM rec_change_queue').fetchone()[0] == 1
    revision = store.rec_refresh.revision()
    store.rec_refresh.enqueue()
    with store.conn:
        store.conn.execute('BEGIN')
        store.rec_refresh.acknowledge(revision)
    assert store.rec_refresh.has_pending_changes()
    store.conn.close()
    store = Store(path)
    try:
        store.init_schema()
        assert store.rec_refresh.has_pending_changes()
        assert model_refresh.rebuild(store, 2000)
        assert not store.rec_refresh.has_pending_changes()
    finally:
        store.conn.close()


def test_background_fit_leaves_event_loop_and_last_good_home_available(tmp_path, monkeypatch):
    store = Store(tmp_path / 'state.db')
    store.init_schema()
    seed(store)
    established_pipeline(store, 2000)
    app = create_app(store, lambda: {}, now_fn=lambda: 2000, background_work=False)
    ctx = SimpleNamespace(store=store, work=Activity(), now_fn=lambda: 2000,
                          logger=logging.getLogger(__name__))
    worker = RecWorker(ctx, debounce_s=0)
    started, release = threading.Event(), threading.Event()
    original = embed.build_vectors_from_baskets
    main_thread = threading.get_ident()
    def slow_fit(*args, **kwargs):
        assert threading.get_ident() != main_thread
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', slow_fit)

    async def exercise():
        worker.trigger()
        assert await asyncio.to_thread(started.wait, 5)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            for _ in range(3):
                response = await asyncio.wait_for(client.get('/home/cards'), 1)
                assert response.status_code == 200
                assert await asyncio.wait_for(asyncio.to_thread(store.rec_model.rec_vectors_count), 1) > 0
                await asyncio.sleep(0.01)
        assert not release.is_set()
    try:
        asyncio.run(exercise())
    finally:
        release.set()
        worker.shutdown()
        worker.join(5)
        store.conn.close()


def test_cancelled_fit_never_publishes_partial_models(store, monkeypatch):
    seed(store)
    before = store.rec_model.get_rec_vectors()
    def cancelled(*args, **kwargs):
        raise WorkClosing('stopped')
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', cancelled)
    with pytest.raises(WorkClosing):
        model_refresh.rebuild(store, 2000)
    assert store.rec_model.get_rec_vectors() == before
    assert store.rec_refresh.has_pending_changes()
