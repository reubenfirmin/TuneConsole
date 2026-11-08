"""One actual installed-v88 journey retains reference and interrupted-audit evidence."""
from contextlib import closing
from dataclasses import replace
import sqlite3

import pytest

from tests.core.test_domain_contract_preparation import finish
from tests.core.test_early_schema_upgrade import rename_owner, restore_programs
from tests.core.test_installed_genre_contract import earlier_database
from tests.genre_catalog_support import bound_catalog
from yt_playlist.core.migrations import current, migrate, registry, regular, runner
from yt_playlist.core.preparation import domain_contracts, installed_genre_contract as repair
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL owners do not construct an HTTP application."""


@pytest.fixture(scope='module', autouse=True)
def isolated_genre_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('earlier-genre-home')))
        bound_catalog(patch, entries=(('rock', 'Rock'), ('jazz', 'Jazz'), ('blues', 'Blues')))
        yield


@pytest.fixture(scope='module')
def earlier(tmp_path_factory):
    path = tmp_path_factory.mktemp('earlier-genre') / 'installed.db'
    with closing(earlier_database(path)) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=OFF')
        yield dict(conn=conn, path=path, version=88,
                   numbered=list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))))


def assert_reference(state):
    conn = state['conn']
    assert tuple(conn.execute('SELECT axis_id,value,updated_at FROM rec_lean').fetchone()) == (7, 1.4, 10)
    archive = dict(conn.execute("SELECT * FROM taste_axis_legacy WHERE row_key='genre:Jazz'").fetchone())
    assert {name: archive[name] for name in ('table_name', 'row_key', 'raw_axis', 'axis_id')} == {
        'table_name': 'rec_lean', 'row_key': 'genre:Jazz', 'raw_axis': 'genre:Jazz', 'axis_id': 7}
    if 'lean_value' in archive:
        assert archive['lean_value'] is None
    assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))[:88] == state['numbered']
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


def test_exact_earlier_v88_contract_admits_without_writes_or_advancing_history(earlier):
    conn = earlier['conn']
    assert [row[1] for row in conn.execute('PRAGMA table_info(taste_axis_legacy)')] == [
        'table_name', 'row_key', 'raw_axis', 'axis_id']
    choice = conn.execute("SELECT sql FROM sqlite_schema WHERE name='entry_field_choices'").fetchone()[0]
    assert 'WHERE key=o.genre_key' in choice and 'genre_redirects' not in choice
    trigger = conn.execute("SELECT sql FROM sqlite_schema WHERE name='catalog_tracks_update'").fetchone()[0]
    assert 'SELECT key FROM genre_catalog WHERE key=NEW.genre OR name=NEW.genre COLLATE NOCASE' in trigger
    before = conn.total_changes
    assert migrate(conn, defer_validation=True) is True
    assert conn.total_changes == before
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 88
    assert_reference(earlier)


@pytest.mark.parametrize('version', range(1, len(regular.CHANGES) + 1))
def test_each_real_regular_owner_keeps_earlier_numbered_receipts_and_reference(earlier, version):
    conn = earlier['conn']
    regular.migrate(conn, changes=regular.CHANGES[:version], clock=lambda: 2.)
    assert regular.validate(conn) == version
    assert_reference(earlier)


@pytest.fixture(scope='module')
def audited(earlier):
    conn = earlier['conn']
    with conn:
        # A second valid axis supplies the changing audit subject. The original
        # Jazz axis remains available for the independent reference contract.
        conn.execute("INSERT INTO taste_axes(id,kind,genre_key) VALUES (8,'genre','blues')")
    selected = repair.job(conn)
    selected.install(conn)
    earlier['audit'] = selected
    earlier['checkpoint'] = finish(selected, conn)
    return earlier


def test_real_audit_input_commits_before_a_later_subject_change(audited):
    assert audited['audit'].progress(audited['conn']).status == 'prepared'
    assert_reference(audited)


def test_interrupted_audit_rolls_back_only_the_failed_batch_and_real_retry_keeps_commits(audited):
    conn, selected = audited['conn'], audited['audit']
    before = list(map(tuple, conn.execute('SELECT * FROM '+domain_contracts.table(selected, 'rows'))))
    with conn:
        conn.execute("UPDATE taste_axes SET genre_key='rock' WHERE id=8")

    def fail(*args):
        selected.copy(*args)
        raise OSError('controlled audit failure')

    with pytest.raises(OSError, match='controlled audit failure'):
        replace(selected, copy=fail).step(conn)
    assert selected.progress(conn).status == 'failed'
    assert selected.progress(conn).processed == audited['checkpoint'].processed
    assert list(map(tuple, conn.execute('SELECT * FROM '+domain_contracts.table(selected, 'rows')))) == before
    regular.migrate(conn)
    selected.step(conn, retry=True)
    finish(selected, conn)
    domain_contracts.verify(conn, selected)
    assert_reference(audited)


@pytest.mark.parametrize('version', range(89, 103))
def test_each_actual_numbered_owner_keeps_the_audit_reference_and_future_write_guards(audited, version):
    conn = audited['conn']
    runner.migrate(conn, registry=registry.MIGRATIONS[:version], clock=lambda: 3.)
    audited['version'] = version
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
    assert_reference(audited)
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute("INSERT INTO taste_axes(kind) VALUES ('other')")


def test_actual_account_rename_retains_the_original_reference_and_audit(audited):
    rename_owner(audited)
    assert_reference(audited)


def test_actual_program_restoration_keeps_the_adopted_audit_and_guards(audited):
    restore_programs(audited)
    runner.validate_version(audited['conn'], 103, data=False, foreign_keys=False)
    assert_reference(audited)


@pytest.mark.parametrize('version', range(104, len(registry.MIGRATIONS) + 1))
def test_actual_later_owners_preserve_earlier_reference_and_completed_audit(audited, version):
    runner.migrate(audited['conn'], registry=registry.MIGRATIONS[:version], clock=lambda: 3.)
    audited['version'] = version
    assert_reference(audited)


def test_current_contract_and_idempotent_admission_keep_both_histories_and_audit(audited):
    conn, selected = audited['conn'], audited['audit']
    current.validate(conn)
    regular.migrate(conn)
    selected.validate(conn)
    finish(selected, conn)
    domain_contracts.verify(conn, selected)
    before = conn.total_changes
    migrate(conn)
    assert conn.total_changes == before
    assert_reference(audited)
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute("INSERT INTO taste_axes(kind) VALUES ('other')")


def test_fresh_store_reopens_the_earlier_reference_and_accepts_continuing_writes(audited):
    store = Store(audited['path'])
    with closing(store.conn):
        store.init_schema(defer_metadata=True)
        assert tuple(store.conn.execute('SELECT axis_id,value,updated_at FROM rec_lean').fetchone()) == (7, 1.4, 10)
        store.settings.set_setting('after-earlier-upgrade', 'accepted')
        assert store.settings.get_setting('after-earlier-upgrade') == 'accepted'
        regular.validate(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
