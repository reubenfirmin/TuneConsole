"""Disposable disk-backed ordering for a bounded group selection session."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from yt_playlist.core.activity import check_cancellable_work


class GroupOrderingRepo:
    def __init__(self, conn):
        self._conn = conn
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA cache_size=-1024')
        conn.execute('PRAGMA temp_store=FILE')
        conn.execute('PRAGMA mmap_size=0')
        conn.execute('CREATE TABLE ordering(rank INTEGER PRIMARY KEY,key TEXT,artist TEXT,album TEXT,'
                     'bucket INTEGER,taste REAL,fit REAL)')

    @classmethod
    @contextmanager
    def open(cls):
        with TemporaryDirectory(prefix='tuneconsole-group-cards-') as directory:
            conn = sqlite3.connect(Path(directory) / 'ordering.db')
            try:
                yield cls(conn)
            finally:
                conn.close()

    def clear(self):
        self._conn.execute('DELETE FROM ordering')

    def add(self, rows):
        self._conn.executemany('INSERT INTO ordering VALUES (?,?,?,?,?,?,?)', rows)

    def ranked(self):
        cursor = self._conn.execute('SELECT * FROM ordering ORDER BY bucket,taste,fit DESC,rank')
        try:
            while batch := cursor.fetchmany(128):
                check_cancellable_work()
                yield from batch
        finally:
            cursor.close()
