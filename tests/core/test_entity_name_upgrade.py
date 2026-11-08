"""One populated naming journey retains custom programs and retry custody."""
import sqlite3
from unittest.mock import Mock

import pytest

from tests.core.test_catalog import complete_regular_input
from tests.core.test_entity_names import installed_predecessor, rows
from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import capture_template
from yt_playlist.core import playlist_scopes, schema_definition
from yt_playlist.core.migrations import Migration, MigrationError, current, registry, regular, runner, v103_entity_names
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Naming owns SQL and files without an HTTP application."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('naming-home')))
        bound_catalog(patch)
        yield


@pytest.fixture(scope='module')
def populated_names(tmp_path_factory):
    source = installed_predecessor(tmp_path_factory.mktemp('naming-input') / 'source.db')
    try:
        with source.conn:
            source.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (4,'Other','credential',0)")
            source.conn.execute("INSERT INTO playlist_scopes(ref,provider_id,identity_id,kind) VALUES ('PL','PL',NULL,'shared')")
            source.conn.execute("INSERT INTO playlist_membership_refs(id,scope_ref,identity_id) VALUES (100,'PL',4)")
            source.conn.execute("INSERT INTO cleanup_ignored VALUES ('PL','empty',1)")
            source.conn.execute("INSERT INTO suppressed_overlaps VALUES ('@account:3:LM','PL',2)")
            source.conn.execute("INSERT INTO overlap_kept VALUES ('@account:3:LM','PL',3)")
            source.conn.execute('CREATE TABLE installed_receipts(account_id INTEGER REFERENCES identities(id),message TEXT)')
            source.conn.execute("INSERT INTO installed_receipts VALUES (3,'identity_id')")
            source.conn.execute('CREATE VIEW installed_accounts AS SELECT id,label FROM identities')
            source.conn.execute('CREATE TRIGGER installed_label AFTER UPDATE OF label ON identities '
                'BEGIN UPDATE installed_receipts SET message=NEW.label WHERE account_id=NEW.id; END')
        yield capture_template(source, dict(rows=rows(source.conn), vibes=dict(source.conn.execute(
            "SELECT name,sql FROM sqlite_schema WHERE name IN ('sample_attributes','measurements',"
            "'measurement_samples','predictions','prediction_artists','prediction_inputs','recording_vibes')"))))
    finally:
        source.conn.close()


def test_names_input_keeps_exact_installed_rows_custom_literals_and_read_only_sql(populated_names):
    source = populated_names.source.conn
    runner.validate_version(source, 102)
    assert source.execute('PRAGMA query_only').fetchone()[0] == 1
    assert rows(source) == populated_names.result['rows']
    assert source.execute('SELECT message FROM installed_receipts').fetchone()[0] == 'identity_id'


def test_names_reference_contains_the_real_canonical_account_contract():
    assert 'CREATE TABLE accounts' in '\n'.join(schema_definition.statements(version=103))


@pytest.fixture(scope='module')
def naming_branch(populated_names, tmp_path_factory):
    store = Store(tmp_path_factory.mktemp('naming-journey') / 'installed.db')
    try:
        populated_names.restore(store)
        yield dict(store=store, original=populated_names.result)
    finally:
        store.conn.close()


def rename(branch):
    restore, guards = Mock(), Mock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v103_entity_names, 'restore_programs', restore)
        patch.setattr(regular, 'after_owner_upgrade', guards)
        runner.migrate(branch['store'].conn, registry=registry.MIGRATIONS[:103])
    restore.assert_called_once()
    guards.assert_called_once_with(branch['store'].conn)
    branch['programs'] = restore.call_args
    return branch


@pytest.fixture(scope='module')
def renamed(naming_branch):
    return rename(naming_branch)


def test_real_name_cutover_preserves_every_populated_owner_and_identifier(renamed):
    conn = renamed['store'].conn
    assert rows(conn) == renamed['original']['rows']
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 103
    for table, mapping in v103_entity_names.COLUMNS.items():
        columns = {row[1] for row in conn.execute('PRAGMA table_info(' + table + ')')}
        assert not columns & mapping.keys() and set(mapping.values()) <= columns
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


def restore_programs(branch):
    captured, conn = branch['programs'], branch['store'].conn
    with conn:
        conn.execute('BEGIN')
        v103_entity_names.restore_programs(*captured.args, **captured.kwargs)
        regular.after_owner_upgrade(conn)
    return branch


@pytest.fixture(scope='module')
def restored(renamed):
    return restore_programs(renamed)


def assert_custom_programs(conn):
    assert list(map(tuple, conn.execute('SELECT * FROM installed_accounts ORDER BY id'))) == [(3, 'Main'), (4, 'Other')]
    assert conn.execute('SELECT message FROM installed_receipts').fetchone()[0] == 'identity_id'
    conn.execute('BEGIN')
    try:
        conn.execute("UPDATE accounts SET label='Renamed' WHERE id=3")
        assert conn.execute('SELECT message FROM installed_receipts').fetchone()[0] == 'Renamed'
    finally:
        conn.rollback()


def test_real_program_restoration_preserves_custom_views_triggers_foreign_keys_and_literals(restored):
    conn = restored['store'].conn
    runner.validate_version(conn, 103)
    assert_custom_programs(conn)
    assert rows(conn) == restored['original']['rows']


@pytest.mark.parametrize('target', range(104, len(registry.MIGRATIONS) + 1))
def test_later_registered_edges_keep_the_complete_name_only_population(restored, target):
    runner.migrate(restored['store'].conn, registry=registry.MIGRATIONS[:target])
    assert rows(restored['store'].conn) == restored['original']['rows']


@pytest.fixture(scope='module')
def current_names(restored):
    store = restored['store']
    complete_regular_input(store.conn)
    store.init_schema(defer_metadata=True)
    return restored


def test_real_current_admission_keeps_rows_measurement_definitions_and_live_repository_writes(current_names):
    store, original = current_names['store'], current_names['original']
    current.validate(store.conn)
    after = rows(store.conn)
    assert {name: after[name] for name in original['rows']} == original['rows']
    assert dict(store.conn.execute("SELECT name,sql FROM sqlite_schema WHERE name IN "
        "('sample_attributes','measurements','measurement_samples','predictions',"
        "'prediction_artists','prediction_inputs','recording_vibes')")) == original['vibes']
    assert store.accounts.get_master_account().id == 3
    assert store.playlists.get_playlist(55).playlist_scope_id == '@account:3:LM'
    assert playlist_scopes.resolve(store.conn, 'LM', 3) == '@account:3:LM'
    assert store.playlists.upsert_playlist(4, 'PL', 'Recreated', 0, 'hash', 1) > 100
    assert tuple(store.conn.execute('SELECT playlist_scope_id,account_id FROM playlist_membership_refs WHERE id=100').fetchone()) == ('PL', 4)
    assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='identities'").fetchone()
    assert any(row[2] == 'accounts' and row[3] == 'account_id' for row in store.conn.execute('PRAGMA foreign_key_list(playlists)'))
    for sql, reason in [
        ("UPDATE playlist_scopes SET account_id=4 WHERE id='@account:3:LM'", 'immutable'),
        ("INSERT INTO playlist_membership_refs(playlist_scope_id,account_id) VALUES ('@account:3:LM',4)", 'account'),
    ]:
        with pytest.raises(sqlite3.IntegrityError, match=reason):
            store.conn.execute(sql)
        store.conn.rollback()
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def interrupted(populated_names, tmp_path_factory):
    store = Store(tmp_path_factory.mktemp('naming-interruption') / 'installed.db')
    try:
        populated_names.restore(store)
        before = list(store.conn.iterdump())

        def stopped(conn, context):
            v103_entity_names.apply(conn, context)
            raise RuntimeError('interrupted naming cutover')

        with pytest.raises(MigrationError, match='interrupted naming cutover'):
            runner.migrate(store.conn, registry=(*registry.MIGRATIONS[:102], Migration(103, 'entity_names', stopped)))
        assert list(store.conn.iterdump()) == before
        path = store._db_path
    finally:
        store.conn.close()
    reopened = Store(path)
    try:
        yield dict(store=reopened, original=populated_names.result)
    finally:
        reopened.conn.close()


def test_actual_interrupted_rename_restores_every_row_program_and_stamp_on_disk(interrupted):
    conn = interrupted['store'].conn
    assert rows(conn) == interrupted['original']['rows']
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 102


def test_reopened_interrupted_file_keeps_the_complete_original_v102_contract(interrupted):
    conn = interrupted['store'].conn
    runner.validate_version(conn, 102)


@pytest.fixture(scope='module')
def retried(interrupted):
    return rename(interrupted)


def test_retry_after_fresh_file_open_runs_the_real_name_cutover(retried):
    assert rows(retried['store'].conn) == retried['original']['rows']
    assert retried['store'].conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 103


def test_real_retry_restores_custom_programs_and_the_complete_installed_contract(retried):
    restore_programs(retried)
    runner.validate_version(retried['store'].conn, 103)
    assert_custom_programs(retried['store'].conn)
    assert not retried['store'].conn.execute('PRAGMA foreign_key_check').fetchone()
