"""Backup SQL ownership tested without the application schema or file port."""
from contextlib import closing
from threading import RLock
from types import SimpleNamespace
import sqlite3

import pytest

from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.core import file_receipts
from yt_playlist.core.migrations import playlist_bookkeeping, sync_recovery_storage
from yt_playlist.repos.playlist_backups import PlaylistBackupsRepo


@pytest.mark.parametrize('changed', ['identity', 'filename', 'digest', 'size'])
def test_reservation_commits_and_completion_rejects_another_owner_or_body(tmp_path, changed):
    path = tmp_path / 'backup-owner.db'
    with closing(sqlite3.connect(path, factory=DiagnosticConnection)) as conn:
        with conn:
            file_receipts.playlist_backup.install(conn)
            conn.execute(sync_recovery_storage.RECEIPT_SQL)
            playlist_bookkeeping._install_backup_receipts(conn)
        repo = PlaylistBackupsRepo(SimpleNamespace(conn=conn, _lock=RLock()))
        payload = dict(playlist_id=7, identity_id=3, ytm_playlist_id='REMOTE', captured_at=100)
        identity = repo.reserve(payload, ('a' * 64, 12))
        with closing(sqlite3.connect(path)) as observer:
            assert observer.execute('SELECT owner_id,playlist_id,playlist_account_id,playlist_provider_id,file_status '
                "FROM collection_file_writes WHERE kind='playlist-backup'").fetchone() == (identity, 7, 3, 'REMOTE', 'pending')
        values = dict(identity=identity, filename='/backups/' + identity + '.json', digest='a' * 64, size=12)
        values[changed] = dict(identity='b' * 32, filename='/backups/' + 'c' * 32 + '.json',
                               digest='d' * 64, size=13)[changed]
        with pytest.raises(ValueError, match='reserved identity|reservation changed'):
            repo.complete(values['identity'], (values['filename'], values['digest'], values['size']))
        assert not conn.in_transaction
        assert conn.execute("SELECT file_status FROM collection_file_writes WHERE kind='playlist-backup'").fetchone() == ('pending',)
        repo.complete(identity, ('/backups/' + identity + '.json', 'a' * 64, 12))
        with closing(sqlite3.connect(path)) as observer:
            assert observer.execute("SELECT file_status FROM collection_file_writes WHERE kind='playlist-backup'").fetchone() == ('ready',)
        with pytest.raises(ValueError, match='reservation changed'):
            repo.complete(identity, ('/backups/' + identity + '.json', 'a' * 64, 12))
