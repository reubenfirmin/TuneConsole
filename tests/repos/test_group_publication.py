"""Hidden batches must preserve serving state and the final source revision guard."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
from threading import Event

import pytest

from tests.group_support import publish, recordings
from tests.test_group_preparation import seed
from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.migrations.runner import MigrationError
from yt_playlist.core.migrations import v059_group_publication, v060_group_build_expiry
from yt_playlist.core.activity import WorkClosing
from yt_playlist.core.store import Store
from yt_playlist.rec import group_discovery, group_preparation, group_versions
from yt_playlist.repos import group_publication


@pytest.fixture
def database(tmp_path):
    store = Store(tmp_path / 'group-publication.db')
    store.init_schema()
    yield store
    store.conn.close()


def prepare(store, tracks, previous):
    return group_versions.prepare(group_discovery.discover(tracks), tracks, previous, [],
        now=2000, serial=store.taste_groups.serial(), input_fingerprint='second',
        policy=group_discovery.Policy())


def graph(store):
    return [tuple(row) for row in store.conn.execute('SELECT * FROM rec_group_edges ORDER BY left_key,right_key')]


def test_foreground_can_read_and_write_between_hidden_batches(database, monkeypatch):
    tracks = recordings([('House', 12, .3)])
    first = publish(database, tracks)
    edges = graph(database)
    pending = prepare(database, tracks, first)
    paused, release = Event(), Event()
    original = group_publication._write
    writes = 0

    @contextmanager
    def write(repo):
        nonlocal writes
        with original(repo) as conn:
            yield conn
        writes += 1
        if writes == 2:  # Header and first definitions are already committed.
            paused.set()
            assert release.wait(5)

    monkeypatch.setattr(group_publication, '_write', write)
    with database.read_snapshot() as snapshot:
        assert snapshot.store.taste_groups.head(members=True) == first
        snapshot.store.conn.rollback()
        with ThreadPoolExecutor(2) as pool:
            work = pool.submit(database.taste_groups.stage, pending)
            try:
                assert paused.wait(5)

                def foreground():
                    database.settings.set_setting('foreground-setting', 'responsive')
                    return database.taste_groups.head(members=True), graph(database)

                assert pool.submit(foreground).result(2) == (first, edges)
                assert database.taste_groups.definitions(pending['generation'], members=True) == []
                assert database.taste_groups.definition(pending['groups'][0]['version']) is None
                assert database.taste_groups.leaf_members(pending['generation']) == {}
                assert not any(row['target'] == pending['groups'][0]['version']
                               for row in database.taste_groups.lineage())
            finally:
                release.set()
            owner = work.result(5)
        assert snapshot.publish(lambda: database.taste_groups.finish_staged(pending['generation'], owner),
                                allow_stale_tables=group_publication.STAGED_TABLES)
    assert database.taste_groups.head_generation() == pending['generation']
    assert database.taste_groups.definitions(first['generation'], members=True) == first['groups']
    assert set(database.taste_groups.leaf_members(pending['generation'])) == {r.key for r in tracks}
    assert not database.conn.execute('SELECT 1 FROM rec_group_builds').fetchone()
    assert not database.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('change', ['input', 'unrelated-setting', 'external-writer', 'schema', 'head'])
def test_staging_keeps_original_input_and_publication_guards(database, monkeypatch, change):
    tracks = recordings([('Pop', 12, .2)])
    seed(database, tracks)
    worker = group_preparation.Preparation(database)
    assert worker.step(1000)
    first = database.taste_groups.head(members=True)
    original = database.taste_groups.stage
    staged = []

    def stage(prepared):
        owner = original(prepared)
        staged.append(prepared['generation'])
        if change == 'input':
            with database.conn:
                database.conn.execute('UPDATE rec_musical_profiles SET prepared_at=prepared_at+1')
        elif change == 'unrelated-setting':
            database.settings.set_setting('foreground-setting', 'new')
        elif change in ('external-writer', 'schema'):
            with sqlite3.connect(database._db_path) as other:
                other.execute("INSERT INTO settings VALUES ('external-setting','new')" if change == 'external-writer'
                              else 'CREATE INDEX unrelated_external_index ON settings(value)')
        else:
            publish(database, tracks, 2100, first)
        return owner

    monkeypatch.setattr(database.taste_groups, 'stage', stage)
    worker.request_rebuild()
    assert worker.step(2000) == (change == 'unrelated-setting')
    if change == 'unrelated-setting':
        assert database.taste_groups.head_generation() == staged[0]
    else:
        assert database.taste_groups.head_generation() != staged[0]
        assert database.taste_groups.definitions(staged[0]) == []
        assert database.conn.execute('SELECT ready FROM rec_group_builds WHERE generation=?',
                                     (staged[0],)).fetchone()[0] == 1
    assert database.taste_groups.definitions(first['generation'], members=True) == first['groups']


@pytest.mark.parametrize('failure', ['sql', 'cancel'])
def test_failed_batch_and_expired_cleanup_preserve_published_history(database, monkeypatch, failure):
    tracks = recordings([('House', 12, .3)])
    first = publish(database, tracks)
    edges = graph(database)
    pending = prepare(database, tracks + recordings([('Jazz', 8, .8)]), first)
    pending['edges'].append(('broken', 'edge', None, 1))
    monkeypatch.setattr(group_publication, 'BATCH_ROWS', 3)
    if failure == 'cancel':
        original = group_publication._write
        writes = 0

        @contextmanager
        def interrupted(repo):
            nonlocal writes
            with original(repo) as conn:
                yield conn
            writes += 1
            if writes == 2:
                raise WorkClosing('Injected shutdown between group batches')

        monkeypatch.setattr(group_publication, '_write', interrupted)
    with pytest.raises(sqlite3.IntegrityError if failure == 'sql' else WorkClosing):
        database.taste_groups.stage(pending)
    assert database.taste_groups.head(members=True) == first
    assert graph(database) == edges
    assert database.taste_groups.definitions(pending['generation']) == []
    owner = database.conn.execute('SELECT owner FROM rec_group_builds WHERE generation=?',
                                  (pending['generation'],)).fetchone()[0]
    with pytest.raises(RuntimeError, match='incomplete or no longer owned'):
        database.conn.execute('BEGIN')
        with database.conn:
            database.taste_groups.finish_staged(pending['generation'], owner)
    assert not database.taste_groups.expired_builds()
    assert not group_publication.cleanup(database.taste_groups)  # An active lease stays intact.
    tables = ('rec_group_generations', 'rec_group_builds', 'rec_group_definitions',
              'rec_group_identities', 'rec_group_members', 'rec_group_lineage',
              'rec_group_unknown', 'rec_group_graph')

    def size():
        return sum(database.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in tables)

    for _ in range(200):
        before = size()
        if not group_publication.cleanup(database.taste_groups, before=float('inf')):
            break
        assert 0 < before - size() <= 2 * group_publication.BATCH_ROWS
        assert database.taste_groups.head(members=True) == first
    else:
        pytest.fail('Expired build cleanup did not finish')
    assert not database.conn.execute('SELECT 1 FROM rec_group_generations WHERE generation=?',
                                     (pending['generation'],)).fetchone()
    assert graph(database) == edges
    assert not database.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_new_head_hides_old_assignments_before_bounded_cache_cleanup(database, monkeypatch):
    tracks = recordings([('House', 12, .3)])
    first = publish(database, tracks)
    database.conn.execute('BEGIN')
    with database.conn:
        database.taste_groups.publish_assignments([
            ('candidate', first['generation'], first['groups'][0]['version'], .5, .7, 'old', 1000)])
    assert database.taste_groups.assignment('candidate')['profile_digest'] == 'old'
    pending = prepare(database, tracks, first)
    owner = database.taste_groups.stage(pending)
    database.conn.execute('BEGIN')
    with database.conn:
        database.taste_groups.finish_staged(pending['generation'], owner)
    assert database.taste_groups.assignment('candidate') is None
    database.conn.execute('BEGIN')
    with database.conn:
        database.taste_groups.publish_assignments([
            ('candidate', pending['generation'], pending['groups'][0]['version'], .8, .9, 'new', 2000)])
    assert database.conn.execute('SELECT COUNT(*) FROM rec_group_assignments').fetchone()[0] == 2
    current_edges = graph(database)
    monkeypatch.setattr(group_publication, 'BATCH_ROWS', 3)
    for _ in range(200):
        if not database.taste_groups.cleanup_staged():
            break
    else:
        pytest.fail('Obsolete cache cleanup did not finish')
    assert database.taste_groups.assignment('candidate')['profile_digest'] == 'new'
    assert database.conn.execute('SELECT COUNT(*) FROM rec_group_assignments').fetchone()[0] == 1
    assert graph(database) == current_edges
    assert database.taste_groups.definitions(first['generation'], members=True) == first['groups']


def test_published_history_rejects_appends_and_incomplete_head_switches(database):
    first = publish(database, recordings([('House', 8, .3)]))
    version = first['groups'][0]['version']
    mutations = [
        ('INSERT INTO rec_group_members VALUES (?,\'new\',.5,.5)', (version,)),
        ('INSERT INTO rec_group_unknown VALUES (?,\'new\',\'missing\')', (first['generation'],)),
        ('DELETE FROM rec_group_publications WHERE generation=?', (first['generation'],)),
        ('UPDATE rec_group_identities SET created_at=0 WHERE group_id=?', (first['groups'][0]['group_id'],)),
    ]
    for sql, values in mutations:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            with database.conn:
                database.conn.execute(sql, values)
    pending = prepare(database, recordings([('Jazz', 8, .8)]), first)
    owner = database.taste_groups.stage(pending)
    with pytest.raises(sqlite3.IntegrityError, match='unpublished'):
        with database.conn:
            database.conn.execute('UPDATE rec_group_head SET generation=?', (pending['generation'],))
    with pytest.raises(RuntimeError, match='incomplete or no longer owned'):
        database.conn.execute('BEGIN')
        with database.conn:
            database.taste_groups.finish_staged(pending['generation'], owner + '-wrong')
    assert database.taste_groups.head(members=True) == first


def test_upgrade_preserves_existing_head_history_graph_and_assignments(tmp_path):
    store = Store(tmp_path / 'version58.db')
    try:
        migrate(store.conn, registry=MIGRATIONS[:58])
        with store.conn:
            store.conn.execute("INSERT INTO rec_group_generations VALUES ('old','policy','fingerprint',10,0,'{}')")
            store.conn.execute("INSERT INTO rec_group_identities VALUES ('group',NULL,10)")
            store.conn.execute('INSERT INTO rec_group_definitions VALUES (\'version\',\'old\',\'group\',NULL,?)',
                               (json.dumps({'name': 'Existing', 'leaf': True}),))
            store.conn.execute("INSERT INTO rec_group_members VALUES ('version','song',.8,.9)")
            store.conn.execute("INSERT INTO rec_group_unknown VALUES ('old','unknown','missing')")
            store.conn.execute("INSERT INTO rec_group_head VALUES (1,'old')")
            store.conn.execute("INSERT INTO rec_group_edges VALUES ('song','other',.7,.9)")
            store.conn.execute("INSERT INTO rec_group_assignments VALUES ('assigned','old','version',.6,.7,'digest',10)")
        tables = ('rec_group_generations', 'rec_group_identities', 'rec_group_definitions',
                  'rec_group_members', 'rec_group_unknown', 'rec_group_head', 'rec_group_edges',
                  'rec_group_assignments')
        before = {table: [tuple(row) for row in store.conn.execute(f'SELECT * FROM {table}')] for table in tables}
        store.init_schema()
        assert before == {table: [tuple(row) for row in store.conn.execute(f'SELECT * FROM {table}')] for table in tables}
        assert store.taste_groups.head_generation() == 'old'
        assert store.taste_groups.assignment('assigned')['version'] == 'version'
        assert store.taste_groups.leaf_members('old') == {'song': 'version'}
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        with store.conn:
            store.conn.execute('DROP TRIGGER immutable_rec_group_members_insert')
        with pytest.raises(MigrationError, match='immutable_rec_group_members_insert'):
            v059_group_publication.validate(store.conn)
    finally:
        store.conn.close()


@pytest.mark.parametrize('existing_index', ['absent', 'present', 'incompatible'])
def test_already_applied_group_migration_can_reach_expiry_lookup_upgrade(tmp_path, existing_index):
    store = Store(tmp_path / 'version59.db')
    try:
        migrate(store.conn, registry=MIGRATIONS[:59])
        with store.conn:
            store.conn.execute("INSERT INTO rec_group_generations VALUES ('retained','policy','fp',10,0,'{}')")
            store.conn.execute("INSERT INTO rec_group_publications VALUES ('retained')")
            store.conn.execute("INSERT INTO rec_group_head VALUES (1,'retained')")
            if existing_index != 'absent':
                store.conn.execute(v060_group_build_expiry.SQL if existing_index == 'present' else
                    'CREATE INDEX ix_group_build_expiry ON rec_group_builds(owner)')
        before = [tuple(row) for row in store.conn.execute('SELECT * FROM rec_group_generations')]
        # Startup validates v59 before attempting v60. The later lookup must not
        # retroactively become a v59 requirement and prevent reaching its upgrade.
        if existing_index == 'incompatible':
            with pytest.raises(MigrationError, match='ix_group_build_expiry'):
                migrate(store.conn)
            assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 59
        else:
            store.init_schema()
            assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == MIGRATIONS[-1].version
            v060_group_build_expiry.validate(store.conn)
            assert store.taste_groups.head_generation() == 'retained'
        assert before == [tuple(row) for row in store.conn.execute('SELECT * FROM rec_group_generations')]
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()
