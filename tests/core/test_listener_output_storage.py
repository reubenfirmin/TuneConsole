"""The three moved file references survive collection and separate restores."""
from contextlib import closing
from pathlib import Path
import sqlite3

from tests.store_fixture_support import file_consumer_store
from yt_playlist.core import collection_recovery, model_state, recommendation_outputs
from yt_playlist.core.migrations.listener_output_storage import RETIRED
from yt_playlist.core.store import Store
from yt_playlist.repos.collection_retention import OwnershipScan


def test_moved_outputs_are_independent_and_restorable(file_consumer_store, tmp_path):
    store = file_consumer_store
    conn = store.conn
    assert not ({row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")} & set(RETIRED))

    store.rec_model.publish_model_state(
        recording=model_state.RecordingModelUpdate(encoder='{"recording":1}', generation='first'),
        artist=model_state.ArtistModelUpdate(encoder='{"artist":1}', fit_fingerprint='artist fit'))
    surface = recommendation_outputs.AUTOTUNE_SURFACE
    raw_report = '{"ran_at":1710000000.1234567}'
    store.settings.set_values({surface: raw_report})
    old_recording = store.rec_model.recording_state().reference
    artist = store.rec_model.artist_state()
    autotune = recommendation_outputs.autotune_reference(conn)

    store.rec_model.publish_model_state(recording=model_state.RecordingModelUpdate(encoder='{"recording":2}'))
    recording = store.rec_model.recording_state()
    assert recording.reference != old_recording
    assert recording.generation == 'first'
    assert store.rec_model.artist_state() == artist
    assert recommendation_outputs.autotune_reference(conn) == autotune

    model_paths = {recording.reference[0], artist.reference[0]}
    assert OwnershipScan().required(conn, model_paths | {old_recording[0]}, 128) == model_paths
    with closing(store.group_evidence.output_collector()) as collector:
        paths = [autotune['filename'], str(tmp_path / 'orphan.json')]
        assert store.group_evidence.repository.protected_output_files(paths, collector.owners) == {paths[0]}

    conn.execute('BEGIN')
    try:
        collections = collection_recovery.export(conn, tmp_path / 'collections')
        recommendations = recommendation_outputs.export(conn, tmp_path / 'recommendations')
    finally:
        conn.rollback()
    restored_path = tmp_path / 'restored.db'
    with closing(sqlite3.connect(restored_path)) as destination:
        conn.backup(destination)
    restored = Store(restored_path)
    with closing(restored.conn):
        restored.init_schema()
        collection_recovery.restore(restored.conn, collections)
        recommendation_outputs.restore(restored.conn, recommendations)
        assert restored.rec_model.recording_encoder() == '{"recording":2}'
        assert restored.rec_model.artist_encoder() == '{"artist":1}'
        assert restored.settings.get_setting(surface) == raw_report
        for expected, relocated in ((recording, restored.rec_model.recording_state()),
                                    (artist, restored.rec_model.artist_state())):
            assert expected.reference[1:] == relocated.reference[1:]
            assert Path(relocated.reference[0]).is_relative_to(collection_recovery.directory(restored.conn))
        relocated = recommendation_outputs.autotune_reference(restored.conn)
        assert Path(relocated['filename']).is_relative_to(recommendation_outputs.directory(restored.conn))
        assert {key: value for key, value in relocated.items() if key != 'filename'} == {
            key: value for key, value in autotune.items() if key != 'filename'}
