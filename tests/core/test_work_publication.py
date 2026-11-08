"""Verified atomic publication and continuing writes across the retained format."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.independent_schema_support import independent_regular_ddl
from tests.prepared_fixture_support import capture_template, coordinates
from tests.store_fixture_support import publication_consumer_reads
from tests.test_installed_fixture import state
from yt_playlist.core import file_owners, recommendation_work
from yt_playlist.core.migrations import MigrationError, migrate, registry, regular
from yt_playlist.core.preparation.recommendation_state import BYTE_LIMITS, JOBS
from yt_playlist.core.preparation import work_publication
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Publication owns SQL/files and does not construct an HTTP lifespan."""


def prepare(store):
    store.rec_proposals.put_proposals('daily_model_schedule', {'next_at': 123}, 100)
    store.rec_proposals.put_proposals('tv_preparation', {'failed': False, 'at': 99}, 101)
    JOBS['work'].install(store.conn)
    return JOBS['work'].step(store.conn).prepared_revision


def test_work_schema_input_keeps_real_installed_history_and_guards(_bounded_installed_schema):
    source = _bounded_installed_schema(81).source
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert source.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 81
    assert source._snapshot_revisions.conn is source.conn


@pytest.fixture(scope='module')
def published_work_template(tmp_path_factory, _bounded_installed_schema):
    root = tmp_path_factory.mktemp('published-work-template')
    with _bounded_installed_schema(81).open(root / 'source.db') as source:
        work_publication.publish(source.conn, revision=prepare(source))
        assert not coordinates(source.conn)
        yield capture_template(source, None)


def test_published_work_input_keeps_read_only_history_and_real_private_guards(published_work_template):
    source = published_work_template.source
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert source.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 81
    assert source._snapshot_revisions.conn is source.conn
    assert source.model_selection.next_daily_at() == 123
    assert source.tv_preparation.state() == {'failed': False, 'at': 99}


@pytest.fixture
def published_work(tmp_path, published_work_template):
    with published_work_template.open(tmp_path / 'invalid.db') as store:
        history = [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
        assert recommendation_work.prepared_owner(store.conn)
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 81
        assert store.conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert store.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        yield store
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')] == history
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def published_state(conn):
    result = state(conn)
    # Independent real producers observe different wall-clock instants. Keep
    # all other schema/data/history values, including source observation times.
    for table, column in ((JOBS['work'].state, 'updated_at'), ('published_work', 'published_at')):
        columns = [row[1] for row in conn.execute(f'PRAGMA table_info({table})')]
        index = columns.index(column)
        kind, sql, rows = result[table]
        result[table] = kind, sql, sorted((row[:index] + row[index + 1:] for row in rows), key=repr)
    return result


@pytest.mark.parametrize('installed', [79, 81], indirect=True)
def test_publication_preserves_originals_and_supports_existing_and_new_readers(installed, publication_consumer_reads):
    store, version = installed
    revision = prepare(store)
    assert revision is not None and not recommendation_work.available(store.conn)
    original = [tuple(r) for r in store.conn.execute("SELECT * FROM rec_proposals WHERE surface IN "
                                                   "('daily_model_schedule','tv_preparation') ORDER BY surface")]
    work_publication.publish(store.conn, revision=revision)
    work_publication.publish(store.conn, revision=revision)  # idempotent, no recopy
    assert recommendation_work.available(store.conn)
    assert store.model_selection.next_daily_at() == 123
    assert store.tv_preparation.state() == {'failed': False, 'at': 99}
    # Prove runtime reads the admitted typed owner. The temporary change is
    # rolled back and cannot conceal a stale retained source write.
    store.conn.execute('BEGIN')
    store.conn.execute('UPDATE prepared_model_selection_schedule SET next_at=456')
    assert store.model_selection.next_daily_at() == 456
    store.conn.rollback()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert reopened.model_selection.next_daily_at() == 123
        reopened.model_selection.schedule_daily(234, 200)
        reopened.tv_preparation.record(True, 201)
    finally:
        reopened.conn.close()
    assert store.model_selection.next_daily_at() == 234
    assert store.rec_proposals.get_proposals('tv_preparation') == {'failed': True, 'at': 201}
    with closing(sqlite3.connect(store._db_path)) as old_writer, old_writer:
        old_writer.execute('UPDATE rec_proposals SET payload=?,built_at=300 WHERE surface=?',
                           ('{"next_at":345}', 'daily_model_schedule'))
    assert store.model_selection.next_daily_at() == 345
    store.rec_proposals.put_proposals('tv_preparation', {}, 301)
    assert not store.rec_proposals.has_proposals('tv_preparation')
    with store.conn:
        store.conn.execute("UPDATE rec_proposals SET surface='retained-other',payload='opaque {text' WHERE surface='daily_model_schedule'")
    assert store.model_selection.next_daily_at() == 0
    store.model_selection.schedule_daily(500, 500)
    assert store.model_selection.next_daily_at() == 500
    assert [tuple(r) for r in store.conn.execute('SELECT * FROM published_work_originals ORDER BY surface')] == original
    assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == version
    assert JOBS['work'].progress(store.conn).prepared_revision is None


@pytest.mark.parametrize('installed', [79, 81], indirect=True)
def test_real_independent_ddl_keeps_work_publication_and_original_history(installed, independent_regular_ddl):
    store, version = installed
    work_publication.publish(store.conn, revision=prepare(store))
    # The real runner must commit independent DDL over a published owner.
    # Complete application-registry upgrades belong to its retained owners in
    # test_independent_schema_changes, rather than every publication consumer.
    regular.migrate(store.conn)
    assert recommendation_work.available(store.conn)
    assert store.model_selection.next_daily_at() == 123
    assert store.tv_preparation.state() == {'failed': False, 'at': 99}
    assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version


@pytest.mark.parametrize('installed', [81], indirect=True)
def test_private_work_input_matches_independent_real_publication(
        installed, published_work, published_work_template):
    control, _ = installed
    work_publication.publish(control.conn, revision=prepare(control))
    template = published_work_template.source.conn
    assert published_state(published_work.conn) == published_state(template) == published_state(control.conn)


@pytest.mark.parametrize('installed', [81], indirect=True)
def test_private_work_copy_cannot_mutate_its_source_or_independent_producer(
        installed, published_work, published_work_template):
    control, _ = installed
    work_publication.publish(control.conn, revision=prepare(control))
    template = published_work_template.source.conn
    before = list(template.iterdump())
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        template.execute('UPDATE prepared_model_selection_schedule SET next_at=999')
    assert published_work._db_path != control._db_path and published_work._evidence_key != control._evidence_key
    for module in file_owners.owners().values():
        assert module.directory(published_work.conn) != module.directory(control.conn)
    published_work.model_selection.schedule_daily(456, 200)
    assert published_work.model_selection.next_daily_at() == 456
    assert control.model_selection.next_daily_at() == 123
    assert list(template.iterdump()) == before


@pytest.mark.parametrize('installed', [81], indirect=True)
def test_stale_or_corrupted_staging_cannot_publish(installed):
    store, _ = installed
    revision = prepare(store)
    with store.conn:
        store.conn.execute('UPDATE prepared_model_selection_schedule SET next_at=999')
    with pytest.raises(MigrationError, match='differs from its verified input'):
        work_publication.publish(store.conn, revision=revision)
    assert not recommendation_work.available(store.conn)
    with store.conn:
        store.conn.execute('UPDATE prepared_model_selection_schedule SET next_at=123')
    store.model_selection.schedule_daily(789, 200)
    with pytest.raises(MigrationError, match='reconciled preparation revision'):
        work_publication.publish(store.conn, revision=revision)
    revision = JOBS['work'].step(store.conn).prepared_revision
    work_publication.publish(store.conn, revision=revision)
    assert store.model_selection.next_daily_at() == 789


@pytest.mark.parametrize('version', [79, 81])
def test_interrupted_publication_rolls_back_and_explicit_historical_route_still_works(
        tmp_path, version, _bounded_installed_schema):
    with _bounded_installed_schema(version).open(tmp_path / 'historical.db') as store:
        revision = prepare(store)
        class InterruptCommit:
            def __getattr__(self, name):
                return getattr(store.conn, name)
            def commit(self):
                raise KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            work_publication.publish(InterruptCommit(), revision=revision)
        assert not store.conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='published_work'").fetchone()
        assert store.model_selection.next_daily_at() == 123
        work_publication.publish(store.conn, revision=revision)
        store.model_selection.schedule_daily(999, 200)
        # The actual frozen callback can still advance the genuine v81 stamp;
        # publication retained every predecessor document and migration stamp.
        # Frozen table rebuilds run on a physical-owner connection, without
        # a serving connection's TEMP read adapters shadowing installed owners.
        with closing(sqlite3.connect(store._db_path)) as converter:
            converter.row_factory = sqlite3.Row
            converter.execute('PRAGMA foreign_keys=ON')
            migrate(converter, registry=registry.MIGRATIONS[:82])
        assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 82
        assert not recommendation_work.prepared_owner(store.conn)
        assert recommendation_work.read(store.conn, 'daily_model_schedule') == {'next_at': 999}


@pytest.mark.parametrize('surface,payload', [
    ('daily_model_schedule', payload) for payload in (
        '{"next_at":null}', '{"next_at":true}', '{"next_at":"1"}', '{"extra":1}', '[]',
        '{"next_at":1e999}', '{"next_at":1,"next_at":2}', '{}' + ' ' * BYTE_LIMITS['work'])
] + [
    ('tv_preparation', payload) for payload in (
        '{"failed":0}', '{"at":1}', '{"failed":true,"at":null}',
        '{"failed":false,"at":true}', '{"failed":false,"at":1e999}',
        '{"failed":false,"unknown":1}')
])
def test_publication_fences_invalid_continuing_source_writes(published_work, surface, payload):
    store = published_work
    with pytest.raises(sqlite3.DatabaseError):
        with store.conn:
            store.conn.execute('UPDATE rec_proposals SET payload=? WHERE surface=?',
                               (payload, surface))
    assert store.model_selection.next_daily_at() == 123
    assert json.loads(store.conn.execute("SELECT payload FROM rec_proposals WHERE surface='daily_model_schedule'").fetchone()[0]) == {'next_at': 123}


@pytest.mark.parametrize('installed', [81], indirect=True)
def test_startup_rejects_an_incomplete_published_contract(installed):
    store, _ = installed
    work_publication.publish(store.conn, revision=prepare(store))
    store.conn.execute('BEGIN')
    try:
        store.conn.execute('DROP TRIGGER published_work_sync_update')
        with pytest.raises(MigrationError, match='publication object'):
            regular.validate(store.conn)
    finally:
        store.conn.rollback()
