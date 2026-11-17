"""Deliberate annotation SQL/FAO restart, backup and offline-cutover checks."""
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from tests.file_custody_support import expire_file
from tests.store_fixture_support import file_consumer_store, native_sql_calls, without_query_timing
from yt_playlist.core import audio_annotation_files as files, audio_annotations, fao
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.core.db_rebuild import build, destination
from yt_playlist.core.store import Store
from yt_playlist.repos import annotation_file_writes

pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


def prediction():
    return {'version': 'frozen-model-1', 'taxonomy_version': TAXONOMY_VERSION,
            'vibes': ['relax'], 'ranking': [{'vibe': 'relax', 'score': .9}],
            'model': {'sha256': 'a' * 64, 'feature_version': 'features-1',
                      'training_songs': 9, 'trained_labels': ['relax', 'cruise']},
            'arc': {'opaque': ['original', 'output']}}


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These storage checks do not start an HTTP application."""


@pytest.mark.parametrize('failure', ['file', 'status'])
def test_interruption_retains_the_judgment_and_pending_receipt_after_restart(file_consumer_store, monkeypatch, failure):
    store = file_consumer_store
    store.audio_vibes.save_playback('run', 'video', 'Song', 'Artist', 1)
    store.audio_vibes.save_feedback('run', [], ['cruise'], TAXONOMY_VERSION, prediction(), 2)
    previous = store.audio_vibes.feedback('run')
    target, method = ((store.audio_vibes.annotation_files, 'write_reserved') if failure == 'file'
                      else (store.audio_vibes.repository, 'publish_annotation'))
    def interrupted(*args, **kwargs):
        raise OSError('interrupted annotation')
    with monkeypatch.context() as patch:
        patch.setattr(target, method, interrupted)
        with pytest.raises(OSError, match='interrupted annotation'):
            store.audio_vibes.save_feedback('run', [], ['relax'], TAXONOMY_VERSION, None, 3)
    assert store.audio_vibes.feedback('run') == previous
    assert not store.conn.in_transaction
    with closing(sqlite3.connect(store._db_path)) as observer:
        rows = observer.execute('SELECT id,file_status FROM annotation_file_writes ORDER BY id').fetchall()
    assert [row[1] for row in rows] == ['ready', 'pending']
    path = files.record_path(store.audio_vibes.annotation_files.root, rows[-1][0])
    assert path.exists() is (failure == 'status')
    restarted = Store(store._db_path)
    try:
        restarted.init_schema(defer_metadata=True)
        assert restarted.audio_vibes.feedback('run') == previous
        restarted.audio_vibes.save_feedback('run', [], ['relax'], TAXONOMY_VERSION, None, 4)
        assert restarted.audio_vibes.feedback('run')['vibes'] == ['relax']
        assert [tuple(row) for row in restarted.conn.execute(
            'SELECT revision,sealed FROM audio_annotations ORDER BY id')] == [(1, 1), (2, 1)]
        assert not restarted.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        restarted.conn.close()


def test_backup_restores_native_ids_and_age_for_every_judgment_revision(file_consumer_store, tmp_path):
    store = file_consumer_store
    store.audio_vibes.save_playback('run', 'video', 'Song', 'Artist', 1)
    store.audio_vibes.save_feedback('run', [], ['cruise'], TAXONOMY_VERSION, prediction(), 2)
    store.audio_vibes.save_feedback('run', [], [], TAXONOMY_VERSION, None, 3)
    annotations = [dict(row) for row in store.conn.execute('SELECT * FROM audio_annotations ORDER BY id')]
    before = {}
    for row in annotations:
        expire_file(row['model_output_filename'])
        with fao.read(row['model_output_filename']) as stream:
            before[row['id']] = stream.read(files.MAX_BYTES + 1), stream.info
    copy = sqlite3.connect(tmp_path / 'restored.db')
    try:
        copy.row_factory = sqlite3.Row
        copy.execute('PRAGMA foreign_keys=ON')
        store.conn.execute('BEGIN')
        try:
            manifest = files.export(store.conn, tmp_path / 'backup')
            store.conn.backup(copy)
        finally:
            store.conn.rollback()
        files.restore(copy, manifest)
        for row in copy.execute('SELECT * FROM audio_annotations ORDER BY id'):
            assert files.record_id(row['model_output_filename']) == files.record_id(annotations[row['id'] - 1]['model_output_filename'])
            assert Path(row['model_output_filename']).is_relative_to(files.directory(copy))
            with fao.read(row['model_output_filename']) as stream:
                encoded, age = before[row['id']]
                assert stream.read(files.MAX_BYTES + 1) == encoded
                assert (stream.info.ctime_ns, stream.info.mtime_ns) == (age.ctime_ns, age.mtime_ns)
            assert files.AnnotationFiles(files.directory(copy)).read(dict(row))['prediction'] == (
                prediction() if row['revision'] == 1 else None)
        assert not copy.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        copy.close()


def test_offline_cutover_retains_historical_ids_memberships_and_captured_predictions(
        file_consumer_store, tmp_path):
    store = file_consumer_store
    # Supply the real installed filename constraint below the cutover. The
    # source is private and empty; the candidate still uses the real builder.
    with store.conn.atomic():
        guards = list(store.conn.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='audio_annotations'"))
        indexes = list(store.conn.execute("SELECT sql FROM sqlite_schema WHERE type='index' AND tbl_name='audio_annotations' AND sql IS NOT NULL"))
        sql = store.conn.execute("SELECT sql FROM sqlite_schema WHERE name='audio_annotations'").fetchone()[0]
        store.conn.execute('DROP TABLE audio_annotations')
        store.conn.execute(sql.replace(annotation_file_writes.NATIVE_CHECK, annotation_file_writes.LEGACY_CHECK))
        for (statement,) in indexes:
            store.conn.execute(statement)
        for _, statement in guards:
            store.conn.execute(statement)
    assert not annotation_file_writes.native(store.conn)
    store.audio_vibes.save_playback('run', 'video', 'Song', 'Artist', 1)
    store.audio_vibes.save_sample(dict(id='sample', run_id='run', video_id='video', point='thirty',
                                      title='Song', artist='Artist'), 2)
    with store.conn.atomic():
        first = audio_annotations.save(store.conn, 'run', ['sample'], ['cruise'], TAXONOMY_VERSION, prediction(), 3)
        audio_annotations.save(store.conn, 'run', ['sample'], [], TAXONOMY_VERSION, None, 4, state='withdrawn')
    tables = ('audio_annotations', 'audio_annotation_samples', 'audio_annotation_vibes',
              'audio_annotation_evaluations', 'audio_annotation_scores', 'audio_annotation_predictions',
              'audio_annotation_model_labels', 'schema_migrations', 'schema_changes')
    before = {table: [tuple(row) for row in store.conn.execute('SELECT * FROM ' + table + ' ORDER BY 1,2')]
              for table in tables}
    candidate = tmp_path / 'cutover.db'
    contract = destination.for_source(store._db_path)
    def interrupted(phase, detail):
        if phase == 'copy' and detail['stage'] == 'copy:audio_annotations':
            raise InterruptedError('annotation prefix committed')
    with pytest.raises(InterruptedError, match='prefix committed'):
        build.construct(store._db_path, candidate, contract, batch_size=1, progress=interrupted)
    with closing(sqlite3.connect(candidate)) as pending:
        checkpoint = build._checkpoint(pending)
        assert checkpoint['rows']['copy:audio_annotations'] == 1
        assert pending.execute('SELECT id FROM audio_annotations').fetchall() == [(first,)]
    report = build.construct(store._db_path, candidate, contract, batch_size=2)
    assert report['rows']['copy:audio_annotations'] == 2
    assert {table: [tuple(row) for row in store.conn.execute('SELECT * FROM ' + table + ' ORDER BY 1,2')]
            for table in tables} == before
    rebuilt = Store(candidate)
    try:
        rebuilt.init_schema(defer_metadata=True)
        assert annotation_file_writes.native(rebuilt.conn)
        for table in tables[:-2]:
            assert [tuple(row) for row in rebuilt.conn.execute('SELECT * FROM ' + table + ' ORDER BY 1,2')] == before[table]
        row = dict(rebuilt.conn.execute('SELECT * FROM audio_annotations WHERE id=?', (first,)).fetchone())
        assert rebuilt.audio_vibes.annotation_files.read(row)['prediction'] == prediction()
        assert rebuilt.audio_vibes.feedback('run')['vibes'] == []
        rebuilt.audio_vibes.save_feedback('run', ['sample'], ['relax'], TAXONOMY_VERSION, prediction(), 5)
        latest = rebuilt.audio_vibes.repository.annotation('run', kind='session', taxonomy_version=TAXONOMY_VERSION)
        assert latest['revision'] == 3 and files.record_id(latest['model_output_filename']) is not None
        assert not rebuilt.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        rebuilt.conn.close()
