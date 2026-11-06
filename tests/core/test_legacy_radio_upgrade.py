"""Saved Radio state through a genuine installed schema before the account rename."""
import json
import sys
import time
import traceback

import pytest

from yt_playlist.core import catalog, db_handles, radio_storage
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.migrations.runner import MigrationError, validate_version
from yt_playlist.core.store import Store
from yt_playlist.core.storage_maintenance import StorageMaintenance
from tests.prepared_fixture_support import roots


def seed_installed_radio(store):
    with store.conn:
        store.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) "
                           "VALUES (3,'Listener','credential',1)")
        for video in ('first', 'second', 'pending'):
            catalog.upsert(store.conn, video, video, 'Artist', preferred_key=video+'|artist')
        state = dict(version=2, episode_id='retained-episode', identity=3,
                     phase='appending', playlist='RADIO', position=1,
                     tracks=[dict(video_id=video, title=video, artist='Artist')
                             for video in ('first', 'second', 'first')],
                     initial_tracks=[dict(video_id='second')],
                     pending=dict(video_id='pending'), pending_sent=True,
                     pending_started_at=10, started_at=1, heard=['first'],
                     tuning={'retained': True})
        raw = json.dumps(state)
        store.conn.execute('INSERT INTO settings VALUES (?,?)', (radio_storage.SESSION_KEY, raw))
    validate_version(store.conn, 93)
    return state, raw


@pytest.mark.parametrize(('playlist', 'scope_account'), [('RADIO', None), ('LM', 3)])
def test_saved_radio_upgrades_before_account_rename_and_preserves_checkpoint(
        tmp_path, installed_radio, legacy_radio_template, playlist, scope_account):
    store, state, raw = installed_radio(tmp_path/'radio.db', playlist=playlist)
    try:
        template = legacy_radio_template
        assert state is not template.result[0]
        assert template.result[0]['playlist'] == 'RADIO'
        assert template.source.conn.execute('SELECT value FROM settings WHERE key=?',
            (radio_storage.SESSION_KEY,)).fetchone()[0] == template.result[1]
        assert store.conn.execute('SELECT value FROM settings WHERE key=?',
            (radio_storage.SESSION_KEY,)).fetchone()[0] == raw
        assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations'))) == list(
            map(tuple, template.source.conn.execute('SELECT * FROM schema_migrations')))
        destinations = roots(store.conn)
        for name, source in template.files.items():
            assert source != destinations[name]
            for original in source.rglob('*'):
                if original.is_file():
                    copied = destinations[name] / original.relative_to(source)
                    assert copied.read_bytes() == original.read_bytes()
                    assert (copied.stat().st_dev, copied.stat().st_ino) != (
                        original.stat().st_dev, original.stat().st_ino)
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


def test_saved_radio_with_unknown_account_retains_original_and_history(tmp_path, installed_radio):
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


def test_routine_startup_keeps_saved_radio_and_continuing_checkpoint_writes(
        tmp_path, installed_radio, monkeypatch, record_property):
    setup, state, raw = installed_radio(tmp_path/'retained-radio.db')
    history = list(map(tuple, setup.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    setup.conn.close()
    store = Store(tmp_path/'retained-radio.db')
    maintenance = StorageMaintenance(store)
    progress, started = [], time.perf_counter()
    original_progress = maintenance._progress

    def observe(phase, detail=None):
        progress.append({'seconds': time.perf_counter() - started, 'phase': phase, 'detail': detail})
        return original_progress(phase, detail)

    monkeypatch.setattr(maintenance, '_progress', observe)
    try:
        store.init_schema(defer_metadata=True)
        assert not store.activity_paused
        assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        assert radio_storage.load_published(store.conn) == (state, None)
        assert store.conn.execute('SELECT value FROM settings WHERE key=?',
                                  (radio_storage.SESSION_KEY,)).fetchone()[0] == raw
        # The runtime's legacy writer keeps the original recovery wire format.
        state = {**state, 'position': 2, 'heard': ['first', 'second']}
        store.settings.set_setting(radio_storage.SESSION_KEY, json.dumps(state))
        started = time.perf_counter()
        maintenance.start()
        try:
            maintenance.join(10)
        finally:
            # Capture before cancellation changes the worker's phase or stack.
            record_property('maintenance_seconds', time.perf_counter() - started)
            record_property('maintenance_progress', json.dumps(progress))
            diagnostic = dict(phase=maintenance.phase, detail=maintenance.detail,
                error=maintenance.error, ready=maintenance.ready.is_set())
            try:
                diagnostic['durable'] = db_handles.state(store._db_path)
            except (OSError, ValueError) as error:
                diagnostic['recovery_read_error'] = repr(error)
            thread = maintenance._thread
            if thread is not None and thread.is_alive():
                frame = sys._current_frames().get(thread.ident)
                diagnostic['worker_stack'] = ''.join(traceback.format_stack(frame)) if frame is not None else 'unavailable'
            record_property('maintenance_diagnostic', json.dumps(diagnostic, default=str))
        assert maintenance.ready.is_set() and maintenance.phase == 'done', diagnostic
        token = ('retained-episode', 1)
        assert radio_storage.load_published(store.conn) == (state, token)
        # Maintenance installs a rebuilt database, retaining the genuine
        # source history in its dedicated provenance owner.
        assert list(map(tuple, store.conn.execute(
            'SELECT * FROM database_rebuild_source_history ORDER BY version'))) == history
        current_history = list(map(tuple, store.conn.execute(
            'SELECT * FROM schema_migrations ORDER BY version')))
        assert [(version, name) for version, name, _ in current_history] == [
            (migration.version, migration.name) for migration in registry.MIGRATIONS]
        assert radio_storage.available(store.conn)
        continued = {**state, 'position': 1, 'heard': ['second', 'first']}
        with store.conn.atomic():
            next_token = radio_storage.save(store.conn, continued, expected=token, now=20)
        assert next_token == ('retained-episode', 2)
        assert radio_storage.load_published(store.conn) == (continued, next_token)
        with pytest.raises(ValueError, match='changed in another worker'), store.conn.atomic():
            radio_storage.save(store.conn, state, expected=token, now=21)
        assert radio_storage.load_published(store.conn) == (continued, next_token)
    finally:
        maintenance.stop()
        maintenance.join(5)
        store.conn.close()
    reopened = Store(tmp_path/'retained-radio.db')
    try:
        reopened.init_schema(defer_metadata=True)
        assert radio_storage.load_published(reopened.conn) == (continued, next_token)
        assert list(map(tuple, reopened.conn.execute(
            'SELECT * FROM database_rebuild_source_history ORDER BY version'))) == history
        assert list(map(tuple, reopened.conn.execute(
            'SELECT * FROM schema_migrations ORDER BY version'))) == current_history
    finally:
        reopened.conn.close()
