"""File publication stays outside SQL while receipt and annotation writes remain guarded."""
import sqlite3
from pathlib import Path

import pytest

from yt_playlist.core import audio_annotation_files
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.providers.base import MetadataObservation


def test_annotation_publication_is_safe_from_collection_without_holding_a_sql_writer(
        file_backed_store, monkeypatch):
    store = file_backed_store
    store.audio_vibes.save_playback('run', 'video', 'Song', 'Artist', 1)
    repository = store.audio_vibes.repository
    publish = repository.publish_annotation
    other = sqlite3.connect(store._db_path, timeout=0)

    def collecting(*args, **kwargs):
        assert not store.conn.in_transaction
        filename, _digest = kwargs['reference']
        assert audio_annotation_files.collect_orphans(other) == 0
        assert Path(filename).is_file()
        return publish(*args, **kwargs)

    monkeypatch.setattr(repository, 'publish_annotation', collecting)
    try:
        store.audio_vibes.save_feedback('run', [], ['cruise'], TAXONOMY_VERSION, {'original': True}, 2)
        assert store.audio_vibes.feedback('run')['prediction'] == {'original': True}
        assert audio_annotation_files.collect_orphans(other) == 0
    finally:
        other.close()


def test_failed_annotation_sql_keeps_the_previous_judgment_and_retry_advances_once(store):
    store.audio_vibes.save_playback('run', 'video', 'Song', 'Artist', 1)
    store.audio_vibes.save_feedback('run', [], ['cruise'], TAXONOMY_VERSION, None, 2)
    previous = store.audio_vibes.feedback('run')
    store.conn.execute("CREATE TEMP TRIGGER fail_annotation_seal BEFORE UPDATE OF sealed ON audio_annotations "
                       "WHEN NEW.sealed=1 BEGIN SELECT RAISE(ABORT,'annotation interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError, match='annotation interrupted'):
        store.audio_vibes.save_feedback('run', [], ['relax'], TAXONOMY_VERSION, {'new': True}, 3)
    assert not store.conn.in_transaction
    assert store.audio_vibes.feedback('run') == previous
    assert store.conn.execute('SELECT COUNT(*) FROM audio_annotations').fetchone()[0] == 1
    # The failed publication releases its file lease; its unreferenced bytes can
    # expire independently, while the previous judgment remains readable.
    assert audio_annotation_files.collect_orphans(store.conn, memory_key=store._evidence_key) == 1
    store.conn.execute('DROP TRIGGER fail_annotation_seal')
    store.audio_vibes.save_feedback('run', [], ['relax'], TAXONOMY_VERSION, {'new': True}, 4)
    store.audio_vibes.save_feedback('run', [], ['relax'], TAXONOMY_VERSION, {'new': True}, 5)
    assert store.audio_vibes.feedback('run')['vibes'] == ['relax']
    assert [tuple(row) for row in store.conn.execute('SELECT revision,sealed FROM audio_annotations ORDER BY id')] == [
        (1, 1), (2, 1)]


def test_an_outdated_sample_receipt_plan_cannot_insert_over_a_committed_receipt(store):
    sample = dict(id='sample', run_id='run', video_id='video', point='thirty', title='Song', artist='Artist')
    repository = store.audio_vibes.repository
    observed = repository.sample_conflicts('run', 'sample')
    store.audio_vibes.save_sample(sample, 1)
    with pytest.raises(ValueError, match='already been saved'):
        repository.save_sample(sample, 'outdated-file-reference', 2, expected=observed)
    assert store.conn.execute('SELECT COUNT(*) FROM sample_attributes').fetchone()[0] == 1
    assert store.audio_vibes.samples('run') == [sample]


def test_metadata_repository_returns_references_without_reading_provider_files(store, monkeypatch):
    from yt_playlist.core import metadata_delivery_files, metadata_runtime_files
    entry = store.tracks.upsert_track('video', 'Song', 'Artist', '', 240)
    store.enrichment_state.record_metadata_observations(entry, 'acousticbrainz', [MetadataObservation(
        'mood_aggressive', .8, 'recording', 'recording-mbid', 'prediction',
        match_evidence={'method': 'supplied_recording_mbid'})], run_id='provider', now=1)

    def forbidden(*args, **kwargs):
        pytest.fail('The SQL repository must return a detached metadata reference')

    with monkeypatch.context() as patch:
        patch.setattr(metadata_delivery_files, 'read', forbidden)
        patch.setattr(metadata_runtime_files, 'read', forbidden)
        patch.setattr(metadata_runtime_files, 'read_at', forbidden)
        inputs = store.audio_vibes.repository.base_metadata_inputs('video')
    assert inputs['metadata']['entry_id'] == entry
    assert len(inputs['sources']) == 1
    assert store.audio_vibes.base_metadata('video')['mood_aggressive'] == .8
