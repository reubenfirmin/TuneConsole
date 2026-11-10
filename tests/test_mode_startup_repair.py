"""Startup coordination and real group repair have separate bounded owners."""
from contextlib import closing
import threading

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tests.genre_catalog_support import bound_catalog
from tests.model_support import build_content_model, publish_modes
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer
from yt_playlist.core.store import Store
from yt_playlist.core.storage_maintenance import StorageMaintenance
from yt_playlist.rec import (embedding_training as training, embedding_vectors as embed,
                             mode_surfaces, model_refresh, model_selection,
                             recommendation_retirement, rec_params, rec_worker,
                             taste_modes, taste_viz)
from yt_playlist.web.app import create_app


@pytest.fixture(scope='module')
def group_schema():
    with bounded_consumer_schema((('techno', 'Techno'), ('jazz', 'Jazz'), ('rock', 'Rock'))) as schema:
        yield schema


@pytest.fixture
def native_store(tmp_path, group_schema, monkeypatch):
    bound_catalog(monkeypatch, entries=(('techno', 'Techno'), ('jazz', 'Jazz'), ('rock', 'Rock')))
    store = Store(tmp_path / 'groups.db')
    with closing(store.conn):
        # These cases own committed SQL visibility, not power-loss durability.
        store.conn.execute('PRAGMA synchronous=OFF')
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        initialize_consumer(store, group_schema)
        yield store


def seed_regions(store, per_genre):
    """Completed catalog inputs; no provider ingestion or artist inference."""
    items = [{'key': f'{genre.lower()} {index}|artist', 'video_id': f'{genre}-{index}',
              'title': f'{genre} {index}', 'artist': 'Artist', 'genre': genre}
             for genre in ('Techno', 'Jazz', 'Rock') for index in range(per_genre)]
    seed_publication_subjects(store, items)
    account = store.accounts.upsert_account('main', 'cred', None, True)
    rec_params.set_param(store, 'modes_k', 3)
    rec_params.set_param(store, 'modes_min_members', 20)
    assert build_content_model(store) == len(items)
    store.history.import_play_events(account, [
        (f'techno {index}|artist', f'Techno-{index}', 2000.) for index in range(4)])
    return items


@pytest.mark.parametrize('reason,per_genre', [('stale', 27), ('missing', 27), ('missing', 60)],
                         ids=['stale', 'missing', 'full-library-control'])
def test_real_group_repair_preserves_regions_identity_and_recent_listening(
        native_store, reason, per_genre, record_property):
    store = native_store
    items = seed_regions(store, per_genre)
    record_property('library_tracks', len(items))
    assert len(embed.load_content_vectors(store)[0]) == 3 * per_genre
    assert publish_modes(store, 1000.) == 3
    old_modes = taste_modes.live_modes(store)
    old_ids = {mode['mode_id'] for mode in old_modes}
    if reason == 'missing':
        with store.conn:
            store.conn.execute('DELETE FROM taste_modes')
        assert not store.modes.list_modes()
    else:
        old_space = embed.content_space_id(store)
        # Completed lower-layer metadata changes the actual encoder's basis.
        with store.conn:
            store.conn.execute("UPDATE youtube_items SET mb_year='2001' WHERE video_id LIKE 'Jazz-%'")
        assert build_content_model(store) == len(items)
        assert embed.content_space_id(store) != old_space
    assert not taste_viz.overview(store, 2000.)['recent']['styles_current']

    # Actual discovery, cross-space reconciliation, high-water allocation and
    # SQL replacement. No mode/fitting/publication collaborator is mocked here.
    assert publish_modes(store, 2000.) == 3
    live = taste_modes.live_modes(store)
    ids = {mode['mode_id'] for mode in live}
    if reason == 'stale':
        assert ids == old_ids
    else:
        assert ids.isdisjoint(old_ids)
        assert min(ids) > max(old_ids)
    assert {tuple(map(tuple, mode['families'])) for mode in live} == {
        tuple(map(tuple, mode['families'])) for mode in old_modes}
    assert sorted(mode['size'] for mode in live) == [per_genre] * 3
    assert all(mode['last_seen'] == 2000. for mode in live)
    assert all(np.isclose(np.linalg.norm(mode['centroid']), 1.) for mode in live)
    recent = taste_viz.overview(store, 2000.)['recent']
    assert recent['styles_current'] and recent['n'] == 4
    assert len(recent['shifts']) == 3
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_startup_keeps_taste_requests_available_until_worker_repair_finishes(native_store, monkeypatch):
    store = native_store
    items = seed_regions(store, 4)
    vectors = store.rec_model.content_vectors_by_keys([item['key'] for item in items])
    space = embed.content_space_id(store)
    # This owner receives completed lower-layer groups. The parameterized owner
    # above independently runs real discovery on both 81 and 180 recordings.
    completed = [{'mode_id': index + 1, 'label': genre.lower(),
                  'families': [(genre.lower(), 4)], 'size': 4,
                  'centroid': np.frombuffer(vectors[f'{genre.lower()} 0|artist'], dtype=np.float32),
                  'space': space,
                  'rep_keys': [f'{genre.lower()} {i}|artist' for i in range(4)]}
                 for index, genre in enumerate(('Techno', 'Jazz', 'Rock'))]
    store.settings.set_setting('enrich_worker_enabled', '0')
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    request_thread = threading.get_ident()

    def forbidden(*args, **kwargs):
        raise AssertionError('Local startup and requests must not contact a provider or fit models')

    def completed_fit(target, now, **kwargs):
        assert target is store and now == 2000.
        assert threading.get_ident() != request_thread
        calls.append('models')
        started.set()
        assert release.wait(3)
        store.modes.replace_modes(completed, [], now)
        store.rec_refresh.acknowledge(store.rec_refresh.revision())
        return {'tracks': len(items)}

    def completed_candidates(target, now):
        assert target is store and now == 2000.
        assert len(taste_modes.live_modes(store)) == 3
        calls.append('candidates')
        return {'built_at': now}

    original = rec_worker.RecWorker._do_mode_refresh

    def refresh(worker):
        try:
            original(worker)
        finally:
            finished.set()

    monkeypatch.setattr(model_refresh, 'rebuild', completed_fit)
    monkeypatch.setattr(mode_surfaces, 'prepare_scheduled', completed_candidates)
    monkeypatch.setattr(model_selection, 'maintain', lambda *args, **kwargs: None)
    monkeypatch.setattr(recommendation_retirement, 'drain', lambda *args: True)
    monkeypatch.setattr(training, 'build_vectors_from_baskets', forbidden)
    monkeypatch.setattr(rec_worker.RecWorker, 'start_ticker', lambda self: None)
    monkeypatch.setattr(rec_worker.RecWorker, 'trigger_home_refresh', lambda self, **kwargs: None)
    monkeypatch.setattr(rec_worker.RecWorker, '_prepare_groups', lambda self: None)
    monkeypatch.setattr(rec_worker.RecWorker, '_do_mode_refresh', refresh)
    # Native current SQL is already installed. Schema admission/maintenance is
    # below this lifecycle owner and has independent upgrade/failure tests.
    monkeypatch.setattr(StorageMaintenance, 'start', lambda self: self.ready.set())
    app = create_app(store, forbidden, now_fn=lambda: 2000., background_work=True)
    worker = app.state.ctx.rec_worker
    worker.debounce_s = 0
    with TestClient(app, base_url='http://127.0.0.1') as client:
        try:
            assert started.wait(3)
            assert client.get('/taste').status_code == 200
            pending = client.get('/taste/panels/overview')
            assert pending.status_code == 200
            assert 'hx-trigger="every 3s"' in pending.text
            assert 'Updating your music groups automatically' in pending.text
            assert not finished.is_set()
        finally:
            release.set()
        assert finished.wait(3)
        worker.join(3)
        assert not worker.busy
        assert calls == ['models', 'candidates']
        current = client.get('/taste/panels/overview')
        assert current.status_code == 200
        assert 'hx-trigger="every 3s"' not in current.text
        assert 'genre groups need to be updated' not in current.text
        assert taste_viz.overview(store, 2000.)['recent']['n'] == 4
