"""Keep adopted future-write guards across real numbered owner transitions."""
from contextlib import closing
import sqlite3
from unittest.mock import Mock

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core import schema_definition
from yt_playlist.core.migrations import registry, regular, runner, v103_entity_names
from yt_playlist.core.migrations.runner import Context
from yt_playlist.core.preparation import installed_genre_contract
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('genre-guard-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield


def numbered_history(conn):
    return tuple(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))


def regular_history(conn):
    return tuple(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version')))


@pytest.fixture(scope='module', params=[79, 85, 87, 92])
def installed(request, tmp_path_factory):
    path = tmp_path_factory.mktemp('genre-guard-upgrade') / 'installed.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        # These isolated logical-upgrade checks exercise SQL/FK behavior;
        # power-loss durability is outside their contract.
        conn.execute('PRAGMA synchronous=OFF')
        install_numbered_input(conn, request.param)
        with conn:
            conn.execute("INSERT INTO settings VALUES ('retained-guard-setting','original')")
            if request.param < 88:
                conn.execute("INSERT INTO rec_weights VALUES ('genre:Jazz',1.25,10)")
            else:
                conn.execute("INSERT INTO taste_axes(kind,genre_key) VALUES ('genre','jazz')")
                conn.execute("INSERT INTO rec_weights SELECT id,1.25,10 FROM taste_axes WHERE genre_key='jazz'")
        yield {'conn': conn, 'path': path, 'version': request.param,
               'numbered': numbered_history(conn)}


def test_installed_input_is_the_real_supported_numbered_contract(installed):
    conn = installed['conn']
    runner.validate_version(conn, installed['version'])
    assert numbered_history(conn) == installed['numbered']
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def first_regular_owners(installed):
    regular.migrate(installed['conn'], changes=regular.CHANGES[:7], clock=lambda: 2.)
    return installed


def test_original_seven_regular_owners_do_not_advance_numbered_history(first_regular_owners):
    state = first_regular_owners
    assert numbered_history(state['conn']) == state['numbered']
    assert [version for version, _, _ in regular_history(state['conn'])] == list(range(1, 8))


@pytest.fixture(scope='module')
def lifecycle_owner(first_regular_owners):
    regular.migrate(first_regular_owners['conn'], changes=regular.CHANGES[:8], clock=lambda: 2.)
    return first_regular_owners


def test_real_lifecycle_regular_owner_is_applied_before_future_numbered_edges(lifecycle_owner):
    conn = lifecycle_owner['conn']
    assert tuple(conn.execute('SELECT version,name FROM schema_changes WHERE version=8').fetchone()) == (
        8, 'installed_taste_guard_lifecycle')
    assert numbered_history(conn) == lifecycle_owner['numbered']
    if lifecycle_owner['version'] >= 88:
        installed_genre_contract.validate(conn)


@pytest.fixture(scope='module')
def adopted(lifecycle_owner):
    conn = lifecycle_owner['conn']
    # The guard lifecycle has its actual regular runner above. Supply the
    # unrelated completed regular DDL once, in one fixture transaction, instead
    # of validating each growing prefix for every installed origin.
    with conn:
        conn.execute('BEGIN')
        for change in regular.CHANGES[8:]:
            change.apply(conn, Context(2.))
            conn.execute('INSERT INTO schema_changes VALUES (?,?,2)', (change.version, change.name))
    regular.migrate(conn)
    lifecycle_owner['regular'] = regular_history(conn)
    return lifecycle_owner


def test_real_regular_adoption_preserves_original_numbered_history_and_weight(adopted):
    conn = adopted['conn']
    assert numbered_history(conn) == adopted['numbered']
    assert [(version, name) for version, name, _ in adopted['regular']] == [
        (change.version, change.name) for change in regular.CHANGES]
    if adopted['version'] < 88:
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name LIKE 'installed_taste_axis_guard_%'").fetchone()
        assert tuple(conn.execute('SELECT * FROM rec_weights').fetchone()) == ('genre:Jazz', 1.25, 10)
    else:
        assert_guards_and_weight(adopted)


def assert_guards_and_weight(state):
    conn = state['conn']
    installed_genre_contract.validate(conn)
    assert regular_history(conn) == state['regular']
    assert numbered_history(conn)[:state['version']] == state['numbered']
    assert conn.execute("SELECT value FROM settings WHERE key='retained-guard-setting'").fetchone()[0] == 'original'
    assert tuple(conn.execute('SELECT a.kind,a.genre_key,w.weight,w.updated_at FROM rec_weights w '
        'JOIN taste_axes a ON a.id=w.axis_id').fetchone()) == ('genre', 'jazz', 1.25, 10)
    assert {name for name, in conn.execute(
        "SELECT name FROM sqlite_schema WHERE name LIKE 'installed_taste_axis_guard_%'")} == {
            'installed_taste_axis_guard_insert', 'installed_taste_axis_guard_update'}
    rows = tuple(map(tuple, conn.execute('SELECT * FROM taste_axes ORDER BY id')))
    with pytest.raises(sqlite3.IntegrityError, match='Invalid future taste axis'), conn:
        conn.execute("INSERT INTO taste_axes(kind) VALUES ('other')")
    with pytest.raises(sqlite3.IntegrityError, match='Invalid future taste axis'), conn:
        conn.execute("UPDATE taste_axes SET kind='other',genre_key=NULL")
    assert tuple(map(tuple, conn.execute('SELECT * FROM taste_axes ORDER BY id'))) == rows
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('target', range(88, 103))
def test_registered_numbered_edges_keep_exact_regular_history_and_future_write_guards(adopted, target):
    target = max(adopted['version'], target)
    runner.migrate(adopted['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 3.)
    assert numbered_history(adopted['conn'])[-1][0] == target
    assert_guards_and_weight(adopted)


def test_account_cutover_reference_keeps_the_taste_guard_owners(adopted):
    definitions = '\n'.join(schema_definition.statements(version=103))
    assert 'CREATE TABLE accounts' in definitions and 'CREATE TABLE taste_axes' in definitions


@pytest.fixture(scope='module')
def renamed(adopted):
    conn = adopted['conn']
    runner.migrate(conn, registry=registry.MIGRATIONS[:102])
    restore, guards = Mock(), Mock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v103_entity_names, 'restore_programs', restore)
        patch.setattr(regular, 'after_owner_upgrade', guards)
        runner.migrate(conn, registry=registry.MIGRATIONS[:103], clock=lambda: 3.)
    restore.assert_called_once()
    guards.assert_called_once_with(conn)
    adopted['programs'] = restore.call_args
    return adopted


def test_real_account_renaming_keeps_original_numbered_and_regular_receipts(renamed):
    conn = renamed['conn']
    assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='accounts'").fetchone()
    assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='identities'").fetchone()
    assert numbered_history(conn)[:renamed['version']] == renamed['numbered']
    assert regular_history(conn) == renamed['regular']
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def restored(renamed):
    conn, captured = renamed['conn'], renamed['programs']
    with conn:
        conn.execute('BEGIN')
        v103_entity_names.restore_programs(*captured.args, **captured.kwargs)
        regular.after_owner_upgrade(conn)
    return renamed


def test_real_program_restoration_reinstalls_the_adopted_guard_contract(restored):
    runner.validate_version(restored['conn'], 103, foreign_keys=False, data=False)
    assert_guards_and_weight(restored)


@pytest.mark.parametrize('target', range(104, len(registry.MIGRATIONS) + 1))
def test_later_registered_edges_keep_guards_and_all_original_history(restored, target):
    runner.migrate(restored['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 3.)
    assert_guards_and_weight(restored)


def test_regular_revalidation_is_immutable_after_the_final_numbered_upgrade(restored):
    conn = restored['conn']
    before = list(conn.iterdump())
    regular.validate(conn)
    regular.migrate(conn)
    assert list(conn.iterdump()) == before
    assert_guards_and_weight(restored)


def test_fresh_store_reopen_keeps_guards_history_and_continuing_writes(restored):
    store = Store(restored['path'])
    try:
        store.init_schema()
        assert store.settings.get_setting('retained-guard-setting') == 'original'
        assert numbered_history(store.conn) == numbered_history(restored['conn'])
        assert regular_history(store.conn) == restored['regular']
        store.settings.set_setting('continuing-guard-write', 'available')
        assert store.settings.get_setting('continuing-guard-write') == 'available'
        regular.validate(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        store.conn.close()
