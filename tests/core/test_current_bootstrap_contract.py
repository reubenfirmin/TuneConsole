"""Fresh bootstrap and reopen use the shipped current schema."""
from contextlib import closing
from pathlib import Path
import runpy
import select
import sqlite3
import subprocess
import sys

import pytest

from tests.core.test_current_schema import _forbid_historical_schema, assert_fresh_settings, connect, objects
from tests.genre_catalog_support import bound_catalog
from yt_playlist.core import credit_retention, vibe_versions
from yt_playlist.core.migrations import current, migrate, registry


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Schema owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def bounded_bootstrap_inputs(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('bootstrap-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        bound_catalog(patch, entries=(('rock', 'Rock'), ('jazz', 'Jazz'), ('techno', 'Techno')))
        yield


@pytest.fixture(scope='module')
def cold_home(tmp_path_factory):
    return tmp_path_factory.mktemp('cold-bootstrap')


@pytest.fixture(scope='class', params=['fresh', 'reopen'])
def cold_admission(cold_home, request):
    cold = '''
import os
import sys
from pathlib import Path
root, home = map(Path, sys.argv[1:3])
sys.path[:0] = [str(root), str(root / 'src')]
os.environ['YT_PLAYLIST_HOME'] = str(home)
os.environ['YT_PLAYLIST_AUDIO_VIBES'] = '0'
os.environ['YT_PLAYLIST_AUDIO_VIBE_MODEL'] = ''
import pytest
from tests.core.test_current_schema import _forbid_historical_schema
from tests.genre_catalog_support import bound_catalog
from yt_playlist.core.store import Store
from yt_playlist.core import destination_schema
from yt_playlist.core.migrations import schema_definition
with pytest.MonkeyPatch.context() as patch:
    bound_catalog(patch, entries=(('rock','Rock'),('jazz','Jazz'),('techno','Techno')))
    _forbid_historical_schema(patch)
    assert 'yt_playlist.core.preparation.model_trials' not in sys.modules
    store = None
    print('imports guarded', flush=True)
    try:
        for line in sys.stdin:
            command = line.strip()
            if command == 'reference':
                expected = schema_definition._schema_contract(destination_schema.statements())
                assert expected['accounts'][0] == 'table'
                assert expected['playlists_scope_insert'][0] == 'trigger'
                assert expected['listening_days'][0] == 'view'
            elif command == 'admit':
                store = Store(home / 'current.db')
                store.init_schema()
                if sys.argv[3] == 'reopen':
                    assert store.settings.get_setting('cold-current') == 'persisted é'
                store.settings.set_setting('cold-current', 'persisted é')
                assert store.settings.get_setting('cold-current') == 'persisted é'
                before = list(store.conn.iterdump())
            elif command == 'readmit':
                store.init_schema()
                assert list(store.conn.iterdump()) == before
            elif command == 'close':
                break
            else:
                raise AssertionError(command)
            print(command + ' preserved', flush=True)
    finally:
        if store is not None:
            store.conn.close()
    print('closed', flush=True)
'''
    process = subprocess.Popen([sys.executable, '-I', '-u', '-c', cold,
        str(Path(__file__).parents[2]), str(cold_home), request.param],
        cwd=cold_home, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def cold_response(process, expected, command=None):
    if command is not None:
        process.stdin.write(command+'\n')
        process.stdin.flush()
    ready, _, _ = select.select([process.stdout], [], [], 60)
    assert ready, 'Cold schema child did not complete its requested operation'
    actual = process.stdout.readline().strip()
    if actual != expected:
        process.wait(timeout=5)
        pytest.fail(f'Expected {expected!r}, got {actual!r}: {process.stderr.read()}')


class TestColdAdmission:
    def test_imports_guard_every_historical_callback(self, cold_admission):
        cold_response(cold_admission, 'imports guarded')

    def test_compiled_reference_keeps_actual_tables_views_and_guards(self, cold_admission):
        cold_response(cold_admission, 'reference preserved', 'reference')

    def test_real_admission_preserves_cross_process_setting(self, cold_admission):
        cold_response(cold_admission, 'admit preserved', 'admit')

    def test_repeat_admission_preserves_every_sql_row(self, cold_admission):
        cold_response(cold_admission, 'readmit preserved', 'readmit')

    def test_closes_real_connection_and_exits_successfully(self, cold_admission):
        cold_response(cold_admission, 'closed', 'close')
        assert cold_admission.wait(timeout=5) == 0, cold_admission.stderr.read()


def test_maintained_current_export_is_text_only_and_matches_checked_in_sources(monkeypatch):
    from yt_playlist.core.migrations import schema_definition

    exporter = runpy.run_path(Path(__file__).parents[2] / 'scripts/export-current-schema.py')
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: pytest.fail('Exporter opened a database'))
    for path, expected in exporter['render'](schema_definition).items():
        assert path.read_text(encoding='utf-8') == expected


@pytest.fixture(scope='module')
def fresh(tmp_path_factory):
    path = tmp_path_factory.mktemp('current-bootstrap') / 'current.db'
    with closing(connect(path)) as conn:
        with pytest.MonkeyPatch.context() as patch:
            _forbid_historical_schema(patch)
            migrate(conn)
        yield conn, path


def test_real_bootstrap_has_all_seeds_programs_and_normalized_versions(fresh):
    conn, _ = fresh
    assert_fresh_settings(conn)
    assert credit_retention.state(conn) == dict(enabled=0, cursor=0, completed=0, next_scan_at=0.0)
    assert tuple(conn.execute('SELECT position,group_key,revision,cursor FROM rec_vibe_pending WHERE position>0'
                              ).fetchone()) == (1, '*', 2, '')
    assert conn.execute("SELECT 1 FROM genre_catalog WHERE name='Techno'").fetchone()
    for kind, name in [('index', 'ix_genre_catalog_name'), ('view', 'listening_days'),
            ('trigger', 'playlists_scope_insert')]:
        assert conn.execute('SELECT 1 FROM sqlite_schema WHERE type=? AND name=?', (kind, name)).fetchone()
    assert vibe_versions.normalized(conn)
















def test_current_constraints_reject_invalid_calendar_confirmation_and_rollback(fresh):
    conn, _ = fresh
    before = list(conn.iterdump())
    conn.execute("INSERT INTO accounts(id,label,is_master) VALUES (1,'Main',1)")
    with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed'):
        conn.execute("UPDATE accounts SET listening_timezone='UTC',listening_timezone_confirmed=2 WHERE id=1")
    conn.rollback()
    assert list(conn.iterdump()) == before


def test_current_readmission_preserves_personal_settings_and_full_history(fresh, monkeypatch):
    conn, _ = fresh
    _forbid_historical_schema(monkeypatch)
    with conn:
        conn.execute("INSERT INTO settings VALUES ('personal-setting','retain')")
    before = list(conn.iterdump())
    migrate(conn)
    assert list(conn.iterdump()) == before
    assert list(map(tuple, conn.execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        (migration.version, migration.name) for migration in registry.MIGRATIONS]


def test_fresh_disk_reopen_preserves_every_row_without_historical_callbacks(fresh, monkeypatch):
    conn, path = fresh
    before = list(conn.iterdump())
    _forbid_historical_schema(monkeypatch)
    with closing(connect(path)) as reopened:
        migrate(reopened)
        assert list(reopened.iterdump()) == before
