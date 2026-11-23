"""Cold startup and reopen use the shipped current schema."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys

import pytest

from tests.genre_catalog_support import bound_catalog
from yt_playlist.core import association_work, credit_retention, destination_schema, mode_retention, vibe_versions
from yt_playlist.core.metadata_contract import ACTIVE, EXPIRY
from yt_playlist.core.migrations import Migration, current, registry, regular, schema_definition
from yt_playlist.core.migrations import control_settings_storage, preparation_checkpoint_storage


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Schema owners do not construct an HTTP lifespan."""


def _forbid_historical_schema(patch):
    def forbidden(*args, **kwargs):
        raise AssertionError('historical schema used by current database')

    patch.setattr(registry, 'MIGRATIONS', tuple(
        Migration(m.version, m.name, forbidden) for m in registry.MIGRATIONS))
    patch.setattr(regular, 'CHANGES', tuple(
        regular.Change(c.version, c.name, forbidden, c.validate) for c in regular.CHANGES))


def _assert_fresh_settings(conn):
    settings = dict(conn.execute('SELECT key,value FROM settings'))
    assert settings.pop(ACTIVE) == '0'
    assert settings.pop(association_work.SERIAL) == '0'
    assert json.loads(settings.pop(EXPIRY)) == {'gc_cursor': 0, 'gc_after': 0, 'gc_shard': 0}
    assert settings.pop(mode_retention.HIGH_WATER) == '0'
    assert json.loads(settings.pop(mode_retention.CHECKPOINT)) == {'enabled': 0, 'cursor': 0, 'next_scan_at': 0}
    assert json.loads(settings.pop(credit_retention.CHECKPOINT)) == {
        'enabled': 0, 'cursor': 0, 'completed': 0, 'next_scan_at': 0}
    for key in ('playlist_recipe_id_high_water', 'playlist_canvas_id_high_water',
                'home_display_files_id_high_water', 'listening_output_files_id_high_water',
                'choice_learning_id_high_water', 'content_pending_serial', 'prepared_home_id_high_water',
                'prepared_listening_id_high_water', 'prepared_report_id_high_water'):
        assert settings.pop(key) == '0'
    assert settings == current.SEED_SETTINGS | preparation_checkpoint_storage.SEEDS


def _assert_fresh_schema(conn):
    _assert_fresh_settings(conn)
    assert credit_retention.state(conn) == dict(enabled=0, cursor=0, completed=0, next_scan_at=0.0)
    assert tuple(conn.execute('SELECT position,group_key,revision,cursor FROM rec_vibe_pending WHERE position>0'
                              ).fetchone()) == (1, '*', 2, '')
    assert conn.execute("SELECT 1 FROM genre_catalog WHERE name='Techno'").fetchone()
    objects = dict(conn.execute('SELECT name,type FROM sqlite_schema'))
    retired = control_settings_storage.RETIRED + preparation_checkpoint_storage.RETIRED
    assert not set(retired).intersection(objects)
    expected = schema_definition._schema_contract(destination_schema.statements())
    for name, kind in (('accounts', 'table'), ('listening_days', 'view'), ('playlists_scope_insert', 'trigger')):
        assert expected[name][0] == objects[name] == kind
    assert objects['ix_genre_catalog_name'] == 'index'
    assert vibe_versions.normalized(conn)
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert list(map(tuple, conn.execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        (m.version, m.name) for m in registry.MIGRATIONS]

    before = list(conn.iterdump())
    conn.execute("INSERT INTO accounts(id,label,is_master) VALUES (1,'Main',1)")
    with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed'):
        conn.execute("UPDATE accounts SET listening_timezone='UTC',listening_timezone_confirmed=2 WHERE id=1")
    conn.rollback()
    assert list(conn.iterdump()) == before


def _cold_admission(path, reopening):
    from yt_playlist.core.store import Store

    assert path.exists() == reopening
    with pytest.MonkeyPatch.context() as patch:
        bound_catalog(patch, entries=(('rock', 'Rock'), ('jazz', 'Jazz'), ('techno', 'Techno')))
        _forbid_historical_schema(patch)
        assert 'yt_playlist.core.preparation.model_trials' not in sys.modules
        store = Store(path)
        with closing(store.conn) as conn:
            store.init_schema()
            if not reopening:
                _assert_fresh_schema(conn)
                store.settings.set_setting('cold-current', 'persisted é')
            assert store.settings.get_setting('cold-current') == 'persisted é'
            before = list(conn.iterdump())
            if not reopening:
                store.init_schema()
                assert list(conn.iterdump()) == before
    return hashlib.sha256('\n'.join(before).encode()).hexdigest()


def test_real_admission_preserves_cross_process_setting(tmp_path):
    cold = '''
import os
from pathlib import Path
import sys

root, home = map(Path, sys.argv[1:3])
sys.path[:0] = [str(root), str(root / 'src')]
os.environ['YT_PLAYLIST_HOME'] = str(home)
os.environ['YT_PLAYLIST_AUDIO_VIBES'] = '0'
os.environ['YT_PLAYLIST_AUDIO_VIBE_MODEL'] = ''
from tests.core.test_current_bootstrap_contract import _cold_admission
print(_cold_admission(home / 'current.db', sys.argv[3] == 'reopen'))
'''
    receipts = {}
    for phase in ('fresh', 'reopen'):
        result = subprocess.run([sys.executable, '-I', '-c', cold,
            str(Path(__file__).parents[2]), str(tmp_path), phase], cwd=tmp_path,
            capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, (phase, result.stdout, result.stderr)
        receipts[phase] = result.stdout.strip()
    assert receipts['fresh'] == receipts['reopen']


def test_maintained_current_export_is_text_only_and_matches_checked_in_sources(monkeypatch):
    exporter = runpy.run_path(Path(__file__).parents[2] / 'scripts/export-current-schema.py')
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: pytest.fail('Exporter opened a database'))
    for path, expected in exporter['render'](schema_definition).items():
        assert path.read_text(encoding='utf-8') == expected
