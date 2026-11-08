"""Populated alias rollback, registered advancement and current reopen."""
from contextlib import closing
import sqlite3
from unittest.mock import Mock

import pytest

from tests.catalog_migration_support import catalog_reader
from tests.core.test_catalog import complete_regular_input
from tests.genre_catalog_support import bound_catalog
from tests.repos.test_album_aliases import OLD
from yt_playlist.core import schema_definition
from yt_playlist.core.migrations import Migration, MigrationError, registry, regular, runner
from yt_playlist.core.migrations import v062_album_aliases, v103_entity_names
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """No HTTP lifespan is needed by these SQL/file owners."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('album-alias-upgrade-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield


@pytest.fixture(scope='module')
def alias_predecessor():
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        runner.migrate(conn, registry=registry.MIGRATIONS[:61], clock=lambda: 1.)
        yield conn


def test_alias_predecessor_is_the_actual_frozen_v61_schema(alias_predecessor):
    runner.validate_version(alias_predecessor, 61)
    assert alias_predecessor.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 61


@pytest.fixture(scope='module')
def alias_upgrade(alias_predecessor, tmp_path_factory):
    path = tmp_path_factory.mktemp('album-alias-upgrade') / 'installed.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        alias_predecessor.backup(conn)
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        with conn:
            album = conn.execute("INSERT INTO albums(title,artist,metadata_rank) VALUES ('Album','Artist',2)").lastrowid
            conn.execute("INSERT INTO album_provider_ids VALUES ('youtube',?,?)", (OLD, album))
            conn.execute("INSERT INTO album_library_memberships(album_id,identity_id,source) VALUES (?,NULL,'youtube')", (album,))
            conn.execute('INSERT INTO album_share_snapshots VALUES (?,?,?)', (OLD, 'Frozen', '["source"]'))
        yield conn, path, album


def test_alias_input_keeps_exact_populated_ids_and_share_bytes(alias_upgrade):
    conn, _, album = alias_upgrade
    assert tuple(conn.execute('SELECT provider,provider_id,album_id FROM album_provider_ids').fetchone()) == ('youtube', OLD, album)
    assert tuple(conn.execute('SELECT * FROM album_share_snapshots').fetchone()) == (OLD, 'Frozen', '["source"]')


def test_interrupted_actual_alias_conversion_rolls_back_every_row_and_schema_object(alias_upgrade):
    conn, _, _ = alias_upgrade
    before = list(conn.iterdump())
    migration = registry.MIGRATIONS[61]

    def fail(connection, context):
        v062_album_aliases.apply(connection, context)
        raise RuntimeError('interrupted alias upgrade')

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(registry, 'MIGRATIONS', (*registry.MIGRATIONS[:61], Migration(migration.version, migration.name, fail)))
        with pytest.raises(MigrationError, match='interrupted alias upgrade'):
            runner.migrate(conn, registry=registry.MIGRATIONS)
    assert list(conn.iterdump()) == before
    assert not conn.in_transaction
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


def assert_alias_rows(conn, album):
    assert tuple(conn.execute('SELECT id,title,artist FROM albums').fetchone()) == (album, 'Album', 'Artist')
    assert tuple(conn.execute('SELECT provider,provider_id,album_id,is_primary FROM album_provider_ids').fetchone()) == ('youtube', OLD, album, 1)
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('target', range(62, 103))
def test_each_registered_upgrade_keeps_the_original_album_and_primary_provider_id(alias_upgrade, target):
    conn, _, album = alias_upgrade
    runner.migrate(conn, registry=registry.MIGRATIONS[:target], clock=lambda: 1.)
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == target
    assert_alias_rows(conn, album)
    if target == 62:
        runner.validate_version(conn, 62)


def test_account_cutover_reference_retains_album_aliases(alias_upgrade):
    definitions = '\n'.join(schema_definition.statements(version=103))
    assert 'CREATE TABLE accounts' in definitions
    assert 'CREATE TABLE album_provider_ids' in definitions


@pytest.fixture(scope='module')
def alias_renamed(alias_upgrade):
    conn, _, _ = alias_upgrade
    runner.migrate(conn, registry=registry.MIGRATIONS[:102])
    restore, regular_guards = Mock(), Mock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v103_entity_names, 'restore_programs', restore)
        patch.setattr(regular, 'after_owner_upgrade', regular_guards)
        runner.migrate(conn, registry=registry.MIGRATIONS[:103])
    restore.assert_called_once()
    regular_guards.assert_called_once_with(conn)
    return alias_upgrade, restore.call_args


def test_alias_account_cutover_preserves_real_rows_ids_and_foreign_keys(alias_renamed):
    (conn, _, album), captured = alias_renamed
    assert captured.args[0] is conn
    assert captured.args[1]
    assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='accounts'").fetchone()
    assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='identities'").fetchone()
    assert_alias_rows(conn, album)


@pytest.fixture(scope='module')
def alias_restored(alias_renamed):
    value, captured = alias_renamed
    conn, _, _ = value
    with conn:
        conn.execute('BEGIN')
        v103_entity_names.restore_programs(*captured.args, **captured.kwargs)
        regular.after_owner_upgrade(conn)
    return value


def test_real_account_program_restoration_keeps_alias_constraints(alias_restored):
    conn, _, album = alias_restored
    runner.validate_version(conn, 103, foreign_keys=False, data=False)
    assert_alias_rows(conn, album)


@pytest.mark.parametrize('target', range(104, len(registry.MIGRATIONS) + 1))
def test_later_registered_upgrades_keep_album_aliases_and_raw_share_references(alias_restored, target):
    conn, _, album = alias_restored
    runner.migrate(conn, registry=registry.MIGRATIONS[:target], clock=lambda: 1.)
    assert_alias_rows(conn, album)
    assert conn.execute('SELECT raw_source_ref FROM album_share_items').fetchone()[0] == 'source'


@pytest.fixture(scope='module')
def alias_regular(alias_restored):
    conn, _, _ = alias_restored
    runner.migrate(conn, registry=registry.MIGRATIONS)
    complete_regular_input(conn)
    return alias_restored


def test_current_admission_keeps_source_ids_and_all_completed_histories(alias_regular):
    conn, _, album = alias_regular
    before = list(conn.iterdump())
    assert runner.migrate(conn, defer_validation=True) is False
    assert list(conn.iterdump()) == before
    assert_alias_rows(conn, album)
    regular.validate(conn)


def test_repository_reads_the_original_saved_provider_id_after_conversion(alias_regular):
    _, path, _ = alias_regular
    with catalog_reader(path, 107) as store:
        assert store.collection.saved_album_ids() == {OLD}


def test_fresh_store_reopen_keeps_every_converted_row_and_raw_source_reference(alias_regular):
    conn, path, album = alias_regular
    before = list(conn.iterdump())
    store = Store(path)
    try:
        store.init_schema()
        assert list(store.conn.iterdump()) == before
        runner.validate_version(store.conn, len(registry.MIGRATIONS))
        assert_alias_rows(store.conn, album)
        assert store.collection.saved_album_ids() == {OLD}
        assert store.conn.execute('SELECT raw_source_ref FROM album_share_items').fetchone()[0] == 'source'
        assert not (path.parent / 'backups').exists()
    finally:
        store.conn.close()
