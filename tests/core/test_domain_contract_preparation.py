"""Future-write guards and historical audit proofs have independent lifetimes."""
from dataclasses import replace
import json
import sqlite3

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.prepared_fixture_support import assert_upgrade_phase, custody_predecessor, custody_upgrade_cases
from tests.store_fixture_support import assert_installed_consumer_input, publication_consumer_reads
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import domain_contracts as audit
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture
def disable_periodic_library_sync():
    pass


@pytest.mark.parametrize('installed', [79, 93, 103, 107], indirect=True)
def test_domain_preparation_input_keeps_admitted_history_and_private_guards(installed):
    assert_installed_consumer_input(*installed)


def finish(job, conn):
    for _ in range(100):
        progress = job.step(conn,batch_size=512)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Contract audit did not finish bounded fixture')


@pytest.mark.parametrize('installed',[79,93,103,107],indirect=True)
def test_future_guards_are_independent_of_audit_and_preserve_history(installed):
    store, _ = installed
    store.tracks.upsert_track('one','One','Artist',None,10)
    job = audit.job(store.conn,'youtube_items')
    job.install(store.conn)
    assert job.progress(store.conn).processed == 0
    assert not store.conn.execute('SELECT 1 FROM '+audit.table(job,'rows')).fetchone()
    with pytest.raises(sqlite3.IntegrityError,match='Invalid future youtube_items.available'):
        with store.conn:
            store.conn.execute("UPDATE main.youtube_items SET available=2 WHERE video_id='one'")
    with store.conn:
        store.conn.execute("UPDATE main.youtube_items SET available=NULL WHERE video_id='one'")
    finish(job,store.conn)
    verified = audit.verify(store.conn,job)
    row = store.conn.execute('SELECT * FROM '+audit.table(job,'rows')).fetchone()
    assert row['violation_count'] == 0
    assert audit.original(store.conn,job,row['source_key'],row['source_digest']) == {
        'key':['one'],'fields':{'available':{'type':'null','value':None}}}
    with store.conn:
        store.conn.execute("UPDATE main.youtube_items SET available=0 WHERE video_id='one'")
    with pytest.raises(MigrationError,match='reconciled preparation'):
        audit.verify(store.conn,job)
    finish(job,store.conn)
    assert audit.verify(store.conn,job).input_revision > verified.input_revision


@pytest.mark.parametrize('installed',[93],indirect=True)
def test_invalid_history_is_retained_and_blocks_verification_without_blocking_ddl(installed,monkeypatch):
    store, _ = installed
    store.discovery.upsert_discovered_artist('Candidate',2,[],[],None,1)
    with store.conn:
        store.conn.execute('UPDATE main.discovered_artists SET score=?',(float('inf'),))
    job = audit.job(store.conn,'discovered_artists')
    job.install(store.conn)
    finish(job,store.conn)
    captured = store.conn.execute('SELECT * FROM '+audit.table(job,'rows')).fetchone()
    assert json.loads(captured['violations']) == ['score']
    original = audit.original(store.conn,job,captured['source_key'],captured['source_digest'])
    assert original['fields']['score'] == {'type':'real','value':'+Infinity'}
    with pytest.raises(MigrationError,match='Invalid historical discovered_artists'):
        audit.verify(store.conn,job)
    progress = job.progress(store.conn)
    def install(conn,context):
        conn.execute('CREATE TABLE independent_contract_ddl(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='independent_contract_ddl'").fetchone()
    monkeypatch.setattr(regular,'CHANGES',(regular.Change(1,'independent_contract_ddl',install,validate),))
    regular.migrate(store.conn)
    assert job.progress(store.conn) == progress
    with pytest.raises(sqlite3.IntegrityError,match='Invalid future discovered_artists.score'):
        with store.conn:
            store.conn.execute('UPDATE main.discovered_artists SET score=?',(float('-inf'),))
    with store.conn:
        store.conn.execute('UPDATE main.discovered_artists SET score=3')
    finish(job,store.conn)
    audit.verify(store.conn,job)
    assert audit.original(store.conn,job,captured['source_key'],captured['source_digest']) == original


@pytest.mark.parametrize('installed',[93],indirect=True)
def test_bounded_batches_continuing_sources_failure_and_reopened_retry(installed):
    store, _ = installed
    # This subject audits stored availability values. Catalog ingestion has its
    # own owners; populate the minimum real rows the audit needs in one commit.
    with store.conn:
        recording = store.conn.execute(
            "INSERT INTO recordings(identity_key) VALUES ('audit-recording')").lastrowid
        store.conn.executemany(
            'INSERT INTO youtube_items(video_id,recording_id,available) VALUES (?,?,1)',
            [('video-'+str(index), recording) for index in range(140)])
    job = audit.job(store.conn,'youtube_items')
    job.install(store.conn)
    checkpoint = job.step(store.conn,batch_size=512)
    assert checkpoint.processed == 32
    def fail(*args):
        job.copy(*args)
        raise OSError('controlled audit failure')
    with pytest.raises(OSError,match='audit failure'):
        replace(job,copy=fail).step(store.conn)
    assert job.progress(store.conn).status == 'failed'
    assert job.progress(store.conn).processed == checkpoint.processed
    with pytest.raises(sqlite3.IntegrityError,match='Invalid future'):
        with store.conn:
            store.conn.execute("UPDATE main.youtube_items SET available=2 WHERE video_id='video-0'")
    store.tracks.upsert_track('z-later','Later','Artist',None,10)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError,match='retry explicitly'):
            job.step(reopened.conn)
        job.step(reopened.conn,retry=True)
        finish(job,reopened.conn)
        audit.verify(reopened.conn,job)
        assert reopened.conn.execute('SELECT count(*) FROM '+audit.table(job,'rows')).fetchone()[0] == 141
    finally:
        reopened.conn.close()


def prepare_native_comparison(store):
    store.tracks.upsert_track('one','One','Artist',None,10)
    job = audit.job(store.conn,'youtube_items')
    job.install(store.conn)
    finish(job,store.conn)
    captured = store.conn.execute('SELECT * FROM '+audit.table(job,'rows')).fetchone()
    return captured['source_key'], captured['source_digest'], audit.original(store.conn, job, captured['source_key'], captured['source_digest'])


@pytest.fixture(scope='module')
def native_inputs(upgraded_custody_inputs):
    def get(target, **options):
        return upgraded_custody_inputs(79, target, 'contract-native-agreement', prepare_native_comparison, **options)

    get.release = lambda: upgraded_custody_inputs.release(79, 'contract-native-agreement')
    return get


def assert_native_original(template):
    key, digest, original = template.result
    conn = template.source.conn
    job = audit.job(conn, 'youtube_items')
    assert audit.original(conn, job, key, digest) == original


def test_native_comparison_input_keeps_exact_preparation(native_inputs):
    template = native_inputs(79)
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert_native_original(template)


@pytest.mark.parametrize('version,target,phase', custody_upgrade_cases((79,)))
def test_native_upgrade_phase_keeps_exact_preparation(native_inputs, version, target, phase):
    template = native_inputs(target, phase=phase)
    assert_upgrade_phase(template, target if phase == 'converted' else custody_predecessor(version, target))
    assert_native_original(template)


def test_native_serving_input_keeps_private_read_guards(native_inputs):
    template = native_inputs(107)
    assert_upgrade_phase(template, 107)
    assert template.source._snapshot_revisions.conn is template.source.conn


@pytest.mark.parametrize('version', [79])
def test_historical_conversion_preserves_guard_and_audit_custody(native_inputs, version, tmp_path):
    with native_inputs(107).open(tmp_path / 'contract-upgrade.db') as upgraded:
        job = audit.job(upgraded.conn, 'youtube_items')
        job.validate(upgraded.conn)
        finish(job,upgraded.conn)
        audit.verify(upgraded.conn,job)
        key, digest, original = native_inputs(version).result
        assert audit.original(upgraded.conn, job, key, digest) == original
        with pytest.raises(sqlite3.IntegrityError):
            with upgraded.conn:
                upgraded.conn.execute("UPDATE main.youtube_items SET available=2 WHERE video_id='one'")
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed',[103],indirect=True)
@pytest.mark.parametrize('owner', audit.CONTRACTS)
def test_future_field_guards_match_each_owner_on_real_predecessor(installed, owner):
    from tests.core.test_domain_contracts import seed
    store, _ = installed
    seed(store.conn)
    selected = audit.job(store.conn,owner)
    selected.install(store.conn)
    finish(selected,store.conn)
    audit.verify(store.conn,selected)
    assert store.conn.execute('SELECT count(*) FROM main.' + owner).fetchone()[0] > 0
    store.conn.execute('SAVEPOINT future_contract')
    try:
        for column,expression in audit.CONTRACTS[owner].items():
            bad = float('inf') if 'abs(' in expression else 2 if ' IN (0,1)' in expression else 0 if '>=1' in expression else -1
            with pytest.raises(sqlite3.IntegrityError):
                store.conn.execute('UPDATE main.' + owner + ' SET ' + column + '=?',(bad,))
            if 'integer' in expression and 'abs(' not in expression:
                with pytest.raises(sqlite3.IntegrityError):
                    store.conn.execute('UPDATE main.' + owner + ' SET ' + column + '=?',(1.5,))
            value = -1 if 'abs(' in expression else 1 if '>=1' in expression else 0
            store.conn.execute('UPDATE main.' + owner + ' SET ' + column + '=?',(value,))
            if 'IS NULL OR' in expression:
                store.conn.execute('UPDATE main.' + owner + ' SET ' + column + '=NULL')
    finally:
        store.conn.execute('ROLLBACK TO future_contract')
        store.conn.execute('RELEASE future_contract')
    audit.verify(store.conn,selected)


@pytest.mark.parametrize('installed',[93],indirect=True)
def test_exact_float_values_are_not_rounded_by_json_formatting(installed):
    store, _ = installed
    store.discovery.upsert_discovered_artist('Candidate',1.2345678901234567,[],[],None,1774980178.6276371)
    selected = audit.job(store.conn,'discovered_artists')
    selected.install(store.conn)
    finish(selected,store.conn)
    staged = store.conn.execute('SELECT * FROM '+audit.table(selected,'rows')).fetchone()
    captured = audit.original(store.conn,selected,staged['source_key'],staged['source_digest'])
    actual = store.conn.execute('SELECT score,found_at FROM main.discovered_artists').fetchone()
    assert captured['fields']['score']['value'] == actual['score']
    assert captured['fields']['found_at']['value'] == actual['found_at']


def test_native_sources_finish_with_committed_read_only_inputs(native_inputs):
    for source in native_inputs.release():
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            source.conn.execute('SELECT 1')
