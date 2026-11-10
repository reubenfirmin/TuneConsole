"""Scheduling and preparation outcomes survive independent cache replacement."""
from contextlib import closing
import sqlite3

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input, native_sql_calls, without_query_timing
from yt_playlist.core import schema_inspection
from yt_playlist.core.migrations import current, migrate, registry
from yt_playlist.core.migrations import v082_recommendation_work as migration
from yt_playlist.core.migrations.runner import Migration, MigrationError
from yt_playlist.core.store import Store
from yt_playlist.rec import daily_model_selection


@pytest.fixture
def disable_periodic_library_sync():
    pass

@pytest.fixture
def predecessor(tmp_path, _pristine_installed_schema):
    store = Store(tmp_path / 'work.db')
    with closing(store.conn):
        _pristine_installed_schema(81).backup(store.conn)
        store._bind_repositories()
        yield store


@pytest.fixture
def restart_input(tmp_path, monkeypatch, native_sql_calls, without_query_timing):
    # This consumer needs the completed edge input, not replay of its prefix.
    bound_catalog(monkeypatch)
    store = Store(tmp_path / 'work-restart.db')
    with closing(store.conn), schema_inspection.reuse_tables(store.conn):
        store.conn.execute('PRAGMA synchronous=OFF')
        # Build once on the real private file; no intermediate SQL clone is used.
        install_numbered_input(store.conn, 81)
        store.conn.execute('PRAGMA journal_mode=WAL')
        store._schema_compatibility = True
        store._rebuild_source_version = None
        store._schema_recovery_pending = False
        store._bind_repositories()
        yield store


def seed(store):
    rows = [('daily_model_schedule', '{"next_at":1234.5}', 1000),
            ('tv_preparation', '{"failed":false,"at":999.5}', 1001),
            ('unrelated-output', '{"keep":"original"}', 1002)]
    with store.conn:
        store.conn.executemany('INSERT INTO rec_proposals VALUES (?,?,?)', rows)


def upgrade(store):
    # Exercise this historical boundary in isolation. Later retirement of the
    # generic proposal owner intentionally rejects the unrelated fixture row.
    migrate(store.conn, registry=registry.MIGRATIONS[:82])
    store._bind_repositories()


def test_upgrade_restart_and_cache_eviction_preserve_work_state(restart_input):
    store = restart_input
    seed(store)
    upgrade(store)
    assert store.model_selection.next_daily_at() == 1234.5
    assert store.tv_preparation.state() == {'failed': False, 'at': 999.5}
    assert tuple(store.conn.execute('SELECT next_at,updated_at FROM model_selection_schedule').fetchone()) == (1234.5, 1000)
    assert store.conn.execute('SELECT surface FROM rec_proposals').fetchall()[0][0] == 'unrelated-output'
    with store.conn:
        store.conn.execute('DELETE FROM rec_proposals')
    reopened = Store(store._db_path)
    with closing(reopened.conn), schema_inspection.reuse_tables(reopened.conn):
        reopened.conn.execute('PRAGMA synchronous=OFF')
        reopened.init_schema()
        assert reopened.model_selection.next_daily_at() == 1234.5
        assert reopened.tv_preparation.state() == {'failed': False, 'at': 999.5}
        assert not daily_model_selection.due(reopened, 1200)
        assert daily_model_selection.due(reopened, 1300)
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('surface,raw', [
    ('daily_model_schedule', 'original invalid JSON'),
    ('daily_model_schedule', '{"next_at":true}'),
    ('daily_model_schedule', '{"next_at":"1234"}'),
    ('daily_model_schedule', '{"next_at":NaN}'),
    ('daily_model_schedule', '{"next_at":1234,"unclassified":"retain me"}'),
    ('tv_preparation', '{"failed":1}'),
    ('tv_preparation', '{"failed":false,"at":Infinity}'),
    ('tv_preparation', '{"failed":false,"unclassified":"retain me"}'),
])
def test_invalid_or_unclassified_legacy_state_stops_before_replacement(predecessor, surface, raw):
    store = predecessor
    seed(store)
    with store.conn:
        store.conn.execute('UPDATE rec_proposals SET payload=? WHERE surface=?', (raw, surface))
    before = list(store.conn.iterdump())
    with pytest.raises(MigrationError, match='original work state retained'):
        upgrade(store)
    assert list(store.conn.iterdump()) == before
    assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='model_selection_schedule'").fetchone()


def test_interruption_after_relocation_rolls_back_then_retries(predecessor):
    store = predecessor
    seed(store)
    before = list(store.conn.iterdump())

    def interrupted(conn, context):
        migration.apply(conn, context)
        raise KeyboardInterrupt('Controlled interruption before work-state commit')

    with pytest.raises(KeyboardInterrupt):
        migrate(store.conn, registry=(*registry.MIGRATIONS[:81],
                                    Migration(82, 'recommendation_work_state', interrupted)))
    assert list(store.conn.iterdump()) == before
    upgrade(store)
    assert store.model_selection.next_daily_at() == 1234.5
    assert store.tv_preparation.state() == {'failed': False, 'at': 999.5}


def test_fresh_and_upgraded_work_owners_have_the_same_contract(predecessor):
    upgrade(predecessor)
    migrate(predecessor.conn, registry=registry.MIGRATIONS)
    fresh = Store(':memory:')
    try:
        fresh.init_schema()
        for name in migration.OBJECTS:
            old = predecessor.conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()[0]
            new = fresh.conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()[0]
            assert current._tokens(old) == current._tokens(new)
        current.validate(predecessor.conn)
        current.validate(fresh.conn)
    finally:
        fresh.conn.close()


def test_predecessor_still_rejects_a_superseded_metadata_owner(predecessor):
    with predecessor.conn:
        predecessor.conn.execute('CREATE TABLE metadata_findings (retired TEXT)')
    before = list(predecessor.conn.iterdump())
    with pytest.raises(MigrationError, match='Superseded current owner remains: metadata_findings'):
        migrate(predecessor.conn, registry=registry.MIGRATIONS)
    assert list(predecessor.conn.iterdump()) == before


def test_current_validation_rejects_retired_work_even_after_recreated_guards(store):
    names = ('retired_recommendation_work_insert', 'retired_recommendation_bucket_insert')
    definitions = [store.conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()[0]
                   for name in names]
    with store.conn:
        for name in names:
            store.conn.execute('DROP TRIGGER ' + name)
        store.conn.execute("INSERT INTO rec_proposals VALUES ('daily_model_schedule','{}',1)")
        for sql in definitions:
            store.conn.execute(sql)
    with pytest.raises(MigrationError, match='Retired recommendation work state remains'):
        current.validate(store.conn)


@pytest.mark.parametrize('statement,values', [
    ('INSERT INTO model_selection_schedule VALUES (?,?,?)', (2, 1000, 1)),
    ('INSERT INTO model_selection_schedule VALUES (?,?,?)', (1, float('inf'), 1)),
    ('INSERT INTO model_selection_schedule VALUES (?,?,?)', (1, 'tomorrow', 1)),
    ('INSERT INTO tv_preparation_state VALUES (?,?,?,?)', (2, 0, None, 1)),
    ('INSERT INTO tv_preparation_state VALUES (?,?,?,?)', (1, 7, None, 1)),
    ('INSERT INTO tv_preparation_state VALUES (?,?,?,?)', (1, 0, float('inf'), 1)),
    ('INSERT INTO rec_proposals VALUES (?,?,?)', ('daily_model_schedule', '{}', 1)),
    ('INSERT INTO rec_proposals VALUES (?,?,?)', ('tv_preparation', '{}', 1)),
])
def test_direct_sql_rejects_invalid_work_state_and_retired_writers(store, statement, values):
    with pytest.raises(sqlite3.IntegrityError), store.conn:
        store.conn.execute(statement, values)


def test_domain_writers_join_outer_publication_and_rollback_together(store):
    """A rejected enclosing write cannot absorb another repository's durable unit."""
    store.model_selection.schedule_daily(1000, 1)
    store.tv_preparation.record(False, 2)
    with pytest.raises(ValueError, match='Controlled rollback'), store.conn.atomic():
        with pytest.raises(RuntimeError, match='requires an idle connection'):
            store.model_selection.schedule_daily(2000, 3)
        store.conn.execute("INSERT INTO settings VALUES ('publication-marker','changed')")
        raise ValueError('Controlled rollback')
    assert store.model_selection.next_daily_at() == 1000
    assert store.tv_preparation.state() == {'failed': False, 'at': 2}
    assert store.settings.get_setting('publication-marker') is None
    store.model_selection.schedule_daily(2000, 3)
    assert store.model_selection.next_daily_at() == 2000
    assert store.tv_preparation.state() == {'failed': False, 'at': 2}
    store.tv_preparation.record(True, 4)
    assert store.tv_preparation.state() == {'failed': True, 'at': 4}


def test_legacy_call_boundary_routes_to_typed_owners_and_retains_unknown_tv_time(store):
    store.rec_proposals.put_proposals('daily_model_schedule', {'next_at': 1000}, 1)
    store.rec_proposals.put_proposals('tv_preparation', {'failed': True}, 2)
    assert store.model_selection.next_daily_at() == 1000
    assert store.tv_preparation.state() == {'failed': True}
    assert store.rec_proposals.get_proposals('tv_preparation') == {'failed': True}
    assert store.rec_proposals.has_proposals('daily_model_schedule')
    assert not store.conn.execute("SELECT 1 FROM rec_proposals WHERE surface IN ('daily_model_schedule','tv_preparation')").fetchone()
    store.rec_proposals.put_proposals('daily_model_schedule', {}, 3)
    assert store.model_selection.next_daily_at() == 0
    assert not store.rec_proposals.has_proposals('daily_model_schedule')
