"""Independent candidate-engine checks using a small explicit domain conversion.

These controlled fixtures test transaction and recovery behavior. They do not
stand in for the production schema-64 or actual-library conversion acceptance.
"""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3

import pytest

from tests.db_rebuild_support import production64
from yt_playlist.core.db_rebuild import build


@pytest.fixture
def conversion(tmp_path):
    source, output = tmp_path/'source.db', tmp_path/'candidate.db'
    with closing(sqlite3.connect(source)) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE captures(id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        conn.executemany('INSERT INTO captures VALUES(?,?)',
                         [(i, json.dumps({'theme': 'theme-'+str(i), 'genre': 'raw genre'})) for i in range(1, 12)])
        conn.commit()

    copied = []
    def initialize(target):
        target.execute('CREATE TABLE choices(id INTEGER PRIMARY KEY, theme TEXT NOT NULL, genre TEXT NOT NULL)')

    def copy(reader, target, cursor, limit):
        rows = reader.execute('SELECT id,body FROM captures WHERE id>? ORDER BY id LIMIT ?',
                              (0 if cursor is None else cursor, limit)).fetchall()
        for identity, body in rows:
            value = json.loads(body)
            target.execute('INSERT INTO choices VALUES(?,?,?)', (identity, value['theme'], value['genre']))
            copied.append(identity)
        return build.Batch(cursor=rows[-1][0] if rows else cursor, rows=len(rows), done=len(rows)<limit)

    def verify(reader, target):
        expected = [(identity, json.loads(body)['theme'], json.loads(body)['genre'])
                    for identity, body in reader.execute('SELECT id,body FROM captures ORDER BY id')]
        assert target.execute('SELECT * FROM choices ORDER BY id').fetchall() == expected

    destination = build.Destination('choices-v1', initialize, (build.Stage('choices', copy),), verify)
    return source, output, destination, copied


def pending(path):
    with closing(sqlite3.connect(path)) as conn:
        return build._checkpoint(conn)


def test_direct_copy_normalizes_values_and_preserves_source(conversion):
    source, output, destination, copied = conversion
    before = source.read_bytes()
    report = build.construct(source, output, destination, batch_size=3)
    assert report['destination'] == 'choices-v1' and report['rows'] == {'choices': 11}
    assert copied == list(range(1, 12))
    assert source.read_bytes() == before
    with closing(sqlite3.connect(output)) as conn:
        assert conn.execute('SELECT * FROM choices WHERE id=7').fetchone() == (7, 'theme-7', 'raw genre')
        assert not conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (build.CHECKPOINT,)).fetchone()


def test_resume_does_not_repeat_committed_batches(conversion):
    source, output, destination, copied = conversion
    def stop(phase, detail):
        if phase == 'copy':
            raise InterruptedError('stopped after a committed batch')
    with pytest.raises(InterruptedError):
        build.construct(source, output, destination, batch_size=3, progress=stop)
    assert pending(output)['cursor'] == 3
    build.construct(source, output, destination, batch_size=2)
    assert copied == list(range(1, 12))


def test_failed_batch_rolls_back_its_rows_and_bookmark(conversion):
    source, output, destination, _ = conversion
    original = destination.stages[0].copy
    fail = True
    def copy(reader, target, cursor, limit):
        nonlocal fail
        batch = original(reader, target, cursor, limit)
        if cursor == 3 and fail:
            fail = False
            raise RuntimeError('interrupted mid-batch')
        return batch
    destination = replace(destination, stages=(build.Stage('choices', copy),))
    with pytest.raises(RuntimeError, match='mid-batch'):
        build.construct(source, output, destination, batch_size=3)
    assert pending(output)['cursor'] == 3
    with closing(sqlite3.connect(output)) as conn:
        assert conn.execute('SELECT id FROM choices ORDER BY id').fetchall() == [(1,), (2,), (3,)]
    assert build.construct(source, output, destination, batch_size=3)['rows'] == {'choices': 11}


@pytest.mark.parametrize('changed', ['source', 'contract', 'stages'])
def test_resume_refuses_changed_source_or_destination(conversion, changed):
    source, output, destination, _ = conversion
    def stop(*_):
        raise InterruptedError()
    with pytest.raises(InterruptedError):
        build.construct(source, output, destination, progress=stop, batch_size=3)
    original = output.read_bytes()
    if changed == 'source':
        with closing(sqlite3.connect(source)) as conn:
            conn.execute('UPDATE captures SET body=? WHERE id=1', ('{"theme":"changed","genre":"new"}',))
            conn.commit()
    elif changed == 'contract':
        destination = replace(destination, version='choices-v2')
    else:
        destination = replace(destination, stages=())
    with pytest.raises(ValueError, match='contract changed'):
        build.construct(source, output, destination)
    assert output.read_bytes() == original


def test_stage_cannot_commit_data_without_its_bookmark(conversion):
    source, output, destination, _ = conversion
    original = destination.stages[0].copy
    def copy(reader, target, cursor, limit):
        batch = original(reader, target, cursor, limit)
        target.commit()
        return batch
    broken = replace(destination, stages=(build.Stage('choices', copy),))
    with pytest.raises(sqlite3.DatabaseError, match='authorized'):
        build.construct(source, output, broken)
    assert pending(output)['cursor'] is None
    with closing(sqlite3.connect(output)) as conn:
        assert conn.execute('SELECT count(*) FROM choices').fetchone() == (0,)
    assert build.construct(source, output, destination)['rows'] == {'choices': 11}


def test_domain_savepoint_stays_inside_the_atomic_batch(conversion):
    source, output, destination, _ = conversion
    original = destination.stages[0].copy
    def copy(reader,target,cursor,limit):
        target.execute('SAVEPOINT domain_admission')
        batch = original(reader,target,cursor,limit)
        target.execute('RELEASE domain_admission')
        raise InterruptedError('failed before bookmark')
    interrupted = replace(destination,stages=(build.Stage('choices',copy),))
    with pytest.raises(InterruptedError,match='before bookmark'):
        build.construct(source,output,interrupted)
    assert pending(output)['cursor'] is None
    with closing(sqlite3.connect(output)) as conn:
        assert conn.execute('SELECT count(*) FROM choices').fetchone()==(0,)
    assert build.construct(source,output,destination)['rows']=={'choices':11}


@pytest.mark.parametrize('without_rowid', [False, True])
def test_sql_copy_resumes_exact_key_ranges_and_retains_storage_types(tmp_path, without_rowid):
    from yt_playlist.core.db_rebuild import destination as schema
    source, output = tmp_path/'source.db', tmp_path/'candidate.db'
    definition = ('CREATE TABLE captures(name TEXT PRIMARY KEY NOT NULL,body BLOB,text TEXT,number REAL)'
                  + (' WITHOUT ROWID' if without_rowid else ''))
    with closing(sqlite3.connect(source)) as original:
        original.execute(definition)
        values = [('z',b'\x00\xff','caf\u00e9',1.25),('a',None,None,None),
                  ('m',b'', 'unchanged',-2.5),('b',b'\x01','\u97f3\u697d',0.)]
        original.executemany('INSERT INTO captures VALUES(?,?,?,?)',values)
        original.commit()
        expected = original.execute('SELECT '+('' if without_rowid else 'rowid,')
                                    +'* FROM captures ORDER BY '+('name' if without_rowid else 'rowid')).fetchall()
    before = source.read_bytes()
    statements = []
    def initialize(target):
        target.execute(definition.replace('captures','retained').replace('name TEXT','renamed TEXT'))
        target.execute("INSERT INTO retained VALUES('initial',NULL,NULL,NULL)")
    mapping = {'name':'renamed','body':'body','text':'text','number':'number'}
    if not without_rowid:
        mapping = {'_legacy_id':'rowid',**mapping}
    def copy(reader, target, cursor, limit):
        target.set_trace_callback(statements.append)
        return schema._copy(reader,target,cursor,limit,table='captures',destination='retained',mapping=mapping)
    def verify(reader,target):
        assert target.execute('SELECT '+('' if without_rowid else 'rowid,')
                              +'* FROM retained ORDER BY '+('renamed' if without_rowid else 'rowid')).fetchall()==expected
    contract = build.Destination('sql-copy-v1',initialize,(build.Stage('rows',copy,batch_size=8192),),verify)
    def interrupt(phase,detail):
        if phase=='copy':
            raise InterruptedError('SQL range committed')
    with pytest.raises(InterruptedError,match='range committed'):
        build.construct(source,output,contract,batch_size=2,progress=interrupt)
    with closing(sqlite3.connect(output)) as target:
        assert target.execute('SELECT count(*) FROM retained').fetchone()[0]==2
    build.construct(source,output,contract,batch_size=2)
    assert any('FROM rebuild_source."captures"' in statement for statement in statements)
    assert source.read_bytes()==before


def test_sql_copy_window_bounds_utf8_and_blob_bytes(tmp_path,monkeypatch):
    from yt_playlist.core.db_rebuild import source
    path = tmp_path/'input.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE captures(id INTEGER PRIMARY KEY,body TEXT,data BLOB)')
        conn.executemany('INSERT INTO captures VALUES(?,?,?)',[(i,'\u97f3'*10,b'x'*10) for i in range(1,5)])
        monkeypatch.setattr(source,'MAX_BATCH_BYTES',80)
        bookmark,count,done = source.window(conn,'captures',None,8192)
        assert bookmark==[2] and count==2 and not done
        assert source.window(conn,'captures',bookmark,8192)==([4],2,False)
        assert source.window(conn,'captures',[4],8192)==([4],0,True)


def test_converter_cannot_write_through_the_attached_source(conversion):
    source,output,contract,_ = conversion
    original = source.read_bytes()
    def attempted_write(reader,target,cursor,limit):
        target.execute("UPDATE rebuild_source.captures SET body='changed' WHERE id=1")
        return build.Batch(done=True)
    broken = replace(contract,stages=(build.Stage('choices',attempted_write),))
    with pytest.raises(sqlite3.OperationalError,match='readonly'):
        build.construct(source,output,broken)
    assert source.read_bytes()==original


def test_repository_writes_and_bookmark_share_one_transaction(production64, tmp_path):
    from yt_playlist.core.db_rebuild import destination as schema
    from yt_playlist.core.migrations import current, regular
    from yt_playlist.core.store import Store

    output = tmp_path / 'repositories.db'
    fail = True

    def prepare(reader, store, cursor, limit):
        nonlocal fail
        assert isinstance(store, Store)
        assert store.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        expected = str(cursor) if cursor is not None else None
        with store.read_view() as reader:
            assert reader.settings.get_setting('repository_batch') == expected
        value = (cursor or 0) + 1
        store.settings.set_setting('repository_batch', value)
        # A repository's normal commit must remain private until its bookmark.
        with store.read_view() as reader:
            assert reader.settings.get_setting('repository_batch') == expected
        if cursor == 1 and fail:
            fail = False
            raise InterruptedError('repository batch before bookmark')
        return build.Batch(value, rows=1, done=value == 3)

    def verify(reader, target):
        current.validate(target)
        regular.validate(target)
        assert target.execute("SELECT value FROM settings WHERE key='repository_batch'").fetchone()[0] == '3'

    contract = build.Destination('repositories-v1', schema.initialize,
        (build.Stage('indexes', schema.indexes), build.Stage('guards', schema.finish),
         build.Stage('preparation', prepare, repositories=True)), verify)
    with pytest.raises(InterruptedError, match='before bookmark'):
        build.construct(production64, output, contract)
    assert pending(output)['cursor'] == 1
    with closing(sqlite3.connect(output)) as conn:
        assert conn.execute("SELECT value FROM settings WHERE key='repository_batch'").fetchone()[0] == '1'
    assert build.construct(production64, output, contract)['rows']['preparation'] == 3


def test_validation_failure_is_resumable_without_recopy(conversion):
    source, output, destination, copied = conversion
    def reject(*_):
        raise ValueError('independent preservation check failed')
    with pytest.raises(ValueError, match='preservation check'):
        build.construct(source, output, replace(destination, verify=reject))
    assert pending(output)['stage'] == 1
    build.construct(source, output, destination)
    assert copied == list(range(1, 12))


def test_verification_cannot_change_the_data_it_accepts(conversion):
    source, output, destination, _ = conversion
    def mutate(reader, target):
        target.execute("UPDATE choices SET theme='silently changed'")
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        build.construct(source, output, replace(destination, verify=mutate))
    build.construct(source, output, destination)


def test_concurrent_source_commit_invalidates_completed_candidate(conversion):
    source, output, destination, _ = conversion
    def change(phase, detail):
        if phase == 'verify':
            with closing(sqlite3.connect(source)) as conn:
                conn.execute('INSERT INTO captures VALUES(12,?)', ('{"theme":"new","genre":"new"}',))
                conn.commit()
    with pytest.raises(ValueError, match='Source changed'):
        build.construct(source, output, destination, progress=change)
    assert pending(output)['stage'] == 1


def test_commit_between_snapshot_and_first_fingerprint_is_detected(conversion, monkeypatch):
    source, output, destination, _ = conversion
    identity = build._source_identity
    changed = False
    def change(path):
        nonlocal changed
        if not changed:
            changed = True
            with closing(sqlite3.connect(source)) as writer:
                writer.execute('INSERT INTO captures VALUES(12,?)', ('{"theme":"new","genre":"new"}',))
                writer.commit()
        return identity(path)
    monkeypatch.setattr(build, '_source_identity', change)
    with pytest.raises(ValueError, match='Source changed'):
        build.construct(source, output, destination)
    assert pending(output)['stage'] == 1


def test_invalid_reference_cannot_pass_installation_validation(conversion):
    source, output, destination, _ = conversion
    initialize = destination.initialize
    def schema(target):
        initialize(target)
        target.execute('CREATE TABLE references_to_choices(choice_id INTEGER REFERENCES choices(id))')
        target.execute('INSERT INTO references_to_choices VALUES(999)')
    with pytest.raises(ValueError, match='foreign'):
        build.construct(source, output, replace(destination, initialize=schema))
    assert pending(output)['stage'] == 1


def test_engine_reads_populated_released_source_without_advancing_its_history(production64, tmp_path):
    """Exercise the engine against genuine source DDL, with a narrow domain.

    This establishes source readability and ID/order preservation by the engine;
    the final destination's full domain mappings have separate acceptance.
    """
    source, output = production64, tmp_path/'candidate.db'
    before = source.read_bytes()
    def initialize(target):
        target.execute('CREATE TABLE queues(playlist INTEGER,account INTEGER,provider_id TEXT,'
                       'video TEXT,position INTEGER,PRIMARY KEY(playlist,position))')
    def copy(reader, target, cursor, limit):
        rows = reader.execute('SELECT p.id,p.identity_id,p.ytm_playlist_id,i.video_id,i.position '
            'FROM playlists p JOIN playlist_items i ON i.playlist_id=p.id '
            'ORDER BY p.id,i.position LIMIT ? OFFSET ?', (limit, cursor or 0)).fetchall()
        target.executemany('INSERT INTO queues VALUES(?,?,?,?,?)', rows)
        return build.Batch(cursor=(cursor or 0)+len(rows), rows=len(rows), done=len(rows)<limit)
    def verify(reader, target):
        assert target.execute('SELECT DISTINCT playlist,account,provider_id FROM queues ORDER BY playlist').fetchall() == [
            (41, 11, 'LM'), (73, 29, 'LM')]
        for playlist in (41, 73):
            assert target.execute('SELECT video FROM queues WHERE playlist=? ORDER BY position',
                                  (playlist,)).fetchall() == [('video-19',), ('video-3',), ('video-8',)]
        assert reader.execute('SELECT max(version) FROM schema_migrations').fetchone() == (64,)
        assert reader.execute('SELECT id,recording_id FROM play_events').fetchall() == [(91, 8)]
    destination = build.Destination('queue-engine-check-v1', initialize, (build.Stage('queues', copy),), verify)
    assert build.construct(source, output, destination, batch_size=2)['rows'] == {'queues': 6}
    assert source.read_bytes() == before
