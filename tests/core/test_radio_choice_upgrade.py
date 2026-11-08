"""Captured radio branches retain their offered menu across real registered upgrades."""
from contextlib import ExitStack, closing
import sqlite3
from unittest.mock import Mock

import pytest

from tests.core.test_radio_choice_persistence import publish_choice
from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import capture_template, roots
from yt_playlist.core import radio_storage, schema_definition
from yt_playlist.core.migrations import registry, regular, runner, v103_entity_names


STARTS = (79, 93, 97)


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL and file owners do not construct an HTTP application."""


@pytest.fixture(scope='module', autouse=True)
def bounded_genre_vocabulary():
    with pytest.MonkeyPatch.context() as patch:
        bound_catalog(patch)
        yield


@pytest.fixture(scope='module')
def radio_choice_sources(_bounded_installed_schema, tmp_path_factory):
    root = tmp_path_factory.mktemp('radio-choice-sources')
    templates = {}
    with ExitStack() as resources:
        def source(version):
            if version not in templates:
                store = resources.enter_context(_bounded_installed_schema(version).open(root / f'{version}.db'))
                state, row = publish_choice(store, version, {'id': 'branch', 'menu_id': 'offered-menu'})
                assert row['choice_id'] == 'branch'
                templates[version] = capture_template(store, state)
            return templates[version]
        yield source


def assert_checkpoint(conn, expected, version):
    assert radio_storage.load(conn)[0] == expected
    assert expected['replacement']['choice_id'] == {'id': 'branch', 'menu_id': 'offered-menu'}
    if version >= 98:
        assert conn.execute('SELECT choice_id FROM radio_operations').fetchone()[0] == 'branch'
        assert radio_storage.load_published(conn)[0] == expected
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('start', STARTS)
def test_installed_radio_source_keeps_the_original_branch_and_offered_menu(radio_choice_sources, start):
    template = radio_choice_sources(start)
    assert template.source.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == start
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert template.files and any(path.exists() for path in template.files.values())
    assert_checkpoint(template.source.conn, template.result, start)


@pytest.fixture(scope='module')
def radio_upgrade_copies(radio_choice_sources, tmp_path_factory):
    root = tmp_path_factory.mktemp('radio-choice-upgrades')
    copies = {}
    with ExitStack() as resources:
        def branch(start):
            if start not in copies:
                template = radio_choice_sources(start)
                path = root / f'{start}.db'
                resources.enter_context(template.open(path))
                # The explicit migration command owns a fresh connection, with
                # no serving TEMP adapters referring to the preceding layout.
                conn = resources.enter_context(closing(sqlite3.connect(path)))
                conn.row_factory = sqlite3.Row
                conn.execute('PRAGMA foreign_keys=ON')
                copies[start] = conn, path, template.result
            return copies[start]
        yield branch


@pytest.mark.parametrize('start', STARTS)
def test_upgrade_copy_keeps_private_files_wal_and_the_complete_checkpoint(
        radio_upgrade_copies, radio_choice_sources, start):
    conn, path, expected = radio_upgrade_copies(start)
    template = radio_choice_sources(start)
    assert path != template.source._db_path
    assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    destinations = roots(conn)
    for name, source in template.files.items():
        assert source != destinations[name]
        for original in source.rglob('*'):
            if original.is_file():
                copied = destinations[name] / original.relative_to(source)
                assert copied.is_file()
                assert (copied.stat().st_dev, copied.stat().st_ino) != (
                    original.stat().st_dev, original.stat().st_ino)
    assert_checkpoint(conn, expected, start)


def advance(branch, target):
    conn, _, expected = branch
    runner.migrate(conn, registry=registry.MIGRATIONS[:target])
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == target
    assert_checkpoint(conn, expected, target)


@pytest.mark.parametrize('target', range(80, 103))
def test_released_checkpoint_keeps_its_branch_at_each_registered_edge(radio_upgrade_copies, target):
    advance(radio_upgrade_copies(79), target)


@pytest.mark.parametrize('target', range(94, 103))
def test_installed_checkpoint_keeps_its_branch_at_each_registered_edge(radio_upgrade_copies, target):
    advance(radio_upgrade_copies(93), target)


@pytest.mark.parametrize('target', range(98, 103))
def test_native_checkpoint_keeps_its_branch_at_each_registered_edge(radio_upgrade_copies, target):
    advance(radio_upgrade_copies(97), target)


def test_account_reference_keeps_the_actual_radio_and_account_owners():
    definitions = '\n'.join(schema_definition.statements(version=103))
    assert 'CREATE TABLE accounts' in definitions
    assert 'CREATE TABLE radio_operations' in definitions


@pytest.fixture(scope='module')
def renamed_radio(radio_upgrade_copies):
    branches = {}
    def renamed(start):
        if start not in branches:
            conn, path, expected = radio_upgrade_copies(start)
            runner.migrate(conn, registry=registry.MIGRATIONS[:102])
            accounts = list(map(tuple, conn.execute('SELECT id,label FROM identities ORDER BY id')))
            restore, guards = Mock(), Mock()
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(v103_entity_names, 'restore_programs', restore)
                patch.setattr(regular, 'after_owner_upgrade', guards)
                runner.migrate(conn, registry=registry.MIGRATIONS[:103])
            restore.assert_called_once()
            guards.assert_called_once_with(conn)
            branches[start] = conn, path, expected, accounts, restore.call_args
        return branches[start]
    return renamed


@pytest.mark.parametrize('start', STARTS)
def test_account_rename_keeps_real_radio_rows_and_account_ids(renamed_radio, start):
    conn, _, _, accounts, captured = renamed_radio(start)
    assert captured.args[0] is conn and captured.args[1]
    assert list(map(tuple, conn.execute('SELECT id,label FROM accounts ORDER BY id'))) == accounts
    assert conn.execute('SELECT choice_id FROM radio_operations').fetchone()[0] == 'branch'
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def restored_radio(renamed_radio):
    restored = {}
    def branch(start):
        if start not in restored:
            conn, path, expected, _, captured = renamed_radio(start)
            with conn:
                conn.execute('BEGIN')
                v103_entity_names.restore_programs(*captured.args, **captured.kwargs)
                regular.after_owner_upgrade(conn)
            restored[start] = conn, path, expected
        return restored[start]
    return branch


@pytest.mark.parametrize('start', STARTS)
def test_restored_sql_programs_keep_the_complete_radio_checkpoint(restored_radio, start):
    conn, _, expected = restored_radio(start)
    runner.validate_version(conn, 103, foreign_keys=False, data=False)
    assert_checkpoint(conn, expected, 103)


@pytest.mark.parametrize('start', STARTS)
@pytest.mark.parametrize('target', range(104, len(registry.MIGRATIONS) + 1))
def test_later_registered_edges_keep_original_branch_menu_and_custody(restored_radio, start, target):
    advance(restored_radio(start), target)
