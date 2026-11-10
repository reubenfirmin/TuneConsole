"""Offer storage survives migration, output eviction, rollback and restarts."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.store_fixture_support import _numbered_input, native_sql_calls, without_query_timing
from yt_playlist.core import schema_inspection
from yt_playlist.core.migrations import current, migrate, registry
from yt_playlist.core.migrations import v083_home_offers as migration
from yt_playlist.core.migrations.runner import Migration, MigrationError
from yt_playlist.core.store import Store


@pytest.fixture
def disable_periodic_library_sync():
    pass

@pytest.fixture
def predecessor(tmp_path, _pristine_installed_schema):
    store = Store(tmp_path / 'offers.db')
    with closing(store.conn):
        _pristine_installed_schema(82).backup(store.conn)
        store._bind_repositories()
        yield store


@pytest.fixture
def restart_input(tmp_path, monkeypatch, native_sql_calls, without_query_timing):
    # This consumer needs the completed edge input, not replay of its prefix.
    with _numbered_input(82, tmp_path, monkeypatch) as (store, _):
        yield store


def seed(store, value=None):
    value = {'epoch': 4, 'signatures': ['first', 'second']} if value is None else value
    with store.conn:
        store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)',
                           ('home_shown', json.dumps(value), 1234.5))
        store.conn.execute("INSERT INTO rec_proposals VALUES ('unrelated-output','{}',9)")


def upgrade(store):
    migrate(store.conn, registry=registry.MIGRATIONS[:83])
    store._bind_repositories()


def test_upgrade_eviction_restart_and_continuing_writes_preserve_offer_state(restart_input):
    store = restart_input
    seed(store)
    upgrade(store)
    assert store.rec_proposals.get_proposals('home_shown') == {'epoch': 4, 'signatures': ['first', 'second']}
    assert tuple(store.conn.execute('SELECT epoch,built_at FROM home_offer_dedup').fetchone()) == (4, 1234.5)
    assert [row[0] for row in store.conn.execute('SELECT surface FROM rec_proposals')] == ['unrelated-output']
    updated = {'epoch': 4, 'signatures': ['first', 'second', 'third']}
    store.rec_proposals.put_proposals('home_shown', updated, 2000)
    assert store.rec_proposals.get_proposals('home_shown') == updated
    with store.conn:
        store.conn.execute('DELETE FROM rec_proposals')
    reopened = Store(store._db_path)
    with closing(reopened.conn), schema_inspection.reuse_tables(reopened.conn):
        reopened.conn.execute('PRAGMA synchronous=OFF')
        reopened.init_schema()
        assert reopened.rec_proposals.get_proposals('home_shown') == updated
        assert reopened.conn.execute('SELECT built_at FROM home_offer_dedup').fetchone()[0] == 2000
        reopened.rec_proposals.put_proposals('home_shown', {'epoch': 5, 'signatures': ['first']}, 2200)
        assert reopened.rec_proposals.get_proposals('home_shown') == {'epoch': 5, 'signatures': ['first']}
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('value', [None, [], {'signatures': []}, {'epoch': True, 'signatures': []},
    {'epoch': -1, 'signatures': []}, {'epoch': 1, 'signatures': 'wrong'},
    {'epoch': 1, 'signatures': ['same', 'same']}, {'epoch': 1, 'signatures': ['']},
    {'epoch': 1, 'signatures': [None]}, {'epoch': 1, 'signatures': ['é' * 513]},
    {'epoch': 1, 'signatures': [str(i) for i in range(101)]},
    {'epoch': 1, 'signatures': [], 'unknown': 'retain me'}])
def test_unclassified_legacy_state_is_retained_before_any_replacement(predecessor, value):
    seed(predecessor, {})
    with predecessor.conn:
        predecessor.conn.execute("UPDATE rec_proposals SET payload=? WHERE surface='home_shown'",
                                 (json.dumps(value),))
    before = list(predecessor.conn.iterdump())
    with pytest.raises(MigrationError, match='original retained'):
        upgrade(predecessor)
    assert list(predecessor.conn.iterdump()) == before


def test_malformed_json_is_retained_before_any_replacement(predecessor):
    seed(predecessor)
    with predecessor.conn:
        predecessor.conn.execute("UPDATE rec_proposals SET payload='original invalid JSON' WHERE surface='home_shown'")
    before = list(predecessor.conn.iterdump())
    with pytest.raises(MigrationError, match='original retained'):
        upgrade(predecessor)
    assert list(predecessor.conn.iterdump()) == before


def test_empty_legacy_state_does_not_invent_an_offer_window(predecessor):
    seed(predecessor, {})
    upgrade(predecessor)
    assert predecessor.rec_proposals.get_proposals('home_shown') is None
    assert not predecessor.rec_proposals.has_proposals('home_shown')
    value = {'epoch': 0, 'signatures': ['first']}
    predecessor.rec_proposals.put_proposals('home_shown', value, 2000)
    assert predecessor.rec_proposals.get_proposals('home_shown') == value


def test_interrupted_cutover_rolls_back_then_retries(predecessor):
    seed(predecessor)
    before = list(predecessor.conn.iterdump())

    def interrupted(conn, context):
        migration.apply(conn, context)
        raise KeyboardInterrupt('Controlled interruption before offer-state commit')

    with pytest.raises(KeyboardInterrupt):
        migrate(predecessor.conn, registry=(*registry.MIGRATIONS[:82],
            Migration(83, 'home_offer_deduplication', interrupted)))
    assert list(predecessor.conn.iterdump()) == before
    upgrade(predecessor)
    assert predecessor.rec_proposals.get_proposals('home_shown') == {'epoch': 4, 'signatures': ['first', 'second']}


def test_fresh_and_upgraded_offer_owners_have_the_same_contract(predecessor, store):
    upgrade(predecessor)
    migrate(predecessor.conn, registry=registry.MIGRATIONS)
    for name in migration.OBJECTS:
        old = predecessor.conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()[0]
        fresh = store.conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()[0]
        assert current._tokens(old) == current._tokens(fresh)
    current.validate(predecessor.conn)
    current.validate(store.conn)


def test_offer_storage_joins_and_rolls_back_with_outer_publication(store):
    store.rec_proposals.put_proposals('home_shown', {'epoch': 1, 'signatures': ['original']}, 1000)
    with pytest.raises(ValueError, match='Controlled rollback'), store.conn.atomic():
        store.rec_proposals.put_proposals('home_shown', {'epoch': 1, 'signatures': ['new']}, 1100)
        store.conn.execute("INSERT INTO settings VALUES ('offer-publication','changed')")
        raise ValueError('Controlled rollback')
    assert store.rec_proposals.get_proposals('home_shown') == {'epoch': 1, 'signatures': ['original']}
    assert store.settings.get_setting('offer-publication') is None
    store.rec_proposals.put_proposals('home_shown', {'epoch': 1, 'signatures': ['new']}, 1200)
    assert store.rec_proposals.get_proposals('home_shown') == {'epoch': 1, 'signatures': ['new']}


def test_signature_budget_is_bounded_for_domain_writer_and_raw_sql(store):
    original = [f'{i:03}' for i in range(100)]
    store.rec_proposals.put_proposals('home_shown', {'epoch': 1, 'signatures': original}, 1000)
    with pytest.raises(sqlite3.IntegrityError, match='window budget'), store.conn:
        store.conn.execute("INSERT INTO home_offer_signatures VALUES (1,'new')")
    with pytest.raises(ValueError, match='Invalid Home offer signatures'):
        store.rec_proposals.put_proposals('home_shown', {'epoch': 1, 'signatures': original + ['new']}, 1100)
    assert store.rec_proposals.get_proposals('home_shown') == {'epoch': 1, 'signatures': original}
    store.rec_proposals.put_proposals('home_shown', {'epoch': 2, 'signatures': ['new']}, 1200)
    assert store.rec_proposals.get_proposals('home_shown') == {'epoch': 2, 'signatures': ['new']}


@pytest.mark.parametrize('sql,values', [
    ('INSERT INTO home_offer_dedup VALUES (?,?,?)', (2, 1, 1)),
    ('INSERT INTO home_offer_dedup VALUES (?,?,?)', (1, -1, 1)),
    ('INSERT INTO home_offer_dedup VALUES (?,?,?)', (1, 'wrong', 1)),
    ('INSERT INTO home_offer_dedup VALUES (?,?,?)', (1, 1, float('inf'))),
    ('INSERT INTO home_offer_signatures VALUES (?,?)', (1, 'missing-owner')),
    ('INSERT INTO home_offer_signatures VALUES (?,?)', (1, '')),
    ('INSERT INTO home_offer_signatures VALUES (?,?)', (1, 'é' * 513)),
    ('INSERT INTO rec_proposals VALUES (?,?,?)', ('home_shown', '{}', 1)),
])
def test_direct_sql_rejects_invalid_owner_state_and_retired_insert(store, sql, values):
    with pytest.raises(sqlite3.IntegrityError), store.conn:
        store.conn.execute(sql, values)


def test_retired_update_is_rejected_and_validation_detects_bypassed_insert_guard(store):
    # The later generic-owner fence is stronger than this historical one.
    # Bypass it only to establish the controlled corrupted-fixture rows.
    generic = store.conn.execute("SELECT sql FROM sqlite_schema WHERE name='retired_recommendation_bucket_insert'").fetchone()[0]
    with store.conn:
        store.conn.execute('DROP TRIGGER retired_recommendation_bucket_insert')
        store.conn.execute("INSERT INTO rec_proposals VALUES ('unrelated-output','{}',1)")
        store.conn.execute(generic)
    with pytest.raises(sqlite3.IntegrityError, match='typed owner'), store.conn:
        store.conn.execute('BEGIN')
        store.conn.execute('DROP TRIGGER retired_recommendation_bucket_update')
        store.conn.execute("UPDATE rec_proposals SET surface='home_shown' WHERE surface='unrelated-output'")
    name = 'retired_home_offer_insert'
    sql = store.conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()[0]
    with store.conn:
        store.conn.execute('DROP TRIGGER ' + name)
        store.conn.execute('DROP TRIGGER retired_recommendation_bucket_insert')
        store.conn.execute("INSERT INTO rec_proposals VALUES ('home_shown','{}',1)")
        store.conn.execute(sql)
        store.conn.execute(generic)
    with pytest.raises(MigrationError, match='Retired Home offer deduplication remains'):
        current.validate(store.conn)
