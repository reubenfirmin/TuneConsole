"""Frozen choice guards do not depend on a successful historical audit."""
from dataclasses import replace
import json
import sqlite3

import pytest

from tests.store_fixture_support import numbered_store as installed
from tests.store_fixture_support import publication_consumer_reads
from yt_playlist.core import albums, album_tracklists
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import frozen_shares as audit
from yt_playlist.core.store import Store



pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """This owner runs directly without the HTTP application."""

def finish(conn):
    for _ in range(150):
        progress = audit.JOB.step(conn,batch_size=512)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Frozen share fixture did not finish bounded batches')


def capture(conn, videos=('one','missing','one'), title='Frozen'):
    with conn:
        album = albums.observe(conn,'release',title='Release',artist='Artist',rank=1)
        return album_tracklists.freeze(conn,album,'release',title,list(videos),now=1.2345678901234567)


def sources(conn):
    return {owner:list(map(tuple,conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+','.join(keys))))
            for owner,keys in audit.JOB.keys.items()}


def assert_originals(conn):
    for owner,keys in audit.JOB.keys.items():
        for row in conn.execute('SELECT * FROM main.'+owner):
            key = json.dumps([row[column] for column in keys],separators=(',',':'))
            target = audit.SUMMARIES if owner == 'album_share_snapshots' else audit.MEMBERS
            predicate = 'snapshot_id=?' if owner == 'album_share_snapshots' else 'snapshot_id=? AND position=?'
            digest = conn.execute('SELECT source_digest FROM '+target+' WHERE '+predicate,json.loads(key)).fetchone()[0]
            assert audit.original(conn,owner,key,digest) == dict(row)


@pytest.mark.parametrize('installed',[99,104,107],indirect=True)
def test_guard_precedes_audit_and_exact_empty_repeated_unknown_choices_survive(installed):
    store, _ = installed
    frozen = capture(store.conn)
    empty = capture(store.conn,(),title='Empty')
    before = sources(store.conn)
    identities = store.conn.execute('SELECT count(*) FROM main.catalog_entries').fetchone()[0]
    audit.JOB.install(store.conn)
    assert audit.JOB.progress(store.conn).processed == 0
    assert not store.conn.execute('SELECT 1 FROM '+audit.MEMBERS).fetchone()
    for position in (0,3):
        with pytest.raises(sqlite3.IntegrityError,match='cannot extend or reorder'):
            with store.conn:
                store.conn.execute('INSERT OR REPLACE INTO main.album_share_items '
                    'SELECT snapshot_id,?,tracklist_id,occurrence_id,entry_id,raw_source_ref '
                    'FROM main.album_share_items WHERE snapshot_id=? AND position=0',(position,frozen))
    with pytest.raises(MigrationError,match='reconciled preparation'):
        audit.verify(store.conn)
    finish(store.conn)
    audit.verify(store.conn)
    assert_originals(store.conn)
    assert store.conn.execute('SELECT member_count,expected_count FROM '+audit.SUMMARIES+' WHERE snapshot_id=?',(empty,)).fetchone()[:] == (0,0)
    assert sources(store.conn) == before
    assert store.conn.execute('SELECT count(*) FROM main.catalog_entries').fetchone()[0] == identities
    plan = store.conn.execute('EXPLAIN QUERY PLAN SELECT snapshot_id FROM '+audit.SUMMARIES+' WHERE blocked=1 ORDER BY snapshot_id LIMIT 1').fetchall()
    assert any(audit.SUMMARIES+'_blocked' in row[3] for row in plan)


@pytest.mark.parametrize('installed',[104],indirect=True)
@pytest.mark.parametrize('corruption',['append','missing','reordered'])
def test_bad_history_stays_explicit_without_blocking_guard_or_regular_ddl(installed,corruption,monkeypatch):
    store, _ = installed
    frozen = capture(store.conn)
    with store.conn:
        if corruption == 'append':
            store.conn.execute('INSERT INTO main.album_share_items SELECT snapshot_id,3,tracklist_id,occurrence_id,entry_id,raw_source_ref '
                               'FROM main.album_share_items WHERE snapshot_id=? AND position=0',(frozen,))
        else:
            extra = store.conn.execute('INSERT INTO main.album_share_snapshots(album_id,tracklist_id,captured_provider_id,title,created_at) '
                "SELECT album_id,tracklist_id,captured_provider_id,'Partial',created_at FROM main.album_share_snapshots WHERE id=?",(frozen,)).lastrowid
            store.conn.execute('INSERT INTO main.album_share_items SELECT ?, '+('position' if corruption == 'missing' else '2-position')+
                ',tracklist_id,occurrence_id,entry_id,raw_source_ref FROM main.album_share_items WHERE snapshot_id=?'+
                (' AND position<2' if corruption == 'missing' else ''),(extra,frozen))
    before = sources(store.conn)
    audit.JOB.install(store.conn)
    finish(store.conn)
    with pytest.raises(MigrationError,match='changed membership; original rows retained'):
        audit.verify(store.conn)
    assert_originals(store.conn)
    assert sources(store.conn) == before
    progress = audit.JOB.progress(store.conn)
    def install(conn,context):
        conn.execute('CREATE TABLE independent_share_ddl(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='independent_share_ddl'").fetchone()
    monkeypatch.setattr(regular,'CHANGES',(*regular.CHANGES,regular.Change(len(regular.CHANGES) + 1,'independent_share_ddl',install,validate)))
    regular.migrate(store.conn)
    assert audit.JOB.progress(store.conn) == progress
    capture(store.conn,('later',),title='Usable continuing writer')
    finish(store.conn)
    with pytest.raises(MigrationError,match='changed membership'):
        audit.verify(store.conn)


@pytest.mark.parametrize('installed',[104],indirect=True)
def test_bounded_batches_failure_interruption_and_reopened_retry_keep_counts_exact(installed,monkeypatch):
    store, _ = installed
    capture(store.conn,tuple('video-'+str(index) for index in range(1040)))
    before = sources(store.conn)
    audit.JOB.install(store.conn)
    progress = audit.JOB.step(store.conn,batch_size=512)
    assert progress.processed <= 32
    progress = audit.JOB.step(store.conn,batch_size=512)
    assert progress.processed == 33
    def fail(*args):
        audit.JOB.copy(*args)
        raise OSError('controlled frozen audit failure')
    counts = list(map(tuple,store.conn.execute('SELECT snapshot_id,member_count FROM '+audit.SUMMARIES)))
    with pytest.raises(OSError,match='controlled frozen audit failure'):
        replace(audit.JOB,copy=fail).step(store.conn)
    assert audit.JOB.progress(store.conn).processed == progress.processed
    assert list(map(tuple,store.conn.execute('SELECT snapshot_id,member_count FROM '+audit.SUMMARIES))) == counts
    failed = audit.JOB.progress(store.conn)
    def install(conn,context):
        conn.execute('CREATE TABLE independent_failed_share_ddl(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='independent_failed_share_ddl'").fetchone()
    monkeypatch.setattr(regular,'CHANGES',(*regular.CHANGES,regular.Change(len(regular.CHANGES) + 1,'independent_failed_share_ddl',install,validate)))
    regular.migrate(store.conn)
    assert audit.JOB.progress(store.conn) == failed
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError,match='retry explicitly'):
            audit.JOB.step(reopened.conn)
        def interrupt(*args):
            audit.JOB.copy(*args)
            raise KeyboardInterrupt('controlled frozen interruption')
        with pytest.raises(KeyboardInterrupt):
            replace(audit.JOB,copy=interrupt).step(reopened.conn,retry=True)
        assert audit.JOB.progress(reopened.conn) == failed
        audit.JOB.step(reopened.conn,retry=True)
        finish(reopened.conn)
        audit.verify(reopened.conn)
        assert_originals(reopened.conn)
        assert sources(reopened.conn) == before
        assert reopened.conn.execute('SELECT member_count FROM '+audit.SUMMARIES).fetchone()[0] == 1040
        capture(reopened.conn,('new','new'),title='Later')
        with pytest.raises(MigrationError,match='reconciled preparation'):
            audit.verify(reopened.conn)
        finish(reopened.conn)
        audit.verify(reopened.conn)
        assert_originals(reopened.conn)
        # Repeated dependency rescans must not increment already staged counts.
        with reopened.conn:
            reopened.conn.execute('UPDATE main.album_track_occurrences SET recording_id=recording_id')
        finish(reopened.conn)
        audit.verify(reopened.conn)
        assert reopened.conn.execute('SELECT sum(member_count) FROM '+audit.SUMMARIES).fetchone()[0] == 1042
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed',[104],indirect=True)
def test_native_upgrade_preserves_independent_capture_and_guards(installed,tmp_path):
    store, _ = installed
    capture(store.conn)
    audit.JOB.install(store.conn)
    finish(store.conn)
    before = sources(store.conn)
    upgraded = Store(tmp_path/'frozen-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn,registry=registry.MIGRATIONS)
        audit.JOB.validate(upgraded.conn)
        finish(upgraded.conn)
        audit.verify(upgraded.conn)
        assert sources(upgraded.conn) == before
        assert_originals(upgraded.conn)
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed',[104],indirect=True)
def test_oversized_new_capture_fails_without_changing_last_good_evidence(installed):
    store, _ = installed
    first = capture(store.conn)
    audit.JOB.install(store.conn)
    finish(store.conn)
    before = tuple(store.conn.execute('SELECT * FROM '+audit.SUMMARIES+' WHERE snapshot_id=?',(first,)).fetchone())
    capture(store.conn,(),title='x'*(audit.MAX_ROW_BYTES+1))
    with pytest.raises(ValueError,match='byte budget'):
        finish(store.conn)
    assert tuple(store.conn.execute('SELECT * FROM '+audit.SUMMARIES+' WHERE snapshot_id=?',(first,)).fetchone()) == before
    with pytest.raises(MigrationError,match='retry explicitly'):
        audit.JOB.step(store.conn)
