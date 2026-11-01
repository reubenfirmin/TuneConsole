"""Verified atomic publication and continuing writes across the retained format."""
import json
import sqlite3

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import recommendation_work
from yt_playlist.core.migrations import MigrationError, migrate, registry, regular
from yt_playlist.core.preparation.recommendation_state import BYTE_LIMITS, JOBS
from yt_playlist.core.preparation import work_publication
from yt_playlist.core.store import Store


def prepare(store):
    store.rec_proposals.put_proposals('daily_model_schedule', {'next_at': 123}, 100)
    store.rec_proposals.put_proposals('tv_preparation', {'failed': False, 'at': 99}, 101)
    JOBS['work'].install(store.conn)
    return JOBS['work'].step(store.conn).prepared_revision


@pytest.mark.parametrize('installed', [79, 81], indirect=True)
def test_publication_preserves_originals_and_supports_existing_and_new_readers(installed):
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
        reopened.init_schema()
        assert reopened.model_selection.next_daily_at() == 123
        reopened.model_selection.schedule_daily(234, 200)
        reopened.tv_preparation.record(True, 201)
    finally:
        reopened.conn.close()
    assert store.model_selection.next_daily_at() == 234
    assert store.rec_proposals.get_proposals('tv_preparation') == {'failed': True, 'at': 201}
    with sqlite3.connect(store._db_path) as old_writer:
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
    regular.migrate(store.conn)


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
def test_interrupted_publication_rolls_back_and_explicit_historical_route_still_works(tmp_path, version):
    store = Store(tmp_path / 'historical.db')
    try:
        migrate(store.conn, registry=registry.MIGRATIONS[:version])
        store.init_schema()
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
        with sqlite3.connect(store._db_path) as converter:
            converter.row_factory = sqlite3.Row
            converter.execute('PRAGMA foreign_keys=ON')
            migrate(converter, registry=registry.MIGRATIONS[:82])
        assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 82
        assert not recommendation_work.prepared_owner(store.conn)
        assert recommendation_work.read(store.conn, 'daily_model_schedule') == {'next_at': 999}
    finally:
        store.conn.close()


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
def test_publication_fences_invalid_continuing_source_writes(tmp_path, surface, payload):
    path = tmp_path / 'invalid.db'
    store = Store(path)
    try:
        migrate(store.conn, registry=registry.MIGRATIONS[:81])
        store.init_schema()
        revision = prepare(store)
        work_publication.publish(store.conn, revision=revision)
        with pytest.raises(sqlite3.DatabaseError):
            with store.conn:
                store.conn.execute('UPDATE rec_proposals SET payload=? WHERE surface=?',
                                   (payload, surface))
        assert store.model_selection.next_daily_at() == 123
        assert json.loads(store.conn.execute("SELECT payload FROM rec_proposals WHERE surface='daily_model_schedule'").fetchone()[0]) == {'next_at': 123}
    finally:
        store.conn.close()


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
