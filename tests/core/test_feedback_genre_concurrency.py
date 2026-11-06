"""Feedback must use the committed track facet, not another writer's draft."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import genre_identity
from yt_playlist.repos.rec_query import RecQueryRepo
from yt_playlist.util.matching import identity_key
from yt_playlist.web.app import create_app
from yt_playlist.web.routes.suggest import _feedback_axis


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
@pytest.mark.parametrize('http', [False, True], ids=['resolver', 'http'])
def test_feedback_ignores_a_concurrent_rolled_back_family(installed, monkeypatch, http):
    store, _ = installed
    store.accounts.upsert_account('Main', 'credentials', None, True)
    entry = store.tracks.upsert_track('feedback-genre', 'Feedback song', 'Artist', None, 200)
    store.tracks.set_track_genre(entry, 'Synth Pop')
    key = identity_key('Feedback song', 'Artist')
    assert _feedback_axis(store, {}, 'vibe', key) == 'genre:pop'
    begin, locked, resolved = (threading.Event() for _ in range(3))
    metadata, feature_axes = RecQueryRepo.track_genres, genre_identity.feature_axes
    observed = []

    def pause_after_metadata(repo, keys):
        result = metadata(repo, keys)
        if key in keys:
            begin.set()
            assert locked.wait(5)
        return result

    def observe_family(conn, labels):
        assert conn is not store.conn or store._lock.held_by_current_thread
        value = feature_axes(conn, labels)
        if locked.is_set():
            assert conn is not store.conn
            observed.extend(family for family, _ in value.values())
            resolved.set()
        return value

    def writer():
        assert begin.wait(5)
        with store._lock:
            store.conn.execute('BEGIN IMMEDIATE')
            try:
                store.conn.execute("UPDATE genre_catalog SET family_key='jazz' WHERE key='synth pop'")
                locked.set()
                assert resolved.wait(5)
            finally:
                store.conn.rollback()

    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        monkeypatch.setattr(RecQueryRepo, 'track_genres', pause_after_metadata)
        monkeypatch.setattr(genre_identity, 'feature_axes', observe_family)
        with ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(writer)
            try:
                if http:
                    response = client.post('/recs/feedback', data={
                        'item': key, 'surface': 'suggest', 'kind': 'dismiss', 'reason': 'vibe'})
                    assert response.status_code == 200
                else:
                    axis = _feedback_axis(store, {}, 'vibe', key)
            finally:
                resolved.set()
            work.result(5)
    monkeypatch.setattr(RecQueryRepo, 'track_genres', metadata)
    monkeypatch.setattr(genre_identity, 'feature_axes', feature_axes)
    assert observed == ['pop']
    assert _feedback_axis(store, {}, 'vibe', key) == 'genre:pop'
    if http:
        assert store.rec_signals.get_theme('genre:pop') < 0
        assert not store.rec_signals.get_theme('genre:jazz')
        assert key in store.rec_model.suppressed_keys('suggest', now=1001)
    else:
        assert axis == 'genre:pop'


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
def test_feedback_reuses_the_callers_owned_transaction(installed):
    store, _ = installed
    entry = store.tracks.upsert_track('owned-feedback', 'Owned song', 'Artist', None, 200)
    store.tracks.set_track_genre(entry, 'Synth Pop')
    key = identity_key('Owned song', 'Artist')
    with store._lock:
        store.conn.execute('BEGIN IMMEDIATE')
        try:
            store.conn.execute("UPDATE genre_catalog SET family_key='jazz' WHERE key='synth pop'")
            assert _feedback_axis(store, {}, 'vibe', key) == 'genre:jazz'
            assert _feedback_axis(store, {'axis': 'genre:rock'}, 'vibe', key) == 'genre:rock'
            assert store.conn.in_transaction
        finally:
            store.conn.rollback()
    assert _feedback_axis(store, {}, 'vibe', key) == 'genre:pop'
