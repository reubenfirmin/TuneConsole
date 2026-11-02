"""Prepare retained content encoders and fingerprints without fitting models.

File publication runs outside SQLite transactions. Installed settings remain
authoritative; final owner publication must fence both independent job proofs.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .. import model_state, recommendation_outputs as files
from ..schema_contract import MigrationError
from . import collection_controls
from .ordered_rows import OrderedJob
from .rows import Job, main as run


MAX_BYTES = 16 * 1024 * 1024
PART_CHARACTERS = 64 * 1024
MAX_PARTS = 256
ARTIFACTS = tuple(key for key, (_owner, field) in model_state.FIELDS.items() if field == 'model')
FINGERPRINTS = tuple(key for key in model_state.FIELDS if key not in ARTIFACTS)
HEAD = 'prepared_content_encoder_generations'
PARTS = 'prepared_content_encoder_parts'
CURRENT = 'prepared_content_encoder_heads'
FINGERPRINT_TABLE = 'prepared_content_encoder_fingerprints'
ORIGINALS = 'prepared_content_encoder_fingerprint_originals'


def _unused(*args):
    raise MigrationError('Content encoder preparation requires its bounded file worker')


DEFINITION = Job('content_encoders', 79, 93, {'settings': ('key',)}, (
    (HEAD, f'''CREATE TABLE {HEAD} (
        source TEXT NOT NULL CHECK(source IN ({','.join(repr(key) for key in ARTIFACTS)})),
        generation INTEGER NOT NULL CHECK(generation>=0),
        owner TEXT NOT NULL CHECK(owner IN ('recording','artist')),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','encoder')),
        raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64),
        raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 0 AND {MAX_BYTES}),
        part_count INTEGER NOT NULL CHECK(part_count BETWEEN 0 AND {MAX_PARTS}),
        CHECK((source_kind='encoder' AND raw_size>0 AND part_count>0)
           OR (source_kind<>'encoder' AND raw_size=0 AND part_count=0)),
        CHECK((source='rec_content_model' AND owner='recording')
           OR (source='rec_artist_content_model' AND owner='artist')),
        PRIMARY KEY(source,generation)) WITHOUT ROWID'''),
    (PARTS, f'''CREATE TABLE {PARTS} (
        source TEXT NOT NULL, generation INTEGER NOT NULL,
        position INTEGER NOT NULL CHECK(position BETWEEN 0 AND {MAX_PARTS - 1}),
        filename TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES}),
        PRIMARY KEY(source,generation,position), FOREIGN KEY(source,generation) REFERENCES {HEAD}(source,generation)) WITHOUT ROWID'''),
    (CURRENT, f'''CREATE TABLE {CURRENT} (
        source TEXT PRIMARY KEY, generation INTEGER NOT NULL,
        FOREIGN KEY(source,generation) REFERENCES {HEAD}(source,generation)) WITHOUT ROWID'''),
), _unused, _unused,
    filters={'settings': '{prefix}key IN (' + ','.join(repr(key) for key in ARTIFACTS) + ')'},
    projections={'settings': ('key', f'CASE WHEN value IS NULL THEN NULL ELSE '
        f"COALESCE(substr(CAST(value AS BLOB),1,{MAX_BYTES + 1}),X'') END AS value")})


@dataclass(frozen=True)
class Plan:
    digest: str
    size: int
    kind: str
    units: tuple


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Content encoder preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'content-encoders'


def prepare(conn, row):
    raw = bytes(row['value']) if row['value'] is not None else b''
    if len(raw) > MAX_BYTES:
        raise MigrationError('Content encoder exceeds its preparation byte budget')
    text = raw.decode('utf-8')
    if text and not isinstance(files.decode_json(text), dict):
        raise ValueError('Content encoder must be a supported JSON object; original retained')
    kind = 'null' if row['value'] is None else 'blank' if not text else 'encoder'
    parts = tuple(files.publish(directory(conn), files.encode(row['key'], str(position), text[start:start + PART_CHARACTERS]))
        for position, start in enumerate(range(0, len(text), PART_CHARACTERS)))
    if len(parts) > MAX_PARTS:
        raise MigrationError('Content encoder exceeds its original part budget')
    return Plan(hashlib.sha256(raw).hexdigest(), len(raw), kind, parts)


def begin(conn, row, generation, plan):
    conn.execute(f'INSERT INTO {HEAD} VALUES (?,?,?,?,?,?,?)',
        (row['key'], generation, model_state.FIELDS[row['key']][0], plan.kind, plan.digest, plan.size, len(plan.units)))


def copy(conn, row, generation, position, part):
    conn.execute(f'INSERT INTO {PARTS} VALUES (?,?,?,?,?,?)', (row['key'], generation, position, *part))


def complete(conn, row, generation, plan):
    conn.execute(f'INSERT INTO {CURRENT} VALUES (?,?) ON CONFLICT(source) DO UPDATE SET generation=excluded.generation',
                 (row['key'], generation))


def delete(conn, owner, source_key):
    key, = json.loads(source_key)
    if key in ARTIFACTS:
        conn.execute(f'DELETE FROM {CURRENT} WHERE source=?', (key,))


def read_original(conn, row):
    parts = conn.execute(f'SELECT position,filename,digest,byte_size FROM {PARTS} '
        'WHERE source=? AND generation=? ORDER BY position LIMIT ?',
        (row['source'], row['generation'], MAX_PARTS + 1)).fetchall()
    return decode_original(row, parts)


def decode_original(row, parts):
    """Verify a captured manifest without holding a database transaction."""
    if [part[0] for part in parts] != list(range(row['part_count'])):
        raise MigrationError('Prepared content encoder original parts are incomplete')
    raw = ''.join(files.read(*part[1:], surface=row['source'], chunk_key=str(part[0])) for part in parts)
    if len(raw.encode()) != row['raw_size'] or hashlib.sha256(raw.encode()).hexdigest() != row['raw_digest']:
        raise MigrationError('Prepared content encoder differs from its original digest')
    return None if row['source_kind'] == 'null' else raw


ARTIFACT_JOB = OrderedJob(DEFINITION, prepare, begin, copy, complete, delete)


def fingerprint(conn, owner, source_key, row):
    raw, digest = collection_controls.retain(conn, 'content_encoder_fingerprint', row)
    kind = 'null' if raw is None else 'blank' if raw == '' else 'recorded'
    subject, field = model_state.FIELDS[row['key']]
    conn.execute(f'INSERT INTO {FINGERPRINT_TABLE} VALUES (?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET '
        'owner=excluded.owner,field=excluded.field,source_kind=excluded.source_kind,token=excluded.token,source_digest=excluded.source_digest',
        (row['key'], subject, field, kind, raw if kind == 'recorded' else None, digest))


def delete_fingerprint(conn, owner, source_key):
    key, = json.loads(source_key)
    if key in FINGERPRINTS:
        conn.execute(f'DELETE FROM {FINGERPRINT_TABLE} WHERE source=?', (key,))


FINGERPRINT_JOB = collection_controls.job('content_encoder_fingerprint', FINGERPRINTS, (
    (ORIGINALS, f'''CREATE TABLE {ORIGINALS} (
        source TEXT NOT NULL CHECK(source IN ({','.join(repr(key) for key in FINGERPRINTS)})),
        digest TEXT NOT NULL CHECK(length(digest)=64),
        raw_value TEXT CHECK(raw_value IS NULL OR length(CAST(raw_value AS BLOB))<={collection_controls.MAX_BYTES}),
        PRIMARY KEY(source,digest)) WITHOUT ROWID'''),
    (FINGERPRINT_TABLE, f'''CREATE TABLE {FINGERPRINT_TABLE} (
        source TEXT PRIMARY KEY CHECK(source IN ({','.join(repr(key) for key in FINGERPRINTS)})),
        owner TEXT NOT NULL CHECK(owner IN ('recording','artist')),
        field TEXT NOT NULL CHECK(field IN ('generation','input_fingerprint','genre_revision','fit_fingerprint')),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','recorded')),
        token TEXT, source_digest TEXT NOT NULL,
        FOREIGN KEY(source,source_digest) REFERENCES {ORIGINALS}(source,digest),
        CHECK((source_kind='recorded' AND token IS NOT NULL AND length(token)>0)
           OR (source_kind<>'recorded' AND token IS NULL))) WITHOUT ROWID'''),
), fingerprint, delete_fingerprint, ())


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=('encoders', 'fingerprints'))
    args, remaining = parser.parse_known_args(argv)
    return run(ARTIFACT_JOB if args.job == 'encoders' else FINGERPRINT_JOB, remaining)


if __name__ == '__main__':
    raise SystemExit(main())
