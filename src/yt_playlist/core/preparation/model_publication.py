"""Publish encoders and their admission tokens together after independent preparation.

File integrity and source comparison run outside the writer transaction. The
bounded cutover rechecks both proofs and the complete finite manifest. Published
parts are immutable; compatible writers keep updating retained settings.
"""
import hashlib
import json
import sqlite3
import time

from .. import model_state, recommendation_outputs as files
from ..model_reads import native, selected
from ..schema_contract import MigrationError
from . import collection_controls, model_artifacts as preparation
from .selection import invalidators


NAME = 'content-models'
HEAD = 'published_content_model_heads'
GENERATIONS = 'published_content_model_generations'
LINKS = 'published_content_model_encoders'
TOKENS = 'published_content_model_fingerprints'
JOBS = {'encoders': preparation.DEFINITION, 'fingerprints': preparation.FINGERPRINT_JOB}


def definitions():
    yield GENERATIONS, f'''CREATE TABLE {GENERATIONS} (
        artifact_revision INTEGER NOT NULL CHECK(artifact_revision>=0),
        fingerprint_revision INTEGER NOT NULL CHECK(fingerprint_revision>=0), published_at REAL NOT NULL,
        PRIMARY KEY(artifact_revision,fingerprint_revision)) WITHOUT ROWID'''
    yield HEAD, f'''CREATE TABLE {HEAD} (
        name TEXT PRIMARY KEY CHECK(name={NAME!r}), artifact_revision INTEGER NOT NULL,
        fingerprint_revision INTEGER NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)),
        FOREIGN KEY(artifact_revision,fingerprint_revision)
            REFERENCES {GENERATIONS}(artifact_revision,fingerprint_revision)) WITHOUT ROWID'''
    yield LINKS, f'''CREATE TABLE {LINKS} (
        artifact_revision INTEGER NOT NULL, fingerprint_revision INTEGER NOT NULL,
        source TEXT NOT NULL, generation INTEGER NOT NULL,
        PRIMARY KEY(artifact_revision,fingerprint_revision,source),
        FOREIGN KEY(artifact_revision,fingerprint_revision)
            REFERENCES {GENERATIONS}(artifact_revision,fingerprint_revision),
        FOREIGN KEY(source,generation) REFERENCES {preparation.HEAD}(source,generation)) WITHOUT ROWID'''
    yield TOKENS, f'''CREATE TABLE {TOKENS} (
        artifact_revision INTEGER NOT NULL, fingerprint_revision INTEGER NOT NULL,
        source TEXT NOT NULL CHECK(source IN ({','.join(repr(key) for key in preparation.FINGERPRINTS)})),
        owner TEXT NOT NULL CHECK(owner IN ('recording','artist')),
        field TEXT NOT NULL CHECK(field IN ('generation','input_fingerprint','genre_revision','fit_fingerprint')),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','recorded')),
        raw_value TEXT CHECK(raw_value IS NULL OR length(CAST(raw_value AS BLOB))<={collection_controls.MAX_BYTES}),
        digest TEXT NOT NULL CHECK(length(digest)=64),
        PRIMARY KEY(artifact_revision,fingerprint_revision,source),
        FOREIGN KEY(artifact_revision,fingerprint_revision)
            REFERENCES {GENERATIONS}(artifact_revision,fingerprint_revision)) WITHOUT ROWID'''
    for table in (GENERATIONS, LINKS, TOKENS):
        for operation in ('UPDATE', 'DELETE'):
            name = table + '_' + operation.lower()
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{table} '
                "BEGIN SELECT RAISE(ABORT,'Published content models require the future cleanup procedure'); END")
    for table in (preparation.HEAD, preparation.PARTS):
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            sides = ('OLD', 'NEW') if operation == 'UPDATE' else ('OLD',) if operation == 'DELETE' else ('NEW',)
            condition = ' OR '.join(f'(source={side}.source AND generation={side}.generation)' for side in sides)
            name = 'published_' + table + '_' + operation.lower()
            yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.{table} '
                f'WHEN EXISTS(SELECT 1 FROM {LINKS} WHERE {condition}) '
                "BEGIN SELECT RAISE(ABORT,'Published content encoder parts are retained'); END")
    yield from invalidators(HEAD, JOBS, names={name: NAME for name in JOBS})


def validate(conn):
    from ..schema_contract import tokens as _tokens
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible content model publication object: ' + name)


def validate_installed(conn):
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
        return
    validate(conn)
    if not native(conn):
        for job in JOBS.values():
            job.validate(conn)
    if not conn.execute(f'SELECT 1 FROM {HEAD} h JOIN {GENERATIONS} g '
                        'USING(artifact_revision,fingerprint_revision) WHERE h.name=?', (NAME,)).fetchone():
        raise MigrationError('Content model publication has no committed generation')


def proof(conn, job, revision):
    job._version(conn)
    job.validate(conn)
    progress = job.progress(conn)
    if (progress is None or progress.status != 'prepared' or progress.prepared_revision != revision
            or progress.input_revision != revision or conn.execute(f'SELECT 1 FROM {job.changes} LIMIT 1').fetchone()):
        raise MigrationError('Content model publication requires both requested reconciled preparation revisions')
    return progress


def capture(conn):
    heads = conn.execute(f'SELECT g.* FROM {preparation.CURRENT} h JOIN {preparation.HEAD} g '
                         'USING(source,generation) ORDER BY source LIMIT 3').fetchall()
    if len(heads) > len(preparation.ARTIFACTS):
        raise MigrationError('Content model publication exceeds its encoder owner set')
    encoders = []
    for head in heads:
        parts = conn.execute(f'SELECT position,filename,digest,byte_size FROM {preparation.PARTS} '
            'WHERE source=? AND generation=? ORDER BY position LIMIT ?',
            (head['source'], head['generation'], preparation.MAX_PARTS + 1)).fetchall()
        encoders.append((dict(head), list(map(tuple, parts))))
    tokens = conn.execute(f'SELECT p.*,o.raw_value,o.digest AS original_digest FROM {preparation.FINGERPRINT_TABLE} p '
                          f'LEFT JOIN {preparation.ORIGINALS} o ON o.source=p.source AND o.digest=p.source_digest '
                          'ORDER BY p.source LIMIT 6').fetchall()
    if len(tokens) > len(preparation.FINGERPRINTS):
        raise MigrationError('Content model publication exceeds its fingerprint owner set')
    return encoders, list(map(dict, tokens))


def verify(captured, sources):
    encoders, tokens = captured
    if {head['source'] for head, _parts in encoders} != sources.keys() & set(preparation.ARTIFACTS):
        raise MigrationError('Prepared encoders differ from the installed source selection')
    if {row['source'] for row in tokens} != sources.keys() & set(preparation.FINGERPRINTS):
        raise MigrationError('Prepared fingerprints differ from the installed source selection')
    for head, parts in encoders:
        raw = preparation.decode_original(head, parts)
        original = sources[head['source']]
        if (raw.encode() if raw is not None else None) != original:
            raise MigrationError('Prepared encoder differs from the installed source')
        kind = 'null' if raw is None else 'blank' if raw == '' else 'encoder'
        if head['source_kind'] != kind or head['owner'] != model_state.FIELDS[head['source']][0]:
            raise MigrationError('Prepared encoder owner differs from its verified input')
        if raw and not isinstance(files.decode_json(raw), dict):
            raise MigrationError('Prepared encoder is not a supported JSON object')
    for row in tokens:
        original = sources[row['source']]
        raw = original.decode('utf-8') if original is not None else None
        if raw is not None and len(original) > collection_controls.MAX_BYTES:
            raise MigrationError('Content model fingerprint exceeds its publication byte budget')
        digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False).encode()).hexdigest()
        kind = 'null' if raw is None else 'blank' if raw == '' else 'recorded'
        if (row['raw_value'] != raw or row['source_digest'] != digest or row['original_digest'] != digest or row['source_kind'] != kind
                or (row['owner'], row['field']) != model_state.FIELDS[row['source']]
                or row['token'] != (raw if kind == 'recorded' else None)):
            raise MigrationError('Prepared fingerprint differs from its verified input')


def publish(conn, *, artifact_revision, fingerprint_revision, clock=time.time):
    if conn.in_transaction:
        raise MigrationError('Publication requires an idle connection')
    revisions = (artifact_revision, fingerprint_revision)
    try:
        conn.execute('BEGIN')
        proofs = tuple(proof(conn, job, revision) for job, revision in zip(JOBS.values(), revisions))
        captured = capture(conn)
        sources = {}
        for job in JOBS.values():
            for row in conn.execute('SELECT ' + ','.join(job.projections['settings']) +
                                    ' FROM main.settings WHERE ' + job._filter('settings')):
                sources[row['key']] = bytes(row['value']) if row['value'] is not None else None
        conn.rollback()
        # Potentially multi-megabyte reads, hashing and JSON checks are outside
        # both the read snapshot and the bounded writer reservation.
        verify(captured, sources)
        conn.execute('BEGIN IMMEDIATE')
        if (tuple(proof(conn, job, revision) for job, revision in zip(JOBS.values(), revisions)) != proofs
                or capture(conn) != captured):
            raise MigrationError('Content model preparation changed during publication verification')
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
            validate(conn)
        else:
            for _, sql in definitions():
                conn.execute(sql)
        if conn.execute(f'SELECT 1 FROM {GENERATIONS} WHERE artifact_revision=? AND fingerprint_revision=?', revisions).fetchone():
            if selected(conn) != revisions:
                raise MigrationError('Retained content model revisions cannot replace a different selection')
            conn.rollback()
            return
        conn.execute(f'INSERT INTO {GENERATIONS} VALUES (?,?,?)', (*revisions, clock()))
        encoders, tokens = captured
        conn.executemany(f'INSERT INTO {LINKS} VALUES (?,?,?,?)',
                         [(*revisions, head['source'], head['generation']) for head, _parts in encoders])
        conn.executemany(f'INSERT INTO {TOKENS} VALUES (?,?,?,?,?,?,?,?)',
                         [(*revisions, row['source'], row['owner'], row['field'], row['source_kind'],
                           row['raw_value'], row['source_digest']) for row in tokens])
        conn.execute(f'INSERT INTO {HEAD} VALUES (?,?,?,1) ON CONFLICT(name) DO UPDATE SET '
                     'artifact_revision=excluded.artifact_revision,fingerprint_revision=excluded.fingerprint_revision,active=1',
                     (NAME, *revisions))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def main(argv=None):
    import argparse
    from contextlib import closing
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--artifact-revision', type=int, required=True)
    parser.add_argument('--fingerprint-revision', type=int, required=True)
    args = parser.parse_args(argv)
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw', uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            publish(conn, artifact_revision=args.artifact_revision, fingerprint_revision=args.fingerprint_revision)
            print(json.dumps({'selection': selected(conn)}))
    except (MigrationError, sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f'Content model publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
