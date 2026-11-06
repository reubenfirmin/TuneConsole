"""Direct SQL and real reconciliation against account and recipe constraints.

Controlled schema-69 predecessor checks, without changing the registered tail.
"""
import sqlite3

import pytest

from yt_playlist.core.config import AccountConfig
from yt_playlist.core.migrations import v078_account_contracts as migration
from yt_playlist.core.migrations.runner import Context, MigrationError
from yt_playlist.core.store import Store


@pytest.fixture
def predecessor(tmp_path, _pristine_installed_schema):
    store = Store(tmp_path / 'accounts.db')
    _pristine_installed_schema(69).backup(store.conn)
    store._bind_repositories()
    with store.conn:
        store.conn.executemany('INSERT INTO identities(id,label,credential_ref,brand_account_id,is_master,active) '
            "VALUES (?,?,'bridge',?,?,?)", [(1, 'Main', None, 1, 1), (2, 'Other', 'other', 0, 1),
                                            (3, 'Retired', 'retired', 1, 0)])
    yield store
    store.conn.close()


def apply(store):
    assert not store.conn.in_transaction
    store.conn.execute('PRAGMA foreign_keys=OFF')
    try:
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            migration.apply(store.conn, Context(10))
            migration.validate(store.conn)
    finally:
        store.conn.execute('PRAGMA foreign_keys=ON')
    assert store.conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1


def accounts(store):
    return list(map(tuple, store.conn.execute('SELECT * FROM identities ORDER BY id')))


def historical_playlist(store, owner, remote, title):
    """Seed genuine v69 columns; its triggers retain membership and revisions."""
    scoped = remote in ('LM', 'SE')
    scope = f'@account:{owner}:{remote}' if scoped else remote
    with store.conn:
        store.conn.execute('INSERT OR IGNORE INTO playlist_scopes VALUES (?,?,?,?)',
                           (scope, remote, owner if scoped else None, 'account' if scoped else 'shared'))
        return store.conn.execute('INSERT INTO playlists(identity_id,ytm_playlist_id,title,track_count,'
            'content_hash,first_seen,last_seen,last_changed,scope_ref) VALUES (?,?,?,0,\'\',1,1,1,?)',
            (owner, remote, title, scope)).lastrowid


def test_account_values_child_history_and_installed_sql_survive_replacement(predecessor):
    store = predecessor
    current = historical_playlist(store, 1, 'shared', 'Retained')
    retired = historical_playlist(store, 2, 'shared', 'Former')
    with store.conn:
        store.conn.execute('DELETE FROM playlists WHERE id=?', (retired,))
    with store.conn:
        store.conn.execute('CREATE TABLE installed_account_receipts(account_id INTEGER REFERENCES identities(id), value TEXT)')
        store.conn.execute("INSERT INTO installed_account_receipts VALUES (2,'original')")
        store.conn.execute('CREATE VIEW installed_account_labels AS SELECT id,label FROM identities')
        store.conn.execute('CREATE TRIGGER installed_account_label_update AFTER UPDATE OF label ON identities '
                           'BEGIN UPDATE installed_account_receipts SET value=NEW.label WHERE account_id=NEW.id; END')
    original = accounts(store)
    memberships = list(map(tuple, store.conn.execute('SELECT * FROM playlist_membership_refs ORDER BY id')))
    installed = list(map(tuple, store.conn.execute("SELECT name,sql FROM sqlite_schema WHERE type IN ('view','trigger') ORDER BY name")))
    apply(store)
    assert accounts(store) == original
    assert list(map(tuple, store.conn.execute('SELECT * FROM playlist_membership_refs ORDER BY id'))) == memberships
    assert store.conn.execute('SELECT id FROM playlists').fetchone()[0] == current
    assert list(map(tuple, store.conn.execute('SELECT * FROM installed_account_labels ORDER BY id'))) == [
        (1, 'Main'), (2, 'Other'), (3, 'Retired')]
    after = dict(store.conn.execute("SELECT name,sql FROM sqlite_schema WHERE type IN ('view','trigger')"))
    assert all(after[name] == sql for name, sql in installed)
    assert store.conn.execute('SELECT value FROM installed_account_receipts').fetchone()[0] == 'original'
    with store.conn:
        store.conn.execute("UPDATE identities SET label='Renamed' WHERE id=2")
    assert store.conn.execute('SELECT value FROM installed_account_receipts').fetchone()[0] == 'Renamed'
    before = accounts(store)
    for column, value in (('active', 7), ('active', -1), ('active', 0.5),
                          ('is_master', 2), ('is_master', 'yes'), ('is_master', None)):
        with pytest.raises(sqlite3.IntegrityError), store.conn:
            store.conn.execute(f'UPDATE identities SET {column}=? WHERE id=2', (value,))
        assert accounts(store) == before, (column, value)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_active_master_uniqueness_allows_transactional_exchange_and_empty_setup(predecessor):
    store = predecessor
    apply(store)
    with pytest.raises(sqlite3.IntegrityError), store.conn:
        store.conn.execute('UPDATE identities SET is_master=1 WHERE id=2')
    with pytest.raises(sqlite3.IntegrityError), store.conn:
        store.conn.execute('UPDATE identities SET active=1 WHERE id=3')
    with store.conn:
        store.conn.execute('UPDATE identities SET is_master=0 WHERE id=1')
        assert store.conn.execute('SELECT count(*) FROM identities WHERE active=1 AND is_master=1').fetchone()[0] == 0
        store.conn.execute('UPDATE identities SET is_master=1 WHERE id=2')
    assert store.accounts.get_master_account().id == 2
    with store.conn:
        store.conn.execute('UPDATE identities SET active=0')
    assert store.accounts.get_accounts() == []
    with store.conn:
        store.conn.execute('DELETE FROM identities')
    assert not accounts(store)


def test_reconciliation_swaps_labels_master_and_retains_retired_ids(predecessor):
    store = predecessor
    apply(store)
    ids = store.accounts.replace_accounts([
        AccountConfig('Other', 'bridge', None, False, 1),
        AccountConfig('Main', 'bridge', 'other', True, 2),
    ])
    assert ids == {'Other': 1, 'Main': 2}
    assert store.accounts.get_master_account().id == 2
    assert store.conn.execute('SELECT active FROM identities WHERE id=3').fetchone()[0] == 0
    before = accounts(store)
    with pytest.raises(RuntimeError, match='interrupted'):
        with store.accounts.reconciled([
            AccountConfig('Other', 'bridge', None, True, 1),
            AccountConfig('Main', 'bridge', 'other', False, 2),
        ]):
            raise RuntimeError('interrupted runtime preparation')
    assert accounts(store) == before
    ids = store.accounts.replace_accounts([
        AccountConfig('Main', 'bridge', 'other', False, 2),
        AccountConfig('Returned', 'bridge', 'retired', True, 3),
    ])
    assert ids == {'Main': 2, 'Returned': 3}
    assert store.conn.execute('SELECT active FROM identities WHERE id=1').fetchone()[0] == 0


def test_scope_owner_agreement_keeps_shared_and_unknown_historical_subjects(predecessor):
    store = predecessor
    historical_playlist(store, 1, 'LM', 'Liked')
    historical_playlist(store, 1, 'shared', 'Shared one')
    historical_playlist(store, 2, 'shared', 'Shared two')
    apply(store)
    with store.conn:
        store.conn.execute('INSERT INTO playlist_membership_refs(scope_ref,identity_id) VALUES (NULL,NULL)')
        store.conn.execute("INSERT INTO playlist_membership_refs(scope_ref,identity_id) VALUES ('shared',2)")
    for statement in (
        "INSERT INTO playlist_membership_refs(scope_ref,identity_id) VALUES ('@account:1:LM',2)",
        "INSERT INTO playlist_library_revisions VALUES ('@account:1:LM',2,1)",
        "UPDATE playlist_library_revisions SET identity_id=2 WHERE scope_ref='@account:1:LM'",
        "UPDATE playlist_scopes SET identity_id=2 WHERE ref='@account:1:LM'",
        "UPDATE playlist_scopes SET provider_id='another' WHERE ref='shared'",
    ):
        with pytest.raises(sqlite3.IntegrityError), store.conn:
            store.conn.execute(statement)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('corruption,message', [
    ('UPDATE identities SET active=7 WHERE id=2', 'Invalid stored account flags'),
    ('UPDATE identities SET is_master=1 WHERE id=2', 'Multiple active master accounts'),
    ("ALTER TABLE identities ADD COLUMN installed_extra TEXT", 'Unexpected account columns'),
])
def test_invalid_predecessor_stops_without_ddl_or_data_loss(predecessor, corruption, message):
    store = predecessor
    with store.conn:
        store.conn.execute(corruption)
    before = accounts(store)
    schema = list(map(tuple, store.conn.execute('SELECT name,sql FROM sqlite_schema ORDER BY name')))
    with pytest.raises(MigrationError, match=message):
        apply(store)
    assert accounts(store) == before
    assert list(map(tuple, store.conn.execute('SELECT name,sql FROM sqlite_schema ORDER BY name'))) == schema
    assert store.conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1


def test_recipe_replacement_preserves_draft_ids_sequence_and_supported_boundaries(predecessor):
    store = predecessor
    first = store.road_trip.save_road_trip_recipe(None, 'Short', 0, ['Artist'], ['Rock'], 15, -1,
                                                familiarity_pct=100)
    retired = store.road_trip.save_road_trip_recipe(None, 'Retired', 100, [], [], 720, 2,
                                                  familiarity_pct=0)
    store.road_trip.save_road_trip_draft(first, {'revision': 'retained', 'slots': []}, 3)
    store.road_trip.delete_road_trip_recipe(retired)
    recipe_rows = list(map(tuple, store.conn.execute('SELECT * FROM road_trip_recipes')))
    drafts = list(map(tuple, store.conn.execute('SELECT * FROM road_trip_drafts')))
    apply(store)
    assert list(map(tuple, store.conn.execute('SELECT * FROM road_trip_recipes'))) == recipe_rows
    assert list(map(tuple, store.conn.execute('SELECT * FROM road_trip_drafts'))) == drafts
    returned = store.road_trip.save_road_trip_recipe(None, 'Long', 100, [], [], 720, 4, familiarity_pct=0)
    assert returned > retired
    assert store.road_trip.get_road_trip_draft(first) == {'revision': 'retained', 'slots': []}
    before = list(map(tuple, store.conn.execute('SELECT * FROM road_trip_recipes ORDER BY id')))
    for column, value in (
            ('own_pct', -1), ('own_pct', 101), ('own_pct', 0.5), ('own_pct', None),
            ('familiarity_pct', -1), ('familiarity_pct', 101), ('familiarity_pct', 0.5),
            ('target_minutes', 14), ('target_minutes', 721), ('target_minutes', 30.5),
            ('created_at', float('inf')), ('updated_at', float('-inf')), ('updated_at', 'unknown')):
        with pytest.raises(sqlite3.IntegrityError), store.conn:
            store.conn.execute(f'UPDATE road_trip_recipes SET {column}=? WHERE id=?', (value, first))
        assert list(map(tuple, store.conn.execute(
            'SELECT * FROM road_trip_recipes ORDER BY id'))) == before, (column, value)
        assert store.road_trip.get_road_trip_draft(first) == {'revision': 'retained', 'slots': []}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_invalid_legacy_recipe_aborts_without_changing_original_or_draft(predecessor):
    store = predecessor
    recipe = store.road_trip.save_road_trip_recipe(None, 'Trip', 50, [], [], 270, 1)
    store.road_trip.save_road_trip_draft(recipe, {'revision': 'original'}, 2)
    with store.conn:
        store.conn.execute('UPDATE road_trip_recipes SET own_pct=140 WHERE id=?', (recipe,))
    before = list(map(tuple, store.conn.execute('SELECT * FROM road_trip_recipes')))
    with pytest.raises(MigrationError, match='Invalid stored recipe'):
        apply(store)
    assert list(map(tuple, store.conn.execute('SELECT * FROM road_trip_recipes'))) == before
    assert store.road_trip.get_road_trip_draft(recipe) == {'revision': 'original'}
