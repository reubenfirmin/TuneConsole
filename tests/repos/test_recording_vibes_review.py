"""Independent checks for upgrade identity safety and repaired evidence."""
from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3

import pytest
from tests.prepared_fixture_support import roots

from tests.rec.test_vibe_prediction import capture, drain, fits, track
from tests.repos.test_recording_vibes import recording_vibe_predecessor
from tests.test_audio_vibes import sample
from yt_playlist.core import sample_files
from yt_playlist.core.db_revisions import column_group_reads
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.migrations.runner import validate_version
from yt_playlist.core.store import Store


@pytest.fixture
def legacy_recording_vibes(tmp_path, recording_vibe_predecessor):
    with ExitStack() as resources:
        def restore(name):
            directory = tmp_path / name
            directory.mkdir()
            store = Store(directory / 'legacy.db')
            resources.callback(store.conn.close)
            return store, recording_vibe_predecessor.restore(store)
        yield restore


def externalize_recording_vibes(store):
    # These alternatives belong to v61. The populated upgrade/reopen owner in
    # test_recording_vibes retains the full historical suffix and current API.
    migrate(store.conn, registry=registry.MIGRATIONS[:61])
    validate_version(store.conn, 61)
    store._bind_repositories()
    assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 61


@pytest.mark.parametrize('retain_alias', [True, False])
def test_upgrade_unknown_video_cannot_change_an_existing_text_identity(legacy_recording_vibes, retain_alias):
    store, _value = legacy_recording_vibes('identity-upgrade')
    try:
        original = tuple(store.conn.execute('SELECT id,identity_key FROM recordings').fetchone())
        extra = sample()
        extra.update(video_id='unmatched-upload', title='Known', artist='Artist')
        with store.conn:
            if not retain_alias:
                store.conn.execute('DELETE FROM recording_aliases')
            store.conn.execute('INSERT INTO audio_vibe_samples VALUES (?,?,?,?,?)',
                (extra['id'], extra['run_id'], extra['point'], json.dumps(extra), 11))
        externalize_recording_vibes(store)
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


def test_upgrade_last_good_scores_preserve_missing_times_and_contributors(legacy_recording_vibes):
    store, value = legacy_recording_vibes('last-good')
    try:
        cases = [
            ('audio_measurement', 'old-model'),
            ('provider_measurement', 'old-model'),
            ('audio_measurement', 'earlier-model'),
            ('provider_measurement', 'earlier-model'),
        ]
        recordings = []
        with store.conn:
            for index, (source, published_version) in enumerate(cases):
                if index == 0:
                    key = store.conn.execute('SELECT identity_key FROM rec_vibe_tracks').fetchone()[0]
                    excerpt = value
                else:
                    key, video = f'measurement-{index}|artist', f'measurement-{index}'
                    rid = store.conn.execute('INSERT INTO recordings(identity_key,title,artist,created_at) '
                        'VALUES (?,?,?,1)', (key, f'Measurement {index}', 'Artist')).lastrowid
                    store.conn.execute('INSERT INTO youtube_items(video_id,recording_id,title,artist) '
                        'VALUES (?,?,?,?)', (video, rid, f'Measurement {index}', 'Artist'))
                    store.conn.execute('INSERT INTO catalog_entries(recording_id,video_id) VALUES (?,?)',
                        (rid, video))
                    excerpt = sample()
                    excerpt['video_id'] = video
                    store.conn.execute('INSERT INTO audio_vibe_samples VALUES (?,?,?,?,?)',
                        (excerpt['id'], excerpt['run_id'], excerpt['point'], json.dumps(excerpt), 10))
                    evidence = {'source': 'audio_measurement', 'sample_ids': [excerpt['id']],
                                'fits': {'relax': {'score': .8, 'support': 1.}}}
                    store.conn.execute('INSERT INTO rec_vibe_tracks VALUES (?,?,?,?,?)',
                        (key, None, 'psybient', json.dumps(evidence), 'old-model'))
                    store.conn.execute('INSERT INTO audio_session_labels VALUES (?,?,?,?,?,?)',
                        (excerpt['run_id'], 'retained-taxonomy', json.dumps([excerpt['id']]), 'null', '["relax"]', 11))
                profile = {'vibes': {'inputs_version': published_version, 'fits': {
                    'relax': {'score': .17, 'support': .24, 'source': source}}}}
                store.conn.execute('INSERT OR REPLACE INTO rec_musical_profiles VALUES (?,?,?,?,?)',
                    (key, json.dumps(profile), 'old', 'digest', 1333))
                recordings.append((key, source, published_version, excerpt))
            store.conn.execute("INSERT INTO recordings(identity_key,title,artist,created_at) "
                               "VALUES ('last-good-target','Target','Other artist',1)")
            profile = {'vibes': {'fits': {'relax': {'score': .17, 'support': .24, 'source': 'genre'}}}}
            store.conn.execute('INSERT INTO rec_vibe_tracks VALUES (?,?,?,?,?)',
                               ('last-good-target', None, 'psybient', None, 'old-model'))
            store.conn.execute('INSERT INTO rec_musical_profiles VALUES (?,?,?,?,?)',
                               ('last-good-target', json.dumps(profile), 'old', 'digest', 13))
        externalize_recording_vibes(store)
        for key, source, published_version, excerpt in recordings:
            historical = store.conn.execute(
                'SELECT m.* FROM recording_vibes v JOIN measurements m ON m.id=v.measurement_id '
                'JOIN recordings r ON r.id=m.recording_id WHERE r.identity_key=?', (key,)).fetchone()
            assert (historical['score'], historical['support'], historical['source'], historical['model_version']) == (
                .17, .24, source, published_version)
            # Publication time cannot supply an unknown original measurement time.
            assert historical['measured_at'] is None
            original = store.conn.execute('SELECT score,measured_at FROM measurements WHERE recording_id=? AND id<>?',
                (historical['recording_id'], historical['id'])).fetchall()
            assert list(map(tuple, original)) == [(.8, None)]
            assert store.audio_vibes.samples(excerpt['run_id']) == [excerpt]
            owner = store.conn.execute('SELECT recording_id FROM sample_attributes WHERE id=?',
                (excerpt['id'],)).fetchone()[0]
            assert owner == historical['recording_id']
        row = store.conn.execute('SELECT p.* FROM predictions p JOIN recordings r ON r.id=p.recording_id '
                                 "WHERE r.identity_key='last-good-target'").fetchone()
        assert (row['score'], row['support'], row['predicted_at'], row['lineage_complete']) == (.17, .24, 13, 0)
        assert not store.conn.execute('SELECT 1 FROM prediction_inputs WHERE prediction_id=?', (row['id'],)).fetchone()
        assert store.conn.execute('SELECT score FROM recording_vibes WHERE prediction_id=?',
                                  (row['id'],)).fetchone()[0] == .17
        before = list(map(tuple, store.conn.execute('SELECT * FROM measurements ORDER BY id')))
        predictions = list(map(tuple, store.conn.execute('SELECT * FROM predictions ORDER BY id')))
        assert len(before) == 2 * len(cases)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        externalize_recording_vibes(store)
        assert list(map(tuple, store.conn.execute('SELECT * FROM measurements ORDER BY id'))) == before
        assert list(map(tuple, store.conn.execute('SELECT * FROM predictions ORDER BY id'))) == predictions
    finally:
        store.conn.close()


def test_v60_predecessor_copies_keep_inline_evidence_and_private_upgrade_files(
        recording_vibe_predecessor, legacy_recording_vibes):
    template = recording_vibe_predecessor
    before = list(template.source.conn.iterdump())
    first, value = legacy_recording_vibes('first')
    second, other = legacy_recording_vibes('second')
    assert value == other == template.result
    assert value is not other and value is not template.result
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        template.source.conn.execute("UPDATE recordings SET title='forbidden'")
    for consumer in (first, second):
        assert list(consumer.conn.iterdump()) == before
        assert consumer.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 60
        assert not consumer.conn.execute('PRAGMA foreign_key_check').fetchall()
    # v60 retains these samples inline. Each subsequent real upgrade must
    # externalize its own exact sample into its own managed artifact tree.
    assert template.coordinates == ()
    owned = [roots(consumer.conn) for consumer in (first, second)]
    for name, directory in template.files.items():
        assert len({directory, owned[0][name], owned[1][name]}) == 3, name
    for consumer in (first, second):
        externalize_recording_vibes(consumer)
        assert consumer.audio_vibes.samples(value['run_id']) == [value]
        assert not consumer.conn.execute('PRAGMA foreign_key_check').fetchall()
    paths = [Path(consumer.conn.execute('SELECT filename FROM sample_attributes WHERE id=?',
                                      (value['id'],)).fetchone()[0]) for consumer in (first, second)]
    assert all(sample_files.read(path) == value for path in paths)
    assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 2
    paths[0].unlink()
    with first.conn:
        first.conn.execute("UPDATE recordings SET title='private mutation'")
    value['title'] = 'private payload mutation'
    assert second.audio_vibes.samples(other['run_id']) == [other]
    assert second.conn.execute('SELECT title FROM recordings').fetchone()[0] == 'Known'
    assert list(template.source.conn.iterdump()) == before
    assert template.result == other


@pytest.mark.parametrize('storage', ['memory', 'file'])
@pytest.mark.parametrize('change', ['metadata', 'rename', 'insert', 'delete', 'rollback', 'unscoped'])
def test_identity_read_guard_keeps_membership_and_unscoped_dependencies(request, storage, change):
    store = request.getfixturevalue('store' if storage == 'memory' else 'file_backed_store')
    try:
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
