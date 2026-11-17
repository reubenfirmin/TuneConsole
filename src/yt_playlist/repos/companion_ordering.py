"""Disposable complete rankings for album companions, with bounded read pages."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from yt_playlist.core.activity import check_cancellable_work


class CompanionOrderingRepo:
    def __init__(self, conn):
        self._conn = conn
        conn.execute('PRAGMA cache_size=-1024')
        conn.execute('PRAGMA temp_store=FILE')
        conn.execute('PRAGMA mmap_size=0')
        conn.execute('BEGIN')
        conn.execute('CREATE TABLE tracks(recording INTEGER PRIMARY KEY,metadata TEXT)')
        conn.execute('CREATE TABLE ranking(album INTEGER,recording INTEGER,score REAL, '
                     'PRIMARY KEY(album,recording)) WITHOUT ROWID')
        conn.execute('CREATE INDEX ranking_order ON ranking(album,score DESC,recording)')

    @classmethod
    @contextmanager
    def open(cls):
        with TemporaryDirectory(prefix='tuneconsole-companions-') as directory:
            conn = sqlite3.connect(Path(directory) / 'ordering.db')
            try:
                yield cls(conn)
            finally:
                conn.close()

    def add_tracks(self, tracks):
        self._conn.executemany('INSERT INTO tracks VALUES (?,?)',
            ((track['recording_id'], json.dumps(track)) for track in tracks))

    def add_scores(self, rows):
        self._conn.executemany('INSERT INTO ranking VALUES (?,?,?)', rows)

    def ranked(self, album):
        cursor = self._conn.execute('SELECT t.metadata FROM ranking r JOIN tracks t ON t.recording=r.recording '
                                   'WHERE r.album=? ORDER BY r.score DESC,r.recording', (album,))
        try:
            while rows := cursor.fetchmany(128):
                check_cancellable_work()
                yield from (json.loads(row[0]) for row in rows)
        finally:
            cursor.close()
