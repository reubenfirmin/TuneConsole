import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.store import Store


def connect(path):
    conn = sqlite3.connect(path, timeout=2)
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def test_takeout_coverage_upgrade_preserves_legacy_rows_without_inventing_authority():
    from yt_playlist.core.migrations.registry import MIGRATIONS
    store = Store(':memory:')
    migrate(store.conn, registry=MIGRATIONS[:10])
    store.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (1,'main','bridge',1)")
    store.conn.execute('INSERT INTO history_snapshots(id,identity_id,taken_at) VALUES (1,1,100)')
    store.conn.execute("INSERT INTO history_items(snapshot_id,identity_key) VALUES (1,'legacy')")
    store.conn.commit()
    store.init_schema()
    row = store.conn.execute('SELECT identity_key,source FROM history_items').fetchone()
    assert tuple(row) == ('legacy', 'unknown')
    assert store.conn.execute('SELECT COUNT(*) FROM takeout_imports').fetchone()[0] == 0
    store.init_schema()
    assert store.conn.execute('SELECT COUNT(*) FROM history_items').fetchone()[0] == 1


def test_early_fixture_preserves_ids_and_backup(tmp_path):
    path = tmp_path / 'legacy.db'
    with connect(path) as conn:
        conn.executescript((Path(__file__).parents[1] / 'fixtures/migrations/early_core.sql').read_text())
    store = Store(path)
    assert not hasattr(store, 'tracks')
    store.init_schema()
    assert store.get_setting('user-setting') == 'keep me'
    assert tuple(store.conn.execute('SELECT id,orig_title,orig_artist FROM tracks').fetchone()) == (17, 'Original title', 'Artist')
    assert tuple(store.conn.execute('SELECT * FROM playlist_tracks').fetchone()) == (8,17,0)
    before = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations')))
    repo = store.tracks
    store.init_schema()
    assert store.tracks is repo
    assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations'))) == before
    backups = list((tmp_path / 'backups').glob('*.db'))
    assert len(backups) == 1
    with connect(backups[0]) as backup:
        assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not backup.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
        assert backup.execute('SELECT id FROM tracks').fetchone()[0] == 17


def test_failure_rolls_back_ddl_data_and_version_then_resumes(tmp_path):
    path = tmp_path / 'state.db'
    def first(conn, ctx):
        conn.execute('CREATE TABLE evidence (value TEXT)')
        conn.execute("INSERT INTO evidence VALUES ('original')")
    def broken(conn, ctx):
        conn.execute('ALTER TABLE evidence ADD COLUMN changed INTEGER')
        conn.execute("UPDATE evidence SET value='lost'")
        raise ValueError('injected interruption')
    registry = (Migration(1,'first',first), Migration(2,'second',broken))
    with connect(path) as conn:
        with pytest.raises(MigrationError, match='2 .*second.*injected'):
            migrate(conn, registry=registry)
    with connect(path) as reopened:
        assert reopened.execute('SELECT * FROM evidence').fetchall() == [('original',)]
        assert reopened.execute('SELECT version FROM schema_migrations').fetchall() == [(1,)]
        migrate(reopened, registry=(registry[0],Migration(2,'second',lambda c,x: c.execute("UPDATE evidence SET value='done'"))))
        assert reopened.execute('SELECT * FROM evidence').fetchall() == [('done',)]


def test_baseline_failure_removes_history_and_all_changes(tmp_path):
    def broken(conn, ctx):
        conn.execute('CREATE TABLE half_ready (id INTEGER)')
        raise KeyboardInterrupt()
    with connect(tmp_path / 'state.db') as conn:
        with pytest.raises(KeyboardInterrupt):
            migrate(conn, registry=[Migration(1,'broken',broken)])
        assert conn.execute('SELECT name FROM sqlite_master').fetchall() == []


@pytest.mark.parametrize('journal', ['DELETE', 'WAL'])
def test_backup_contains_committed_wal_and_failure_prevents_changes(tmp_path, journal):
    path = tmp_path / 'state.db'
    with connect(path) as conn:
        conn.execute(f'PRAGMA journal_mode={journal}')
        conn.execute('CREATE TABLE data (v INTEGER)')
        conn.execute('INSERT INTO data VALUES (42)')
        conn.commit()
        migration = Migration(1,'replace',lambda c,x: c.execute('DELETE FROM data'))
        def fail(*args):
            raise OSError('no backup space')
        with pytest.raises(MigrationError, match='no backup space'):
            migrate(conn, registry=[migration], snapshot=fail)
        assert conn.execute('SELECT * FROM data').fetchall() == [(42,)]
        migrate(conn, registry=[migration])
    with connect(next((tmp_path / 'backups').glob('*.db'))) as backup:
        assert backup.execute('SELECT * FROM data').fetchall() == [(42,)]
        assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_rejects_caller_transaction_without_committing_or_rolling_it_back():
    conn = connect(':memory:')
    conn.execute('CREATE TABLE data (v INTEGER)')
    conn.execute('INSERT INTO data VALUES (1)')
    with pytest.raises(MigrationError, match='outstanding'):
        migrate(conn)
    assert conn.in_transaction
    conn.rollback()
    assert not conn.execute('SELECT * FROM data').fetchall()


def test_frozen_like_repair_matches_existing_dry_run_and_preserves_stamp(legacy_store):
    from yt_playlist.core.migrations.registry import MIGRATIONS
    from yt_playlist.rec.repair import plan_like_ratchet_repair
    s = legacy_store
    now = 1_000_000
    s.conn.execute('DELETE FROM schema_migrations WHERE version=7')
    s.conn.execute("DELETE FROM settings WHERE key='like_ratchet_repaired'")
    for n in range(3):
        key = f'song{n}|artist'
        s.conn.execute("INSERT INTO tracks(video_id,title,artist,identity_key,genre,mb_year) VALUES (?,?, 'Artist',?,'House','1993-04-05')", (str(n),f'Song{n}',key))
        s.conn.execute("INSERT INTO rec_feedback(surface,item_key,kind,created_at) VALUES ('like',?,'like',?)", (key,now-100+n))
    for axis in ('genre:house','era:1990','artist:Artist'):
        s.conn.execute('INSERT INTO rec_weights VALUES (?,2,?)', (axis,now))
        for n in range(14):
            s.conn.execute("INSERT INTO rec_grad_log(axis,source,score,factor,new_weight,created_at) VALUES (?,'like',2,1.05,2,?)", (axis,now-50+n))
    s.conn.commit()
    expected = plan_like_ratchet_repair(s,now)
    migrate(s.conn,registry=MIGRATIONS[:7],clock=lambda:now)
    for adjustment in expected['adjustments']:
        assert s.get_weights(now=now)[adjustment['axis']] == pytest.approx(adjustment['weight_after'])
    stamp = s.get_setting('like_ratchet_repaired')
    assert '3 once-ever graduation stamp(s)' in stamp
    before = s.weight_records()
    s.init_schema();s.init_schema()
    assert s.get_setting('like_ratchet_repaired') == stamp
    assert s.weight_records() == before


@pytest.mark.parametrize('sql', [
    'CREATE TABLE unrelated (id INTEGER)',
    'CREATE TABLE tracks (id TEXT PRIMARY KEY)',
    'CREATE TABLE schema_migrations (version TEXT)',
    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at REAL NOT NULL); INSERT INTO schema_migrations VALUES (99,'future',1)",
    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at REAL NOT NULL); INSERT INTO schema_migrations VALUES (2,'gap',1)",
])
def test_invalid_database_unchanged(sql):
    conn = connect(':memory:')
    conn.executescript(sql)
    before = list(conn.iterdump())
    with pytest.raises(MigrationError):
        migrate(conn)
    assert list(conn.iterdump()) == before


def test_competing_startups_apply_once(tmp_path):
    path = tmp_path / 'state.db'
    barrier = threading.Barrier(2)
    errors = []
    def run():
        try:
            with connect(path) as conn:
                barrier.wait()
                migrate(conn, registry=[Migration(1,'once',lambda c,x: c.execute('CREATE TABLE once_only (id INTEGER)'))])
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with connect(path) as conn:
        assert conn.execute('SELECT version FROM schema_migrations').fetchall() == [(1,)]


def test_process_death_rolls_back(tmp_path):
    path = tmp_path / 'state.db'
    source = '''import os,sqlite3,sys
c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN IMMEDIATE')
c.execute('CREATE TABLE interrupted (id INTEGER)'); os._exit(7)
'''
    assert subprocess.run([sys.executable,'-c',source,str(path)]).returncode == 7
    with connect(path) as conn:
        assert not conn.execute('SELECT name FROM sqlite_master').fetchall()
        migrate(conn)


def test_fresh_and_current_create_no_backup(tmp_path):
    store = Store(tmp_path / 'fresh.db')
    store.init_schema()
    store.init_schema()
    assert not (tmp_path / 'backups').exists()


def test_corrupted_versioned_schema_is_not_silently_repaired(tmp_path):
    store = Store(tmp_path / 'state.db')
    store.init_schema()
    store.conn.execute('DROP TABLE rec_mode_picks')
    with pytest.raises(MigrationError, match='rec_mode_picks'):
        store.init_schema()


@pytest.mark.parametrize('version', [8, 9])
def test_installed_catalog_upgrades_with_original_trigger_and_verified_backup(tmp_path, version):
    from yt_playlist.core.migrations.registry import MIGRATIONS
    path = tmp_path / 'installed.db'
    with connect(path) as conn:
        migrate(conn, registry=MIGRATIONS[:version])
        installed_sql = (Path(__file__).parents[1] / 'fixtures/migrations/play_events_subject_v8.sql').read_text().strip()
        assert conn.execute("SELECT sql FROM sqlite_master WHERE name='play_events_subject'").fetchone()[0] == installed_sql
        conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (1,'main','credential',1)")
        conn.execute("INSERT INTO recordings(id,identity_key) VALUES (1,'song|artist')")
        conn.execute("INSERT INTO youtube_items(video_id,recording_id) VALUES ('known',1)")
        for event_id, video in enumerate(('', '   ', None, 'known', 'unknown'), 1):
            conn.execute("INSERT INTO play_events(id,identity_id,identity_key,video_id,played_at,source,is_generated) VALUES (?,1,'song|artist',?,?,'live',1)", (event_id,video,event_id))
        conn.execute("INSERT INTO rec_weights VALUES ('genre:house',1.4,1)")
        conn.execute("INSERT INTO rec_vectors VALUES ('song|artist',x'01020304')")
        conn.commit()
        raw = conn.execute('SELECT id,raw_identity_key,video_id,played_at,source,is_generated FROM play_events ORDER BY id').fetchall()
        def no_backup(*args):
            raise OSError('backup unavailable')
        with pytest.raises(MigrationError, match='backup unavailable'):
            migrate(conn, snapshot=no_backup)
        assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 9
        assert conn.execute("SELECT sql FROM sqlite_master WHERE name='play_events_subject'").fetchone()[0] == installed_sql
    store = Store(path)
    store.init_schema()
    assert list(map(tuple, store.conn.execute('SELECT id,raw_identity_key,video_id,played_at,source,is_generated FROM play_events ORDER BY id'))) == raw
    subjects = list(map(tuple, store.conn.execute('SELECT identity_key,recording_id FROM play_events ORDER BY id')))
    assert subjects == [('song|artist',1)] * 4 + [('@unresolved-video:unknown',None)]
    assert tuple(store.conn.execute('SELECT * FROM rec_weights').fetchone()) == ('genre:house',1.4,1)
    assert not store.conn.execute('SELECT * FROM rec_vectors').fetchall()
    store.init_schema()
    assert list(map(tuple, store.conn.execute('SELECT identity_key,recording_id FROM play_events ORDER BY id'))) == subjects
    backups = list((tmp_path / 'backups').glob('*.db'))
    assert len(backups) == 1
    with connect(backups[0]) as backup:
        assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert backup.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 9
        assert backup.execute("SELECT sql FROM sqlite_master WHERE name='play_events_subject'").fetchone()[0] == installed_sql
    store.conn.close()
