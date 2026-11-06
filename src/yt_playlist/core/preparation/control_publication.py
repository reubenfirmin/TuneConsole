"""Publish verified finite controls while retaining compatible installed writers.

Published generations are immutable. A source/reference edit invalidates its
selection in the writing transaction, so readers immediately use installed
settings until explicit reconciliation and publication select a new revision.
"""
import hashlib
import json
import sqlite3
import time

from ..control_reads import selected
from ..schema_contract import MigrationError
from . import collection_controls as preparation
from .selection import invalidators


HEAD = 'published_control_heads'
GENERATIONS = 'published_control_generations'
ORIGINALS = 'published_control_originals'
ALBUM = 'published_album_controls'
GENRES = 'published_album_control_genres'
RUNS = 'published_model_selection_pointers'
NATIVE = {'album-controls': 'album_menu_controls', 'model-pointers': 'model_selection_heads'}


def definitions():
    yield GENERATIONS, f'''CREATE TABLE {GENERATIONS} (
        name TEXT NOT NULL CHECK(name IN ('album-controls','model-pointers')),
        revision INTEGER NOT NULL CHECK(revision>=0), published_at REAL NOT NULL,
        PRIMARY KEY(name,revision)) WITHOUT ROWID'''
    yield HEAD, f'''CREATE TABLE {HEAD} (
        name TEXT PRIMARY KEY CHECK(name IN ('album-controls','model-pointers')), revision INTEGER NOT NULL,
        active INTEGER NOT NULL CHECK(active IN (0,1)),
        FOREIGN KEY(name,revision) REFERENCES {GENERATIONS}(name,revision)) WITHOUT ROWID'''
    yield ORIGINALS, f'''CREATE TABLE {ORIGINALS} (
        name TEXT NOT NULL, revision INTEGER NOT NULL, source TEXT NOT NULL,
        digest TEXT NOT NULL CHECK(length(digest)=64),
        raw_value TEXT CHECK(raw_value IS NULL OR length(CAST(raw_value AS BLOB))<={preparation.MAX_BYTES}),
        PRIMARY KEY(name,revision,source), FOREIGN KEY(name,revision) REFERENCES {GENERATIONS}(name,revision)) WITHOUT ROWID'''
    yield ALBUM, f'''CREATE TABLE {ALBUM} (
        name TEXT NOT NULL CHECK(name='album-controls'), revision INTEGER NOT NULL,
        source TEXT NOT NULL CHECK(source='album_menu_tuning'), source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','controls')),
        expires_at REAL, novelty REAL, control_style TEXT,
        PRIMARY KEY(name,revision), FOREIGN KEY(name,revision,source) REFERENCES {ORIGINALS}(name,revision,source),
        CHECK((source_kind='controls' AND expires_at IS NOT NULL AND abs(expires_at)<=1.7976931348623157e308
                 AND novelty IS NOT NULL AND novelty BETWEEN 0 AND 1
                 AND control_style IS NOT NULL AND control_style IN ('novelty','legacy'))
           OR (source_kind<>'controls' AND expires_at IS NULL AND novelty IS NULL AND control_style IS NULL))) WITHOUT ROWID'''
    yield GENRES, f'''CREATE TABLE {GENRES} (
        name TEXT NOT NULL CHECK(name='album-controls'), revision INTEGER NOT NULL,
        position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 99), raw_label TEXT NOT NULL CHECK(length(raw_label) BETWEEN 1 AND 100),
        genre_key TEXT, unresolved_reason TEXT, PRIMARY KEY(name,revision,position),
        FOREIGN KEY(name,revision) REFERENCES {ALBUM}(name,revision),
        CHECK((genre_key IS NOT NULL AND unresolved_reason IS NULL) OR
            (genre_key IS NULL AND unresolved_reason IS NOT NULL AND unresolved_reason IN ('unknown_genre','ambiguous_genre')))) WITHOUT ROWID'''
    allowed = ','.join(repr(key) for key in preparation.model_selection_heads.KEYS)
    yield RUNS, f'''CREATE TABLE {RUNS} (
        name TEXT NOT NULL CHECK(name='model-pointers'), revision INTEGER NOT NULL,
        source TEXT NOT NULL CHECK(source IN ({allowed})), source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','recorded')),
        recorded_run_id INTEGER, run_id INTEGER, unresolved_reason TEXT,
        PRIMARY KEY(name,revision,source), FOREIGN KEY(name,revision,source) REFERENCES {ORIGINALS}(name,revision,source),
        CHECK((source_kind='recorded' AND recorded_run_id IS NOT NULL AND recorded_run_id>0 AND
                  ((run_id IS NOT NULL AND run_id=recorded_run_id AND unresolved_reason IS NULL) OR
                   (run_id IS NULL AND unresolved_reason IS NOT NULL AND unresolved_reason='missing_run')))
           OR (source_kind<>'recorded' AND recorded_run_id IS NULL AND run_id IS NULL AND unresolved_reason IS NULL))) WITHOUT ROWID'''
    for table in (GENERATIONS, ORIGINALS, ALBUM, GENRES, RUNS):
        for operation in ('UPDATE', 'DELETE'):
            name = table + '_' + operation.lower()
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{table} '
                "BEGIN SELECT RAISE(ABORT,'Published control generations require the future cleanup procedure'); END")
    yield from invalidators(HEAD, preparation.JOBS)


def validate(conn):
    from ..schema_contract import tokens as _tokens
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible control publication object: ' + name)


def validate_installed(conn):
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
        return
    validate(conn)
    for name, revision in conn.execute(f'SELECT name,revision FROM {HEAD}'):
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (NATIVE[name],)).fetchone():
            continue
        preparation.JOBS[name].validate(conn)
        if not conn.execute(f'SELECT 1 FROM {GENERATIONS} WHERE name=? AND revision=?', (name, revision)).fetchone():
            raise MigrationError('Active control publication has no committed generation')


def _verify(conn, name):
    job = preparation.JOBS[name]
    columns = ','.join(job.projections['settings'])
    sources = conn.execute('SELECT ' + columns + ' FROM main.settings WHERE ' + job._filter('settings')).fetchall()
    table = preparation.ALBUM if name == 'album-controls' else preparation.RUNS
    staged = {row['source']: dict(row) for row in conn.execute(f'SELECT * FROM {table} LIMIT 5')}
    if set(staged) != {row['key'] for row in sources}:
        raise MigrationError('Prepared controls differ from the installed source selection')
    originals, fields, genres = [], [], []
    for source in sources:
        raw = bytes(source['value']).decode('utf-8') if source['value'] is not None else None
        if raw is not None and len(raw.encode()) > preparation.MAX_BYTES:
            raise MigrationError('Control publication exceeds its source byte budget')
        digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False).encode()).hexdigest()
        row = staged[source['key']]
        retained = conn.execute(f'SELECT raw_value FROM {preparation.originals(job.name)} '
                               'WHERE source=? AND digest=?', (source['key'], digest)).fetchone()
        if retained is None or retained[0] != raw or row['source_digest'] != digest:
            raise MigrationError('Prepared control original differs from its installed document')
        if name == 'album-controls':
            expected, genres = preparation.album_values(conn, raw)
            actual = tuple(row[key] for key in ('source_kind', 'expires_at', 'novelty', 'control_style'))
        else:
            expected = preparation.pointer_values(conn, raw)
            actual = tuple(row[key] for key in ('source_kind', 'recorded_run_id', 'run_id', 'unresolved_reason'))
        if actual != expected:
            raise MigrationError('Prepared control fields differ from their verified input')
        originals.append((source['key'], digest, raw))
        fields.append((source['key'], *expected))
    if name == 'album-controls':
        actual = list(map(tuple, conn.execute(f'SELECT position,raw_label,genre_key,unresolved_reason '
                                             f'FROM {preparation.GENRES} ORDER BY position LIMIT 101')))
        if actual != genres:
            raise MigrationError('Prepared album selections differ from their verified input')
    return originals, fields, genres


def publish(conn, name, *, revision, clock=time.time):
    if conn.in_transaction:
        raise MigrationError('Publication requires an idle connection')
    job = preparation.JOBS[name]
    try:
        conn.execute('BEGIN IMMEDIATE')
        job._version(conn)
        job.validate(conn)
        progress = job.progress(conn)
        if (progress is None or progress.status != 'prepared' or progress.input_revision != revision
                or progress.prepared_revision != revision or conn.execute(f'SELECT 1 FROM {job.changes} LIMIT 1').fetchone()):
            raise MigrationError('Control publication requires the requested reconciled preparation revision')
        originals, fields, genres = _verify(conn, name)
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
            validate(conn)
        else:
            for _, sql in definitions():
                conn.execute(sql)
        if conn.execute(f'SELECT 1 FROM {GENERATIONS} WHERE name=? AND revision=?', (name, revision)).fetchone():
            if selected(conn, name) != revision:
                raise MigrationError('Retained publication revision cannot replace a different selection')
            conn.rollback()
            return
        conn.execute(f'INSERT INTO {GENERATIONS} VALUES (?,?,?)', (name, revision, clock()))
        conn.executemany(f'INSERT INTO {ORIGINALS} VALUES (?,?,?,?,?)',
                         [(name, revision, *row) for row in originals])
        if name == 'album-controls':
            conn.executemany(f'INSERT INTO {ALBUM} VALUES (?,?,?,?,?,?,?)',
                             [(name, revision, *row) for row in fields])
            conn.executemany(f'INSERT INTO {GENRES} VALUES (?,?,?,?,?,?)',
                             [(name, revision, *row) for row in genres])
        else:
            conn.executemany(f'INSERT INTO {RUNS} VALUES (?,?,?,?,?,?,?)',
                             [(name, revision, *row) for row in fields])
        conn.execute(f'INSERT INTO {HEAD} VALUES (?,?,1) ON CONFLICT(name) DO UPDATE SET revision=excluded.revision,active=1',
                     (name, revision))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def main(argv=None):
    import argparse
    from contextlib import closing
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job', choices=tuple(preparation.JOBS))
    parser.add_argument('database', type=Path)
    parser.add_argument('--revision', type=int, required=True)
    args = parser.parse_args(argv)
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw', uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            publish(conn, args.job, revision=args.revision)
            print(json.dumps({'job': args.job, 'revision': selected(conn, args.job)}))
    except (MigrationError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f'Control publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
