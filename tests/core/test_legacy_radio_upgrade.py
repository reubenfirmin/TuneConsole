"""Saved Radio state through a genuine installed schema before the account rename."""
import json

import pytest

from yt_playlist.core import catalog, radio_storage
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.migrations.runner import MigrationError, validate_version
from yt_playlist.core.store import Store
from yt_playlist.core.storage_maintenance import StorageMaintenance


def installed_radio(path, *, account_id=3, playlist='RADIO'):
    store = Store(path)
    try:
        migrate(store.conn, registry=registry.MIGRATIONS[:93])
        with store.conn:
            store.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) "
                               "VALUES (3,'Listener','credential',1)")
            for video in ('first', 'second', 'pending'):
                catalog.upsert(store.conn, video, video, 'Artist', preferred_key=video+'|artist')
            state = dict(version=2, episode_id='retained-episode', identity=account_id,
                         phase='appending', playlist=playlist, position=1,
                         tracks=[dict(video_id=video, title=video, artist='Artist')
                                 for video in ('first', 'second', 'first')],
                         initial_tracks=[dict(video_id='second')],
                         pending=dict(video_id='pending'), pending_sent=True,
                         pending_started_at=10, started_at=1, heard=['first'],
                         tuning={'retained': True})
            raw = json.dumps(state)
            store.conn.execute('INSERT INTO settings VALUES (?,?)', (radio_storage.SESSION_KEY, raw))
        validate_version(store.conn, 93)
        return store, state, raw
    except BaseException:
        store.conn.close()
        raise


@pytest.mark.parametrize(('playlist', 'scope_account'), [('RADIO', None), ('LM', 3)])
def test_saved_radio_upgrades_before_account_rename_and_preserves_checkpoint(tmp_path, playlist, scope_account):
    store, state, _ = installed_radio(tmp_path/'radio.db', playlist=playlist)
    try:
        assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='accounts'").fetchone()
        # Explicitly exercise the frozen converter; routine startup retains v93.
        migrate(store.conn, registry=registry.MIGRATIONS)
        validate_version(store.conn, len(registry.MIGRATIONS))
        published, token = radio_storage.load_published(store.conn)
        assert published == state
        assert token == ('retained-episode', 1)
        assert radio_storage.load(store.conn) == (state, token)
        assert [tuple(row) for row in store.conn.execute(
            'SELECT position,video_id,state FROM radio_queue_items ORDER BY position')] == [
                (0, 'first', 'played'), (1, 'second', 'current'), (2, 'first', 'queued'),
                (3, 'second', 'initial'), (4, 'pending', 'pending')]
        assert tuple(store.conn.execute('SELECT s.account_id,p.account_id,p.provider_playlist_id '
            'FROM radio_sessions s JOIN playlist_scopes p ON p.id=s.playlist_scope_id').fetchone()) == (
                3, scope_account, playlist)
        assert not store.conn.execute('SELECT 1 FROM settings WHERE key=?',
                                      (radio_storage.SESSION_KEY,)).fetchone()
    finally:
        store.conn.close()


def test_saved_radio_with_unknown_account_retains_original_and_history(tmp_path):
    store, _, raw = installed_radio(tmp_path/'unknown-account.db', account_id=999)
    try:
        with pytest.raises(MigrationError, match='recorded account'):
            migrate(store.conn, registry=registry.MIGRATIONS)
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 93
        assert store.conn.execute('SELECT value FROM settings WHERE key=?',
                                  (radio_storage.SESSION_KEY,)).fetchone()[0] == raw
        assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='radio_sessions'").fetchone()
        validate_version(store.conn, 93)
    finally:
        store.conn.close()


def test_routine_startup_keeps_saved_radio_and_continuing_checkpoint_writes(tmp_path):
    setup, state, raw = installed_radio(tmp_path/'retained-radio.db')
    history = list(map(tuple, setup.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    setup.conn.close()
    store = Store(tmp_path/'retained-radio.db')
    maintenance = StorageMaintenance(store)
    try:
        store.init_schema(defer_metadata=True)
        assert not store.activity_paused
        assert radio_storage.load_published(store.conn) == (state, None)
        assert store.conn.execute('SELECT value FROM settings WHERE key=?',
                                  (radio_storage.SESSION_KEY,)).fetchone()[0] == raw
        # The runtime's legacy writer keeps the original recovery wire format.
        state = {**state, 'position': 2, 'heard': ['first', 'second']}
        store.settings.set_setting(radio_storage.SESSION_KEY, json.dumps(state))
        maintenance.start()
        maintenance.join(10)
        assert maintenance.ready.is_set() and maintenance.phase == 'done'
        assert radio_storage.load_published(store.conn) == (state, None)
        assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        assert not radio_storage.available(store.conn)
    finally:
        maintenance.stop()
        maintenance.join(5)
        store.conn.close()
