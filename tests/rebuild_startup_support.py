"""Controlled converter outputs beneath real startup and file installation."""
from contextlib import closing, contextmanager
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from tests.conftest import FakeClient
from tests.db_rebuild_support import populate_catalog
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import bounded_consumer_schema, install_numbered_input
from yt_playlist.core import db_handles
from yt_playlist.core.db_rebuild import build, destination
from yt_playlist.core.runtime import Runtime
from yt_playlist.core.setup import write_config
from yt_playlist.core.store import Store
from yt_playlist.rec.choice_evidence import STORAGE_PENDING
from yt_playlist.web.app import create_app


@pytest.fixture(scope='module')
def startup_development_source():
    with closing(sqlite3.connect(':memory:', check_same_thread=False)) as conn:
        with pytest.MonkeyPatch.context() as patch:
            bound_catalog(patch)
            install_numbered_input(conn, 93)
        with conn:
            populate_catalog(conn)
        conn.execute('PRAGMA query_only=ON')
        yield conn


@pytest.fixture(scope='module')
def startup_destination():
    # Startup consumes a completed catalog from the converter. Domain conversion
    # has independent real registered-owner tests; none of it is repeated here.
    # The actual construct/checkpoint, job, installer and analytics protocol run.
    # This raw converter connection has no serving readers. Installation still
    # creates the genuine Store revision guards when it reopens the candidate.
    with bounded_consumer_schema((('rock', 'Rock'), ('jazz', 'Jazz')), snapshot_guards=False) as schema:
        statements = tuple(sql for sql in schema.iterdump() if sql not in ('BEGIN TRANSACTION;', 'COMMIT;'))

    def initialize(conn):
        for sql in statements:
            conn.execute(sql)
        conn.execute(destination.PROVENANCE)
        conn.executemany('INSERT INTO accounts(id,label,credential_ref,is_master) VALUES(?,?,?,?)',
                         [(11, 'Main', 'main.json', 1), (29, 'Other account', 'other.json', 0)])
        conn.executemany('INSERT INTO recordings(id,identity_key,title,artist) VALUES(?,?,?,?)',
                         [(key, f'song {key}|artist', f'Song {key}', 'Artist') for key in (3, 8, 19)])
        conn.executemany('INSERT INTO youtube_items(video_id,recording_id,duration_s,available) VALUES(?,?,200,1)',
                         [(f'video-{key}', key) for key in (3, 8, 19)])
        conn.executemany('INSERT INTO catalog_entries(id,recording_id,video_id) VALUES(?,?,?)',
                         [(key + 100, key, f'video-{key}') for key in (3, 8, 19)])
        conn.executemany("INSERT INTO library_memberships VALUES(?,'released fixture',1000)",
                         [(key + 100,) for key in (3, 8, 19)])
        conn.executemany('INSERT INTO playlist_scopes(id,provider_playlist_id,account_id,kind) '
                         "VALUES(?,'LM',?,'account')", [(f'@account:{key}:LM', key) for key in (11, 29)])
        conn.executemany('INSERT INTO playlists(id,account_id,provider_playlist_id,title,playlist_scope_id) '
                         "VALUES(?,?,'LM','Favorites',?)",
                         [(playlist, account, f'@account:{account}:LM') for account, playlist in ((11, 41), (29, 73))])
        conn.executemany('INSERT INTO playlist_items VALUES(?,?,?)',
                         [(playlist, f'video-{key}', position) for playlist in (41, 73)
                          for position, key in enumerate((19, 3, 8))])

    def runtime(reader, target, cursor, limit):
        target.executemany('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            reader.execute('SELECT key,value FROM settings'))
        target.execute('UPDATE settings SET value=\'0\' WHERE key=?', (STORAGE_PENDING,))
        target.executemany('INSERT INTO database_rebuild_source_history VALUES(?,?,?)',
                            reader.execute('SELECT * FROM schema_migrations'))
        return build.Batch(rows=3, done=True)

    def analytics(reader, target, cursor, limit):
        target.execute("INSERT INTO settings VALUES('startup-analytics-finished','1')")
        return build.Batch(rows=1, done=True)

    def verify(reader, target):
        assert target.execute('SELECT count(*) FROM recordings').fetchone()[0] == 3
        assert [row[0] for row in target.execute('SELECT id FROM playlists ORDER BY id')] == [41, 73]
        assert target.execute('SELECT value FROM settings WHERE key=?', (STORAGE_PENDING,)).fetchone()[0] == '0'

    return build.Destination('controlled-startup-converter-v1', initialize,
        (build.Stage('completed-runtime-input', runtime), build.Stage('completed-analytics-input', analytics)),
        verify, runtime_stages=1, verify_runtime=verify, verify_analytics=verify)


@contextmanager
def admitted_source(source, root):
    name, template = source
    path = root / 'startup.db'
    with closing(sqlite3.connect(path)) as conn:
        template.backup(conn)
        with conn:
            conn.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (STORAGE_PENDING, '1'))
    store = Store(path)
    try:
        store.init_schema(defer_metadata=True)
        yield store, name == 'production64'
    finally:
        store.conn.close()


@contextmanager
def startup_application(store, root, patch, contract):
    from yt_playlist.core.db_rebuild import job
    from yt_playlist.web import app as web_app

    patch.setattr(destination, 'for_source', lambda path, **kwargs: contract)
    patch.setattr(web_app, '_background_sync_loop', lambda *args, **kwargs: None)
    patch.setattr(web_app, '_background_update_loop', lambda *args, **kwargs: None)
    patch.setattr(web_app, '_install_idle_shutdown', lambda *args: None)
    from yt_playlist.web.routes import app_update, audio_preview, playlists
    patch.setattr(web_app, 'build_all', lambda ctx: [
        app_update.build(ctx), audio_preview.build(ctx), playlists.build(ctx)])
    patch.setattr('yt_playlist.core.runtime.build_client', lambda *args: FakeClient())
    config = root / 'config.toml'
    write_config([{'id': 11, 'label': 'Main', 'credential_ref': 'main.json', 'is_master': True},
                  {'id': 29, 'label': 'Other account', 'credential_ref': 'other.json'}], config)
    runtime = Runtime(store, config, root)
    runtime.load()
    assert runtime.configured and set(runtime.clients()) == {11, 29}
    app = create_app(store, runtime.clients, setup=runtime, background_work=True)
    ctx = app.state.ctx
    assert store._evidence_recovery_pending
    state = SimpleNamespace(app=app, ctx=ctx, started=[], copying=threading.Event(),
        copy_release=threading.Event(), staged=threading.Event(), staging_release=threading.Event(),
        analytics_entered=threading.Event(),
        analytics_release=threading.Event(), analytics_completed=threading.Event(),
        workers_started=threading.Event(), phases=[], failures=[])
    finish = job.finish_analytics

    def finish_analytics(*args, **kwargs):
        assert not store.activity_paused and not ctx.storage_maintenance.active
        assert not ctx.storage_maintenance.ready.is_set() and not state.started
        assert store.settings.get_setting(STORAGE_PENDING) == '0'
        store.settings.set_setting('accepted-during-analytics', 'preserved')
        state.analytics_entered.set()
        assert state.analytics_release.wait(10)
        result = finish(*args, **kwargs)
        assert not state.started
        state.analytics_completed.set()
        return result

    def start_worker(name):
        assert state.analytics_completed.is_set() and ctx.storage_maintenance.ready.is_set()
        assert not store.activity_paused and not store._evidence_recovery_pending
        assert store.settings.get_setting('startup-analytics-finished') == '1'
        state.started.append(name)
        if name == 'enrichment':
            state.workers_started.set()

    patch.setattr(job, 'finish_analytics', finish_analytics)
    patch.setattr(ctx.radio, 'prepare', lambda: None)
    patch.setattr(ctx.rec_worker, 'start_ticker', lambda: start_worker('recommendations'))
    patch.setattr(ctx.rec_worker, 'trigger_mode_refresh', lambda: None)
    patch.setattr(ctx.enrich_worker, 'start_ticker', lambda: start_worker('enrichment'))
    try:
        yield state
    finally:
        state.copy_release.set()
        state.staging_release.set()
        state.analytics_release.set()


def gate_copy(state, patch, *, fail=False):
    progress = state.ctx.storage_maintenance._progress

    def report(phase, detail=None):
        state.phases.append(phase)
        progress(phase, detail)
        if phase == 'copy':
            state.copying.set()
            assert state.copy_release.wait(10)
        if phase == 'staging':
            state.staged.set()
            assert state.staging_release.wait(10)
        if fail and phase == 'reopening' and not state.failures:
            state.failures.append(phase)
            raise RuntimeError('controlled reopen failure')

    patch.setattr(state.ctx.storage_maintenance, '_progress', report)


def assert_original(store, history):
    original = db_handles.state(store._db_path)['original']
    with closing(sqlite3.connect(f'file:{original}?mode=ro', uri=True)) as conn:
        assert conn.execute('SELECT value FROM settings WHERE key=?', (STORAGE_PENDING,)).fetchone() == ('1',)
        assert conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall() == history
