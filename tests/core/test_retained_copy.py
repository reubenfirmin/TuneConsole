"""Fresh copying preserves installed owners, IDs and complete retained ranks."""
from contextlib import closing
from dataclasses import replace
import sqlite3

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.store_fixture_support import bounded_consumer_schema, native_sql_calls
from yt_playlist.core import recommendation_pools as pools
from yt_playlist.core.db_rebuild import build as rebuild
from yt_playlist.core.store import Store
from yt_playlist.repos import recommendation_publication as publication


pytestmark = pytest.mark.usefixtures('native_sql_calls')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """The compactor operates on private files without application workers."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    # Copying owns installed rows, retained files and reference/format checks;
    # genre inference and initial application admission are lower inputs.
    with bounded_consumer_schema(()) as schema:
        yield schema


@pytest.mark.parametrize('installed', [79, 93, 107], indirect=True)
def test_exact_installed_format_personal_values_hidden_ids_and_sequences(installed, tmp_path):
    store, version = installed
    store.settings.set_setting('personal value', 'retained')
    with store.conn:
        store.conn.execute('CREATE TABLE compaction_personal(id INTEGER PRIMARY KEY AUTOINCREMENT,value TEXT)')
        store.conn.execute("INSERT INTO compaction_personal VALUES(300,'high water')")
        store.conn.execute('DELETE FROM compaction_personal')
        store.conn.execute("INSERT INTO compaction_personal VALUES(7,'retained')")
        store.conn.execute('CREATE TABLE compaction_implicit(value TEXT,other TEXT,PRIMARY KEY(value,other))')
        store.conn.execute("INSERT INTO compaction_implicit(rowid,value,other) VALUES(91,'same','text')")
    before = list(store.conn.iterdump())
    output = tmp_path/'compact.db'
    report = rebuild.build(store._db_path, output)
    assert report['installed_version'] == version
    assert report['freelist_pages'] == 0
    assert list(store.conn.iterdump()) == before
    with closing(sqlite3.connect(output)) as copied:
        assert copied.execute('SELECT rowid,value,other FROM compaction_implicit').fetchall() == [(91,'same','text')]
        assert copied.execute("SELECT seq FROM sqlite_sequence WHERE name='compaction_personal'").fetchone() == (300,)
        assert copied.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall() == [tuple(row) for row in
            store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
        with copied:
            copied.execute("INSERT INTO compaction_personal(value) VALUES('continuing')")
        assert copied.execute('SELECT max(id) FROM compaction_personal').fetchone() == (301,)


@pytest.fixture
def ranked(tmp_path, consumer_store):
    store = consumer_store(tmp_path/'ranked.db')
    store.rec_ledger.record_serving_policy('policy', {}, 1)
    with store.conn:
        store.conn.executemany('INSERT INTO recordings(id,identity_key) VALUES(?,?)', [(i,'recording-'+str(i)) for i in range(1,130)])
    def publish(key, count=129):
        pool = pools.prepare(key,'policy',None,'a'*64,'card',[pools.Candidate(i,1/i) for i in range(1,count+1)])
        plan = publication.Plan(key,'policy',None,1,10,(publication.Slot('home','all','wheelhouse',pool),),())
        build = store.recommendations.claim(plan)
        store.recommendations.stage(plan,build)
        store.recommendations.publish(build,11)
        return build, plan
    try:
        yield store,publish
    finally:
        store.conn.close()


def test_retains_historical_references_live_builds_and_all_candidates(ranked,tmp_path):
    store,publish = ranked
    old,_ = publish('history')
    with store.conn:
        store.conn.execute("INSERT INTO recommendation_generation_refs VALUES('offer','saved',?)",(old.generation_id,))
    obsolete,_ = publish('obsolete',2)
    current,plan = publish('current')
    pending = store.recommendations.claim(replace(plan,external_key='in flight',created_at=20))
    before = list(store.conn.iterdump())
    report = rebuild.build(store._db_path,tmp_path/'compact.db',now=21)
    assert list(store.conn.iterdump()) == before
    assert report['tables']['recommendation_generations']['rows'] == 3
    with closing(sqlite3.connect(tmp_path/'compact.db')) as copied:
        assert copied.execute('SELECT id FROM recommendation_generations ORDER BY id').fetchall() == [
            (old.generation_id,),(current.generation_id,),(pending.generation_id,)]
        assert copied.execute('SELECT count(*) FROM recommendation_candidates').fetchone() == (258,)
        assert not copied.execute('PRAGMA foreign_key_check').fetchone()
        # Guards must already be restored when the builder reports success.
        with pytest.raises(sqlite3.IntegrityError):
            copied.execute('DELETE FROM recommendation_candidates')


def test_unknown_reference_owner_preserves_generation_and_complete_pool(ranked,tmp_path):
    store,publish = ranked
    old,_ = publish('owned history')
    publish('new head',2)
    with store.conn:
        store.conn.execute('CREATE TABLE compaction_history(id INTEGER PRIMARY KEY,generation_id INTEGER '
                           'REFERENCES recommendation_generations(id),evidence TEXT)')
        store.conn.execute("INSERT INTO compaction_history VALUES(1,?,'historical explanation')",(old.generation_id,))
    rebuild.build(store._db_path,tmp_path/'compact.db',now=10000)
    with closing(sqlite3.connect(tmp_path/'compact.db')) as copied:
        assert copied.execute('SELECT count(*) FROM recommendation_candidates').fetchone() == (131,)
        assert copied.execute('SELECT count(*) FROM recommendation_generations').fetchone() == (2,)
        assert not copied.execute('PRAGMA foreign_key_check').fetchone()


def test_metadata_retirement_refuses_an_unaudited_history_reference(tmp_path, consumer_store):
    from yt_playlist.core import metadata_runtime
    store = consumer_store(tmp_path/'history.db')
    try:
        with store.conn:
            metadata_runtime.activate(store.conn)
            store.conn.execute('CREATE TABLE future_metadata_owner(id INTEGER PRIMARY KEY,'
                               'observation_id INTEGER REFERENCES metadata_observations(id))')
        before = list(store.conn.iterdump())
        output = tmp_path/'rejected.db'
        with pytest.raises(ValueError, match='new reference audit: future_metadata_owner.observation_id'):
            rebuild.build(store._db_path, output)
        assert not output.exists()
        assert list(store.conn.iterdump()) == before
    finally:
        store.conn.close()


@pytest.mark.parametrize('failure',['cancelled','concurrent-write','validation'])
@pytest.mark.parametrize('installed',[79],indirect=True)
def test_failed_build_cannot_be_installed_and_leaves_source_exact(installed,tmp_path,monkeypatch,failure):
    store,_ = installed
    before = list(store.conn.iterdump())
    output = tmp_path/'failed.db'
    changed = False
    def progress(phase,detail):
        nonlocal changed
        if failure=='concurrent-write' and phase=='verify' and not changed:
            changed = True
            store.settings.set_setting('accepted meanwhile','retained')
    if failure=='validation':
        def fail(conn):
            raise ValueError('controlled independent validation failure')
        monkeypatch.setattr(rebuild,'_verify',fail)
    with pytest.raises((ValueError,sqlite3.OperationalError)):
        rebuild.build(store._db_path,output,progress=progress,cancelled=lambda:failure=='cancelled')
    assert not output.exists()
    if failure=='concurrent-write':
        assert store.settings.get_setting('accepted meanwhile')=='retained'
    else:
        assert list(store.conn.iterdump()) == before


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('declaration,initial,replacement', [
    ('TEXT COLLATE NOCASE', 'MiXeD personal note é', 'mixed personal note é'),
    ('BLOB', 1.0, 1),
    ('BLOB', b'exact\x00bytes', b'changed\x00bytes'),
])
def test_rejects_persisted_value_faults_despite_sql_equality(installed, tmp_path, monkeypatch,
                                                           declaration, initial, replacement):
    store, _ = installed
    with store.conn:
        store.conn.execute('CREATE TABLE personal_evidence(id INTEGER PRIMARY KEY, value ' + declaration + ')')
        store.conn.execute('INSERT INTO personal_evidence VALUES(1, ?)', (initial,))
    before = list(store.conn.iterdump())
    verify = rebuild._equal_rows
    def corrupt(conn, name, predicate):
        if name == 'personal_evidence':
            conn.execute('UPDATE compacted.personal_evidence SET value=? WHERE id=1', (replacement,))
            conn.commit()
            conn.execute('BEGIN')
        return verify(conn, name, predicate)
    monkeypatch.setattr(rebuild, '_equal_rows', corrupt)
    output = tmp_path / 'rejected.db'
    with pytest.raises(ValueError, match='changed retained values or IDs: personal_evidence'):
        rebuild.build(store._db_path, output)
    assert not output.exists()
    assert list(store.conn.iterdump()) == before
