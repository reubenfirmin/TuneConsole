"""Disk-backed candidate filtering and ordering for one Home preparation."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.util.genre_map import family_distance


@dataclass(frozen=True)
class CandidateFilter:
    pool: int | None
    year_band: int | None = None
    years: tuple = ()
    include: tuple = ()
    avoid: tuple = ()
    tagged: bool = False
    eligible: bool = False
    unseen: bool = False
    excluded: tuple = ()
    near_family: str | None = None


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
        conn.create_function('distance', 2, family_distance, deterministic=True)

    @classmethod
    @contextmanager
    def open(cls):
        with TemporaryDirectory(prefix='tuneconsole-home-') as directory:
            conn = sqlite3.connect(Path(directory) / 'cards.db')
            try:
                yield cls(conn)
            finally:
                conn.close()

    @staticmethod
    def _filter(selected):
        parts, values = ['c.pool=?'], [selected.pool]
        if selected.year_band is not None:
            low, high = selected.years
            parts.append('c.year<=?' if selected.year_band == 0 else
                         'c.year>? AND c.year<=?' if selected.year_band == 1 else 'c.year>?')
            values.extend((low,) if selected.year_band == 0 else
                          (low, high) if selected.year_band == 1 else (high,))
        if selected.tagged:
            parts.append('c.has_genre=1')
        for items, expression in ((selected.include, 'c.family IN'), (selected.avoid, 'c.family NOT IN'),
                                  (selected.excluded, 'c.key NOT IN')):
            if items:
                parts.append(expression + ' (' + ','.join('?' for _ in items) + ')')
                values.extend(items)
        if selected.near_family:
            parts.append('distance(?,c.family)<=0.5')
            values.append(selected.near_family)
        if selected.eligible:
            parts.append('c.eligible=1')
        if selected.unseen:
            parts.append('NOT EXISTS(SELECT 1 FROM seen s WHERE s.key=c.key)')
        return ' AND '.join(parts), values

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

    def count(self, selected):
        predicate, values = self._filter(selected)
        return self._conn.execute('SELECT count(*) FROM candidates c WHERE ' + predicate, values).fetchone()[0]

    def considered(self, card, selected):
        predicate, values = self._filter(selected)
        self._conn.execute('INSERT OR IGNORE INTO considered SELECT ?,c.key,c.delta FROM candidates c WHERE ' + predicate,
                           (card, *values))
        self._conn.execute('INSERT OR IGNORE INTO ordered SELECT c.key,c.delta FROM candidates c WHERE ' + predicate, values)

    def has_negative_ordering(self):
        return self._conn.execute('SELECT 1 FROM ordered WHERE delta<>0 LIMIT 1').fetchone() is not None

    def ranked(self, selected, *, mode=None, ranker='rank', family=None, scale=1):
        predicate, values = self._filter(selected)
        if ranker == 'rank':
            return self._rows('SELECT c.* FROM candidates c WHERE ' + predicate + ' ORDER BY c.rank', values)
        if ranker not in ('ppr', 'cosine'):
            raise ValueError('Unknown Home ordering')
        order = 'COALESCE(p.rank,1073741824)/? - c.delta' if ranker == 'ppr' else '-c.tilt-c.delta'
        parameters = [str(mode), *values]
        if family:
            order = 'distance(?,c.family),' + order
            parameters.append(family)
        if ranker == 'ppr':
            parameters.append(float(scale))
        return self._rows('SELECT c.* FROM candidates c LEFT JOIN ppr p ON p.mode=? AND p.key=c.key '
                          'WHERE ' + predicate + ' ORDER BY ' + order + ',c.rank', parameters)

    def ppr_position(self, mode, key):
        row = self._conn.execute('SELECT rank FROM ppr WHERE mode=? AND key=?', (str(mode), key)).fetchone()
        return row[0] if row else None

    def considered_rows(self, card):
        return self._rows('SELECT key,delta FROM considered WHERE card=? ORDER BY key', (card,))

    def mark_seen(self, selected, keys):
        predicate, values = self._filter(selected)
        self._conn.execute('INSERT OR IGNORE INTO seen SELECT c.key FROM candidates c WHERE ' + predicate, values)
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
