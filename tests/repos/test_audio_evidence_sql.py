"""Check atomic annotation publication and SQL facts for file-backed evidence."""
import sqlite3

import pytest

from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.providers.base import MetadataObservation


@pytest.mark.parametrize('run,samples,labels,taxonomy', [
    ('missing', [], ['cruise'], TAXONOMY_VERSION), ('run', [], ['cruise'], 'unknown'),
    ('run', [], ['unknown'], TAXONOMY_VERSION), ('run', ['sample'], ['cruise'], TAXONOMY_VERSION),
])
def test_invalid_annotation_is_rejected_atomically(store, run, samples, labels, taxonomy):
    store.audio_vibes.save_playback('run', 'video', 'Song', 'Artist', 1)
    store.audio_vibes.save_sample(dict(id='sample', run_id='another-run', video_id='video', point='thirty',
                                      title='Song', artist='Artist'), 1)
    with pytest.raises(ValueError):
        store.audio_vibes.save_feedback(run, samples, labels, taxonomy, None, 2)
    assert not store.conn.in_transaction
    assert not store.conn.execute("SELECT 1 FROM audio_annotations WHERE kind<>'playback'").fetchone()


def test_failed_annotation_sql_keeps_the_previous_judgment_and_retry_advances_once(store):
    store.audio_vibes.save_playback('run', 'video', 'Song', 'Artist', 1)
    store.audio_vibes.save_feedback('run', [], ['cruise'], TAXONOMY_VERSION, None, 2)
    previous = store.audio_vibes.feedback('run')
    store.conn.execute("CREATE TEMP TRIGGER fail_annotation_insert AFTER INSERT ON audio_annotations "
                       "BEGIN SELECT RAISE(ABORT,'annotation interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError, match='annotation interrupted'):
        store.audio_vibes.save_feedback('run', [], ['relax'], TAXONOMY_VERSION, {'new': True}, 3)
    assert not store.conn.in_transaction
    assert store.audio_vibes.feedback('run') == previous
    assert store.conn.execute("SELECT COUNT(*) FROM audio_annotations WHERE kind<>'playback'").fetchone()[0] == 1
    store.conn.execute('DROP TRIGGER fail_annotation_insert')
    store.audio_vibes.save_feedback('run', [], ['relax'], TAXONOMY_VERSION, {'new': True}, 4)
    store.audio_vibes.save_feedback('run', [], ['relax'], TAXONOMY_VERSION, {'new': True}, 5)
    assert store.audio_vibes.feedback('run')['vibes'] == ['relax']
    revisions = store.conn.execute("SELECT revision FROM audio_annotations WHERE kind<>'playback' ORDER BY id")
    assert [row[0] for row in revisions] == [1, 2]


def test_an_outdated_sample_receipt_plan_cannot_insert_over_a_committed_receipt(store):
    sample = dict(id='sample', run_id='run', video_id='video', point='thirty', title='Song', artist='Artist')
    repository = store.audio_vibes.repository
    observed = repository.sample_conflicts('run', 'sample')
    store.audio_vibes.save_sample(sample, 1)
    with pytest.raises(ValueError, match='already been saved'):
        repository.reserve_sample(sample, store.audio_vibes.sample_files.reference(sample), 2, expected=observed)
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
