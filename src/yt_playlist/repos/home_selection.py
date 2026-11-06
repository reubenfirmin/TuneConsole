"""Disk-backed candidate filtering and ordering for one Home preparation."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from yt_playlist.core.activity import check_cancellable_work


class HomeSelectionRepo:
    def __init__(self, conn):
        self._conn = conn
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA cache_size=-1024')
        conn.execute('PRAGMA temp_store=FILE')
        conn.execute('PRAGMA mmap_size=0')
        conn.execute('CREATE TABLE candidates(pool INTEGER,rank INTEGER,key TEXT,artist TEXT,album TEXT,'
            'family TEXT,has_genre INTEGER,year INTEGER,tilt REAL,delta REAL,eligible INTEGER,'
            'PRIMARY KEY(pool,rank)) WITHOUT ROWID')
        conn.execute('CREATE TABLE seen(key TEXT PRIMARY KEY) WITHOUT ROWID')
        conn.execute('CREATE TABLE considered(card INTEGER,key TEXT,delta REAL,PRIMARY KEY(card,key)) WITHOUT ROWID')
        conn.execute('CREATE TABLE ordered(key TEXT PRIMARY KEY,delta REAL) WITHOUT ROWID')
        conn.execute('CREATE TABLE ppr(mode TEXT,key TEXT,rank INTEGER,PRIMARY KEY(mode,key)) WITHOUT ROWID')
        conn.execute('CREATE TABLE distances(source TEXT,target TEXT,distance REAL,PRIMARY KEY(source,target)) WITHOUT ROWID')
        conn.execute("CREATE INDEX candidate_families ON candidates(COALESCE(family,''))")

    @classmethod
    @contextmanager
    def open(cls):
        with TemporaryDirectory(prefix='tuneconsole-home-') as directory:
            conn = sqlite3.connect(Path(directory) / 'cards.db')
            try:
                yield cls(conn)
            finally:
                conn.close()

    def _rows(self, query, values):
        cursor = self._conn.execute(query, values)
        try:
            while rows := cursor.fetchmany(128):
                check_cancellable_work()
                yield from rows
        finally:
            cursor.close()

    def add_candidates(self, rows):
        self._conn.executemany('INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?,?)', rows)

    def add_ppr(self, rows):
        self._conn.executemany('INSERT INTO ppr VALUES (?,?,?)', rows)

    def add_distances(self, rows):
        self._conn.executemany('INSERT OR IGNORE INTO distances VALUES (?,?,?)', rows)

    def missing_distance_families(self, source, *, limit):
        return [row[0] for row in self._conn.execute(
            "SELECT DISTINCT COALESCE(c.family,'') family FROM candidates c WHERE NOT EXISTS("
            "SELECT 1 FROM distances d WHERE d.source=? AND d.target=COALESCE(c.family,'')) "
            "ORDER BY COALESCE(c.family,'') LIMIT ?", (source, limit))]

    def count(self, query):
        return self._conn.execute('SELECT count(*) FROM candidates c WHERE ' + query.predicate, query.values).fetchone()[0]

    def considered(self, card, query):
        self._conn.execute('INSERT OR IGNORE INTO considered SELECT ?,c.key,c.delta FROM candidates c WHERE ' + query.predicate,
                           (card, *query.values))
        self._conn.execute('INSERT OR IGNORE INTO ordered SELECT c.key,c.delta FROM candidates c WHERE ' + query.predicate,
                           query.values)

    def has_negative_ordering(self):
        return self._conn.execute('SELECT 1 FROM ordered WHERE delta<>0 LIMIT 1').fetchone() is not None

    def ranked(self, query):
        join = 'LEFT JOIN ppr p ON p.mode=? AND p.key=c.key ' if query.mode is not None else ''
        values = ((query.mode,) if query.mode is not None else ()) + query.values + query.order_values
        return self._rows('SELECT c.* FROM candidates c ' + join + 'WHERE ' + query.predicate +
                          ' ORDER BY ' + query.order, values)

    def ppr_position(self, mode, key):
        row = self._conn.execute('SELECT rank FROM ppr WHERE mode=? AND key=?', (str(mode), key)).fetchone()
        return row[0] if row else None

    def considered_rows(self, card):
        return self._rows('SELECT key,delta FROM considered WHERE card=? ORDER BY key', (card,))

    def mark_seen(self, query, keys):
        self._conn.execute('INSERT OR IGNORE INTO seen SELECT c.key FROM candidates c WHERE ' + query.predicate, query.values)
        self._conn.executemany('INSERT OR IGNORE INTO seen VALUES (?)', [(key,) for key in keys])


class HomeInputRepo:
    def __init__(self, conn):
        self._conn = conn
        conn.execute('PRAGMA cache_size=-1024')
        conn.execute('PRAGMA mmap_size=0')
        conn.execute('CREATE TABLE inputs(key TEXT PRIMARY KEY,genre TEXT,blocked INTEGER) WITHOUT ROWID')

    @classmethod
    @contextmanager
    def open(cls):
        with TemporaryDirectory(prefix='tuneconsole-home-inputs-') as directory:
            conn = sqlite3.connect(Path(directory) / 'inputs.db')
            try:
                yield cls(conn)
            finally:
                conn.close()

    def get(self, keys):
        if not keys:
            return {}
        return {key: (genre, blocked) for key, genre, blocked in self._conn.execute(
            'SELECT key,genre,blocked FROM inputs WHERE key IN (' + ','.join('?' for _ in keys) + ')', keys)}

    def add(self, rows):
        self._conn.executemany('INSERT OR IGNORE INTO inputs VALUES (?,?,?)', rows)
