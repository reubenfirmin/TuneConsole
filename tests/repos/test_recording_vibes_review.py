"""Independent checks for upgrade identity safety and repaired evidence."""
import json
from pathlib import Path

import pytest

from tests.rec.test_vibe_prediction import capture, drain, fits, track
from tests.repos.test_recording_vibes import legacy
from tests.test_audio_vibes import sample
from yt_playlist.core import sample_files
from yt_playlist.core.db_revisions import column_group_reads
from yt_playlist.core.store import Store


@pytest.mark.parametrize('retain_alias', [True, False])
def test_upgrade_unknown_video_cannot_change_an_existing_text_identity(tmp_path, retain_alias):
    store, _value = legacy(tmp_path / 'identity-upgrade.db')
    try:
        original = tuple(store.conn.execute('SELECT id,identity_key FROM recordings').fetchone())
        extra = sample()
        extra.update(video_id='unmatched-upload', title='Known', artist='Artist')
        with store.conn:
            if not retain_alias:
                store.conn.execute('DELETE FROM recording_aliases')
            store.conn.execute('INSERT INTO audio_vibe_samples VALUES (?,?,?,?,?)',
                (extra['id'], extra['run_id'], extra['point'], json.dumps(extra), 11))
        store.init_schema()
        assert tuple(store.conn.execute('SELECT id,identity_key FROM recordings WHERE id=?',
                                        (original[0],)).fetchone()) == original
        moved = store.conn.execute('SELECT recording_id,filename FROM sample_attributes WHERE id=?',
                                   (extra['id'],)).fetchone()
        assert moved['recording_id'] != original[0]
        assert sample_files.read(moved['filename']) == extra
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()


def test_identical_retry_restores_measurement_after_missing_file_was_processed(store):
    _tid, original = track(store, 'original')
    _tid, target = track(store, 'target')
    values = capture(store, 'original')
    drain(store)
    before = fits(store, target)
    filename = store.conn.execute('SELECT filename FROM sample_attributes WHERE id=?',
                                  (values[0]['id'],)).fetchone()[0]
    Path(filename).unlink()
    # Another ordinary catalog change causes the worker to observe the missing
    # file. The absent file itself need not be watched by the application.
    with store.conn:
        store.conn.execute('INSERT INTO rec_content_pending(identity_key) VALUES (?)', (original,))
    drain(store)
    assert fits(store, target)['relax']['source'] == 'heuristic'
    store.audio_vibes.save_sample(values[0], 500)
    drain(store)
    assert fits(store, target) == before


def test_upgrade_last_good_prediction_does_not_invent_latest_contributors(tmp_path):
    store, _value = legacy(tmp_path / 'last-good.db')
    try:
        with store.conn:
            store.conn.execute("INSERT INTO recordings(identity_key,title,artist,created_at) "
                               "VALUES ('last-good-target','Target','Other artist',1)")
            profile = {'vibes': {'fits': {'relax': {'score': .17, 'support': .24, 'source': 'genre'}}}}
            store.conn.execute('INSERT INTO rec_vibe_tracks VALUES (?,?,?,?,?)',
                               ('last-good-target', None, 'psybient', None, 'old-model'))
            store.conn.execute('INSERT INTO rec_musical_profiles VALUES (?,?,?,?,?)',
                               ('last-good-target', json.dumps(profile), 'old', 'digest', 13))
        store.init_schema()
        row = store.conn.execute('SELECT p.* FROM predictions p JOIN recordings r ON r.id=p.recording_id '
                                 "WHERE r.identity_key='last-good-target'").fetchone()
        assert (row['score'], row['support'], row['predicted_at'], row['lineage_complete']) == (.17, .24, 13, 0)
        assert not store.conn.execute('SELECT 1 FROM prediction_inputs WHERE prediction_id=?', (row['id'],)).fetchone()
        assert store.conn.execute('SELECT score FROM recording_vibes WHERE prediction_id=?',
                                  (row['id'],)).fetchone()[0] == .17
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()


@pytest.mark.parametrize('source', ['audio_measurement', 'provider_measurement'])
@pytest.mark.parametrize('published_version', ['old-model', 'earlier-model'])
def test_upgrade_last_good_measurement_does_not_invent_its_measurement_time(
        tmp_path, source, published_version):
    store, _value = legacy(tmp_path / 'last-good-measurement.db')
    try:
        key = store.conn.execute('SELECT identity_key FROM rec_vibe_tracks').fetchone()[0]
        profile = {'vibes': {'inputs_version': published_version, 'fits': {
            'relax': {'score': .17, 'support': .24, 'source': source}}}}
        with store.conn:
            store.conn.execute('UPDATE rec_musical_profiles SET payload=?,prepared_at=? WHERE identity_key=?',
                               (json.dumps(profile), 1333, key))
        store.init_schema()
        historical = store.conn.execute(
            'SELECT m.* FROM recording_vibes v JOIN measurements m ON m.id=v.measurement_id').fetchone()
        assert (historical['score'], historical['support'], historical['source'], historical['model_version']) == (
            .17, .24, source, published_version)
        # The old profile records when it was published, without retaining the
        # original measurement time. Publication cannot supply that missing fact.
        assert historical['measured_at'] is None
        original = store.conn.execute('SELECT score,measured_at FROM measurements WHERE id<>?',
                                      (historical['id'],)).fetchone()
        assert tuple(original) == (.8, None)
        before = list(map(tuple, store.conn.execute('SELECT * FROM measurements ORDER BY id')))
        assert len(before) == 2
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        store.init_schema()
        assert list(map(tuple, store.conn.execute('SELECT * FROM measurements ORDER BY id'))) == before
    finally:
        store.conn.close()


@pytest.mark.parametrize('storage', ['memory', 'file'])
@pytest.mark.parametrize('change', ['metadata', 'rename', 'insert', 'delete', 'rollback', 'unscoped'])
def test_identity_read_guard_keeps_membership_and_unscoped_dependencies(tmp_path, storage, change):
    store = Store(':memory:' if storage == 'memory' else tmp_path / 'identity-guard.db')
    try:
        store.init_schema()
        with store.conn:
            store.conn.execute("INSERT INTO recordings(identity_key) VALUES ('original')")
        with store.read_snapshot() as snapshot:
            with column_group_reads(snapshot.store, 'recordings', 'identity'):
                assert len(snapshot.store.conn.execute('SELECT id,identity_key FROM recordings').fetchall()) == 1
            if change == 'unscoped':
                snapshot.store.conn.execute('SELECT bpm FROM recordings').fetchall()
            if change == 'insert':
                store.conn.execute("INSERT INTO recordings(identity_key) VALUES ('added')")
            elif change == 'delete':
                store.conn.execute("DELETE FROM recordings WHERE identity_key='original'")
            elif change == 'rename':
                store.conn.execute("UPDATE recordings SET identity_key='renamed'")
            else:
                store.conn.execute('UPDATE recordings SET bpm=130')
            if change == 'rollback':
                store.conn.rollback()
            else:
                store.conn.commit()
            assert snapshot.publish(lambda: None) is (change in ('metadata', 'rollback'))
    finally:
        store.conn.close()
