"""Compare action backup and playlist cleanup state on a private C175 upgrade."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from time import monotonic

import pytest

from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.core import action_backup_files, collection_recovery
from yt_playlist.core.migrations.action_collection_storage import RETIRED
from yt_playlist.core.migrations.prediction_storage import RECEIPT_INDEX
from yt_playlist.core.store import Store


def tables(conn):
    return {name for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def seed_missing_backup(conn):
    action, position = conn.execute('SELECT action_id,max(position)+3 FROM action_backups '
                                    'GROUP BY action_id ORDER BY count(*) DESC,action_id LIMIT 1').fetchone()
    filename = '/missing/old-playlist-backup.json'
    conn.execute("INSERT INTO action_backups VALUES (?,?,?,NULL,NULL,NULL,'missing_historical')",
                 (action, position, filename))
    return action, position, filename


def seed_unresolved_cleanup(conn):
    account, identity = conn.execute('SELECT (SELECT max(id)+1 FROM accounts),max(id)+1 FROM playlist_gc_attempts').fetchone()
    remote, = conn.execute('SELECT provider_playlist_id_snapshot FROM playlist_gc_attempts '
                           'WHERE playlist_scope_id IS NOT NULL ORDER BY id LIMIT 1').fetchone()
    state = {'identity': account, 'ytm': remote, 'attempts': 2, 'last_attempt_at': 12.000000000000002,
             'next_retry_at': 50.00000000000001, 'phase': 'delete', 'failure_class': 'lost reply',
             'error': 'Provider reply lost', 'evidence': {'historical': True}}
    reference = collection_recovery.publish(conn, 'playlist-gc', identity, state)
    conn.execute('INSERT INTO playlist_gc_attempts VALUES (?,?,NULL,NULL,?,?,?,?,?,?,?,?,?,?,?)',
                 (identity, account, remote, '1'*32, state['attempts'], state['last_attempt_at'], state['next_retry_at'],
                  state['phase'], state['failure_class'], state['error'], *reference))
    return account, remote, state


def previous_cleanup(conn):
    states = {}
    for row in conn.execute('SELECT * FROM playlist_gc_attempts'):
        value = collection_recovery.read(row['recovery_filename'], row['recovery_digest'], row['recovery_bytes'],
                                         kind='playlist-gc', owner=row['id'])
        for key in ('attempts', 'last_attempt_at', 'next_retry_at', 'phase', 'failure_class', 'error'):
            if key in value:
                value[key] = row[key]
        states[row['captured_account_id'], row['provider_playlist_id_snapshot']] = value
    return states


def test_installed_action_backups_and_cleanup(tmp_path, monkeypatch):
    report = {}
    path = managed_database(report=report)
    if path is None:
        pytest.skip('Requires a private managed C175 snapshot')
    with closing(sqlite3.connect(path)) as old:
        old.row_factory = sqlite3.Row
        old.execute('PRAGMA foreign_keys=ON')
        before = tables(old)
        installed = {name: old.execute('SELECT count(*) FROM ' + name).fetchone()[0] for name in RETIRED}
        actions = old.execute('SELECT count(*) FROM actions').fetchone()[0]
        with old:
            action, position, filename = seed_missing_backup(old)
            account, remote, unresolved = seed_unresolved_cleanup(old)
        old.execute('BEGIN')
        inventory = action_backup_files.inventory(old)
        previous = previous_cleanup(old)
        manifest = action_backup_files.export(old, tmp_path/'previous')
        old.rollback()

    store = Store(path)
    with closing(store.conn) as conn:
        execute = conn.execute
        obsolete = "DELETE FROM collection_file_writes WHERE kind='playlist-gc'"

        def measure(sql, parameters=()):
            if sql != obsolete:
                return execute(sql, parameters)
            report['delete_plan'] = [row[3] for row in execute('EXPLAIN QUERY PLAN ' + sql)]
            started = monotonic()
            result = execute(sql, parameters)
            report.update(receipt_delete_seconds=round(monotonic() - started, 4), receipts_deleted=result.rowcount)
            return result

        with monkeypatch.context() as timing:
            timing.setattr(conn, 'execute', measure)
            started = monotonic()
            store.init_schema()
            report['startup_seconds'] = round(monotonic() - started, 4)
        assert any('SEARCH predictions' in line and RECEIPT_INDEX in line for line in report['delete_plan'])
        assert report['receipts_deleted'] > 0
        report.update(sqlite_version=sqlite3.sqlite_version,
                      predictions=conn.execute('SELECT count(*) FROM predictions').fetchone()[0],
                      schema_version=conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0])
        after = tables(conn)
        assert set(RETIRED) <= before - after and not after - before
        assert not conn.execute("SELECT 1 FROM collection_file_writes WHERE kind='playlist-gc'").fetchone()
        assert conn.execute('SELECT count(*) FROM actions').fetchone()[0] == actions
        assert action_backup_files.inventory(conn) == inventory
        assert store.actions.repository.backups(action)[-1] == {
            'action_id': action, 'position': position, 'filename': filename, 'availability': 'missing_historical',
            'retained_filename': None, 'digest': None, 'bytes': None}
        assert store.playlists.cleanup_attempts() == previous
        assert set(store.playlists.cleanup_retry_deadlines()) == {
            (owner, target, state['next_retry_at']) for (owner, target), state in previous.items()}
        with store.read_snapshot() as snapshot:
            current = action_backup_files.export(snapshot.store.conn, tmp_path/'current')
        assert Path(current).name == Path(manifest).name
        action_backup_files.restore(conn, manifest)
        for row in action_backup_files.inventory(conn):
            if row['availability'] == 'verified':
                action_backup_files.verified(row['retained_filename'], row['digest'], row['bytes'])
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    store = Store(path)
    with closing(store.conn) as conn:
        store.init_schema()
        assert store.playlists.cleanup_attempt(account, remote) == unresolved
        assert store.playlists.cleanup_attempts() == previous
        assert conn.execute("""SELECT kind FROM playlist_scopes
            WHERE provider_playlist_id=? AND json_type(cleanup_attempts,?)='object'""",
            (remote, f'$."{account}"')).fetchone()[0] == 'legacy'
        store.playlists.set_cleanup_attempt(account, remote, None)
        del previous[account, remote]
        assert store.playlists.cleanup_attempts() == previous
    report.update(tables_before=len(before), tables_after=len(after), installed_rows=installed, actions=actions,
                  seeded_missing_backup=1, seeded_unresolved_cleanup=1, verified_backup_bytes=installed['action_backups'],
                  manifest_preserved=True, same_provider_account_isolation=True, foreign_key_errors=0)
    report_file('action-collection-c190.json').write_text(json.dumps(report, indent=2) + '\n')


def test_fresh_action_collection_schema():
    store = Store(':memory:')
    with closing(store.conn) as conn:
        store.init_schema()
        assert not set(RETIRED).intersection(tables(conn))
        assert action_backup_files.inventory(conn) == []
        assert store.playlists.cleanup_attempts() == {}
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
