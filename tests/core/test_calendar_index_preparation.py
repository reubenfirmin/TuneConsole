"""Calendar accelerators build incrementally and keep serving through writes."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import sqlite3

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.repos.test_foreign_calendar_batches import stamp
from yt_playlist.core import destination_schema, listening, listening_calendar
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import calendar_indexes as indexes
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Calendar preparation checks do not construct an HTTP application."""


@pytest.fixture
def file_store(file_backed_store):
    return file_backed_store


def finish(conn):
    selected_job = indexes.job(conn)
    for _ in range(150):
        progress = selected_job.step(conn,batch_size=512)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Calendar index fixture did not finish bounded batches')


def seed(store, count=4):
    accounts = [store.accounts.upsert_account(label,label,None,index==0) for index,label in enumerate(('First','Second'))]
    at = stamp('2026-10-21T05:30:00')
    for account in accounts:
        with store.conn:
            listening.calendar(store.conn,account,'America/Costa_Rica')
            for index in range(count):
                listening.observe_day(store.conn,account,'song-'+str(index),'2026-10-21','ytm_history','UTC',at,reconcile=False)
        store.history.import_listening_events(account,[('song-'+str(index),None,at) for index in range(count)])
    return accounts,at


def assert_lookup_rows(conn):
    account = indexes.job(conn).account_column
    for owner,target in indexes.OWNERS.items():
        fields = ','.join(account if column == 'account_id' else column for column in indexes.FIELDS[owner])
        expected = list(map(tuple,conn.execute('SELECT '+fields+' FROM main.'+owner+' WHERE '+
            indexes.PREDICATES[owner].format(prefix='')+' ORDER BY id')))
        assert list(map(tuple,conn.execute('SELECT * FROM '+target+' ORDER BY id'))) == expected


def observed(conn,account):
    affected = {('song-0','2026-10-21','UTC'),('song-1','2026-10-21','UTC')}
    return list(map(tuple,listening_calendar._foreign_rows(conn,account,affected,None)))


@pytest.mark.parametrize('version', [100, 103, 105, 107])
def test_calendar_input_keeps_each_installed_contract_and_snapshot_guards(_bounded_installed_schema, version):
    template = _bounded_installed_schema(version)
    source = template.source
    with closing(sqlite3.connect(source._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        runner.validate_version(reader, version)
        regular.validate(reader)
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert source.conn.execute('SELECT count(*) FROM main.play_events').fetchone()[0] == 0
    assert template.revision_guards


@pytest.mark.parametrize('installed',[100,103,105,107],indirect=True)
def test_empty_index_install_bounded_build_and_explicit_dynamic_selection(installed):
    store, _ = installed
    accounts,_ = seed(store,count=40)
    expected = {account:observed(store.conn,account) for account in accounts}
    job = indexes.job(store.conn)
    job.install(store.conn)
    assert not indexes.selected(store.conn)
    assert store.conn.execute('SELECT count(*) FROM '+indexes.EVENTS).fetchone()[0] == 0
    assert store.conn.execute('SELECT count(*) FROM '+indexes.OBSERVATIONS).fetchone()[0] == 0
    assert job.step(store.conn,batch_size=512).processed == 32
    with pytest.raises(MigrationError,match='reconciled preparation'):
        indexes.publish(store.conn,revision=job.progress(store.conn).input_revision)
    before = Store(store._db_path)
    try:
        before.init_schema(defer_metadata=True)
        assert not indexes.selected(before.conn)
        progress = finish(store.conn)
        with pytest.raises(MigrationError,match='reconciled preparation'):
            indexes.publish(store.conn,revision=progress.input_revision+1)
        indexes.publish(store.conn,revision=progress.input_revision)
        assert indexes.selected(before.conn)
        assert_lookup_rows(store.conn)
        sql = []
        before.conn.set_trace_callback(sql.append)
        for account in accounts:
            assert observed(before.conn,account) == expected[account]
        before.conn.set_trace_callback(None)
        query = next(statement for statement in sql if statement.startswith('WITH affected'))
        plan = [row[3] for row in before.conn.execute('EXPLAIN QUERY PLAN '+query)]
        assert any(indexes.OBSERVATIONS+'_scope' in detail and all(field+'=?' in detail
                   for field in ('account_id','unresolved_subject_key','source_day','timezone')) for detail in plan),'\n'.join(plan)
        regular.validate(before.conn)
    finally:
        before.conn.close()


def test_published_lookups_follow_continuing_writes_resolution_and_source_removal(store):
    accounts,at = seed(store)
    indexes.job(store.conn).install(store.conn)
    indexes.publish(store.conn,revision=finish(store.conn).input_revision)
    # The next event makes this foreign date ambiguous. Both indexes must be
    # maintained in its writing transaction, before calendar reconciliation.
    sql = []
    store.conn.set_trace_callback(sql.append)
    store.history.record_play_event(accounts[0],'song-0',None,at+7200)
    store.conn.set_trace_callback(None)
    assert store.conn.execute("SELECT day FROM main.listening_observations WHERE account_id=? AND raw_identity_key='song-0'",(accounts[0],)).fetchone()[0] is None
    query = next(statement for statement in sql if statement.startswith('SELECT pe.played_at,c.day'))
    plan = [row[3] for row in store.conn.execute('EXPLAIN QUERY PLAN '+query)]
    assert any(indexes.EVENTS+'_scope' in detail and all(field in detail for field in
               ('account_id=?','unresolved_subject_key=?','played_at>?','played_at<?')) for detail in plan),'\n'.join(plan)
    assert indexes.selected(store.conn)
    assert indexes.job(store.conn).progress(store.conn).status != 'prepared'
    store.history.import_listening_events(accounts[0],[('known|artist','later',at)])
    with store.conn:
        listening.observe_day(store.conn,accounts[0],'known|artist','2026-10-21','independent','UTC',at)
    assert_lookup_rows(store.conn)
    store.tracks.upsert_track('later','Known','Artist',None,10)
    assert not store.conn.execute('SELECT 1 FROM '+indexes.EVENTS+" WHERE unresolved_subject_key='known|artist'").fetchone()
    assert not store.conn.execute('SELECT 1 FROM '+indexes.OBSERVATIONS+" WHERE unresolved_subject_key='known|artist'").fetchone()
    with store.conn:
        listening.remove_source(store.conn,'ytm_history',accounts[0])
        listening.remove_source(store.conn,'takeout',accounts[0])
    assert_lookup_rows(store.conn)
    assert indexes.selected(store.conn)


def test_failed_copy_does_not_block_ddl_and_reopened_retry_is_idempotent(file_backed_store,monkeypatch):
    store = file_backed_store
    seed(store,count=90)
    job = indexes.job(store.conn)
    job.install(store.conn)
    checkpoint = job.step(store.conn)
    def fail(*args):
        job.copy(*args)
        raise OSError('controlled calendar copy failure')
    with pytest.raises(OSError,match='controlled calendar copy failure'):
        replace(job,copy=fail).step(store.conn)
    failed = job.progress(store.conn)
    assert failed.status == 'failed' and failed.processed == checkpoint.processed
    def install(conn,context):
        conn.execute('CREATE TABLE independent_index_ddl(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='independent_index_ddl'").fetchone()
    change = regular.Change(len(regular.CHANGES) + 1,'independent_index_ddl',install,validate)
    # Model a later shipped build consistently: its bounded change and complete
    # destination both know the new owner. Unknown histories still fail closed.
    definition = deepcopy(destination_schema.definition())
    definition['regular_history'].append([change.version,change.name])
    for key in ('installed_statements','destination_statements'):
        definition[key].append('CREATE TABLE independent_index_ddl(value TEXT)')
    monkeypatch.setattr(destination_schema,'definition',lambda:definition)
    monkeypatch.setattr(regular,'CHANGES',(*regular.CHANGES,change))
    regular.migrate(store.conn)
    assert job.progress(store.conn) == failed
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError,match='retry explicitly'):
            indexes.job(reopened.conn).step(reopened.conn)
        indexes.job(reopened.conn).step(reopened.conn,retry=True)
        progress = finish(reopened.conn)
        assert_lookup_rows(reopened.conn)
        indexes.publish(reopened.conn,revision=progress.input_revision)
        assert indexes.selected(reopened.conn)
    finally:
        reopened.conn.close()


def test_interrupted_publication_and_source_transaction_roll_back_together(store):
    accounts,at = seed(store)
    job = indexes.job(store.conn)
    job.install(store.conn)
    revision = finish(store.conn).input_revision
    store.conn.execute('CREATE TEMP TRIGGER interrupt_calendar_publication AFTER INSERT ON main.'+indexes.SELECTION+
        " BEGIN SELECT RAISE(ABORT,'controlled publication failure'); END")
    with pytest.raises(sqlite3.IntegrityError,match='controlled publication failure'):
        indexes.publish(store.conn,revision=revision)
    assert not indexes.selected(store.conn)
    store.conn.execute('DROP TRIGGER temp.interrupt_calendar_publication')
    indexes.publish(store.conn,revision=revision)
    before = list(map(tuple,store.conn.execute('SELECT * FROM '+indexes.EVENTS+' ORDER BY id')))
    with pytest.raises(RuntimeError,match='controlled source rollback'):
        with store.conn:
            listening.append_event(store.conn,accounts[0],'rolled-back',at+14400)
            raise RuntimeError('controlled source rollback')
    assert list(map(tuple,store.conn.execute('SELECT * FROM '+indexes.EVENTS+' ORDER BY id'))) == before
    assert_lookup_rows(store.conn)




def test_startup_admission_rejects_missing_incremental_maintenance(store):
    seed(store)
    indexes.job(store.conn).install(store.conn)
    indexes.publish(store.conn,revision=finish(store.conn).input_revision)
    store.conn.execute('DROP TRIGGER main.maintain_calendar_lookup_play_events_insert')
    with pytest.raises(MigrationError,match='maintain_calendar_lookup_play_events_insert'):
        regular.validate(store.conn)


@pytest.mark.parametrize('corruption', ['missing', 'changed', 'extra'])
def test_publication_compares_the_complete_lookup_to_its_source(store, corruption):
    accounts,at = seed(store)
    job = indexes.job(store.conn)
    job.install(store.conn)
    revision = finish(store.conn).input_revision
    with store.conn:
        if corruption=='missing':
            store.conn.execute('DELETE FROM '+indexes.EVENTS+' WHERE id=(SELECT min(id) FROM '+indexes.EVENTS+')')
        elif corruption=='changed':
            store.conn.execute('UPDATE '+indexes.EVENTS+' SET played_at=played_at+1')
        else:
            store.conn.execute('INSERT INTO '+indexes.EVENTS+' VALUES (999999,?,?,?)',(accounts[0],'unrecorded',at))
    with pytest.raises(MigrationError,match='Calendar lookup differs|Calendar lookup coverage'):
        indexes.publish(store.conn,revision=revision)
    assert not indexes.selected(store.conn)


@pytest.mark.parametrize('write', ['unrelated', 'source', 'lookup'])
def test_read_verification_leaves_other_writers_available_and_fences_changed_inputs(file_store, monkeypatch, write):
    store = file_store
    accounts,_ = seed(store)
    job = indexes.job(store.conn)
    job.install(store.conn)
    revision = finish(store.conn).input_revision
    rows,verify = indexes.bounded.rows,indexes.verify
    committed = []

    def mutate():
        with closing(sqlite3.connect(store._db_path,timeout=.5)) as other, other:
            if write=='unrelated':
                other.execute("INSERT INTO settings(key,value) VALUES ('calendar-verification-probe','available')")
            elif write=='source':
                other.execute('UPDATE play_events SET played_at=played_at+1 WHERE id=(SELECT min(id) FROM play_events)')
            else:
                other.execute('UPDATE '+indexes.EVENTS+' SET played_at=played_at+1')
        committed.append(write)

    def during(*args, **kwargs):
        result = rows(*args, **kwargs)
        if not committed:
            assert store.conn.in_transaction
            mutate()
        return result

    def after(*args, **kwargs):
        result = verify(*args, **kwargs)
        assert not store.conn.in_transaction
        mutate()
        return result

    if write=='lookup':
        monkeypatch.setattr(indexes,'verify',after)
    else:
        monkeypatch.setattr(indexes.bounded,'rows',during)
    if write=='unrelated':
        indexes.publish(store.conn,revision=revision)
        assert indexes.selected(store.conn)
        assert_lookup_rows(store.conn)
    else:
        with pytest.raises(MigrationError,match='Calendar inputs changed|reconciled preparation'):
            indexes.publish(store.conn,revision=revision)
        assert not indexes.selected(store.conn)
    assert committed==[write]


def test_older_prepared_lookup_requires_verification_before_it_can_serve(store):
    seed(store)
    old = replace(indexes.job(store.conn),additive_destinations=())
    old.install(store.conn)
    for _ in range(150):
        if old.step(store.conn).status=='prepared':
            break
    with store.conn:
        store.conn.execute('INSERT INTO '+indexes.SELECTION+' VALUES (1,?,1)',(old.progress(store.conn).input_revision,))
    assert not indexes.selected(store.conn)
    regular.validate(store.conn)
    indexes.job(store.conn).install(store.conn)
    assert not indexes.selected(store.conn)
    indexes.publish(store.conn,revision=finish(store.conn).input_revision)
    assert indexes.selected(store.conn)
    assert_lookup_rows(store.conn)
    store.conn.execute('DROP TRIGGER '+indexes.EVENTS+'_custody_update')
    with pytest.raises(MigrationError,match='calendar lookup custody'):
        regular.validate(store.conn)


def test_selected_indexes_keep_following_writers_when_a_later_explicit_batch_fails(store):
    accounts,at = seed(store)
    job = indexes.job(store.conn)
    job.install(store.conn)
    indexes.publish(store.conn,revision=finish(store.conn).input_revision)
    store.history.record_play_event(accounts[0],'song-0',None,at+7200)
    def fail(*args):
        job.copy(*args)
        raise OSError('controlled post-publication failure')
    with pytest.raises(OSError,match='post-publication failure'):
        replace(job,copy=fail).step(store.conn)
    assert job.progress(store.conn).status == 'failed'
    store.history.record_play_event(accounts[0],'later',None,at+14400)
    with store.conn:
        listening.observe_day(store.conn,accounts[0],'later','2026-10-21','ytm_history','UTC',at+14400)
    assert job.progress(store.conn).status == 'failed'
    assert indexes.selected(store.conn)
    assert_lookup_rows(store.conn)
    regular.validate(store.conn)
