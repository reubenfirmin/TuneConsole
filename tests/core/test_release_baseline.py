"""Release upgrades preserve data; older local layouts reset atomically."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.release_schema_support import release_schema
from yt_playlist.core.migrations import current, migrate, regular, MigrationError
from yt_playlist.core.migrations.runner import NOTICE


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These isolated SQL checks do not construct an HTTP application."""


@pytest.fixture
def conn():
    with closing(sqlite3.connect(':memory:')) as database:
        database.row_factory = sqlite3.Row
        database.execute('PRAGMA foreign_keys=ON')
        yield database


def test_release_upgrade_retains_rows_and_applied_history(conn):
    with conn:
        release_schema(conn)
        conn.execute("INSERT INTO settings VALUES ('retained-setting','original')")
        conn.execute("INSERT INTO recordings(identity_key,title,created_at) VALUES ('retained','Song',1)")
    history = [tuple(row) for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
    changes = [tuple(row) for row in conn.execute('SELECT * FROM schema_changes ORDER BY version')]
    migrate(conn, clock=lambda: 2.)
    assert [tuple(row) for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')] == history
    assert [tuple(row) for row in conn.execute('SELECT * FROM schema_changes ORDER BY version')][:37] == changes
    assert conn.execute('SELECT count(*) FROM schema_changes').fetchone()[0] == len(regular.CHANGES)
    assert conn.execute("SELECT title FROM recordings WHERE identity_key='retained'").fetchone()[0] == 'Song'
    assert conn.execute("SELECT value FROM settings WHERE key='retained-setting'").fetchone()[0] == 'original'
    assert not conn.execute('SELECT 1 FROM settings WHERE key=?', (NOTICE,)).fetchone()
    current.validate(conn)


def test_released_store_remains_writable_during_bounded_startup_upgrade(tmp_path):
    from yt_playlist.core.storage_maintenance import StorageMaintenance
    from yt_playlist.core.store import Store

    store = Store(tmp_path / 'released.db')
    worker = StorageMaintenance(store)
    try:
        with store.conn:
            release_schema(store.conn)
            store.conn.execute("INSERT INTO settings VALUES ('release-input','retained')")
        store.init_schema(defer_metadata=True)
        assert regular.validate(store.conn) == 37
        assert not store.activity_paused
        store.settings.set_setting('continuing-write', 'accepted')
        store.tracks.upsert_track('released-song', 'Song', 'Artist', None, 200)
        worker.start()
        worker.join()
        assert worker.phase == 'done'
        assert regular.validate(store.conn) == len(regular.CHANGES)
        assert store.settings.get_setting('release-input') == 'retained'
        assert store.settings.get_setting('continuing-write') == 'accepted'
        assert store.conn.execute("SELECT title FROM youtube_items WHERE video_id='released-song'").fetchone()[0] == 'Song'
        assert not store.settings.get_setting(NOTICE)
        current.validate(store.conn)
    finally:
        worker.stop()
        worker.join()
        store.conn.close()


@pytest.mark.parametrize('layout', ['unversioned', 'numbered', 'before-release'])
def test_older_layout_resets_and_schedules_notice_once(conn, layout):
    with conn:
        if layout == 'before-release':
            release_schema(conn, changes=36)
        elif layout == 'numbered':
            conn.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at REAL NOT NULL)')
            conn.execute("INSERT INTO schema_migrations VALUES (1,'baseline',1)")
        conn.execute('CREATE TABLE obsolete_original(value TEXT)')
        conn.execute("INSERT INTO obsolete_original VALUES ('discardable')")
    # The numbered name is owned by the frozen ledger, not guessed by the test.
    if layout == 'numbered':
        from yt_playlist.core.migrations.registry import MIGRATIONS
        with conn:
            conn.execute('UPDATE schema_migrations SET name=?', (MIGRATIONS[0].name,))
    migrate(conn)
    assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='obsolete_original'").fetchone()
    token = conn.execute('SELECT value FROM settings WHERE key=?', (NOTICE,)).fetchone()[0]
    assert len(token) == 32
    assert isinstance(json.loads(conn.execute("SELECT value FROM settings WHERE key='early_adopter_previous_history'").fetchone()[0]), dict)
    before = list(conn.iterdump())
    migrate(conn)
    assert list(conn.iterdump()) == before
    with conn:
        conn.execute('UPDATE settings SET value=? WHERE key=?', ('', NOTICE))
    migrate(conn)
    assert conn.execute('SELECT value FROM settings WHERE key=?', (NOTICE,)).fetchone()[0] == ''


def test_reset_failure_restores_original_database_and_history(conn, monkeypatch):
    from yt_playlist.core import schema_bootstrap
    with conn:
        conn.execute('CREATE TABLE original(value TEXT)')
        conn.execute("INSERT INTO original VALUES ('keep on failed reset')")
    before = list(conn.iterdump())
    bootstrap = schema_bootstrap.bootstrap
    def interrupted(*args, **kwargs):
        bootstrap(*args, **kwargs)
        raise InterruptedError('bootstrap interrupted')
    monkeypatch.setattr(schema_bootstrap, 'bootstrap', interrupted)
    with pytest.raises(InterruptedError, match='bootstrap interrupted'):
        migrate(conn)
    assert list(conn.iterdump()) == before
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1


@pytest.mark.parametrize('layout', ['before-release', 'identities'])
def test_reset_preserves_saved_accounts_and_pending_playback(tmp_path, layout):
    from yt_playlist.core import playback_feedback
    from yt_playlist.core.runtime import Runtime
    from yt_playlist.core.setup import serialize_config
    from yt_playlist.core.store import Store
    from yt_playlist.library.playback_inbox_composition import inbox_for_database

    store = Store(tmp_path / 'old.db')
    config = tmp_path / 'config.toml'
    config.write_text(serialize_config([
        {'id': 7, 'label': 'Main', 'credential_ref': 'browser.json', 'is_master': True},
        {'id': 42, 'label': 'Brand', 'credential_ref': 'browser.json', 'brand_account_id': 'UC9'},
    ]))
    original_config = config.read_bytes()
    try:
        with store.conn:
            if layout == 'before-release':
                release_schema(store.conn, changes=36)
                table = 'accounts'
            else:
                store.conn.execute('CREATE TABLE identities(id INTEGER PRIMARY KEY,label TEXT NOT NULL,'
                    'credential_ref TEXT NOT NULL,brand_account_id TEXT,is_master INTEGER NOT NULL)')
                store.conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
                table = 'identities'
            store.conn.executemany('INSERT INTO '+table+'(id,label,credential_ref,brand_account_id,is_master) '
                'VALUES (?,?,?,?,?)', [(7, 'Main', 'browser.json', None, 1),
                                     (42, 'Brand', 'browser.json', 'UC9', 0),
                                     (60, 'Retired', 'browser.json', 'old-brand', 0)])
            if layout == 'before-release':
                store.conn.execute('UPDATE accounts SET active=0 WHERE id=60')
            store.conn.execute("INSERT INTO settings VALUES ('bridge_paired','1')")
            store.conn.execute("INSERT INTO settings VALUES ('discardable-derived-state','old')")
        # The independent inbox survives replacement of the main database.
        queue = inbox_for_database(store._db_path, 'reset-check')
        queue.capture('player', 42,
            {'kind': 'next', 'videoId': 'known', 'position': 20, 'duration': 180}, 100)
        store.init_schema()
        assert [tuple(row) for row in store.conn.execute('SELECT id,label,brand_account_id FROM accounts ORDER BY id')] == [
            (7, 'Main', None), (42, 'Brand', 'UC9'), (60, 'Retired', 'old-brand')]
        assert store.settings.get_setting(NOTICE)
        assert not store.settings.get_setting('discardable-derived-state')
        runtime = Runtime(store, config, tmp_path / 'creds')
        runtime.load()
        assert runtime.configured and runtime.credentials_present
        assert set(runtime.clients()) == {7, 42}
        assert config.read_bytes() == original_config
        assert store.conn.execute('SELECT active FROM accounts WHERE id=60').fetchone()[0] == 0
        assert playback_feedback.drain(store) == 1
        assert playback_feedback.drain(store) == 0
        assert store.conn.execute('SELECT account_id FROM playback_feedback_receipts').fetchone()[0] == 42
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        store.conn.close()


def test_future_history_is_rejected_without_reset(conn):
    migrate(conn)
    with conn:
        conn.execute('INSERT INTO schema_changes VALUES (?,?,?)', (len(regular.CHANGES)+1, 'future', 1.))
    before = list(conn.iterdump())
    with pytest.raises(MigrationError, match='newer'):
        migrate(conn)
    assert list(conn.iterdump()) == before


def test_fresh_install_does_not_show_reset_notice(conn):
    migrate(conn)
    assert not conn.execute('SELECT 1 FROM settings WHERE key=?', (NOTICE,)).fetchone()


def test_reset_does_not_reuse_file_receipt_ids(conn):
    with conn:
        conn.execute('CREATE TABLE annotation_file_writes(id INTEGER PRIMARY KEY AUTOINCREMENT)')
        conn.execute('INSERT INTO annotation_file_writes(id) VALUES (800)')
        conn.execute('DELETE FROM annotation_file_writes')
    migrate(conn)
    assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='annotation_file_writes'").fetchone()[0] == 800
