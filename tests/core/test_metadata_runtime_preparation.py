"""Preparation can restart, refuses stale cutover and leaves ordinary work alone."""
import json
import time

import pytest

from yt_playlist.core import catalog, metadata_native, metadata_runtime, metadata_runtime_files
from yt_playlist.core.metadata_observations import MetadataObservation
from yt_playlist.core.preparation import metadata_runtime as preparation
from yt_playlist.core.store import Store


def seeded(tmp_path):
    store = Store(tmp_path / 'old.db')
    store.init_schema()
    entry = store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    now = time.time()
    store.enrichment.apply_provider_fields(entry, 'a', {'genre':'Jazz','bpm':120}, providers=('a',))
    store.enrichment_state.record_enrichment_attempt('song|artist', 'a', {'genre':'Jazz','bpm':120}, video_id='video', now=now)
    with store.conn:
        catalog.choose_field(store.conn, entry, 'bpm', None, now=now)
        catalog.choose_field(store.conn, entry, 'bpm', 130, now=now+1)
        catalog.choose_field(store.conn, entry, 'genre', 'Jazz', now=now+2)
    store.enrichment.upsert_conflict(entry, 'genre', [{'provider':'a','value':'Jazz'},{'provider':'b','value':'Rock'}], now=now)
    store.enrichment_state.record_metadata_observations(entry, 'native', [MetadataObservation('mood_party', .8,
        'recording', 'native-id', 'prediction', unit='classifier_probability')], run_id='native', now=now)
    return store, entry


def run(store, path):
    for _ in range(100):
        result = preparation.step(store.conn, path, batch_size=1)
        if result['complete']:
            return result
    raise AssertionError('Preparation failed to finish')


def test_preparation_preserves_real_reader_outputs_queues_and_reopens(tmp_path):
    store, entry = seeded(tmp_path)
    path = tmp_path / 'progress.json'
    try:
        catalog_before = [tuple(row) for row in store.conn.execute('SELECT * FROM catalog_tracks ORDER BY id')]
        genre_before = store.enrichment_reports.genre_provenance(entry)
        key = store.conn.execute('SELECT source_key FROM rec_musical_sources').fetchone()[0]
        native_before = metadata_native.source_values(store.conn, key)
        queues = {name:list(map(tuple, store.conn.execute('SELECT * FROM '+name))) for name in
                  ('rec_change_queue','rec_content_pending','rec_musical_pending','enrichment_provider_jobs')}
        result = run(store, path)
        assert result['counts']['choices'] == 2
        assert not metadata_runtime.active(store.conn)
        preparation.activate(store.conn, path)
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM catalog_tracks ORDER BY id')] == catalog_before
        assert metadata_native.source_values(store.conn, key) == native_before
        assert store.enrichment_reports.genre_provenance(entry) == genre_before
        assert len(store.enrichment_reports.metadata_provenance(entry)['decisions']) == 3
        for name, before in queues.items():
            assert list(map(tuple, store.conn.execute('SELECT * FROM '+name))) == before
        store.init_schema()
        assert metadata_runtime.active(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        store.conn.close()


def test_committed_batch_with_lost_progress_can_be_replayed(tmp_path, monkeypatch):
    store, _ = seeded(tmp_path)
    path = tmp_path / 'progress.json'
    try:
        run(store, path)
        state = json.loads(path.read_bytes())
        state.update(stage=2, cursor=None)
        path.write_text(json.dumps(state))
        counts = store.conn.execute('SELECT id,revision FROM metadata_runtime_choices ORDER BY id').fetchall()
        run(store, path)
        assert list(map(tuple, counts)) == list(map(tuple, store.conn.execute('SELECT id,revision FROM metadata_runtime_choices ORDER BY id')))
        preparation.activate(store.conn, path)
    finally:
        store.conn.close()


def test_continuing_installed_writes_refuse_stale_activation(tmp_path):
    store, entry = seeded(tmp_path)
    path = tmp_path / 'progress.json'
    try:
        preparation.step(store.conn, path, batch_size=1)
        with store.conn:
            catalog.choose_field(store.conn, entry, 'bpm', 140)
        run(store, path)
        with pytest.raises(ValueError, match='source changed'):
            preparation.activate(store.conn, path)
        assert not metadata_runtime.active(store.conn)
        assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0] == 140
    finally:
        store.conn.close()


def test_preparation_retains_newer_identical_native_delivery_freshness(tmp_path):
    store = Store(tmp_path/'native.db')
    store.init_schema()
    try:
        entry = store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
        observation = MetadataObservation('mood_party', .8, 'recording', 'native-id', 'prediction',
                                          unit='classifier_probability')
        for name, at in (('first', 10), ('newest', 30)):
            store.enrichment_state.record_metadata_observations(entry, 'native', [observation], run_id=name, now=at)
        progress = tmp_path/'progress.json'
        run(store, progress)
        preparation.activate(store.conn, progress)
        late = MetadataObservation('mood_party', .1, 'recording', 'native-id', 'prediction',
                                   unit='classifier_probability')
        store.enrichment_state.record_metadata_observations(entry, 'native', [late], run_id='late', now=20)
        key = store.conn.execute('SELECT source_key FROM rec_musical_sources').fetchone()[0]
        assert metadata_native.source_values(store.conn, key)['mood_party']['value'] == .8
        with pytest.raises(ValueError, match='cannot change its result'):
            store.enrichment_state.record_metadata_observations(entry, 'native', [late], run_id='newest', now=30)
    finally:
        store.conn.close()
