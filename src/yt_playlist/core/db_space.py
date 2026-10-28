"""Disk headroom checks for disposable writes and actionable SQLite failures."""
from pathlib import Path
import shutil
import sqlite3

MIN_FREE_BYTES = 256 * 1024 * 1024


class LowDiskSpace(RuntimeError):
    pass


class StorageCompacting(RuntimeError):
    def __init__(self):
        super().__init__('Storage is compacting. Please retry this change when it finishes.')


def require_space(directory, growth=0, *, reserve=MIN_FREE_BYTES):
    free = shutil.disk_usage(directory).free
    needed = reserve + growth
    if free < needed:
        raise LowDiskSpace(f'Insufficient database disk space at {directory}: '
                           f'{free / 1048576:.1f} MiB available, {needed / 1048576:.1f} MiB required. '
                           'Free space or choose a destination on another filesystem; '
                           'existing database and WAL files must be preserved.')


def require_database_space(conn, growth=0):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if path:
        require_space(Path(path).parent, growth)


def failure_hint(conn, error):
    code = getattr(error, 'sqlite_errorcode', 0) & 255
    if code not in (sqlite3.SQLITE_FULL, sqlite3.SQLITE_IOERR):
        return ''
    try:
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        free = shutil.disk_usage(Path(path).parent).free
    except (OSError, sqlite3.Error):
        return ''
    return (f' Database: {path}; available filesystem space: {free / 1048576:.1f} MiB. '
            'Free disk space before retrying; preserve the database and its WAL.'
            if code == sqlite3.SQLITE_FULL or free < MIN_FREE_BYTES else
            f' Database: {path}; available filesystem space: {free / 1048576:.1f} MiB. '
            'Check filesystem health and write access; preserve the database and its WAL.')
