"""Human annotation history is constrained independently of product evidence."""
import json
from pathlib import Path
import sqlite3

import pytest

from yt_playlist.core import audio_annotation_files as files, audio_annotations as annotations
from yt_playlist.core.migrations import Migration, MigrationError, migrate, registry
from yt_playlist.core.migrations import v066_explicit_keys, v067_audio_annotations


VERSION = 'audio-session-targets-1'
UPGRADES = (Migration(66, 'explicit_logical_keys', v066_explicit_keys.apply, rebuild_tables=True),
            Migration(67, 'audio_annotation_relations', v067_audio_annotations.apply))


def upgrade(conn, migration=UPGRADES[1]):
    migrate(conn, registry=(*registry.MIGRATIONS[:65], UPGRADES[0], migration))


def prediction():
    labels = v067_audio_annotations.VOCABULARIES[VERSION]
    return {'version': 'frozen-model-1', 'taxonomy_version': VERSION, 'vibes': ['relax'],
            'ranking': [{'vibe': label, 'score': round(.9 - i * .1, 2)} for i, label in enumerate(labels)],
            'model': {'sha256': 'a' * 64, 'feature_version': 'features-1', 'training_songs': 9,
                      'trained_labels': ['relax', 'cruise']},
            'arc': {'opaque': ['original', 'output']}}


@pytest.fixture
def predecessor(tmp_path):
    conn = sqlite3.connect(tmp_path / 'annotations.db')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    migrate(conn, registry=(*registry.MIGRATIONS[:65], UPGRADES[0]))
    for i, run in enumerate(('real', 'other', 'missing'), 1):
        conn.execute('INSERT INTO recordings(id,identity_key) VALUES (?,?)', (i, run))
        conn.execute('INSERT INTO youtube_items(video_id,recording_id) VALUES (?,?)', (run, i))
        conn.execute('INSERT INTO sample_attributes VALUES (?,?,?,?,?,?,?)',
                     (run + '-sample', i, run, run, 'thirty', '/unused-controlled-fixture.json', i))
        if run != 'missing':
            conn.execute('INSERT INTO audio_vibe_runs VALUES (?,?,?,?,?)', (run, run, run, 'Artist', i))
    conn.execute('INSERT INTO audio_session_labels VALUES (?,?,?,?,?,?)',
                 ('real', VERSION, '["real-sample"]', json.dumps(prediction()), '["relax","cruise"]', 10))
    conn.execute('INSERT INTO audio_session_labels VALUES (?,?,?,?,?,?)',
                 ('missing', VERSION, '["missing-sample","gone","other-sample"]', 'null', '[]', 11))
    conn.execute('INSERT INTO audio_vibe_labels VALUES (?,?,?,?,?,?)',
                 ('real', 'audio-vibe-targets-1', '["legacy-missing"]', '{"version":"old"}', '["gentle","move"]', 8))
    conn.execute('INSERT INTO audio_vibe_feedback VALUES (?,?,?,?,?)', ('missing', '[]', 'null', '', 7))
    conn.commit()
    yield conn
    conn.close()


def latest(conn, run='real', *, kind='session', version=VERSION):
    return annotations.latest(conn, run, kind=kind, taxonomy_version=version)


def test_legacy_meanings_missing_runs_and_unresolved_samples_are_retained(predecessor):
    conn = predecessor
    upgrade(conn)
    assert conn.execute('SELECT COUNT(*) FROM audio_vibe_runs').fetchone()[0] == 2
    assert conn.execute('SELECT COUNT(*) FROM audio_annotations WHERE sealed=1').fetchone()[0] == 4
    missing = annotations.read(conn, latest(conn, 'missing'))
    assert missing['run_id'] is None and missing['legacy_run_key'] == 'missing'
    assert missing['resolution_status'] == 'legacy_missing_run'
    assert missing['state'] == 'empty' and missing['vibes'] == []
    assert missing['sample_ids'] == ['missing-sample']
    assert missing['unresolved_sample_count'] == 2
    assert missing['unresolved_samples'] == [
        {'position': 1, 'sample_id': 'gone', 'reason': 'missing_sample'},
        {'position': 2, 'sample_id': 'other-sample', 'reason': 'wrong_run'}]
    document = files.read(missing['model_output_filename'])
    assert json.loads(document['legacy']['sample_ids']) == ['missing-sample', 'gone', 'other-sample']
    earlier = annotations.read(conn, latest(conn, kind='development', version='audio-vibe-targets-1'))
    assert earlier['vibes'] == ['gentle', 'move']
    assert earlier['prediction'] == {'version': 'old'}
    assert conn.execute("SELECT status FROM audio_taxonomies WHERE taxonomy_version='audio-vibe-targets-1'").fetchone()[0] == 'legacy_observed'
    cleared = annotations.read(conn, latest(conn, 'missing', kind='feedback', version='audio-feedback-presets-1'))
    assert cleared['state'] == 'withdrawn' and cleared['vibes'] == []
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name IN "
                            "('audio_session_labels','audio_vibe_labels','audio_vibe_feedback')").fetchall()


def test_typed_evaluation_and_opaque_original_output_round_trip_separately(predecessor):
    conn = predecessor
    upgrade(conn)
    row = latest(conn)
    original = prediction()
    assert annotations.read(conn, row)['prediction'] == original
    facts = annotations.evaluation(conn, row['id'])
    assert facts == {key: original[key] for key in ('version', 'taxonomy_version', 'vibes', 'ranking', 'model')}
    rows, earlier = annotations.training_rows(conn, VERSION)
    assert earlier == 2
    assert rows[0]['labels'] == ['relax', 'cruise'] and rows[0]['evaluation'] == facts
    assert rows[1]['run_id'] == 'missing' and rows[1]['video_id'] == 'missing'
    assert rows[1]['evaluation'] is None


def test_revisions_keep_labeled_empty_and_withdrawn_judgments(predecessor):
    conn = predecessor
    upgrade(conn)
    first = latest(conn)['id']
    with conn:
        empty = annotations.save(conn, 'real', ['real-sample'], [], VERSION, prediction(), 12)
    with conn:
        withdrawn = annotations.save(conn, 'real', [], [], VERSION, None, 13, state='withdrawn')
    history = [tuple(row) for row in conn.execute(
        "SELECT id,revision,state,supersedes_id FROM audio_annotations WHERE kind='session' AND run_id='real' ORDER BY revision")]
    assert history == [(first, 1, 'labeled', None), (empty, 2, 'empty', first), (withdrawn, 3, 'withdrawn', empty)]
    assert latest(conn)['id'] == withdrawn
    assert conn.execute('SELECT COUNT(*) FROM audio_annotation_vibes WHERE annotation_id=?', (first,)).fetchone()[0] == 2
    for statement, args in (
            ('UPDATE audio_annotations SET state=\'empty\' WHERE id=?', (first,)),
            ('DELETE FROM audio_annotations WHERE id=?', (withdrawn,)),
            ('DELETE FROM audio_annotation_vibes WHERE annotation_id=?', (first,)),
            ('UPDATE audio_annotation_samples SET position=9 WHERE annotation_id=?', (first,))):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement, args)
        conn.rollback()


@pytest.mark.parametrize('samples,labels,version,run', [
    (['gone'], ['relax'], VERSION, 'real'),
    (['other-sample'], ['relax'], VERSION, 'real'),
    (['real-sample', 'real-sample'], ['relax'], VERSION, 'real'),
    ([], ['gentle'], VERSION, 'real'),
    ([], ['relax', 'relax'], VERSION, 'real'),
    ([], [], 'unregistered-taxonomy', 'real'),
    (['missing-sample'], ['relax'], VERSION, 'missing'),
])
def test_new_writers_reject_invalid_memberships_without_fabricating_runs(predecessor, samples, labels, version, run):
    conn = predecessor
    upgrade(conn)
    before = conn.execute('SELECT COUNT(*) FROM audio_annotations').fetchone()[0]
    with pytest.raises(ValueError), conn:
        annotations.save(conn, run, samples, labels, version, None, 12)
    assert conn.execute('SELECT COUNT(*) FROM audio_annotations').fetchone()[0] == before
    assert conn.execute('SELECT COUNT(*) FROM audio_vibe_runs').fetchone()[0] == 2


def test_sample_and_run_retention_rejects_deletion_and_reassociation(predecessor):
    conn = predecessor
    upgrade(conn)
    for sql in ("DELETE FROM sample_attributes WHERE id='real-sample'",
                "UPDATE sample_attributes SET run_id='other' WHERE id='real-sample'",
                "UPDATE sample_attributes SET video_id='other',recording_id=2 WHERE id='real-sample'",
                "DELETE FROM audio_vibe_runs WHERE run_id='real'",
                "UPDATE audio_vibe_runs SET video_id='other' WHERE run_id='real'"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
        conn.rollback()
    with conn:
        conn.execute("INSERT INTO audio_vibe_runs VALUES ('missing','missing','Missing','Artist',12)")
        annotations.resolve_run(conn, 'missing')
    assert latest(conn, 'missing')['resolution_status'] == 'resolved'
    assert latest(conn, 'missing', kind='feedback', version='audio-feedback-presets-1')['run_id'] == 'missing'
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_database_checks_membership_updates_taxonomy_and_single_feedback_label(predecessor):
    conn = predecessor
    upgrade(conn)
    digest = 'a' * 64
    with pytest.raises(sqlite3.IntegrityError, match='revision'):
        conn.execute('INSERT INTO audio_annotations('
            'run_id,kind,taxonomy_version,revision,state,created_at,sealed,resolution_status,'
            'model_output_filename,model_output_digest,file_format) '
            "VALUES ('other','session',?,1,'labeled',12,1,'resolved',?,?,'audio-annotation-1')",
            (VERSION, '/' + digest + '.json', digest))
    conn.rollback()
    with conn:
        annotation_id = conn.execute('INSERT INTO audio_annotations('
            'run_id,kind,taxonomy_version,revision,state,created_at,resolution_status,'
            'model_output_filename,model_output_digest,file_format) '
            "VALUES ('other','session',?,1,'labeled',12,'resolved',?,?,'audio-annotation-1')",
            (VERSION, '/' + digest + '.json', digest)).lastrowid
        conn.execute('INSERT INTO audio_annotation_samples VALUES (?,?,0)', (annotation_id, 'other-sample'))
        for sql, args in (
                ('INSERT INTO audio_annotation_samples VALUES (?,?,1)', (annotation_id, 'gone')),
                ('UPDATE audio_annotation_samples SET sample_id=? WHERE annotation_id=?', ('real-sample', annotation_id)),
                ('INSERT INTO audio_annotation_vibes VALUES (?,?,?,0)',
                 (annotation_id, 'audio-feedback-presets-1', 'focus'))):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, args)
        conn.execute('INSERT INTO audio_annotation_vibes VALUES (?,?,?,0)', (annotation_id, VERSION, 'relax'))
        conn.execute('UPDATE audio_annotations SET sealed=1 WHERE id=?', (annotation_id,))
        feedback = annotations.save(conn, 'real', [], ['focus'], 'audio-feedback-presets-1', None, 12, kind='feedback')
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO audio_annotation_vibes VALUES (?,'audio-feedback-presets-1','move',1)", (feedback,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('UPDATE audio_annotations SET model_output_filename=? WHERE id=?',
                         ('/' + 'b' * 64 + '.json', feedback))


def test_interrupted_file_publication_rolls_back_rows_and_retry_reuses_evidence(predecessor, monkeypatch):
    conn = predecessor
    before = list(conn.iterdump())
    publish = files.publish
    calls = []

    def interrupted(root, document):
        filename = publish(root, document)
        calls.append(filename)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return filename

    monkeypatch.setattr(files, 'publish', interrupted)
    with pytest.raises(KeyboardInterrupt):
        upgrade(conn)
    assert list(conn.iterdump()) == before
    assert all(Path(filename).is_file() for filename in calls)
    monkeypatch.setattr(files, 'publish', publish)
    upgrade(conn)
    assert set(calls) <= {row[0] for row in conn.execute('SELECT model_output_filename FROM audio_annotations')}
    assert files.collect_orphans(conn) == 0


def test_backup_restore_and_orphan_cleanup_preserve_all_revisions(predecessor, tmp_path):
    conn = predecessor
    upgrade(conn)
    with conn:
        annotations.save(conn, 'real', [], [], VERSION, None, 12)
    conn.execute('BEGIN')
    manifest = files.export(conn, tmp_path / 'backup-files')
    backup_path = tmp_path / 'restored' / 'state.db'
    backup_path.parent.mkdir()
    with sqlite3.connect(backup_path) as restored:
        conn.backup(restored)
    conn.rollback()
    old_files = {row[0] for row in conn.execute('SELECT model_output_filename FROM audio_annotations')}
    for filename in old_files:
        Path(filename).unlink()
    with sqlite3.connect(backup_path) as restored:
        files.restore(restored, manifest)
        row = latest(restored)
        assert annotations.read(restored, row)['state'] == 'empty'
        assert restored.execute('SELECT COUNT(*) FROM audio_annotations').fetchone()[0] == 5
        assert {row[0] for row in restored.execute('SELECT model_output_filename FROM audio_annotations')}.isdisjoint(old_files)
        restored.execute('BEGIN IMMEDIATE')
        orphan = files.publish(files.directory(restored), {'format': 'audio-annotation-1', 'interrupted': True})
        restored.rollback()
        pending = Path(orphan).parent / '.pending-interrupted'
        pending.write_bytes(b'incomplete publication')
        assert files.collect_orphans(restored) == 2
        assert not Path(orphan).exists()
        assert not pending.exists()
        assert all(Path(row[0]).is_file() for row in restored.execute('SELECT model_output_filename FROM audio_annotations'))


def test_corrupt_document_cannot_silently_replace_a_frozen_prediction(predecessor):
    conn = predecessor
    upgrade(conn)
    row = latest(conn)
    Path(row['model_output_filename']).write_text('{}')
    with pytest.raises(ValueError, match='content hash'):
        annotations.read(conn, row)
    assert annotations.evaluation(conn, row['id'])['version'] == 'frozen-model-1'


def test_deferred_writer_reserves_file_before_concurrent_orphan_collection(predecessor, monkeypatch):
    conn = predecessor
    upgrade(conn)
    other = sqlite3.connect(conn.execute('PRAGMA database_list').fetchone()[2], timeout=0)
    publish = files.publish

    def collecting(root, document):
        filename = publish(root, document)
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            files.collect_orphans(other)
        assert Path(filename).is_file()
        return filename

    monkeypatch.setattr(files, 'publish', collecting)
    try:
        conn.execute('BEGIN')
        conn.execute('SELECT 1 FROM audio_annotations').fetchone()
        with conn:
            annotation_id = annotations.save(conn, 'real', ['real-sample'], ['relax'], VERSION, prediction(), 12)
        assert latest(conn)['id'] == annotation_id
        assert files.collect_orphans(other) == 0
    finally:
        other.close()


def test_offline_snapshot_exports_relations_and_every_retained_revision(predecessor, tmp_path):
    conn = predecessor
    upgrade(conn)
    with conn:
        annotations.save(conn, 'real', [], [], VERSION, None, 12)
    conn.execute('BEGIN')
    snapshot = annotations.export_snapshot(conn, tmp_path / 'offline')
    assert len(snapshot['tables']) == 9
    assert len(snapshot['tables']['audio_annotations']) == 5
    manifest = files.read(snapshot['files_manifest'], expected={'type': 'backup-manifest'})
    assert len(manifest['files']) == 5
    for entry in manifest['files']:
        assert files.read(tmp_path / 'offline' / entry['relative_filename'])['format'] == 'audio-annotation-1'
    conn.rollback()


def test_v67_remains_a_validated_predecessor_for_the_next_schema(predecessor, monkeypatch):
    from yt_playlist.core.migrations import current
    from yt_playlist.core.migrations.runner import validate_version
    conn = predecessor
    upgrade(conn)
    monkeypatch.setattr(current, 'VERSION', 68)
    validate_version(conn, 67)
    conn.execute('DROP INDEX ix_annotation_sample')
    with pytest.raises(MigrationError, match='ix_annotation_sample'):
        validate_version(conn, 67)
