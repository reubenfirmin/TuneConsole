"""Select verified finite model checkpoints while retaining compatible writers.

Source/reference writes invalidate the selected proof in their transaction.
Readers then use current installed settings until explicit reconciliation and
publication. Native historical owners take precedence when present.
"""
import hashlib
import json
import sqlite3
import time

from .. import content_preparation_state as content
from ..schema_contract import MigrationError
from . import collection_controls, model_checkpoints as preparation
from .selection import invalidators


HEAD = 'published_checkpoint_heads'
GENERATIONS = 'published_checkpoint_generations'
ORIGINALS = 'published_checkpoint_originals'
NATIVE = {'content-checkpoints': 'content_basis_checkpoint', 'group-checkpoints': 'group_preparation_state',
          'candidate-checkpoints': 'candidate_refresh_checkpoint'}
TABLES = {key: table.replace('prepared_', 'published_', 1) for key, table in preparation.TABLES.items()}
LEGACY_PROGRESS = preparation.LEGACY_PROGRESS.replace('prepared_', 'published_', 1)
PROGRESS_V2 = TABLES[preparation.GROUP[0]]
ADDITIVE = frozenset((PROGRESS_V2, PROGRESS_V2 + '_update', PROGRESS_V2 + '_delete'))


def definitions():
    allowed = ','.join(repr(name) for name in preparation.JOBS)
    yield GENERATIONS, f'''CREATE TABLE {GENERATIONS} (
        name TEXT NOT NULL CHECK(name IN ({allowed})), revision INTEGER NOT NULL CHECK(revision>=0),
        published_at REAL NOT NULL, PRIMARY KEY(name,revision)) WITHOUT ROWID'''
    yield HEAD, f'''CREATE TABLE {HEAD} (
        name TEXT PRIMARY KEY CHECK(name IN ({allowed})), revision INTEGER NOT NULL,
        active INTEGER NOT NULL CHECK(active IN (0,1)),
        FOREIGN KEY(name,revision) REFERENCES {GENERATIONS}(name,revision)) WITHOUT ROWID'''
    yield ORIGINALS, f'''CREATE TABLE {ORIGINALS} (
        name TEXT NOT NULL, revision INTEGER NOT NULL, source TEXT NOT NULL,
        digest TEXT NOT NULL CHECK(length(digest)=64),
        raw_value TEXT CHECK(raw_value IS NULL OR length(CAST(raw_value AS BLOB))<={preparation.MAX_BYTES}),
        PRIMARY KEY(name,revision,source), FOREIGN KEY(name,revision) REFERENCES {GENERATIONS}(name,revision)) WITHOUT ROWID'''
    for name, keys in preparation.SOURCES.items():
        for key in keys:
            tables = (LEGACY_PROGRESS, PROGRESS_V2) if key == preparation.GROUP[0] else (TABLES[key],)
            for table in tables:
                fields = ','.join('"' + field + '" ' + sql.format(field)
                    for field, sql in preparation.columns(key, legacy=table == LEGACY_PROGRESS).items())
                yield table, f'''CREATE TABLE {table} (
                    name TEXT NOT NULL CHECK(name={name.replace('_', '-')!r}), revision INTEGER NOT NULL,
                    source TEXT NOT NULL CHECK(source={key!r}), source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','recorded')),
                    {fields}, PRIMARY KEY(name,revision,source),
                    FOREIGN KEY(name,revision,source) REFERENCES {ORIGINALS}(name,revision,source)) WITHOUT ROWID'''
    for table in (GENERATIONS, ORIGINALS, LEGACY_PROGRESS, *TABLES.values()):
        for operation in ('UPDATE', 'DELETE'):
            name = table + '_' + operation.lower()
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{table} '
                "BEGIN SELECT RAISE(ABORT,'Published checkpoint generations require the future cleanup procedure'); END")
    yield from invalidators(HEAD, preparation.JOBS)


def validate(conn):
    from ..schema_contract import tokens as _tokens
    extended = any(conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
                   for name in ADDITIVE)
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None and name in ADDITIVE and not extended:
            continue
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible checkpoint publication object: ' + name)


def installed(conn, name):
    return (conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone() is not None
            and conn.execute(f'SELECT 1 FROM {HEAD} WHERE name=?', (name,)).fetchone() is not None)


def selected(conn, name):
    if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (NATIVE[name],)).fetchone():
        return None
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
        return None
    row = conn.execute(f'SELECT revision FROM {HEAD} WHERE name=? AND active=1', (name,)).fetchone()
    return row[0] if row else None


def validate_installed(conn):
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
        return
    validate(conn)
    for name, revision in conn.execute(f'SELECT name,revision FROM {HEAD}'):
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (NATIVE[name],)).fetchone():
            continue
        preparation.JOBS[name].validate(conn)
        if not conn.execute(f'SELECT 1 FROM {GENERATIONS} WHERE name=? AND revision=?', (name, revision)).fetchone():
            raise MigrationError('Active checkpoint publication has no committed generation')
        if name == 'group-checkpoints':
            key = preparation.GROUP[0]
            original = conn.execute(f'SELECT 1 FROM {ORIGINALS} WHERE name=? AND revision=? AND source=?',
                                    (name, revision, key)).fetchone()
            if original and progress_row(conn, name, revision) is None:
                raise MigrationError('Published grouping progress is missing its typed checkpoint')


def verify(conn, name):
    job = preparation.JOBS[name]
    source_rows = conn.execute('SELECT ' + ','.join(job.projections['settings']) +
                              ' FROM main.settings WHERE ' + job._filter('settings')).fetchall()
    sources = {row['key']: row['value'] for row in source_rows}
    result = []
    for key in preparation.SOURCES[job.name]:
        row = conn.execute(f'SELECT * FROM {preparation.table(conn, key)}').fetchone()
        if (row is not None) != (key in sources):
            raise MigrationError('Prepared checkpoint differs from the installed source selection')
        if row is None:
            continue
        raw = bytes(sources[key]).decode('utf-8') if sources[key] is not None else None
        if raw is not None and len(raw.encode()) > preparation.MAX_BYTES:
            raise MigrationError('Checkpoint publication exceeds its source byte budget')
        digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False).encode()).hexdigest()
        original = conn.execute('SELECT raw_value FROM ' + collection_controls.originals(job.name) +
                                ' WHERE source=? AND digest=?', (key, digest)).fetchone()
        if original is None or original[0] != raw or row['source_digest'] != digest:
            raise MigrationError('Prepared checkpoint original differs from its installed source')
        kind, fields = preparation.values(conn, key, raw)
        if row['source_kind'] != kind or {field: row[field] for field in fields} != fields:
            raise MigrationError('Prepared checkpoint fields differ from their verified input')
        result.append((key, raw, digest, kind, fields))
    return result


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
            raise MigrationError('Checkpoint publication requires the requested reconciled preparation revision')
        rows = verify(conn, name)
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
            validate(conn)
            for object_name, sql in definitions():
                if object_name in ADDITIVE and not conn.execute(
                        'SELECT 1 FROM main.sqlite_schema WHERE name=?', (object_name,)).fetchone():
                    conn.execute(sql)
        else:
            for _, sql in definitions():
                conn.execute(sql)
        if conn.execute(f'SELECT 1 FROM {GENERATIONS} WHERE name=? AND revision=?', (name, revision)).fetchone():
            if selected(conn, name) != revision:
                raise MigrationError('Retained checkpoint revision cannot replace a different selection')
            conn.rollback()
            return
        conn.execute(f'INSERT INTO {GENERATIONS} VALUES (?,?,?)', (name, revision, clock()))
        for key, raw, digest, kind, fields in rows:
            conn.execute(f'INSERT INTO {ORIGINALS} VALUES (?,?,?,?,?)', (name, revision, key, digest, raw))
            names = ('name', 'revision', 'source', 'source_kind', *fields)
            conn.execute(f'INSERT INTO {TABLES[key]} ({",".join(names)}) VALUES ({",".join("?" for _ in names)})',
                         (name, revision, key, kind, *fields.values()))
        conn.execute(f'INSERT INTO {HEAD} VALUES (?,?,1) ON CONFLICT(name) DO UPDATE SET revision=excluded.revision,active=1', (name, revision))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def progress_row(conn, name, revision):
    for table in (PROGRESS_V2, LEGACY_PROGRESS):
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table,)).fetchone():
            row = conn.execute(f'SELECT * FROM {table} WHERE name=? AND revision=? AND source=?',
                               (name, revision, preparation.GROUP[0])).fetchone()
            if row is not None:
                return row
    return None


def read(conn, name, revision, key, default):
    row = (progress_row(conn, name, revision) if key == preparation.GROUP[0] else conn.execute(
        f'SELECT * FROM {TABLES[key]} WHERE name=? AND revision=? AND source=?', (name, revision, key)).fetchone())
    if row is None:
        return default
    if key == 'rec_mode_fit_inputs':
        return row['fit_fingerprint'] if row['source_kind'] == 'recorded' else '' if row['source_kind'] == 'blank' else default
    if key in preparation.CANDIDATES:
        if key == 'rec_candidate_preferences' and row['source_kind'] == 'blank':
            return ''
        field = 'expires_at' if key == 'rec_candidates_expire_at' else 'preferences_fingerprint'
        return str(row[field]) if row[field] is not None else default
    if row['source_kind'] != 'recorded':
        return default
    if key in preparation.CONTENT:
        value = {field: row[field] for field in content.FIELDS[key] if row[field] is not None}
        if key == 'rec_preparation_full':
            value['at'] = row['at']
            if row['has_config']:
                value['config'] = [row[field] for field in content.CONFIG]
        else:
            if row['has_phases']:
                value['phase_bytes'] = {phase: row['bytes_' + phase] for phase in content.PHASES if row['bytes_' + phase] is not None}
            if key == 'rec_preparation_basis' and row['has_modes']:
                value['modes'] = [row[field] for field in content.MODES]
        return json.dumps(value)
    if key == 'rec_group_shadow_progress':
        return json.dumps({'phase': row['phase'], 'at': row['reported_at'], 'reason': row['reason']}) if row['phase'] else default
    return json.dumps({'generation': row['recorded_generation'], 'serial': row['input_serial'],
                       'at': row['checkpoint_at']}) if row['checkpoint_at'] is not None else default


def fallback(conn, key, default):
    row = conn.execute('SELECT value FROM main.settings WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


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
        parser.exit(1, f'Checkpoint publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
